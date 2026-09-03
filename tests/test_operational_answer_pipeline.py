from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from answer.models import AnswerResult, AnswerStatus
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.dps_repository import DpsRepository
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from repositories.workflow_repository import WorkflowRepository
from services.answer_service import AnswerService
from services.dps_enrichment_service import DpsEnrichmentService
from services.inquiry_analysis_service import InquiryAnalysisService
from services.phase9_answer_policy import (
    DELIVERY_DATE_ANSWER,
    DELIVERY_LOOKUP_FAILED_ANSWER,
    DELIVERY_NOT_FOUND_ANSWER,
    ORDER_ID_REQUEST_ANSWER,
)
from workflow.models import StepCode


class ForbiddenEngine:
    def generate(self, request):
        raise AssertionError("general templates must not handle delivery routes")


class ForbiddenHybrid:
    def generate(self, request, rule_result):
        raise AssertionError("GPT must not handle deterministic delivery routes")


class MissingTemplateEngine:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        return AnswerResult(
            status=AnswerStatus.NOT_SUPPORTED,
            category="PRODUCT_GENERAL",
            reason="NO_TEMPLATE",
            answer="",
            provider="rules",
            auto_answerable=False,
            needs_review=True,
        )


class ValidGeneralHybrid:
    def __init__(self) -> None:
        self.calls = 0
        self.rule_results = []

    def generate(self, request, rule_result):
        self.calls += 1
        self.rule_results.append(rule_result)
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
            reason="GPT_FALLBACK",
            answer=(
                "문의하신 자동 켜짐·꺼짐 예약 기능은 모델별 지원 여부가 "
                "다르므로 제품 모델을 확인한 뒤 안내드리겠습니다."
            ),
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


class CountingDps:
    def __init__(self, metadata: dict | None = None) -> None:
        self.metadata = dict(metadata or {})
        self.lookup_calls = 0
        self.skip_calls = 0

    def enrich(self, request, **kwargs):
        self.lookup_calls += 1
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


@pytest.fixture
def database(tmp_path) -> Database:
    value = Database(tmp_path / "operational-pipeline.db")
    value.initialize()
    return value


def _inquiry(
    database: Database,
    source_id: str,
    *,
    content: str,
    order_id: str | None = None,
    inquiry_type: str = "CUSTOMER_INQUIRY",
) -> int:
    return InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "NAVER",
            "source_question_id": source_id,
            "inquiry_type": inquiry_type,
            "title": "상품 문의" if inquiry_type == "PRODUCT_INQUIRY" else "언제올까요?",
            "content": content,
            "order_id": order_id,
            "order_date": "2026-07-01",
            "raw_json": {},
        }
    ).inquiry_id


def _steps(database: Database, inquiry_id: int) -> dict[str, dict]:
    return {
        row["step_code"]: row
        for row in WorkflowRepository(database).list_steps(inquiry_id)
    }


def test_product_feature_uses_one_click_gpt_fallback_without_lookups(
    database: Database,
) -> None:
    inquiry_id = _inquiry(
        database,
        "CASE-A",
        inquiry_type="PRODUCT_INQUIRY",
        content="요일, 시간 설정 후 자동 ON, OFF 기능이 있나요?",
    )
    engine = MissingTemplateEngine()
    hybrid = ValidGeneralHybrid()
    dps = CountingDps()

    outcome = AnswerService(
        database,
        engine=engine,
        hybrid_service=hybrid,
        dps_enrichment=dps,
    ).generate_for_inquiry(inquiry_id)

    analysis = outcome.draft["inquiry_analysis_json"]
    assert analysis["requires_order_lookup"] is False
    assert analysis["requires_dps_lookup"] is False
    assert engine.calls == 1
    assert hybrid.calls == 1
    assert hybrid.rule_results[0].provider == "template_fallback_context"
    assert hybrid.rule_results[0].needs_review is False
    assert dps.lookup_calls == 0
    assert outcome.draft["metadata_json"]["generation_mode"] == "GPT_FALLBACK"
    assert AnswerRepository(database).active_for_inquiry(inquiry_id)["id"] == (
        outcome.draft["id"]
    )
    steps = _steps(database, inquiry_id)
    assert steps["NAVER_ORDER_LOOKUP"]["step_status"] == "SKIPPED"
    assert steps["DPS_LOOKUP"]["step_status"] == "SKIPPED"
    assert steps["ANSWER_GENERATED"]["step_status"] == "COMPLETED"


def test_operating_inquiry_684104045_uses_one_click_gpt_fallback(
    database: Database,
) -> None:
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": "684104045",
            "external_inquiry_id": "684104045",
            "inquiry_type": "PRODUCT_INQUIRY",
            "title": "상품 문의",
            "content": "tv로도 사용하려면 어떻게 해야 하나요??",
            "product_name": (
                "삼성 삼탠바이미 32인치(80cm) M5 스마트 모니터 "
                "IPTV+2in1 이동식 거치대"
            ),
            "raw_json": {},
        }
    ).inquiry_id
    engine = MissingTemplateEngine()
    hybrid = ValidGeneralHybrid()
    dps = CountingDps()

    outcome = AnswerService(
        database,
        engine=engine,
        hybrid_service=hybrid,
        dps_enrichment=dps,
    ).generate_for_inquiry(
        inquiry_id,
        prefer_template=True,
        correlation_id="case-684104045",
    )

    metadata = outcome.draft["metadata_json"]
    analysis = outcome.draft["inquiry_analysis_json"]
    assert analysis["requires_order_lookup"] is False
    assert analysis["requires_dps_lookup"] is False
    assert engine.calls == 1
    assert hybrid.calls == 1
    assert dps.lookup_calls == 0
    assert metadata["generation_mode"] == "GPT_FALLBACK"
    assert metadata["selected_answer_route"] == "GPT_FALLBACK"
    assert metadata["template_preferred"] is True
    assert metadata["template_override"] is False
    assert metadata["gpt_called"] is True
    assert metadata["dps_lookup_attempted"] is False
    assert AnswerRepository(database).active_for_inquiry(inquiry_id)["id"] == (
        outcome.draft["id"]
    )
    assert _steps(database, inquiry_id)["ANSWER_GENERATED"][
        "step_status"
    ] == "COMPLETED"
    events = {
        row["event_code"]
        for row in LogRepository(database).recent_for_inquiry(inquiry_id)
    }
    assert {
        "ANSWER_ROUTING_STARTED",
        "TEMPLATE_SEARCH_STARTED",
        "TEMPLATE_NOT_FOUND",
        "GPT_FALLBACK_STARTED",
        "GPT_FALLBACK_SUCCESS",
        "ANSWER_VALIDATION_PASSED",
        "DRAFT_CREATED",
        "DRAFT_ACTIVATED",
    }.issubset(events)


def test_missing_order_delivery_creates_valid_request_without_external_calls(
    database: Database,
) -> None:
    # 구매 사실이 확인되는 문의여야 주문번호 요청 경로에 도달한다. 구매 상태가
    # 확인되지 않으면 정책상 보류되어 요청 템플릿 자체가 선택되지 않는다.
    inquiry_id = _inquiry(
        database, "CASE-B", content="어제 주문했는데 언제올까요?"
    )
    dps = CountingDps()
    outcome = AnswerService(
        database,
        engine=ForbiddenEngine(),
        hybrid_service=ForbiddenHybrid(),
        dps_enrichment=dps,
    ).generate_for_inquiry(inquiry_id)

    analysis = outcome.draft["inquiry_analysis_json"]
    metadata = outcome.draft["metadata_json"]
    assert analysis["requires_order_lookup"] is True
    assert analysis["requires_dps_lookup"] is True
    assert analysis["can_execute_dps_lookup"] is False
    assert metadata["selected_answer_route"] == "ORDER_ID_REQUEST"
    assert metadata["hybrid"]["validation"]["passed"] is True
    assert outcome.result.answer == ORDER_ID_REQUEST_ANSWER
    assert dps.lookup_calls == 0
    assert metadata["gpt_called"] is False
    steps = _steps(database, inquiry_id)
    assert steps["NAVER_ORDER_LOOKUP"]["step_status"] == "NEEDS_REVIEW"
    assert steps["DPS_LOOKUP"]["step_status"] == "SKIPPED"
    assert steps["ANSWER_GENERATED"]["step_status"] == "COMPLETED"


def test_operating_phrase_and_verified_past_date_create_delivery_answer(
    database: Database,
) -> None:
    inquiry_id = _inquiry(
        database,
        "CASE-C",
        content="주문한지 한달이네요. 언제쯤 받을수있을까요??",
        order_id="2026072912345678",
    )
    dps = CountingDps(
        {
            "lookup_required": True,
            "lookup_status": "SUCCESS",
            "required_delivery_date": "2026-08-03",
            "installation_date": "2026-08-03",
            "installation_date_source": "DPS_ITEM_DETAIL_REQUIRED_DELIVERY_DATE",
            "date_parse_status": "PARSED",
            "source": "DPS_AGENT",
        }
    )
    outcome = AnswerService(
        database,
        engine=ForbiddenEngine(),
        hybrid_service=ForbiddenHybrid(),
        dps_enrichment=dps,
    ).generate_for_inquiry(inquiry_id)

    assert outcome.draft["inquiry_analysis_json"]["detected_intent"] == (
        "DELIVERY_DATE"
    )
    assert outcome.result.answer == DELIVERY_DATE_ANSWER.format(
        delivery_date="2026년 8월 3일"
    )
    assert outcome.result.metadata["selected_answer_route"] == (
        "DELIVERY_WITH_INSTALLATION_DATE"
    )
    assert outcome.result.metadata["hybrid"]["validation"]["passed"] is True
    assert dps.lookup_calls == 1
    assert outcome.result.metadata["gpt_called"] is False
    assert _steps(database, inquiry_id)["ANSWER_GENERATED"]["step_status"] == (
        "COMPLETED"
    )


@pytest.mark.parametrize(
    ("raw", "expected_status", "expected_answer"),
    [
        ({"success": False, "code": "AGENT_CONNECTION_FAILED"}, "AGENT_OFFLINE", DELIVERY_LOOKUP_FAILED_ANSWER),
        ({"success": False, "code": "AGENT_READ_TIMEOUT"}, "TIMEOUT", DELIVERY_LOOKUP_FAILED_ANSWER),
        ({"success": False, "code": "DETAIL_PARSE_FAILED"}, "PARSE_ERROR", DELIVERY_LOOKUP_FAILED_ANSWER),
        ({"success": False, "code": "AGENT_REQUEST_FAILED"}, "AUTOMATION_ERROR", DELIVERY_LOOKUP_FAILED_ANSWER),
        ({"success": True, "found": False, "code": "NO_DPS_RESULT"}, "NOT_FOUND", DELIVERY_NOT_FOUND_ANSWER),
    ],
)
def test_all_dps_failures_create_safe_active_draft(
    database: Database,
    raw: dict,
    expected_status: str,
    expected_answer: str,
) -> None:
    inquiry_id = _inquiry(
        database,
        f"DPS-{expected_status}",
        content="배송 언제 오나요?",
        order_id="2026072912345678",
    )
    workflows = WorkflowRepository(database)
    workflows.initialize_steps(inquiry_id)
    workflows.complete_step(inquiry_id, StepCode.ORDER_IDENTIFIED)
    workflows.complete_step(inquiry_id, StepCode.NAVER_ORDER_LOOKUP)
    enrichment = DpsEnrichmentService(database, client=lambda **kwargs: raw)
    outcome = AnswerService(
        database,
        engine=ForbiddenEngine(),
        hybrid_service=ForbiddenHybrid(),
        dps_enrichment=enrichment,
    ).generate_for_inquiry(inquiry_id)

    assert outcome.result.answer == expected_answer
    assert outcome.result.metadata["answer_source"] == "SAFE_TEMPLATE"
    assert outcome.result.metadata["generation_mode"] == "RULE"
    assert outcome.result.metadata["gpt_called"] is False
    assert AnswerRepository(database).active_for_inquiry(inquiry_id)["id"] == (
        outcome.draft["id"]
    )
    steps = _steps(database, inquiry_id)
    assert steps["NAVER_ORDER_LOOKUP"]["step_status"] == "COMPLETED"
    assert steps["DPS_LOOKUP"]["step_status"] in {"FAILED", "NEEDS_REVIEW"}
    assert steps["ANSWER_GENERATED"]["step_status"] == "COMPLETED"


def test_dps_client_exception_and_corrupt_cache_still_create_safe_draft(
    database: Database,
) -> None:
    inquiry_id = _inquiry(
        database,
        "DPS-CORRUPT-CACHE",
        content="설치 예정일을 알고 싶습니다.",
        order_id="2026072912345678",
    )
    cached = DpsRepository(database).create_lookup_result(
        inquiry_id=inquiry_id,
        order_id="2026072912345678",
        lookup_status="SUCCESS",
        raw_result={},
        normalized_result={"lookup_status": "SUCCESS"},
        queried_at=datetime.now(UTC).isoformat(),
        expires_at="2099-01-01T00:00:00+00:00",
    )
    with database.transaction() as connection:
        connection.execute(
            "UPDATE dps_lookup_results SET normalized_result_json=? WHERE id=?",
            ("{broken", cached["id"]),
        )

    def unavailable(**kwargs):
        raise ConnectionError("network unavailable")

    outcome = AnswerService(
        database,
        engine=ForbiddenEngine(),
        hybrid_service=ForbiddenHybrid(),
        dps_enrichment=DpsEnrichmentService(database, client=unavailable),
    ).generate_for_inquiry(inquiry_id)

    assert outcome.result.answer == DELIVERY_LOOKUP_FAILED_ANSWER
    assert outcome.result.metadata["selected_answer_route"] == "DPS_LOOKUP_FAILED"
    assert outcome.result.metadata["answer_source"] == "SAFE_TEMPLATE"
    assert _steps(database, inquiry_id)["ANSWER_GENERATED"]["step_status"] == (
        "COMPLETED"
    )
    events = {
        row["event_code"]
        for row in LogRepository(database).recent_for_inquiry(
            inquiry_id, limit=100
        )
    }
    assert "DPS_CACHE_CORRUPTED" in events
    assert "DRAFT_CREATED" in events


@pytest.mark.parametrize(
    "value",
    [
        "2026-08-03",
        "2026-08-03T00:00:00",
        "2026-08-03T00:00:00+09:00",
        datetime(2026, 8, 3, tzinfo=UTC),
    ],
)
def test_installation_date_formats_share_one_canonical_answer(
    database: Database,
    value,
) -> None:
    inquiry_id = _inquiry(
        database,
        f"DATE-{value!s}",
        content="설치일이 언제인가요?",
        order_id="2026072912345678",
    )
    dps = CountingDps(
        {
            "lookup_status": "SUCCESS",
            "installation_date": value,
            "installation_date_source": "DPS_ITEM_DETAIL_REQUIRED_DELIVERY_DATE",
            "date_parse_status": "PARSED",
        }
    )
    outcome = AnswerService(
        database,
        engine=ForbiddenEngine(),
        hybrid_service=ForbiddenHybrid(),
        dps_enrichment=dps,
    ).generate_for_inquiry(inquiry_id)
    assert "2026년 8월 3일" in outcome.result.answer
