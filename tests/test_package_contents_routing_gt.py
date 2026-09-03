"""A shipping word in the sentence is not the same as shipping being the question.

Production inquiry, 삼성 삼탠바이미 32인치 M5 스마트 모니터:

    "안녕하세요 혹시 배송 올때 조립에 필요한 일회용? 공구도 같이 오나요?
     혹시 제가 개별로 준비해야하는 공구가 있나요?"

was answered with "택배배송 상품은 오후 3시 이전 결제 주문에 한해 당일
발송되며, 배송은 보통 1영업일에서 2영업일 정도 소요됩니다..." and auto-posted.

The rule engine's shipping block is entered on a bare keyword OR -- 배송, 언제,
받을수, 도착, 설치기사님 -- which is recall-first on purpose so no shipping
question is missed. Nothing afterwards checked what the question was *about*,
so the shipment being merely the occasion ("배송 올 때") was enough. These tests
pin the distinction in both directions: an item-in-the-box question must not be
answered by a shipping rule, and a real shipping question must still be.
"""
from __future__ import annotations

import pytest

from answer.engine import AnswerEngine
from answer.source_adapter import answer_request_from_inquiry
from answer.text_utils import is_package_contents_question
from services.inquiry_analysis_service import InquiryAnalysisService


PRODUCT = "삼성 삼탠바이미 32인치(80cm) M5 스마트 모니터 IPTV+2in1 이동식 거치대"

REAL_QUESTION = (
    "안녕하세요 혹시 배송 올때 조립에 필요한 일회용? 공구도 같이 오나요? "
    "혹시 제가 개별로 준비해야하는 공구가 있나요?"
)

# Every answer this rule can produce; selecting any of them for a contents
# question is the failure being pinned.
SHIPPING_DURATION_MARKERS = ("영업일", "당일 발송", "도서산간")


def rule_result(question: str, product: str = PRODUCT):
    return AnswerEngine().answer(product, question, "")


def analyse(question: str, product: str = PRODUCT) -> dict:
    inquiry = {
        "id": 1,
        "source_type": "PRODUCT_INQUIRY",
        "inquiry_type": "PRODUCT_INQUIRY",
        "source_question_id": "gt",
        "external_inquiry_id": "gt",
        "title": "상품 문의",
        "content": question,
        "product_name": product,
        "order_id": "",
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


def shipping_duration_rule_selected(question: str, product: str = PRODUCT) -> bool:
    answer = rule_result(question, product).answer or ""
    return any(marker in answer for marker in SHIPPING_DURATION_MARKERS)


# --------------------------------------------------------------------------
# GT-C01 .. GT-C07 -- the shipment is the occasion, not the subject
# --------------------------------------------------------------------------


def test_gtc01_the_real_production_question() -> None:
    analysis = analyse(REAL_QUESTION)

    assert not shipping_duration_rule_selected(REAL_QUESTION)
    assert analysis["requires_dps_lookup"] is False
    assert analysis["requires_order_lookup"] is False
    # It is a question about the product's contents, so the evidence pipeline
    # -- product facts, then same-product approved Learning -- must own it.
    assert analysis["question_category"] == "PRODUCT_GENERAL"


@pytest.mark.parametrize(
    ("case", "question"),
    [
        ("GT-C02", "배송 올 때 공구도 같이 오나요?"),
        ("GT-C03", "배송 올 때 리모컨도 같이 오나요?"),
        ("GT-C04", "배송 올 때 스탠드도 같이 오나요?"),
        ("GT-C05", "배송 올 때 케이블도 같이 오나요?"),
        ("GT-C06", "제품 조립하려면 드라이버를 따로 준비해야 하나요?"),
        ("GT-C07", "설치기사님이 공구를 가지고 오시나요?"),
    ],
)
def test_contents_questions_never_get_the_shipping_duration_rule(
    case: str, question: str
) -> None:
    assert not shipping_duration_rule_selected(question), case
    assert analyse(question)["question_category"] == "PRODUCT_GENERAL", case


@pytest.mark.parametrize(
    ("case", "question"),
    [
        ("GT-C05b", "배송 올 때 케이블도 같이 오나요?"),
        ("GT-C05c", "케이블도 구성품에 포함되나요?"),
    ],
)
def test_cable_accessory_is_not_a_cable_television_question(
    case: str, question: str
) -> None:
    """"케이블" meant cable *television* in the smart-monitor RF rule.

    It is also the word for the lead in the box, so a contents question was
    answered with "RF 단자가 없어 지상파를 직접 수신할 수 없습니다".
    """

    answer = rule_result(question).answer or ""

    assert "RF 단자" not in answer, case
    assert "지상파" not in answer, case


def test_cable_television_question_still_gets_its_rule() -> None:
    """The broadcast sense must keep working."""

    answer = rule_result("케이블방송 시청 가능한가요?").answer or ""

    assert "RF 단자" in answer


# --------------------------------------------------------------------------
# GT-C08 .. GT-C10 -- a real shipping question still gets the shipping rule
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "question"),
    [
        ("GT-C08", "배송은 보통 며칠 걸리나요?"),
        ("GT-C09", "택배 배송기간이 얼마나 걸리나요?"),
        ("GT-C10", "도서산간은 배송이 하루 더 걸리나요?"),
    ],
)
def test_shipping_duration_questions_keep_the_shipping_rule(
    case: str, question: str
) -> None:
    assert shipping_duration_rule_selected(question), case


# --------------------------------------------------------------------------
# The predicate itself: both halves are required
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        REAL_QUESTION,
        "배송 올 때 공구도 같이 오나요?",
        "구성품에 리모컨이 포함되나요?",
        "설명서도 동봉되나요?",
        "제품 조립하려면 드라이버를 따로 준비해야 하나요?",
        "설치기사님이 공구를 가지고 오시나요?",
    ],
)
def test_predicate_matches_contents_questions(question: str) -> None:
    assert is_package_contents_question(question)


@pytest.mark.parametrize(
    "question",
    [
        # No item named -- a shipping question.
        "배송은 보통 며칠 걸리나요?",
        "택배 배송기간이 얼마나 걸리나요?",
        "도서산간은 배송이 하루 더 걸리나요?",
        "배송비가 얼마인가요?",
        "배송 가능한 지역인가요?",
        # Regression-protected schedule questions from the previous P0 fix.
        "언제 발송되나요?",
        "모니터 언제 발송되나요?",
        "설치 예정일이 언제인가요?",
        "설치 알림톡은 언제 발송되나요?",
        # An item named, but no inclusion/preparation attribute.
        "스탠드 색상이 뭔가요?",
        "스탠드 설치에 타공이 필요한가요?",
        "벽걸이 브라켓 설치 가능한가요?",
    ],
)
def test_predicate_does_not_match_other_questions(question: str) -> None:
    assert not is_package_contents_question(question)


# --------------------------------------------------------------------------
# GT-R01 .. GT-R05 -- the previous P0 fix, re-asserted here
# --------------------------------------------------------------------------


CASE_B_ORDER = "2026082513661591"


def analyse_with_order(question: str, order_id: str) -> dict:
    inquiry = {
        "id": 1,
        "source_type": "CUSTOMER_INQUIRY",
        "inquiry_type": "CUSTOMER_INQUIRY",
        "source_question_id": "gtr",
        "external_inquiry_id": "gtr",
        "title": "고객 문의",
        "content": question,
        "product_name": "삼성 스마트 모니터 M5 32인치",
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


def test_gtr01_no_order_number_still_asks_for_it() -> None:
    # 구매 사실이 확인되는 문의여야 주문번호 요청 경로에 도달한다.
    analysis = analyse_with_order("어제 주문했는데 언제 발송되나요?", "")

    assert analysis["detected_intent"] == "DELIVERY_DATE"
    assert analysis["requires_order_lookup"] is True
    assert analysis["answer_strategy"] == "REQUEST_ORDER_ID"


@pytest.mark.parametrize(
    ("case", "question"),
    [
        ("GT-R02", "모니터 언제 발송되나요?"),
        ("GT-R03", "TV 언제 배송되나요?"),
        ("GT-R04", "설치 예정일이 언제인가요?"),
    ],
)
def test_gtr02_to_r04_valid_order_still_takes_the_dps_route(
    case: str, question: str
) -> None:
    analysis = analyse_with_order(question, CASE_B_ORDER)

    assert analysis["requires_order_lookup"] is True, case
    assert analysis["requires_dps_lookup"] is True, case
    assert analysis["answer_strategy"] == "DIRECT_FACT_ANSWER", case


def test_gtr05_notification_policy_is_still_separated() -> None:
    analysis = analyse_with_order("설치 알림톡은 언제 발송되나요?", CASE_B_ORDER)

    assert analysis["detected_intent"] == "NOTIFICATION_POLICY"
    assert analysis["requires_order_lookup"] is False
    assert analysis["requires_dps_lookup"] is False
