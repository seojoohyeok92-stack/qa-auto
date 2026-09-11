from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from answer.answer_format import format_final_answer
from answer.models import AnswerRequest, AnswerResult, AnswerStatus
from answer.source_adapter import answer_request_from_inquiry
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.dps_repository import DpsRepository
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from repositories.workflow_repository import WorkflowRepository
from services.answer_service import AnswerService
from services.dps_lookup_policy import DpsLookupPolicy
from services.inquiry_analysis_service import InquiryAnalysisService
from services.phase9_answer_policy import (
    DELIVERY_DATE_ANSWER,
    DELIVERY_DATE_PENDING_ANSWER,
    ORDER_ID_REQUEST_ANSWER,
)
from streamlit.testing.v1 import AppTest


def result(
    answer: str,
    *,
    status: AnswerStatus = AnswerStatus.NEEDS_REVIEW,
) -> AnswerResult:
    return AnswerResult(
        status=status,
        category="일반",
        reason="test",
        answer=answer,
        provider="rules",
        auto_answerable=status is AnswerStatus.GENERATED,
        needs_review=status is not AnswerStatus.GENERATED,
        matched_rule="TEST_TEMPLATE",
    )


class StaticEngine:
    def __init__(self, value: AnswerResult) -> None:
        self.value = value

    def generate(self, request):
        return self.value


class ForbiddenEngine:
    def generate(self, request):
        raise AssertionError(
            "Rule Engine must not be called for delivery schedules"
        )


class ForbiddenHybrid:
    def generate(self, request, rule_result):
        raise AssertionError("GPT must not be called")


class FakeDpsEnrichment:
    def __init__(self, metadata: dict | None = None) -> None:
        self.metadata = metadata
        self.calls: list[str] = []
        self.policy = DpsLookupPolicy()

    def enrich(self, request, **kwargs):
        self.calls.append(request.order_id)
        request.metadata["dps"] = dict(self.metadata or {})
        request.metadata["dps"].setdefault("cache_used", False)
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
    value = Database(tmp_path / "priority.db")
    value.initialize()
    return value


def inquiry(
    database: Database,
    source_id: str,
    *,
    question: str = "배송 언제 오나요?",
    order_id: object = None,
    product_order_id: object = None,
    registered_at: object = None,
) -> int:
    return InquiryRepository(database).upsert_work_item(
        {
            "store_code": "S",
            "source_type": "NAVER",
            "source_question_id": source_id,
            "inquiry_type": "배송",
            "content": question,
            "order_id": order_id,
            "product_order_id": product_order_id,
            "registered_at": registered_at,
            "raw_json": {},
        }
    ).inquiry_id


def test_actual_arrival_date_question_is_delivery_question() -> None:
    """Recognising the question is what this pins; the route follows evidence.

    "도착예정 날짜" is a delivery question, and that has not changed. The
    category did: with nothing said about an order this is a delivery status
    the pipeline cannot establish, not an inquiry that needs an order number.
    """

    analysis = InquiryAnalysisService().analyze(
        AnswerRequest(question="테스트) 도착예정 날짜 알고싶습니다.")
    )
    values = analysis.to_dict()

    assert values["delivery_question"] is True
    assert values["delivery_related"] is True
    assert values["needs_delivery_lookup"] is True
    assert values["question_category"] == "DELIVERY_INSTALLATION_STATUS"


def test_actual_arrival_date_question_with_an_order_needs_the_order_number() -> None:
    analysis = InquiryAnalysisService().analyze(
        AnswerRequest(question="어제 주문했는데 도착예정 날짜 알고싶습니다.")
    )
    values = analysis.to_dict()

    assert values["delivery_question"] is True
    assert values["question_category"] == "ORDER_INFO_REQUIRED"
    assert values["requires_order_id"] is True


@pytest.mark.parametrize(
    "question",
    [
        "도착예정",
        "도착 예정",
        "도착예정일",
        "언제 도착",
        "언제 받을 수",
        "배송 언제",
    ],
)
def test_delivery_schedule_phrase_regressions(question: str) -> None:
    """Each phrase must still read as a delivery-schedule question.

    ``requires_order_id`` used to stand in for that and no longer can: none of
    these fragments says an order exists, so the purchase-state policy holds
    them instead of asking for a number. The recognition is the regression
    being guarded, and the paired test below keeps the order route pinned.
    """

    analysis = InquiryAnalysisService().analyze(
        AnswerRequest(question=question)
    )
    assert analysis.delivery_question is True
    assert analysis.delivery_related is True
    assert analysis.requires_order_id is False
    assert analysis.manual_review_required is True


@pytest.mark.parametrize(
    "question",
    [
        "도착예정",
        "도착 예정",
        "도착예정일",
        "언제 도착",
        "언제 받을 수",
        "배송 언제",
    ],
)
def test_delivery_schedule_phrases_with_an_order_ask_for_the_number(
    question: str,
) -> None:
    analysis = InquiryAnalysisService().analyze(
        AnswerRequest(question=f"어제 주문했는데 {question}인가요?")
    )
    assert analysis.delivery_question is True
    assert analysis.delivery_related is True
    assert analysis.requires_order_id is True


def test_adapter_analyzes_both_title_and_content() -> None:
    request = answer_request_from_inquiry(
        {
            "id": 1,
            "title": "테스트) 도착예정 날짜 알고싶습니다.",
            "content": "확인 부탁드립니다.",
        }
    )
    assert request.question == (
        "테스트) 도착예정 날짜 알고싶습니다.\n확인 부탁드립니다."
    )
    assert request.metadata["question_source_fields"] == [
        "title",
        "content",
    ]
    assert InquiryAnalysisService().analyze(request).delivery_question


def test_empty_generated_answer_is_not_saved_as_success(
    database: Database,
) -> None:
    inquiry_id = inquiry(
        database,
        "EMPTY-GPT",
        question="이 모델의 사양을 확인해 주세요.",
    )

    class EmptyHybrid:
        def generate(self, request, rule_result):
            return SimpleNamespace(
                result=AnswerResult(
                    status=AnswerStatus.GENERATED,
                    category="일반",
                    reason="empty",
                    answer="   ",
                    provider="openai",
                    auto_answerable=True,
                    needs_review=False,
                ),
                events=(),
            )

    outcome = AnswerService(
        database,
        engine=StaticEngine(result("규칙 미매칭")),
        dps_enrichment=FakeDpsEnrichment(),
        hybrid_service=EmptyHybrid(),
    ).generate_for_inquiry(inquiry_id)

    assert outcome.result.metadata["selected_answer_route"] == (
        "REVIEW_REQUIRED_SAFE_DRAFT"
    )
    active = AnswerRepository(database).active_for_inquiry(inquiry_id)
    assert active is not None
    assert active["original_answer"].strip()


def _run_answer_panel(
    monkeypatch,
    database: Database,
    inquiry_id: int,
) -> AppTest:
    monkeypatch.setenv("OJE_AUTOMATION_DB_PATH", str(database.path))
    monkeypatch.setenv("PHASE86_INQUIRY_ID", str(inquiry_id))
    monkeypatch.setenv("PHASE86_PANEL", "answer")
    monkeypatch.setenv("QNA_GPT_PROVIDER", "fake")
    monkeypatch.delenv("PHASE86_FAKE_ANSWER", raising=False)
    app = AppTest.from_file(
        str(Path(__file__).resolve().parents[1] / "uat" / "phase86_streamlit_probe.py")
    ).run(timeout=30)
    button = next(
        item for item in app.button if item.label.endswith("답변 생성")
    )
    button.click()
    return app.run(timeout=30)
