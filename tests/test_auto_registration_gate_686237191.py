"""Regression coverage for review signals resolved after answer generation.

All posting services are fakes.  The production inquiry id appears only as
traceability in this test module; the policy implementation has no inquiry or
intent allow-list.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from answer.models import AnswerRequest, AnswerResult, AnswerStatus
from repositories.answer_repository import AnswerRepository
from repositories.auto_post_repository import AutoPostRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.workflow_repository import WorkflowRepository
from services.auto_post_pipeline_service import AutoPostPipelineService
from services.auto_processing_eligibility_service import (
    AutoProcessingEligibilityService,
)
from services.automatic_draft_service import AutomaticDraftOutcome
from services.inquiry_analysis_service import InquiryAnalysisService


QUESTION = "혼자서 설치 가능한가요?"
SAFE_ANSWER = (
    "확인된 설치 안내에 따라 고객님께서 직접 설치하실 수 있습니다. "
    "동봉된 설치 안내서를 따라 진행해 주세요."
)
SERVICE = AutoProcessingEligibilityService()


def _analysis(**overrides: object) -> dict:
    value = InquiryAnalysisService().analyze(
        AnswerRequest(
            inquiry_id=1,
            question_id="regression-fixture",
            inquiry_type="PRODUCT_INQUIRY",
            question=QUESTION,
            product_name="",
            metadata={},
        )
    ).to_dict()
    value.update(overrides)
    return value


def _stale_analysis(**overrides: object) -> dict:
    value = _analysis(
        inquiry_type="INFORMATION_INSUFFICIENT",
        inquiry_subtype="UNCLASSIFIED",
        question_category="INFORMATION_INSUFFICIENT",
        detected_intent="GENERAL",
        answer_strategy="MANUAL_REVIEW",
        confidence=0.45,
        manual_review_required=True,
        auto_answerable=False,
    )
    value.update(overrides)
    return value


def _draft(
    *,
    analysis: dict | None = None,
    evidence_status: str = "ANSWERABLE",
    evidence_coverage: str = "SUPPORTED",
    validation_status: str = "PASS",
    validator: dict | None = None,
    generated_requires_review: bool = False,
    missing_information: list[str] | None = None,
    self_review_requires_review: bool = False,
    plan_high_risk: bool = False,
    review_status: str = "NEEDS_REVIEW",
    product_fact_guard: dict | None = None,
    route: str = "GPT_DIRECT",
    answer: str = SAFE_ANSWER,
) -> dict:
    analysis_value = analysis or _stale_analysis()
    validator_value = validator or {
        "passed": True,
        "status": "PASS",
        "errors": [],
        "review_signals": [],
        "warnings": [],
    }
    metadata = {
        "requires_manual_review": True,
        "generation_mode": route,
        "selected_answer_route": route,
        "processing_plan": {
            "analysis": analysis_value,
            "needs_staff_review": True,
            "is_high_risk": plan_high_risk,
            "requires_order_lookup": False,
            "requires_dps_lookup": False,
        },
        "hybrid": {
            "enabled": True,
            "draft": {
                "requires_review": generated_requires_review,
                "missing_information": missing_information or [],
            },
            "self_review": {
                "requires_review": self_review_requires_review,
            },
            "validation": validator_value,
            "subquestion_evidence": [
                {
                    "subquestion": QUESTION,
                    "status": evidence_status,
                    "evidence_coverage": evidence_coverage,
                }
            ],
        },
    }
    if product_fact_guard is not None:
        metadata["product_fact_guard"] = product_fact_guard
    return {
        "id": 1,
        "original_answer": answer,
        "validation_status": validation_status,
        "validator_result_json": validator_value,
        "review_status": review_status,
        "posted": False,
        "metadata_json": metadata,
    }


def _evaluate(
    draft: dict,
    *,
    route: str = "GPT_DIRECT",
    inquiry_overrides: dict | None = None,
):
    inquiry = {
        "id": 1,
        "source_type": "PRODUCT_INQUIRY",
        "inquiry_type": "PRODUCT_INQUIRY",
        "source_question_id": "regression-fixture",
        "external_inquiry_id": "regression-fixture",
        "content": QUESTION,
        "product_name": "테스트 제품",
        "raw_json": {},
        "source_answered": 0,
        "post_status": "NOT_POSTED",
    }
    inquiry.update(inquiry_overrides or {})
    return SERVICE.evaluate(
        inquiry=inquiry,
        draft=draft,
        route=route,
    )


def test_current_classifier_no_longer_requires_review_for_exact_question() -> None:
    analysis = _analysis()
    assert analysis["question_category"] == "INSTALLATION_GENERAL"
    assert analysis["inquiry_subtype"] == "PRE_PURCHASE_DELIVERY_GUIDANCE"
    assert analysis["answer_strategy"] == "GENERAL_GUIDANCE"
    assert analysis["manual_review_required"] is False


def test_grounded_validator_pass_resolves_only_derivative_review_flags() -> None:
    result = _evaluate(_draft())
    assert result.safe is True
    assert result.reasons == ()
    assert set(result.soft_reasons) == {
        "INTENT_UNCLASSIFIED_VALIDATOR_CLEAR",
        "PRELIMINARY_REVIEW_RESOLVED",
    }


def test_stale_installation_review_is_cleared_without_intent_allow_list() -> None:
    stored = _analysis(
        manual_review_required=True,
        auto_answerable=False,
        answer_strategy="MANUAL_REVIEW",
    )
    result = _evaluate(_draft(analysis=stored))
    assert result.safe is True
    assert result.reasons == ()
    assert "PRELIMINARY_REVIEW_RESOLVED" in result.soft_reasons


def test_stale_unclassified_signal_cannot_hide_current_high_risk_analysis() -> None:
    question = "배송 중 파손되면 어떻게 하나요?"
    current = InquiryAnalysisService().analyze(
        AnswerRequest(
            inquiry_id=1,
            question_id="current-high-risk",
            inquiry_type="PRODUCT_INQUIRY",
            question=question,
            product_name="",
            metadata={},
        )
    )
    assert current.manual_review_required is True

    result = _evaluate(_draft(), inquiry_overrides={"content": question})
    assert result.safe is False
    assert "ANSWER_REQUIRES_MANUAL_REVIEW" in result.reasons
    assert "PROCESSING_PLAN_REQUIRES_REVIEW" in result.reasons
    assert "DRAFT_REVIEW_REQUIRED" in result.reasons
    assert "PRELIMINARY_REVIEW_RESOLVED" not in result.soft_reasons


def test_validator_pass_with_advisory_warning_remains_safe() -> None:
    result = _evaluate(
        _draft(
            validation_status="PASS_WITH_WARNING",
            validator={
                "passed": True,
                "status": "PASS_WITH_WARNING",
                "errors": [],
                "review_signals": [],
                "warnings": ["표현을 더 간결하게 다듬을 수 있습니다."],
            }
        )
    )
    assert result.safe is True


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (
            {"evidence_status": "NO_RELIABLE_SOURCE", "evidence_coverage": "SUPPORTED"},
            "ANSWER_REQUIRES_MANUAL_REVIEW",
        ),
        (
            {"evidence_status": "ANSWERABLE", "evidence_coverage": "UNSUPPORTED"},
            "ANSWER_REQUIRES_MANUAL_REVIEW",
        ),
        ({"generated_requires_review": True}, "DRAFT_REVIEW_REQUIRED"),
        ({"missing_information": ["특수 설치 조건 확인"]}, "DRAFT_REVIEW_REQUIRED"),
        ({"self_review_requires_review": True}, "DRAFT_REVIEW_REQUIRED"),
        ({"plan_high_risk": True}, "POLICY_OR_HIGH_RISK_REVIEW"),
        ({"review_status": "IN_REVIEW"}, "DRAFT_REVIEW_REQUIRED"),
    ],
)
def test_unresolved_post_generation_or_human_review_signal_stays_held(
    values: dict, expected: str,
) -> None:
    result = _evaluate(_draft(**values))
    assert result.safe is False
    assert expected in result.reasons


@pytest.mark.parametrize(
    "claim",
    [
        "근거 없이 브라켓 호환을 확정했습니다.",
        "근거 없이 배송기간을 확정했습니다.",
        "근거 없이 A/S 기간을 확정했습니다.",
        "근거 없이 설치 가능 여부를 확정했습니다.",
    ],
)
def test_fact_grounding_failure_from_validator_stays_held(claim: str) -> None:
    result = _evaluate(
        _draft(
            validation_status="FAILED_FACT_GROUNDING",
            validator={
                "passed": False,
                "status": "BLOCK",
                "errors": [claim],
                "review_signals": [],
                "warnings": [],
            },
        )
    )
    assert result.safe is False
    assert "VALIDATOR_NOT_PASS" in result.reasons


def test_unverified_product_fact_remains_a_separate_hard_reason() -> None:
    result = _evaluate(
        _draft(
            product_fact_guard={
                "sensitive": True,
                "current_fact_verified": False,
            }
        )
    )
    assert result.safe is False
    assert result.decision == "REVIEW_REQUIRED"
    assert "PRODUCT_FACT_NOT_VERIFIED" in result.reasons
    assert "PRELIMINARY_REVIEW_RESOLVED" in result.soft_reasons


def test_compatibility_guard_remains_independent_after_preliminary_resolution() -> None:
    result = _evaluate(
        _draft(
            analysis=_stale_analysis(detected_intent="PRODUCT_COMPATIBILITY"),
        )
    )
    assert result.safe is False
    assert "PRODUCT_COMPATIBILITY_NOT_VERIFIED" in result.reasons
    assert "PRELIMINARY_REVIEW_RESOLVED" in result.soft_reasons


def test_validator_review_signal_prevents_preliminary_resolution() -> None:
    result = _evaluate(
        _draft(
            validator={
                "passed": True,
                "status": "PASS",
                "errors": [],
                "review_signals": ["직원 확인 필요"],
                "warnings": [],
            }
        )
    )
    assert result.safe is False
    assert "ANSWER_REQUIRES_MANUAL_REVIEW" in result.reasons
    assert "PRELIMINARY_REVIEW_RESOLVED" not in result.soft_reasons


def test_validator_review_required_prevents_preliminary_resolution() -> None:
    result = _evaluate(
        _draft(
            validation_status="REVIEW_REQUIRED",
            validator={
                "passed": True,
                "status": "REVIEW_REQUIRED",
                "errors": [],
                "review_signals": [],
                "warnings": [],
            },
        )
    )
    assert result.safe is False
    assert "VALIDATOR_REVIEW_REQUIRED" in result.reasons
    assert "PRELIMINARY_REVIEW_RESOLVED" not in result.soft_reasons


def test_validator_error_prevents_preliminary_resolution_even_with_pass_status() -> None:
    result = _evaluate(
        _draft(
            validator={
                "passed": True,
                "status": "PASS",
                "errors": ["검증 오류"],
                "review_signals": [],
                "warnings": [],
            }
        )
    )
    assert result.safe is False
    assert "ANSWER_REQUIRES_MANUAL_REVIEW" in result.reasons
    assert "PRELIMINARY_REVIEW_RESOLVED" not in result.soft_reasons


def test_untrusted_dps_result_remains_independent_after_preliminary_resolution() -> None:
    draft = _draft()
    draft["metadata_json"]["processing_plan"].update(
        {
            "requires_dps_lookup": True,
            "dps_lookup_status": "FAILED",
            "valid_dps_snapshot_available": False,
        }
    )
    result = _evaluate(draft)
    assert result.safe is False
    assert "DPS_RESULT_NOT_TRUSTED" in result.reasons
    assert "DPS_SNAPSHOT_NOT_VALIDATED" in result.reasons
    assert "PRELIMINARY_REVIEW_RESOLVED" in result.soft_reasons


def test_existing_naver_answer_remains_idempotency_blocked() -> None:
    result = _evaluate(_draft(), inquiry_overrides={"source_answered": 1})
    assert result.safe is False
    assert result.decision == "BLOCKED"
    assert "ALREADY_ANSWERED_OR_POSTED" in result.reasons


def test_privacy_guard_still_blocks_before_review_resolution() -> None:
    result = _evaluate(_draft(answer="연락처는 010-1234-5678입니다."))
    assert result.decision == "BLOCKED"
    assert "PII_EXPOSURE" in result.reasons


def test_confirmed_dps_installation_date_stays_safe() -> None:
    draft = _draft(
        analysis=_analysis(
            manual_review_required=False,
            auto_answerable=True,
            answer_strategy="DIRECT_FACT_ANSWER",
        ),
        review_status="PENDING",
        route="DELIVERY_WITH_INSTALLATION_DATE",
    )
    metadata = draft["metadata_json"]
    metadata["requires_manual_review"] = False
    metadata["processing_plan"].update(
        {
            "needs_staff_review": False,
            "requires_dps_lookup": True,
            "dps_lookup_status": "SUCCESS",
            "valid_dps_snapshot_available": True,
        }
    )
    result = _evaluate(draft, route="DELIVERY_WITH_INSTALLATION_DATE")
    assert result.safe is True


class _ExistingDraftService:
    def __init__(self, draft_id: int) -> None:
        self.draft_id = draft_id

    def ensure_for_inquiry(self, inquiry_id: int, **_: object):
        return AutomaticDraftOutcome("EXISTING", inquiry_id, self.draft_id, "GPT_DIRECT")


class _RecordingPostService:
    def __init__(self) -> None:
        self.calls = 0

    def post(self, *_: object, **__: object):
        self.calls += 1
        return SimpleNamespace(status="POSTED")


def _database_with_draft(tmp_path: Path, *, safe: bool) -> tuple[Database, int, int]:
    database = Database(tmp_path / ("safe.db" if safe else "held.db"))
    database.initialize()
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": "fixture-question",
            "external_inquiry_id": "fixture-question",
            "inquiry_type": "PRODUCT_INQUIRY",
            "title": "",
            "content": QUESTION,
            "product_name": "테스트 제품",
            "raw_json": {},
        }
    ).inquiry_id
    WorkflowRepository(database).initialize_steps(inquiry_id)
    raw = _draft(generated_requires_review=not safe)
    result = AnswerResult(
        status=AnswerStatus.NEEDS_REVIEW,
        category="fixture",
        reason="fixture",
        answer=raw["original_answer"],
        provider="fake_openai_hybrid",
        auto_answerable=False,
        needs_review=True,
        metadata=raw["metadata_json"],
    )
    stored = AnswerRepository(database).create_program_draft(inquiry_id, result)
    AutoPostRepository(database).save_settings(
        enabled=True, interval_minutes=10, max_retries=1
    )
    return database, inquiry_id, int(stored["id"])


def test_auto_post_on_and_resolved_safe_draft_enters_post_path(tmp_path: Path) -> None:
    database, inquiry_id, draft_id = _database_with_draft(tmp_path, safe=True)
    posts = _RecordingPostService()
    result = AutoPostPipelineService(
        database,
        draft_service=_ExistingDraftService(draft_id),
        post_service=posts,
    ).run_pending(
        run_id="SAFE-RUN",
        owner_id="SAFE-OWNER",
        max_retries=1,
        inquiry_ids=[inquiry_id],
    )
    assert result.succeeded_count == 1
    assert posts.calls == 1


def test_auto_post_on_and_unresolved_review_never_calls_post(tmp_path: Path) -> None:
    database, inquiry_id, draft_id = _database_with_draft(tmp_path, safe=False)
    posts = _RecordingPostService()
    result = AutoPostPipelineService(
        database,
        draft_service=_ExistingDraftService(draft_id),
        post_service=posts,
    ).run_pending(
        run_id="HELD-RUN",
        owner_id="HELD-OWNER",
        max_retries=1,
        inquiry_ids=[inquiry_id],
    )
    assert result.skipped_count == 1
    assert result.succeeded_count == 0
    assert posts.calls == 0
