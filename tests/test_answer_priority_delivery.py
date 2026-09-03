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


def test_delivery_schedule_skips_existing_template_search(
    database: Database,
) -> None:
    """The subject is template search, so the inquiry states its order.

    This case is about a delivery-schedule question going straight to the
    order-number route instead of searching approved templates first. It used
    to say only "배송 언제 오나요?", which under the purchase-state policy is a
    question from someone who may not have ordered and is now held -- a
    different route, and no longer this test's subject. Saying the order exists
    keeps the case pointed at what it was written for; the hold itself is
    covered by test_unconfirmed_delivery_schedule_is_held_without_a_template.
    """

    inquiry_id = inquiry(
        database, "TEMPLATE-FIRST", question="주문했는데 배송 일정 알려주세요.",
    )
    dps = FakeDpsEnrichment()
    outcome = AnswerService(
        database,
        engine=ForbiddenEngine(),
        dps_enrichment=dps,
        hybrid_service=ForbiddenHybrid(),
    ).generate_for_inquiry(inquiry_id)

    assert outcome.result.answer == ORDER_ID_REQUEST_ANSWER
    assert outcome.result.metadata["answer_type"] == "order_id_required"
    assert outcome.result.metadata["gpt_called"] is False
    assert dps.calls == []


def test_unconfirmed_delivery_schedule_is_held_without_a_template(
    database: Database,
) -> None:
    """The same question with no order stated: still no template, still no GPT.

    Template search is skipped either way -- that is the behaviour above --
    but with nothing said about an order there is no order number to ask for,
    so the draft is a review-safe one rather than the request template.
    """

    inquiry_id = inquiry(database, "TEMPLATE-FIRST-UNCONFIRMED")
    dps = FakeDpsEnrichment()
    outcome = AnswerService(
        database,
        engine=ForbiddenEngine(),
        dps_enrichment=dps,
        hybrid_service=ForbiddenHybrid(),
    ).generate_for_inquiry(inquiry_id)

    assert outcome.result.answer != ORDER_ID_REQUEST_ANSWER
    assert outcome.result.metadata["requires_manual_review"] is True
    assert outcome.result.metadata["gpt_called"] is False
    assert dps.calls == []


def test_general_inquiry_still_uses_existing_template_before_gpt(
    database: Database,
) -> None:
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "S",
            "source_type": "NAVER",
            "source_question_id": "GENERAL-TEMPLATE",
            "inquiry_type": "상품",
            "content": "온누리상품권 신청 방법이 궁금합니다.",
            "raw_json": {},
        }
    ).inquiry_id
    exact = "운영자가 등록한 기존 답변 원문"
    outcome = AnswerService(
        database,
        engine=StaticEngine(
            result(exact, status=AnswerStatus.GENERATED)
        ),
        dps_enrichment=FakeDpsEnrichment(),
        hybrid_service=ForbiddenHybrid(),
    ).generate_for_inquiry(inquiry_id)

    assert outcome.result.answer == format_final_answer(exact)
    assert outcome.result.metadata["answer_type"] == "existing_template"
    assert outcome.result.metadata["gpt_called"] is False


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


@pytest.mark.parametrize(
    ("order_id", "product_order_id"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        (None, "202607301234567890"),
    ],
)
def test_delivery_without_general_order_id_creates_fixed_draft(
    database: Database,
    order_id: object,
    product_order_id: object,
) -> None:
    inquiry_id = inquiry(
        database,
        f"NO-ORDER-{repr(order_id)}-{repr(product_order_id)}",
        # The subject is the *missing general order number*, so the customer
        # has to be one who says they ordered -- otherwise the purchase-state
        # policy holds the inquiry and there is no fixed draft to assert on.
        question="주문했는데 배송 일정 알려주세요.",
        order_id=order_id,
        product_order_id=product_order_id,
    )
    dps = FakeDpsEnrichment()
    outcome = AnswerService(
        database,
        engine=ForbiddenEngine(),
        dps_enrichment=dps,
        hybrid_service=ForbiddenHybrid(),
    ).generate_for_inquiry(inquiry_id)

    assert dps.calls == []
    assert outcome.result.answer == ORDER_ID_REQUEST_ANSWER
    assert outcome.draft["original_answer"] == ORDER_ID_REQUEST_ANSWER
    assert outcome.result.metadata["answer_type"] == "order_id_required"
    assert outcome.result.metadata["answer_source"] == "ORDER_ID_REQUEST"
    assert outcome.result.metadata["generation_mode"] == "RULE"
    assert outcome.draft["validation_status"] == "PASS"
    assert outcome.result.metadata["dps_lookup_attempted"] is False
    assert outcome.result.metadata["gpt_called"] is False
    assert bool(outcome.draft["is_active"])
    analysis_log = next(
        row
        for row in LogRepository(database).recent_for_inquiry(inquiry_id)
        if row["event_code"] == "INQUIRY_ANALYSIS_INPUT"
    )
    assert analysis_log["details_json"]["inquiry_text"] == (
        "주문했는데 배송 일정 알려주세요."
    )
    assert analysis_log["details_json"]["delivery_question"] is True
    steps = {
        row["step_code"]: row
        for row in WorkflowRepository(database).list_steps(inquiry_id)
    }
    assert steps["ORDER_IDENTIFIED"]["step_status"] == "NEEDS_REVIEW"
    assert steps["NAVER_ORDER_LOOKUP"]["last_error_code"] == (
        "CUSTOMER_INFORMATION_REQUIRED"
    )
    assert steps["DPS_LOOKUP"]["step_status"] == "SKIPPED"
    assert steps["ANSWER_GENERATED"]["step_status"] == "COMPLETED"


@pytest.mark.parametrize(
    ("raw_date", "display_date"),
    [
        ("2026-08-05", "2026년 8월 5일"),
        ("2026-08-05T00:00:00", "2026년 8월 5일"),
    ],
)
def test_dps_required_delivery_date_is_inserted_without_gpt(
    database: Database,
    raw_date: str,
    display_date: str,
) -> None:
    inquiry_id = inquiry(
        database,
        f"DPS-DATE-{raw_date}",
        order_id="2026073012345678",
    )
    dps = FakeDpsEnrichment(
        {
            "lookup_required": True,
            "lookup_status": "SUCCESS",
            "required_delivery_date": raw_date,
            "installation_date": raw_date,
            "installation_date_source": (
                "DPS_ITEM_DETAIL_REQUIRED_DELIVERY_DATE"
            ),
            "date_parse_status": "PARSED",
        }
    )
    outcome = AnswerService(
        database,
        engine=ForbiddenEngine(),
        dps_enrichment=dps,
        hybrid_service=ForbiddenHybrid(),
    ).generate_for_inquiry(inquiry_id)

    assert dps.calls == ["2026073012345678"]
    assert outcome.result.answer == DELIVERY_DATE_ANSWER.format(
        delivery_date=display_date
    )
    assert outcome.result.metadata["answer_type"] == "delivery_date"
    assert outcome.result.metadata["delivery_date_found"] is True


def test_successful_dps_without_date_creates_pending_template(
    database: Database,
) -> None:
    inquiry_id = inquiry(
        database,
        "DPS-PENDING",
        order_id="2026073012345678",
    )
    dps = FakeDpsEnrichment(
        {
            "lookup_required": True,
            "lookup_status": "SUCCESS",
            "required_delivery_date": None,
            "installation_date": None,
            "installation_date_source": None,
            "date_parse_status": "MISSING",
        }
    )
    outcome = AnswerService(
        database,
        engine=ForbiddenEngine(),
        dps_enrichment=dps,
        hybrid_service=ForbiddenHybrid(),
    ).generate_for_inquiry(inquiry_id)

    assert outcome.result.answer == DELIVERY_DATE_PENDING_ANSWER
    assert outcome.result.metadata["answer_type"] == "delivery_date_pending"
    assert outcome.result.metadata["delivery_date_found"] is False


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


def test_streamlit_missing_order_click_displays_request_template(
    database: Database,
    monkeypatch,
) -> None:
    inquiry_id = inquiry(
        database, "UI-NO-ORDER", question="주문했는데 배송 일정 알려주세요.",
    )
    app = _run_answer_panel(monkeypatch, database, inquiry_id)

    assert not app.exception
    program = next(
        area for area in app.text_area if area.label == "Program Answer"
    )
    assert program.key == f"draft_text_{inquiry_id}"
    assert program.value == ORDER_ID_REQUEST_ANSWER
    assert any("초안이 작성되었습니다" in item.value for item in app.success)


def test_streamlit_cached_dps_date_click_displays_delivery_template(
    database: Database,
    monkeypatch,
) -> None:
    inquiry_id = inquiry(
        database,
        "UI-DPS-DATE",
        order_id="2026073012345678",
        # The cached DPS date below is 2026-08-05, so the inquiry has to have
        # been raised on or before it -- otherwise the schedule is genuinely
        # stale and this stops being a test of the delivery template.
        registered_at="2026-08-04T09:00:00+09:00",
    )
    DpsRepository(database).create_lookup_result(
        inquiry_id=inquiry_id,
        order_id="2026073012345678",
        lookup_status="SUCCESS",
        raw_result={},
        normalized_result={
            "lookup_required": True,
            "lookup_status": "SUCCESS",
            "required_delivery_date": "2026-08-05",
            "installation_date": "2026-08-05",
            "installation_date_source": (
                "DPS_ITEM_DETAIL_REQUIRED_DELIVERY_DATE"
            ),
            "date_parse_status": "PARSED",
            "requires_human_review": False,
            "cache_used": True,
        },
        expires_at="2099-01-01T00:00:00+09:00",
    )
    app = _run_answer_panel(monkeypatch, database, inquiry_id)

    assert not app.exception
    program = next(
        area for area in app.text_area if area.label == "Program Answer"
    )
    assert program.key == f"draft_text_{inquiry_id}"
    assert program.value == DELIVERY_DATE_ANSWER.format(
        delivery_date="2026년 8월 5일"
    )
    assert any("초안이 작성되었습니다" in item.value for item in app.success)
