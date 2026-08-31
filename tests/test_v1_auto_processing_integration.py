from __future__ import annotations

from pathlib import Path

import pytest

from repositories.auto_post_event_repository import AutoPostEventRepository
from repositories.auto_post_repository import AutoPostRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_repository import LearningRepository
from repositories.log_repository import LogRepository
from repositories.workflow_repository import WorkflowRepository
from services.auto_processing_eligibility_service import (
    AutoProcessingEligibilityService,
)
from services.automatic_draft_service import AutomaticDraftOutcome
from services.dashboard_operations_service import DashboardOperationsService
from services.inquiry_sync_service import InquirySyncService


def database(tmp_path: Path) -> Database:
    value = Database(tmp_path / "v1-auto-processing.db")
    value.initialize()
    return value


def work_item(external_id: str = "V1-Q-1") -> dict:
    return {
        "store_code": "OJE_PLUS",
        "source": "PRODUCT_INQUIRY",
        "source_type": "PRODUCT_INQUIRY",
        "source_question_id": external_id,
        "external_inquiry_id": external_id,
        "inquiry_type": "PRODUCT_INQUIRY",
        "title": "제품 문의",
        "content": "제품 사용 방법을 알려주세요.",
        "raw_json": {},
    }


class RecordingAutomaticDrafts:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def ensure_for_inquiry(self, inquiry_id: int, **_: object) -> AutomaticDraftOutcome:
        self.calls.append(int(inquiry_id))
        return AutomaticDraftOutcome("CREATED", int(inquiry_id), 1, "SAFE_RULE")


def test_runtime_off_sync_persists_and_creates_no_automatic_work(tmp_path: Path) -> None:
    db = database(tmp_path)
    automatic = RecordingAutomaticDrafts()
    result = InquirySyncService(
        InquiryRepository(db),
        WorkflowRepository(db),
        LogRepository(db),
        automatic_drafts=automatic,
    ).sync([work_item()], correlation_id="AUTO-SYNC-OFF")

    assert result["new"] == 1
    assert automatic.calls == []
    inquiry = InquiryRepository(db).list()[0]
    event = AutoPostEventRepository(db).get_for_inquiry(int(inquiry["id"]))
    assert event and event["status"] == "BLOCKED_AUTO_POST_OFF"
    assert AutoPostRepository(db).settings()["auto_processing_enabled"] is False


def test_runtime_on_allows_automatic_stage_and_event(tmp_path: Path) -> None:
    db = database(tmp_path)
    AutoPostRepository(db).save_settings(
        enabled=True, interval_minutes=10, max_retries=1
    )
    automatic = RecordingAutomaticDrafts()
    result = InquirySyncService(
        InquiryRepository(db),
        WorkflowRepository(db),
        LogRepository(db),
        automatic_drafts=automatic,
    ).sync([work_item("V1-Q-ON")], correlation_id="AUTO-SYNC-ON")

    assert result["new"] == 1
    assert len(automatic.calls) == 1
    event = AutoPostEventRepository(db).get_for_inquiry(automatic.calls[0])
    assert event and event["status"] == "PENDING"


def draft(*, route: str = "SAFE_RULE", plan: dict | None = None, **values: object) -> dict:
    metadata = {"selected_answer_route": route, "processing_plan": plan or {}}
    return {
        "original_answer": "안전하게 검증된 답변입니다.",
        "review_status": "PENDING",
        "validation_status": "PASSED",
        "validator_result_json": {"passed": True},
        "posted": False,
        "metadata_json": metadata,
        **values,
    }


def inquiry() -> dict:
    return {"source_answered": False, "post_status": "NOT_POSTED"}


@pytest.mark.parametrize(
    ("route", "plan", "reason"),
    [
        (
            "DPS_LOOKUP_FAILED",
            {
                "requires_dps_lookup": True,
                "dps_lookup_status": "FAILED",
                "valid_dps_snapshot_available": False,
            },
            "DPS_RESULT_NOT_TRUSTED",
        ),
        (
            "DPS_LOOKUP_FAILED",
            {
                "requires_dps_lookup": True,
                "dps_lookup_status": "LOGIN_REQUIRED",
                "valid_dps_snapshot_available": False,
            },
            "DPS_RESULT_NOT_TRUSTED",
        ),
        (
            "REVIEW_REQUIRED_SAFE_DRAFT",
            {"needs_staff_review": True},
            "PROCESSING_PLAN_REQUIRES_REVIEW",
        ),
    ],
)
def test_uncertain_conditions_are_review_required(
    route: str, plan: dict, reason: str,
) -> None:
    result = AutoProcessingEligibilityService().evaluate(
        inquiry=inquiry(), draft=draft(route=route, plan=plan), route=route
    )
    assert result.decision == "REVIEW_REQUIRED"
    assert reason in result.reasons


# The two cases below were previously part of the parametrize list above.
# They are not safety requirements: neither condition puts a wrong fact in
# front of a customer, and blocking them was the single largest source of
# unnecessary REVIEW_REQUIRED holds. They are now pinned explicitly, in both
# directions, so the relaxation stays deliberate and cannot silently widen.
def test_order_id_request_reply_is_auto_postable_and_reason_is_kept() -> None:
    """A reply that only asks for the order number asserts no order fact."""
    route = "ORDER_ID_REQUEST"
    result = AutoProcessingEligibilityService().evaluate(
        inquiry=inquiry(),
        draft=draft(
            route=route,
            plan={"requires_order_lookup": True, "order_id_status": "MISSING"},
        ),
        route=route,
    )
    assert result.decision == "SAFE"
    # Still recorded, just not blocking.
    assert "ORDER_ID_REQUESTED_FROM_CUSTOMER" in result.soft_reasons


def test_low_confidence_alone_is_auto_postable_and_reason_is_kept() -> None:
    """A pessimistic confidence score is not itself a factual risk."""
    route = "SAFE_RULE"
    result = AutoProcessingEligibilityService().evaluate(
        inquiry=inquiry(),
        draft=draft(route=route, plan={"analysis": {"confidence": 0.45}}),
        route=route,
    )
    assert result.decision == "SAFE"
    assert "INTENT_CONFIDENCE_LOW" in result.soft_reasons


def test_low_confidence_does_not_rescue_a_real_unsafe_condition() -> None:
    """The relaxation must not leak into a genuine DPS failure."""
    route = "DPS_LOOKUP_FAILED"
    result = AutoProcessingEligibilityService().evaluate(
        inquiry=inquiry(),
        draft=draft(
            route=route,
            plan={
                "analysis": {"confidence": 0.45},
                "requires_dps_lookup": True,
                "dps_lookup_status": "FAILED",
                "valid_dps_snapshot_available": False,
            },
        ),
        route=route,
    )
    assert result.decision == "REVIEW_REQUIRED"
    assert "DPS_RESULT_NOT_TRUSTED" in result.reasons


def test_validator_failure_is_review_and_privacy_failure_is_blocked() -> None:
    validator = draft(validation_status="FAILED")
    review = AutoProcessingEligibilityService().evaluate(
        inquiry=inquiry(), draft=validator, route="SAFE_RULE"
    )
    assert review.decision == "REVIEW_REQUIRED"
    assert "VALIDATOR_NOT_PASS" in review.reasons

    privacy = draft(original_answer="문의는 010-1234-5678로 주세요.")
    blocked = AutoProcessingEligibilityService().evaluate(
        inquiry=inquiry(), draft=privacy, route="SAFE_RULE"
    )
    assert blocked.decision == "BLOCKED"
    assert "PII_EXPOSURE" in blocked.reasons


def test_safe_route_with_trusted_order_and_dps_is_eligible() -> None:
    plan = {
        "requires_order_lookup": True,
        "order_id_status": "VALID",
        "order_lookup_status": "SUCCESS",
        "requires_dps_lookup": True,
        "dps_lookup_status": "SUCCESS",
        "valid_dps_snapshot_available": True,
        "analysis": {"confidence": 0.98},
    }
    result = AutoProcessingEligibilityService().evaluate(
        inquiry=inquiry(),
        draft=draft(route="DELIVERY_WITH_INSTALLATION_DATE", plan=plan),
        route="DELIVERY_WITH_INSTALLATION_DATE",
    )
    assert result.decision == "SAFE"


def test_learning_kpi_counts_creation_and_retrieval_not_naver_post(tmp_path: Path) -> None:
    db = database(tmp_path)
    inquiry_id = InquiryRepository(db).upsert_work_item(work_item("KPI-Q-1")).inquiry_id
    row = LearningRepository(db).upsert(
        {
            "source_key": "kpi-source-1",
            "inquiry_id": inquiry_id,
            "learning_source": "SELLER_ANSWER",
            "question_original_masked": "제품 사용 방법",
            "question_normalized": "제품 사용 방법",
            "final_answer": "안전한 사용 방법입니다.",
            "rating": 3,
            "edit_ratio": 0.0,
            "quality_score": 0.6,
            "style_only": True,
            "version": 1,
            "metadata_json": {
                "facts_authority": "STYLE_ONLY",
                "learning_signal_type": "POSITIVE",
            },
            "active": True,
        }
    )
    created = DashboardOperationsService(db).snapshot()
    assert created["learning_today"] == 1
    assert created["learning_used_today"] == 0
    assert created["auto_posted"] == 0

    LearningRepository(db).mark_used([int(row["id"])])
    used = DashboardOperationsService(db).snapshot()
    assert used["learning_today"] == 1
    assert used["learning_used_today"] == 1
    assert used["auto_posted"] == 0


def test_synced_seller_answer_is_observed_without_automatic_learning(tmp_path: Path) -> None:
    db = database(tmp_path)
    item = work_item("SELLER-STYLE-1")
    item.update({"source_answered": True, "seller_answer": "기존 판매자 답변입니다."})
    InquirySyncService(
        InquiryRepository(db), WorkflowRepository(db), LogRepository(db)
    ).sync([item], correlation_id="SELLER-SYNC")

    rows = LearningRepository(db).candidates(store_code="OJE_PLUS")
    assert rows == []
    inquiry_id = int(InquiryRepository(db).list()[0]["id"])
    events = LogRepository(db).recent_for_inquiry(inquiry_id)
    assert any(row["event_code"] == "SELLER_ANSWER_OBSERVED" for row in events)


def test_streamlit_startup_starts_missing_dps_agent_exactly_once(
    tmp_path: Path, monkeypatch,
) -> None:
    from services import dps_agent_client

    statuses = iter(
        [
            {"agent_running": False, "legacy_agent_running": False},
            {"agent_running": True, "legacy_agent_running": False},
        ]
    )
    launches: list[tuple] = []
    monkeypatch.setenv("DPS_SESSION_MONITOR_ENABLED", "true")
    monkeypatch.setattr(dps_agent_client, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(dps_agent_client, "get_dps_agent_status", lambda: next(statuses))
    monkeypatch.setattr(dps_agent_client.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        dps_agent_client.subprocess,
        "Popen",
        lambda *args, **kwargs: launches.append((args, kwargs)),
    )

    result = dps_agent_client.ensure_dps_session_monitor()
    assert result["agent_running"] is True
    assert len(launches) == 1
    assert launches[0][0][0][1:] == ["-m", "dps.agent_server"]


def test_existing_dps_agent_is_reused_without_subprocess(monkeypatch) -> None:
    from services import dps_agent_client

    launches: list[object] = []
    monkeypatch.setenv("DPS_SESSION_MONITOR_ENABLED", "true")
    monkeypatch.setattr(
        dps_agent_client,
        "get_dps_agent_status",
        lambda: {"agent_running": True, "legacy_agent_running": False},
    )
    monkeypatch.setattr(
        dps_agent_client.subprocess,
        "Popen",
        lambda *args, **kwargs: launches.append((args, kwargs)),
    )
    result = dps_agent_client.ensure_dps_session_monitor()
    assert result["agent_running"] is True
    assert launches == []


def test_streamlit_rerun_does_not_launch_second_agent(
    tmp_path: Path, monkeypatch,
) -> None:
    from services import dps_agent_client

    statuses = iter(
        [
            {"agent_running": False, "legacy_agent_running": False},
            {"agent_running": True, "legacy_agent_running": False},
            {"agent_running": True, "legacy_agent_running": False},
        ]
    )
    launches: list[object] = []
    monkeypatch.setenv("DPS_SESSION_MONITOR_ENABLED", "true")
    monkeypatch.setattr(dps_agent_client, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(dps_agent_client, "get_dps_agent_status", lambda: next(statuses))
    monkeypatch.setattr(dps_agent_client.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        dps_agent_client.subprocess,
        "Popen",
        lambda *args, **kwargs: launches.append((args, kwargs)),
    )
    assert dps_agent_client.ensure_dps_session_monitor()["agent_running"] is True
    assert dps_agent_client.ensure_dps_session_monitor()["agent_running"] is True
    assert len(launches) == 1


@pytest.mark.parametrize("runtime_enabled", [False, True])
def test_auto_processing_switch_does_not_control_dps_agent(
    tmp_path: Path, monkeypatch, runtime_enabled: bool,
) -> None:
    from services import dps_agent_client

    db = database(tmp_path)
    AutoPostRepository(db).save_settings(
        enabled=runtime_enabled, interval_minutes=10, max_retries=1
    )
    calls: list[bool] = []
    monkeypatch.setenv("DPS_SESSION_MONITOR_ENABLED", "true")
    monkeypatch.setattr(
        dps_agent_client,
        "start_dps_agent",
        lambda: calls.append(runtime_enabled) or {"agent_running": True},
    )
    result = dps_agent_client.ensure_dps_session_monitor()
    assert result["agent_running"] is True
    assert calls == [runtime_enabled]
    assert AutoPostRepository(db).settings()["enabled"] is runtime_enabled
