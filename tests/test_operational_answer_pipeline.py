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
from services.dps_lookup_policy import DpsLookupPolicy
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
        self.policy = DpsLookupPolicy()

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
