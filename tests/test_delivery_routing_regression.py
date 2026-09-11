from __future__ import annotations

from types import SimpleNamespace

import pytest

from answer.answer_format import format_final_answer
from answer.models import AnswerRequest, AnswerResult, AnswerStatus
from answer.answer_validator import AnswerValidator
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.answer_service import AnswerService
from services.inquiry_analysis_service import InquiryAnalysisService
from services.dps_lookup_policy import DpsLookupPolicy
from services.phase9_answer_policy import (
    DELIVERY_DATE_ANSWER,
    DELIVERY_DATE_PENDING_ANSWER,
    DELIVERY_DATE_TIME_ANSWER,
    DELIVERY_INVALID_DATE_ANSWER,
    DELIVERY_LOOKUP_FAILED_ANSWER,
    DELIVERY_NOT_FOUND_ANSWER,
    ORDER_ID_REQUEST_ANSWER,
)


class ForbiddenEngine:
    def generate(self, request):
        raise AssertionError("delivery route must skip general templates")


class ForbiddenHybrid:
    def generate(self, request, rule_result):
        raise AssertionError("delivery route must not call GPT")


class StaticEngine:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        return AnswerResult(
            status=AnswerStatus.GENERATED,
            category="정책",
            reason="policy",
            answer=self.answer,
            provider="rules",
            auto_answerable=True,
            needs_review=False,
            matched_rule="DELIVERY_NOTIFICATION_POLICY",
        )


class FakeDps:
    def __init__(self, metadata: dict | None = None) -> None:
        self.metadata = dict(metadata or {})
        self.calls: list[str] = []
        self.policy = DpsLookupPolicy()

    def enrich(self, request, **kwargs):
        self.calls.append(request.order_id)
        request.metadata["dps"] = dict(self.metadata)
        return SimpleNamespace(
            decision=SimpleNamespace(lookup_required=True),
            metadata=request.metadata["dps"],
            lookup_row=None,
        )

    def skip_for_phase9(self, request, **kwargs):
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
    value = Database(tmp_path / "delivery-routing.db")
    value.initialize()
    return value


def create_inquiry(
    database: Database,
    source_id: str,
    *,
    content: str,
    order_id: str | None = None,
    product_order_id: str | None = None,
    inquiry_type: str = "CUSTOMER_INQUIRY",
) -> int:
    return InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE",
            "source_type": "NAVER",
            "source_question_id": source_id,
            "inquiry_type": inquiry_type,
            "content": content,
            "order_id": order_id,
            "product_order_id": product_order_id,
            "raw_json": {"order_snapshot": {"protected": True}},
        }
    ).inquiry_id


def generate(
    database: Database,
    inquiry_id: int,
    dps: FakeDps,
    *,
    engine=None,
):
    return AnswerService(
        database,
        engine=engine or ForbiddenEngine(),
        dps_enrichment=dps,
        hybrid_service=ForbiddenHybrid(),
    ).generate_for_inquiry(inquiry_id)


@pytest.mark.parametrize(
    ("question", "intent"),
    [
        ("몇시에 도착할까요?", "DELIVERY_TIME"),
        ("언제 와요?", "DELIVERY_DATE"),
        ("설치 기사님 몇 시에 오시나요?", "INSTALLATION_TIME"),
        ("설치일이 언제인가요?", "INSTALLATION_DATE"),
    ],
)
def test_delivery_schedule_intents(question: str, intent: str) -> None:
    analysis = InquiryAnalysisService().analyze(
        AnswerRequest(
            question=question,
            inquiry_type="CUSTOMER_INQUIRY",
            order_id="2026072912345678",
        )
    )
    assert analysis.detected_intent == intent
    assert analysis.delivery_question is True
    assert analysis.requires_dps_lookup is True


@pytest.mark.parametrize(
    ("order_id", "product_order_id", "status"),
    [
        (None, None, "MISSING"),
        (None, "202607291234567890", "AMBIGUOUS"),
        ("ORDER-123", None, "INVALID"),
    ],
)
def _successful_dps(**values) -> FakeDps:
    metadata = {
        "lookup_required": True,
        "lookup_status": "SUCCESS",
        "installation_date": "2026-08-02",
        "required_delivery_date": "2026-08-02",
        "installation_date_source": (
            "DPS_ITEM_DETAIL_REQUIRED_DELIVERY_DATE"
        ),
        "date_parse_status": "PARSED",
    }
    metadata.update(values)
    return FakeDps(metadata)


def test_order_id_request_dedicated_validator_passes() -> None:
    validation = AnswerValidator().validate_order_id_request(
        ORDER_ID_REQUEST_ANSWER
    )
    assert validation.passed is True
    assert validation.status == "PASS"
    assert not validation.errors
    assert all(rule.status == "PASS" for rule in validation.rules)


def test_order_id_request_validator_blocks_invented_schedule() -> None:
    validation = AnswerValidator().validate_order_id_request(
        ORDER_ID_REQUEST_ANSWER + "\n2026년 8월 2일 오후 2시에 방문합니다."
    )
    assert validation.passed is False
    assert any(
        rule.code == "NO_INVENTED_SCHEDULE" and rule.status == "BLOCK"
        for rule in validation.rules
    )
