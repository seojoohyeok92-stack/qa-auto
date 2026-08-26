"""Routing for "when is my order coming?", in every wording customers use.

Production inquiry 686472270 asked "언제 발송되나요?" with no order number. The
classifier's delivery vocabulary listed 배송 and 출고 but never 발송, and no
entry tolerated a particle between the noun and 언제 ("배송이 언제"). The
question fell through to UNCLASSIFIED with ``requires_order_lookup=False``, so
the processing plan never reached the ORDER_ID_REQUEST branch; the rule
engine's shipping fall-through answered instead, with guidance about *when
installation notices are sent*, and that was auto-posted to a customer asking
about their own shipment.

These tests pin the shape of the question rather than its spelling, and they
pin both directions: a current-order schedule question must require an order
lookup, and a notification-policy or pre-purchase question must not become one.
"""
from __future__ import annotations

import pytest

from answer.source_adapter import answer_request_from_inquiry
from answer.text_utils import (
    CURRENT_DELIVERY_SCHEDULE_QUERY,
    CURRENT_INSTALLATION_SCHEDULE_QUERY,
)
from services.inquiry_analysis_service import InquiryAnalysisService


PRODUCT = "삼성 4K UHD 스마트 사이니지 TV 43인치 스탠드형"
VALID_ORDER = "2026082351391541"


def analyse(question: str, order_id: str = "") -> dict:
    inquiry = {
        "id": 1,
        "source_type": "PRODUCT_INQUIRY",
        "inquiry_type": "PRODUCT_INQUIRY",
        "source_question_id": "dr",
        "external_inquiry_id": "dr",
        "title": "상품 문의",
        "content": question,
        "product_name": PRODUCT,
        "order_id": order_id,
        "product_order_id": "",
        "raw_json": {},
        "source_answered": 0,
        "post_status": "NOT_POSTED",
    }
    return (
        InquiryAnalysisService()
        .analyze(answer_request_from_inquiry(inquiry))
        .to_dict()
    )


def is_order_number_required(analysis: dict) -> bool:
    """The route that produces the fixed "please send your order number" reply."""

    return bool(
        analysis.get("requires_order_lookup")
        and str(analysis.get("answer_strategy") or "") == "REQUEST_ORDER_ID"
    )


# --------------------------------------------------------------------------
# DR-01 .. DR-06 -- a current-order schedule question, however it is worded
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "question"),
    [
        ("DR-01", "언제 발송되나요?"),
        ("DR-02", "배송 언제 되나요?"),
        ("DR-03", "언제 출고되나요?"),
        ("DR-04", "언제 받을 수 있나요?"),
        ("DR-05", "설치는 언제 되나요?"),
        ("DR-06", "기사님 언제 오시나요?"),
        # Same shape, other spellings -- these are why the fix matches a shape.
        ("DR-01b", "언제 발송돼요?"),
        ("DR-01c", "배송이 언제 시작되나요?"),
        ("DR-01d", "아직 발송 안 됐는데 언제 보내주시나요?"),
        ("DR-02b", "언제 배송되나요?"),
        ("DR-03b", "출고 언제 해주시나요?"),
        ("DR-04b", "언제쯤 받을까요?"),
        ("DR-05b", "설치 예정일 알려주세요."),
        ("DR-05c", "배송 예정일이 언제인가요?"),
        ("DR-05d", "배송 일정 알려주세요."),
    ],
)
def test_current_schedule_question_requires_an_order_number(
    case: str, question: str
) -> None:
    analysis = analyse(question)

    assert analysis["requires_order_lookup"] is True, case
    assert is_order_number_required(analysis), (case, analysis["answer_strategy"])


def test_dr01_the_exact_production_question_no_longer_falls_through() -> None:
    """686472270 verbatim."""

    analysis = analyse("언제 발송되나요?")

    assert analysis["inquiry_subtype"] == "DELIVERY_OR_INSTALLATION_SCHEDULE"
    assert analysis["detected_intent"] == "DELIVERY_DATE"
    assert analysis["question_category"] == "ORDER_INFO_REQUIRED"
    assert analysis["requires_order_lookup"] is True


# --------------------------------------------------------------------------
# DR-07 / DR-08 -- notification policy is a different question
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "question"),
    [
        ("DR-07", "설치 예정일 알림톡은 언제 발송되나요?"),
        ("DR-08", "기사 방문 전 알림톡은 언제 오나요?"),
        ("DR-08b", "배송 안내 문자는 언제 오나요?"),
    ],
)
def test_notification_policy_is_not_a_schedule_lookup(
    case: str, question: str
) -> None:
    """"When is the notice sent?" asks about the notice, not the shipment.

    Both contain 발송 and 언제, so a keyword-level fix would have merged them.
    The notification check runs first and keeps them apart.
    """

    analysis = analyse(question)

    assert analysis["detected_intent"] == "NOTIFICATION_POLICY", case
    assert analysis["requires_order_lookup"] is False, case
    assert not is_order_number_required(analysis), case


# --------------------------------------------------------------------------
# DR-09 -- an order number present must not be asked for again
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    ["언제 발송되나요?", "배송 언제 되나요?", "설치는 언제 되나요?"],
)
def test_dr09_existing_order_number_takes_the_lookup_path(question: str) -> None:
    analysis = analyse(question, order_id=VALID_ORDER)

    assert analysis["requires_order_lookup"] is True
    assert analysis["answer_strategy"] == "DIRECT_FACT_ANSWER"
    assert not is_order_number_required(analysis)


# --------------------------------------------------------------------------
# DR-10 / DR-11 -- asking us to change the schedule is not asking what it is
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "question", "order_id"),
    [
        ("DR-10", "배송일을 다음 주로 변경해주세요.", VALID_ORDER),
        ("DR-10b", "설치일을 이번 주로 당겨주세요.", VALID_ORDER),
        ("DR-11", "배송 좀 빨리 해주세요.", ""),
        ("DR-11b", "더 빨리 받을 수 있나요?", ""),
    ],
)
def test_schedule_change_protection_is_unchanged(
    case: str, question: str, order_id: str
) -> None:
    analysis = analyse(question, order_id=order_id)
    is_change = (
        analysis.get("detected_intent") == "SCHEDULE_CHANGE"
        or analysis.get("inquiry_subtype") == "SCHEDULE_CHANGE_REQUEST"
    )

    assert is_change or analysis.get("manual_review_required") is True, (
        case, analysis.get("detected_intent"), analysis.get("inquiry_subtype")
    )
    assert not is_order_number_required(analysis), case


# --------------------------------------------------------------------------
# DR-12 .. DR-14 -- questions that are not about one order's current schedule
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "question"),
    [
        ("DR-12", "배송 가능한 지역인가요?"),
        ("DR-13", "배송비가 얼마인가요?"),
        ("DR-14", "오늘 주문하면 언제 받을 수 있나요?"),
        ("DR-14b", "지금 주문하면 배송 얼마나 걸리나요?"),
    ],
)
def test_non_schedule_questions_do_not_ask_for_an_order_number(
    case: str, question: str
) -> None:
    analysis = analyse(question)

    assert not is_order_number_required(analysis), (
        case, analysis.get("detected_intent"), analysis.get("answer_strategy")
    )


# --------------------------------------------------------------------------
# DR-15 -- the Naver inquiry type is not the business intent
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source_type", ["PRODUCT_INQUIRY", "CUSTOMER_INQUIRY"]
)
def test_dr15_external_inquiry_type_does_not_block_the_order_lookup(
    source_type: str,
) -> None:
    """A product-board question can still be about the asker's own order."""

    inquiry = {
        "id": 1,
        "source_type": source_type,
        "inquiry_type": source_type,
        "source_question_id": "dr15",
        "external_inquiry_id": "dr15",
        "title": "상품 문의",
        "content": "언제 발송되나요?",
        "product_name": PRODUCT,
        "order_id": "",
        "product_order_id": "",
        "raw_json": {},
        "source_answered": 0,
        "post_status": "NOT_POSTED",
    }
    analysis = (
        InquiryAnalysisService()
        .analyze(answer_request_from_inquiry(inquiry))
        .to_dict()
    )

    assert analysis["requires_order_lookup"] is True
    assert is_order_number_required(analysis)


# --------------------------------------------------------------------------
# The shared predicate itself, and the engine's second line
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "언제발송되나요", "발송언제되나요", "배송이언제시작되나요",
        "언제출고되나요", "언제받을수있나요", "언제보내주시나요",
        "수령은언제인가요",
    ],
)
def test_shared_predicate_matches_the_shape(question: str) -> None:
    assert CURRENT_DELIVERY_SCHEDULE_QUERY.search(question)


@pytest.mark.parametrize(
    "question", ["설치는언제되나요", "기사님언제오시나요", "언제설치되나요", "방문은언제인가요"]
)
def test_shared_predicate_matches_installation_shape(question: str) -> None:
    assert CURRENT_INSTALLATION_SCHEDULE_QUERY.search(question)


@pytest.mark.parametrize(
    "question", ["배송비가얼마인가요", "배송가능한지역인가요", "설치방법알려주세요"]
)
def test_shared_predicate_does_not_match_non_schedule_questions(
    question: str,
) -> None:
    assert not CURRENT_DELIVERY_SCHEDULE_QUERY.search(question)
    assert not CURRENT_INSTALLATION_SCHEDULE_QUERY.search(question)


def test_engine_fallthrough_no_longer_answers_a_schedule_question() -> None:
    """The second line: even reached directly, this branch declines.

    It used to return the installation-notification guidance, which is what
    was auto-posted to 686472270.
    """

    from answer.engine import AnswerEngine

    result = AnswerEngine().answer(PRODUCT, "언제 발송되나요?", "")

    assert "알림톡" not in result.answer
    assert result.status != "답변 가능"


# --------------------------------------------------------------------------
# DR-16 .. DR-21 -- the same intent, then the order number decides the branch
#
# Production inquiry 325262026 asked "모니터 언제 발송되나요?" *with* a valid
# order number 2026082513661591 and still got the installation-notification
# guidance, with DPS reported as not required and never called. Same root
# cause as 686472270: 발송 was not a schedule word, so the delivery intent was
# never detected and the branch on "do we have an order number?" never
# happened. These pin the other side of that branch.
# --------------------------------------------------------------------------


CASE_B_ORDER = "2026082513661591"


@pytest.mark.parametrize(
    ("case", "question"),
    [
        ("DR-16", "모니터 언제 발송되나요?"),
        ("DR-17", "상품 언제 발송되나요?"),
        ("DR-18", "TV 언제 배송되나요?"),
        ("DR-19", "모니터 언제 출고되나요?"),
        ("DR-20", "설치 예정일이 언제인가요?"),
    ],
)
def test_valid_order_number_takes_the_dps_path(case: str, question: str) -> None:
    analysis = analyse(question, order_id=CASE_B_ORDER)

    assert analysis["requires_order_lookup"] is True, case
    assert analysis["requires_dps_lookup"] is True, case
    assert analysis["detected_intent"] in {
        "DELIVERY_DATE", "INSTALLATION_DATE",
    }, (case, analysis["detected_intent"])
    # The order number is present, so the fixed request template must not fire.
    assert not is_order_number_required(analysis), case
    assert analysis["answer_strategy"] == "DIRECT_FACT_ANSWER", case


@pytest.mark.parametrize("order_id", ["", CASE_B_ORDER])
def test_dr21_notification_policy_wins_whether_or_not_an_order_exists(
    order_id: str,
) -> None:
    """"설치 알림톡은 언제 발송되나요?" is about the notice, not the shipment."""

    analysis = analyse("설치 알림톡은 언제 발송되나요?", order_id=order_id)

    assert analysis["detected_intent"] == "NOTIFICATION_POLICY"
    assert analysis["requires_order_lookup"] is False
    assert analysis["requires_dps_lookup"] is False


def test_case_b_verbatim_requires_dps() -> None:
    """325262026 verbatim, CUSTOMER_INQUIRY board."""

    inquiry = {
        "id": 1,
        "source_type": "CUSTOMER_INQUIRY",
        "inquiry_type": "CUSTOMER_INQUIRY",
        "source_question_id": "325262026",
        "external_inquiry_id": "325262026",
        "title": "고객 문의",
        "content": "모니터 언제 발송되나요?",
        "product_name": "삼성 스마트 모니터 M5 32인치",
        "order_id": CASE_B_ORDER,
        "product_order_id": "",
        "raw_json": {},
        "source_answered": 0,
        "post_status": "NOT_POSTED",
    }
    analysis = (
        InquiryAnalysisService()
        .analyze(answer_request_from_inquiry(inquiry))
        .to_dict()
    )

    assert analysis["detected_intent"] == "DELIVERY_DATE"
    assert analysis["requires_order_lookup"] is True
    assert analysis["requires_dps_lookup"] is True


def test_the_two_cases_share_one_intent_and_differ_only_on_the_order_number() -> None:
    """The branch the pipeline is supposed to make, stated as one assertion."""

    without = analyse("언제 발송되나요?")
    with_order = analyse("모니터 언제 발송되나요?", order_id=CASE_B_ORDER)

    assert without["detected_intent"] == with_order["detected_intent"] == "DELIVERY_DATE"
    assert without["requires_order_lookup"] is with_order["requires_order_lookup"] is True

    # No order number -> ask for it, and do not touch DPS.
    assert is_order_number_required(without)
    # Order number present -> look it up and consult DPS.
    assert with_order["answer_strategy"] == "DIRECT_FACT_ANSWER"
    assert with_order["requires_dps_lookup"] is True
