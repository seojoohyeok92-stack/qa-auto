from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from answer.answer_format import (
    DEFAULT_CLOSING,
    DEFAULT_PREFIX,
    FINAL_FALLBACK_NOTICE,
    format_final_answer,
)
from answer.engine import AnswerEngine
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.workflow_repository import WorkflowRepository
from answer.models import AnswerResult, AnswerStatus
from services.answer_service import AnswerService
from services.inquiry_processing_plan_service import (
    InquiryProcessingPlanService,
)
from services.approval_service import ApprovalService
from services.naver_post_payload_builder import NaverPostPayloadBuilder
from workflow.models import StepCode


class ForbiddenEngine:
    def generate(self, request):
        raise AssertionError("delivery routes must not use general templates")


class ForbiddenHybrid:
    def generate(self, request, rule_result):
        raise AssertionError("delivery routes must not call GPT")


class FakeOrderLookup:
    def __init__(self, result: dict):
        self.result = dict(result)
        self.calls = 0

    def lookup_for_inquiry(self, inquiry_id: int, **kwargs):
        self.calls += 1
        return dict(self.result)


class FakeDps:
    def __init__(self, metadata: dict | None = None):
        self.metadata = dict(metadata or {})
        self.calls = 0
        self.skip_calls = 0

    def enrich(self, request, **kwargs):
        self.calls += 1
        request.metadata["dps"] = dict(self.metadata)
        return SimpleNamespace(
            decision=SimpleNamespace(lookup_required=True),
            metadata=request.metadata["dps"],
            lookup_row=None,
        )

    def skip_for_phase9(self, request, **kwargs):
        self.skip_calls += 1
        request.metadata["dps"] = {
            "lookup_required": False,
            "lookup_status": "NOT_REQUIRED",
        }
        return SimpleNamespace(
            decision=SimpleNamespace(lookup_required=False),
            metadata=request.metadata["dps"],
            lookup_row=None,
        )


class StaticEngine:
    def __init__(self, result: AnswerResult):
        self.result = result
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        return self.result


class StaticHybrid:
    def __init__(self, answer: str):
        self.answer = answer
        self.calls = 0

    def generate(self, request, rule_result):
        self.calls += 1
        validation = SimpleNamespace(
            passed=True,
            status="PASS",
            to_dict=lambda: {
                "passed": True,
                "status": "PASS",
                "errors": [],
                "warnings": [],
                "checked_facts": [],
                "rules": [],
            },
        )
        result = AnswerResult(
            status=AnswerStatus.GENERATED,
            category="PRODUCT_GENERAL",
            reason="GPT",
            answer=self.answer,
            provider="fake_hybrid",
            auto_answerable=True,
            needs_review=False,
            metadata={
                "hybrid": {
                    "validation": validation.to_dict(),
                    "fallback_used": False,
                }
            },
        )
        return SimpleNamespace(
            result=result,
            validation=validation,
            fallback_used=False,
            events=(),
        )

@pytest.fixture
def database(tmp_path) -> Database:
    value = Database(tmp_path / "processing-plan.db")
    value.initialize()
    return value


def inquiry(
    database: Database,
    source_id: str,
    text: str,
    *,
    order_id: str | None = None,
    product_order_id: str | None = None,
    inquiry_type: str = "CUSTOMER_INQUIRY",
    product_name: str | None = None,
) -> int:
    return InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": inquiry_type,
            "source_question_id": source_id,
            "external_inquiry_id": source_id,
            "inquiry_type": inquiry_type,
            "title": text,
            "content": text,
            "order_id": order_id,
            "product_order_id": product_order_id,
            "product_name": product_name,
            "registered_at": "2026-08-05T12:00:00+09:00",
            "raw_json": {},
        }
    ).inquiry_id


def steps(database: Database, inquiry_id: int) -> dict[str, dict]:
    return {
        row["step_code"]: row
        for row in WorkflowRepository(database).list_steps(inquiry_id)
    }


def delivery_service(
    database: Database,
    *,
    order_result: dict,
    dps_metadata: dict | None = None,
):
    order = FakeOrderLookup(order_result)
    dps = FakeDps(dps_metadata)
    service = AnswerService(
        database,
        engine=ForbiddenEngine(),
        hybrid_service=ForbiddenHybrid(),
        order_lookup_service=order,
        dps_enrichment=dps,
    )
    return service, order, dps


@pytest.mark.parametrize(
    "text",
    [
        "오늘 주문하면 내일 받을 수 있나요?",
        "오늘 주문하면 토요일 받을 수 있나요?",
        "구매하려는데 배송 얼마나 걸리나요?",
        "구매 예정인데 설치는 언제 가능한가요?",
        "이번 주 안에 받을 수 있나요?",
        "내일 도착 가능한가요?",
        "토요일 설치 가능한가요?",
        "배송 얼마나 걸리나요?",
    ],
)
def test_pre_purchase_delivery_classification_skips_order_and_dps(
    database: Database,
    text: str,
) -> None:
    inquiry_id = inquiry(
        database,
        f"PRE-{text}",
        text,
        product_name="삼성 스마트모니터 M5 32인치",
    )
    plan = InquiryProcessingPlanService(database).create(
        InquiryRepository(database).get(inquiry_id)
    )
    assert plan.detected_intent == "PRE_PURCHASE_DELIVERY"
    assert plan.requires_order_lookup is False
    assert plan.requires_dps_lookup is False
    assert plan.order_lookup_action == "SKIP"
    assert plan.dps_lookup_action == "SKIP"
    assert plan.selected_answer_route not in {
        "ORDER_ID_REQUEST",
        "ORDER_LOOKUP_FAILED",
        "DELIVERY_WITH_INSTALLATION_DATE",
        "DELIVERY_DATE_UNCONFIRMED",
        "DPS_LOOKUP_FAILED",
    }


def test_post_purchase_delivery_without_order_keeps_order_id_request(
    database: Database,
) -> None:
    inquiry_id = inquiry(
        database,
        "POST-PURCHASE-NO-ORDER",
        "주문한 상품이 아직 안 왔어요. 배송 조회해 주세요.",
    )
    plan = InquiryProcessingPlanService(database).create(
        InquiryRepository(database).get(inquiry_id)
    )
    assert plan.detected_intent in {"DELIVERY_DATE", "DELIVERY_STATUS"}
    assert plan.requires_order_lookup is True
    assert plan.requires_dps_lookup is True
    assert plan.selected_answer_route == "ORDER_ID_REQUEST"


def test_pre_purchase_delivery_uses_existing_template_without_lookups(
    database: Database,
) -> None:
    inquiry_id = inquiry(
        database,
        "PRE-PURCHASE-DELIVERY",
        "오늘 주문하면 내일 받을 수 있나요?",
        product_name="삼성 스마트모니터 M5 32인치",
    )
    order = FakeOrderLookup({"success": False, "orders": []})
    dps = FakeDps()
    hybrid = ForbiddenHybrid()
    outcome = AnswerService(
        database,
        engine=AnswerEngine(),
        hybrid_service=hybrid,
        order_lookup_service=order,
        dps_enrichment=dps,
    ).generate_for_inquiry(inquiry_id)

    metadata = outcome.result.metadata
    program_answer = outcome.draft["original_answer"]
    assert metadata["detected_intent"] == "PRE_PURCHASE_DELIVERY"
    assert metadata["selected_answer_route"] == "TEMPLATE"
    assert metadata["answer_type"] == "existing_template"
    assert metadata["gpt_called"] is False
    assert order.calls == 0
    assert dps.calls == 0
    assert dps.skip_calls == 1
    assert steps(database, inquiry_id)["NAVER_ORDER_LOOKUP"]["step_status"] == "SKIPPED"
    assert steps(database, inquiry_id)["DPS_LOOKUP"]["step_status"] == "SKIPPED"
    assert "택배배송 상품은 오후 3시 이전 결제 주문에 한해 당일 발송되며," in program_answer
    assert program_answer == format_final_answer(program_answer)
    assert program_answer.count(DEFAULT_PREFIX) == 1
    assert program_answer.count(FINAL_FALLBACK_NOTICE) == 1

    edited = ApprovalService(database).save_edited_answer(
        inquiry_id=inquiry_id,
        draft_id=int(outcome.draft["id"]),
        edited_answer="직원 확인 문구입니다.",
    )
    assert edited["edited_answer"] == format_final_answer("직원 확인 문구입니다.")
    approved = ApprovalService(database).approve(
        inquiry_id=inquiry_id,
        draft_id=int(outcome.draft["id"]),
    )
    final_answer = approved.draft["final_answer"]
    assert final_answer == format_final_answer("직원 확인 문구입니다.")
    payload = NaverPostPayloadBuilder().build(
        source_type="CUSTOMER_INQUIRY",
        external_id="PRE-PURCHASE-DELIVERY",
        store="OJE_PLUS",
        final_answer=final_answer,
    )
    assert payload.payload["answerComment"] == final_answer


def test_actual_324599122_plan_overrides_stale_skipped_classification(
    database: Database,
) -> None:
    inquiry_id = inquiry(
        database,
        "324599122",
        "7월4일 주문 언제 배송되나요?",
        order_id="2026070448206811",
    )
    workflow = WorkflowRepository(database)
    workflow.initialize_steps(inquiry_id)
    for code in (
        StepCode.ORDER_IDENTIFIED,
        StepCode.NAVER_ORDER_LOOKUP,
        StepCode.DPS_LOOKUP,
    ):
        workflow.skip_step(inquiry_id, code, metadata={"stale": True})

    plan = InquiryProcessingPlanService(database).create(
        InquiryRepository(database).get(inquiry_id)
    )

    assert plan.detected_intent == "DELIVERY_DATE"
    assert plan.is_delivery is True
    assert plan.order_id_status == "VALID"
    assert plan.requires_order_lookup is True
    assert plan.requires_dps_lookup is True
    assert plan.order_lookup_action == "FETCH"
    assert plan.workflow_order_status == "READY"


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("7월4일 주문 언제 배송되나요?", "DELIVERY_DATE"),
        ("주문한지 한달이네요. 언제쯤 받을 수 있을까요?", "DELIVERY_DATE"),
        ("몇시에 도착할까요?", "DELIVERY_TIME"),
        ("설치 기사님은 언제 오시나요?", "INSTALLATION_DATE"),
    ],
)
def test_deterministic_schedule_intents_override_scores(
    database: Database,
    text: str,
    intent: str,
) -> None:
    inquiry_id = inquiry(database, f"INTENT-{intent}-{text}", text)
    plan = InquiryProcessingPlanService(database).create(
        InquiryRepository(database).get(inquiry_id)
    )
    assert plan.detected_intent == intent
    assert plan.is_delivery is True
    assert plan.requires_order_lookup is True
    assert plan.requires_dps_lookup is True


def test_delivery_notification_policy_is_not_a_schedule_lookup(
    database: Database,
) -> None:
    inquiry_id = inquiry(
        database,
        "NOTIFICATION-POLICY",
        "배송 알림톡은 언제 오나요?",
    )
    plan = InquiryProcessingPlanService(database).create(
        InquiryRepository(database).get(inquiry_id)
    )
    assert plan.detected_intent == "NOTIFICATION_POLICY"
    assert plan.is_delivery is False
    assert plan.requires_order_lookup is False
    assert plan.requires_dps_lookup is False


def test_order_identifiers_are_normalized_from_safe_json_paths_only(
    database: Database,
) -> None:
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "CUSTOMER_INQUIRY",
            "source_question_id": "JSON-ORDER",
            "inquiry_type": "CUSTOMER_INQUIRY",
            "title": "배송 언제 오나요?",
            "content": "본문 숫자 9999999999999999는 주문번호로 사용하지 마세요.",
            "raw_json": {
                "order_lookup": {
                    "order_id": "2026070448206811",
                    "product_order_id": "202607044820681199",
                }
            },
        }
    ).inquiry_id
    plan = InquiryProcessingPlanService(database).create(
        InquiryRepository(database).get(inquiry_id)
    )
    assert plan.order_id == "2026070448206811"
    assert plan.product_order_id == "202607044820681199"
    assert plan.order_id_status == "VALID"


@pytest.mark.parametrize(
    ("order_id", "product_order_id", "expected_status"),
    [
        (None, None, "MISSING"),
        ("INVALID", None, "INVALID"),
        (None, "202607044820681199", "AMBIGUOUS_PRODUCT_ORDER_ONLY"),
    ],
)
def test_delivery_without_valid_general_order_id_always_creates_request_draft(
    database: Database,
    order_id: str | None,
    product_order_id: str | None,
    expected_status: str,
) -> None:
    inquiry_id = inquiry(
        database,
        f"NO-ORDER-{expected_status}",
        "배송 언제 오나요?",
        order_id=order_id,
        product_order_id=product_order_id,
    )
    service, order, dps = delivery_service(
        database,
        order_result={"success": False, "orders": []},
    )

    outcome = service.generate_for_inquiry(inquiry_id)
    metadata = outcome.result.metadata

    assert metadata["order_id_status"] == expected_status
    assert metadata["selected_answer_route"] == "ORDER_ID_REQUEST"
    assert order.calls == 0
    assert dps.calls == 0
    assert outcome.draft["original_answer"].strip()
    assert AnswerRepository(database).active_for_inquiry(inquiry_id)
    assert steps(database, inquiry_id)["ANSWER_GENERATED"]["step_status"] == "COMPLETED"


@pytest.mark.parametrize(
    ("order_result", "route"),
    [
        (
            {
                "success": False,
                "orders": [],
                "error_code": "ORDER_LOOKUP_FAILED",
            },
            "ORDER_LOOKUP_FAILED",
        ),
        (
            {
                "success": False,
                "orders": [],
                "error_code": "ORDER_NOT_FOUND",
            },
            "DELIVERY_ORDER_NOT_FOUND",
        ),
    ],
)
def test_order_lookup_failure_or_not_found_creates_safe_draft(
    database: Database,
    order_result: dict,
    route: str,
) -> None:
    inquiry_id = inquiry(
        database,
        f"ORDER-{route}",
        "7월4일 주문 언제 배송되나요?",
        order_id="2026070448206811",
    )
    service, order, dps = delivery_service(
        database, order_result=order_result
    )

    outcome = service.generate_for_inquiry(inquiry_id)
    current_steps = steps(database, inquiry_id)

    assert outcome.result.metadata["selected_answer_route"] == route
    assert outcome.result.metadata["gpt_called"] is False
    assert outcome.draft["original_answer"].strip()
    assert order.calls == 1
    assert dps.calls == 0
    assert current_steps["NAVER_ORDER_LOOKUP"]["step_status"] == "FAILED"
    assert current_steps["DPS_LOOKUP"]["step_status"] == "SKIPPED"
    assert current_steps["ANSWER_GENERATED"]["step_status"] == "COMPLETED"


@pytest.mark.parametrize(
    ("dps_metadata", "route", "contains"),
    [
        ({"lookup_status": "AGENT_OFFLINE"}, "DPS_LOOKUP_FAILED", None),
        ({"lookup_status": "TIMEOUT"}, "DPS_LOOKUP_FAILED", None),
        ({"lookup_status": "PARSE_ERROR"}, "DPS_LOOKUP_FAILED", None),
        ({"lookup_status": "SUCCESS"}, "DELIVERY_DATE_UNCONFIRMED", None),
        (
            {
                "lookup_status": "SUCCESS",
                "installation_date": "2026-08-03",
                "date_parse_status": "PARSED",
            },
            "DELIVERY_WITH_INSTALLATION_DATE",
            "2026년 8월 3일",
        ),
    ],
)
def test_delivery_dps_state_matrix_always_creates_non_empty_draft(
    database: Database,
    dps_metadata: dict,
    route: str,
    contains: str | None,
) -> None:
    inquiry_id = inquiry(
        database,
        f"DPS-{route}-{dps_metadata['lookup_status']}",
        "주문한지 한달이네요. 언제쯤 받을 수 있을까요?",
        order_id="2026070448206811",
    )
    service, order, dps = delivery_service(
        database,
        order_result={
            "success": True,
            "orders": [{"order_id": "2026070448206811"}],
        },
        dps_metadata=dps_metadata,
    )

    outcome = service.generate_for_inquiry(inquiry_id)
    current_steps = steps(database, inquiry_id)

    assert outcome.result.metadata["selected_answer_route"] == route
    assert outcome.result.metadata["gpt_called"] is False
    answer = outcome.draft["original_answer"]
    assert answer.strip()
    assert answer.count(DEFAULT_PREFIX) == 1
    assert answer.count(FINAL_FALLBACK_NOTICE) == 1
    assert answer.endswith(DEFAULT_CLOSING)
    if route == "DELIVERY_DATE_UNCONFIRMED":
        assert "현재 삼성 설치 시스템에는" in answer
        assert "아직 설치(배송) 예정일이 등록되지 않았습니다." in answer
        assert "일정이 등록되면 확인 가능합니다." in answer
    if contains:
        assert contains in answer
    assert order.calls == 1
    assert dps.calls == 1
    assert current_steps["NAVER_ORDER_LOOKUP"]["step_status"] == "COMPLETED"
    assert current_steps["DPS_LOOKUP"]["step_status"] == (
        "COMPLETED" if dps_metadata["lookup_status"] == "SUCCESS" else "FAILED"
    )
    assert current_steps["ANSWER_GENERATED"]["step_status"] == "COMPLETED"


@pytest.mark.parametrize(
    ("text", "order_id", "order_result", "dps_metadata"),
    [
        ("배송 언제 오나요?", None, {"success": False, "orders": []}, None),
        (
            "7월4일 주문 언제 배송되나요?",
            "2026070448206811",
            {"success": False, "orders": [], "error_code": "ORDER_LOOKUP_FAILED"},
            None,
        ),
        (
            "주문한지 한달이네요. 언제쯤 받을 수 있을까요?",
            "2026070448206811",
            {"success": True, "orders": [{"order_id": "2026070448206811"}]},
            {"lookup_status": "TIMEOUT"},
        ),
        (
            "주문한지 한달이네요. 언제쯤 받을 수 있을까요?",
            "2026070448206811",
            {"success": True, "orders": [{"order_id": "2026070448206811"}]},
            {"lookup_status": "SUCCESS"},
        ),
    ],
)
def test_every_non_blocked_inquiry_produces_non_empty_draft(
    database: Database,
    text: str,
    order_id: str | None,
    order_result: dict,
    dps_metadata: dict | None,
) -> None:
    inquiry_id = inquiry(
        database,
        f"INVARIANT-{abs(hash((text, order_id, str(order_result))))}",
        text,
        order_id=order_id,
    )
    service, _, _ = delivery_service(
        database,
        order_result=order_result,
        dps_metadata=dps_metadata,
    )

    outcome = service.generate_for_inquiry(inquiry_id)
    metadata = outcome.result.metadata
    active = AnswerRepository(database).active_for_inquiry(inquiry_id)

    assert metadata["selected_answer_route"]
    assert metadata["can_generate_draft"] is True
    assert outcome.draft["original_answer"].strip()
    assert outcome.draft["validation_status"] in {
        "PASS",
        "PASS_REVIEW_REQUIRED",
    }
    assert active and active["id"] == outcome.draft["id"]
    assert steps(database, inquiry_id)["ANSWER_GENERATED"]["step_status"] == "COMPLETED"


@pytest.mark.parametrize(
    ("mode", "prefer_template", "expected_route"),
    [
        ("template", True, "TEMPLATE"),
        ("fallback", True, "GPT_FALLBACK"),
        ("direct", False, "GPT_DIRECT"),
        ("product", True, "PRODUCT_DB"),
    ],
)
def test_every_non_blocked_general_inquiry_produces_non_empty_draft(
    database: Database,
    mode: str,
    prefer_template: bool,
    expected_route: str,
) -> None:
    inquiry_id = inquiry(
        database,
        f"GENERAL-INVARIANT-{mode}",
        "tv로도 사용하려면 어떻게 해야 하나요?",
        inquiry_type="PRODUCT_INQUIRY",
        product_name="삼성 스마트모니터 M5",
    )
    if mode == "template":
        engine_result = AnswerResult(
            status=AnswerStatus.GENERATED,
            category="사용방법",
            reason="fixed template",
            answer="기존 검증 템플릿의 TV 연결 안내입니다.",
            provider="rules",
            auto_answerable=True,
            needs_review=False,
            matched_rule="FIXED-TV-GUIDE",
        )
    elif mode == "product":
        engine_result = AnswerResult(
            status=AnswerStatus.GENERATED,
            category="모델스펙/TV기능",
            reason="JSON 스펙 조회",
            answer="삼성 스마트모니터 M5의 검증된 TV 기능 안내입니다.",
            provider="rules",
            auto_answerable=True,
            needs_review=False,
            matched_rule="PRODUCT-M5-TV",
        )
    else:
        engine_result = AnswerResult(
            status=AnswerStatus.NOT_SUPPORTED,
            category="PRODUCT_GENERAL",
            reason="NO_TEMPLATE",
            answer="",
            provider="rules",
            auto_answerable=False,
            needs_review=True,
        )
    engine = StaticEngine(engine_result)
    hybrid = StaticHybrid(
        "제품 연결 환경을 확인한 뒤 TV 사용 방법을 안내드리겠습니다."
    )
    dps = FakeDps()

    outcome = AnswerService(
        database,
        engine=engine,
        hybrid_service=hybrid,
        dps_enrichment=dps,
    ).generate_for_inquiry(
        inquiry_id,
        prefer_template=prefer_template,
    )

    metadata = outcome.result.metadata
    assert metadata["selected_answer_route"] == expected_route
    assert metadata["can_generate_draft"] is True
    assert outcome.draft["original_answer"].strip()
    assert outcome.draft["validation_status"] == "PASS"
    assert AnswerRepository(database).active_for_inquiry(inquiry_id)
    assert steps(database, inquiry_id)["NAVER_ORDER_LOOKUP"]["step_status"] == "SKIPPED"
    assert steps(database, inquiry_id)["DPS_LOOKUP"]["step_status"] == "SKIPPED"
    assert steps(database, inquiry_id)["ANSWER_GENERATED"]["step_status"] == "COMPLETED"


def test_apptest_actual_324599122_order_failure_renders_safe_program_answer(
    database: Database,
    monkeypatch,
) -> None:
    inquiry_id = inquiry(
        database,
        "324599122",
        "7월4일 주문 언제 배송되나요?",
        order_id="2026070448206811",
    )
    monkeypatch.setenv("OJE_AUTOMATION_DB_PATH", str(database.path))
    monkeypatch.setenv("PHASE86_INQUIRY_ID", str(inquiry_id))
    monkeypatch.setenv("PHASE86_PANEL", "answer")
    monkeypatch.setenv("PHASE86_ORDER_STATUS", "FAILED")
    monkeypatch.delenv("PHASE86_FAKE_ANSWER", raising=False)
    app = AppTest.from_file(
        str(Path(__file__).resolve().parents[1] / "uat" / "phase86_streamlit_probe.py")
    ).run(timeout=30)

    next(
        button for button in app.button if button.label.endswith("답변 생성")
    ).click()
    app = app.run(timeout=30)

    assert not app.exception
    assert not app.error
    active = AnswerRepository(database).active_for_inquiry(inquiry_id)
    assert active["metadata_json"]["selected_answer_route"] == "ORDER_LOOKUP_FAILED"
    assert active["original_answer"].strip()
    program = next(
        area for area in app.text_area if area.label == "Program Answer"
    )
    assert program.value == active["original_answer"]
    current_steps = steps(database, inquiry_id)
    assert current_steps["NAVER_ORDER_LOOKUP"]["step_status"] == "FAILED"
    assert current_steps["DPS_LOOKUP"]["step_status"] == "SKIPPED"
    assert current_steps["ANSWER_GENERATED"]["step_status"] == "COMPLETED"


def test_apptest_delivery_success_with_date_renders_confirmed_date(
    database: Database,
    monkeypatch,
) -> None:
    inquiry_id = inquiry(
        database,
        "DELIVERY-DATE-APPTEST",
        "주문한지 한달이네요. 언제쯤 받을 수 있을까요?",
        order_id="2026070448206811",
    )
    monkeypatch.setenv("OJE_AUTOMATION_DB_PATH", str(database.path))
    monkeypatch.setenv("PHASE86_INQUIRY_ID", str(inquiry_id))
    monkeypatch.setenv("PHASE86_PANEL", "answer")
    monkeypatch.setenv("PHASE86_ORDER_STATUS", "SUCCESS")
    monkeypatch.setenv("PHASE86_DPS_STATUS", "SUCCESS")
    monkeypatch.setenv("PHASE86_DPS_DATE", "2026-08-03")
    monkeypatch.delenv("PHASE86_FAKE_ANSWER", raising=False)
    app = AppTest.from_file(
        str(Path(__file__).resolve().parents[1] / "uat" / "phase86_streamlit_probe.py")
    ).run(timeout=30)

    next(
        button for button in app.button if button.label.endswith("답변 생성")
    ).click()
    app = app.run(timeout=30)

    assert not app.exception
    assert not app.error
    active = AnswerRepository(database).active_for_inquiry(inquiry_id)
    assert active["metadata_json"]["selected_answer_route"] == (
        "DELIVERY_WITH_INSTALLATION_DATE"
    )
    assert "2026년 8월 3일" in active["original_answer"]
    program = next(
        area for area in app.text_area if area.label == "Program Answer"
    )
    assert program.value == active["original_answer"]


def _open_full_dashboard_inquiry(
    database: Database,
    monkeypatch,
    external_id: str,
) -> AppTest:
    monkeypatch.setenv("OJE_AUTOMATION_DB_PATH", str(database.path))
    monkeypatch.setenv("QNA_LOCAL_AUTH_ENABLED", "false")
    monkeypatch.setenv("NAVER_AUTO_SYNC_ENABLED", "false")
    monkeypatch.setattr(
        "repositories.database.get_database_path",
        lambda path=None: database.path,
    )
    app = AppTest.from_file(
        str(Path(__file__).resolve().parents[1] / "app.py")
    ).run(timeout=30)
    search = next(item for item in app.text_input if item.label == "문의 검색")
    search.set_value(external_id)
    app = app.run(timeout=30)
    selected_buttons = [
        button
        for button in app.button
        if str(button.key or "").startswith("official_select_")
    ]
    if not selected_buttons:
        app = app.run(timeout=30)
        selected_buttons = [
            button
            for button in app.button
            if str(button.key or "").startswith("official_select_")
        ]
    assert selected_buttons, [
        (button.key, button.label) for button in app.button
    ]
    assert str(selected_buttons[0].label) == external_id
    return app


def test_full_app_actual_324599122_shows_ready_order_and_enabled_answer(
    database: Database,
    monkeypatch,
) -> None:
    inquiry_id = inquiry(
        database,
        "324599122",
        "7월4일 주문 언제 배송되나요?",
        order_id="2026070448206811",
    )
    AnswerRepository(database).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.NEEDS_REVIEW,
            category="배송/안전초안",
            reason="전체 화면 주문조회 버튼 상태 검증용 기존 자동 초안",
            answer="안녕하세요, 고객님. 배송 일정은 직원 확인이 필요합니다.",
            provider="safe_rule",
            auto_answerable=False,
            needs_review=True,
            metadata={
                "selected_answer_route": "ORDER_LOOKUP_FAILED",
                "generation_mode": "SAFE_RULE",
            },
        ),
    )
    app = _open_full_dashboard_inquiry(database, monkeypatch, "324599122")

    assert not app.exception
    order_button = next(
        button
        for button in app.button
        if str(button.key or "").startswith("workspace_order_lookup_")
    )
    answer_button = next(
        button
        for button in app.button
        if str(button.key or "").startswith("review_generate_")
    )
    assert order_button.disabled is False
    assert answer_button.disabled is False
    assert any("답변 생성 시 주문 조회" in item.value for item in app.caption)


def test_full_app_actual_684104045_skips_lookups_and_enables_fallback(
    database: Database,
    monkeypatch,
) -> None:
    inquiry(
        database,
        "684104045",
        "tv로도 사용하려면 어떻게 해야 하나요??",
        inquiry_type="PRODUCT_INQUIRY",
    )
    app = _open_full_dashboard_inquiry(database, monkeypatch, "684104045")

    assert not app.exception
    answer_button = next(
        button
        for button in app.button
        if str(button.key or "").startswith("review_generate_")
    )
    assert answer_button.disabled is False
    assert any("주문 및 DPS 조회 없이" in item.value for item in app.info)
    assert any("해당 없음(SKIPPED)" in item.value for item in app.info)
    stored = InquiryRepository(database).get_by_source(
        "OJE_PLUS", "PRODUCT_INQUIRY", "684104045"
    )
    assert stored is not None
    automatic = AnswerRepository(database).active_for_inquiry(stored["id"])
    assert automatic is not None
    assert automatic["original_answer"].strip()


def test_full_app_pre_purchase_delivery_renders_template_wrapper(
    database: Database,
    monkeypatch,
) -> None:
    inquiry_id = inquiry(
        database,
        "PREBUY-APPTEST",
        "오늘 주문하면 내일 받을 수 있나요?",
        product_name="삼성 스마트모니터 M5 32인치",
    )
    app = _open_full_dashboard_inquiry(
        database,
        monkeypatch,
        "PREBUY-APPTEST",
    )
    next(
        button
        for button in app.button
        if str(button.key or "").startswith("official_select_")
    ).click()
    app = app.run(timeout=30)

    assert not app.exception
    active = AnswerRepository(database).active_for_inquiry(inquiry_id)
    assert active is not None
    assert active["metadata_json"]["selected_answer_route"] == "TEMPLATE"
    assert active["metadata_json"]["detected_intent"] == "PRE_PURCHASE_DELIVERY"
    assert active["original_answer"].startswith(DEFAULT_PREFIX)
    assert active["original_answer"].endswith(
        f"{FINAL_FALLBACK_NOTICE}\n\n{DEFAULT_CLOSING}"
    )
    assert steps(database, inquiry_id)["NAVER_ORDER_LOOKUP"]["step_status"] == "SKIPPED"
    assert steps(database, inquiry_id)["DPS_LOOKUP"]["step_status"] == "SKIPPED"
    staff_initial = next(
        area for area in app.text_area if area.label == "직원 수정본"
    )
    assert staff_initial.value == active["original_answer"]
