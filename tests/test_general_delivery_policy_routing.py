"""A delivery word is not a delivery-schedule question, and the notice is not the schedule.

Operational PRODUCT_INQUIRY, no order number:

    "혹시 토요일에도 배달 가능하나요?
     주문시 며칠 소요되나요"

was answered with the existing-order notification template:

    "설치 예정일 관련 알림톡은 설치일 전날 수취인의 카카오톡으로 발송됩니다. ..."

which answers neither question. It reached the customer because the shipping
block in ``answer/engine.py`` ends in a last-resort branch: for an install
product, *anything* that mentioned a shipping keyword and was not recognised
as a new-order question got ``install_existing_order_answer``. That branch is
labelled ``FIXED_POLICY_SHIPPING``, which ``answer_service`` lists in
``EXACT_TEMPLATE_MATCH_KINDS`` -- so the default became the final answer and
GPT was never consulted. The validator then checked that text on its own terms
(no invented date, no leaked internals, internally consistent) and passed it.

Two semantic classes were falling into that default:

  * a general delivery **duration** question ("주문시 며칠 소요되나요",
    "배송기간이 어떻게 되나요") -- which for an install product is honestly
    answered by the new-order body: it depends on installer scheduling;
  * a **weekend/holiday policy** question ("토요일에도 배송되나요") -- for
    which the shipping config holds no verified rule at all, so the only safe
    outcome is to decline and let the evidence pipeline and a person handle it.

These tests keep both out of the notification template while leaving the
question that template actually answers -- "설치일 알림톡 언제 오나요?" --
working exactly as before.
"""
from __future__ import annotations

import pytest

from answer.engine import AnswerEngine
from answer.inquiry_analysis import AnswerStrategy, OrderIdStatus
from answer.source_adapter import answer_request_from_inquiry
from answer.text_utils import (
    GENERAL_DELIVERY_DURATION_QUERY,
    WEEKEND_DELIVERY_POLICY_QUERY,
    is_general_delivery_policy_question,
    is_weekend_delivery_policy_question,
)
from services.inquiry_analysis_service import InquiryAnalysisService


INSTALL_PRODUCT = (
    "삼성 125.7cm(50인치) UHD 4K 1등급 비즈니스TV LH50BEFHLGFXKR 스탠드형"
)
ORDER_NUMBER = "2026082198559811"

# The exact production text, newline included.
FAILING_QUESTION = "혹시 토요일에도 배달 가능하나요?\n주문시 며칠 소요되나요"

# The template that was wrongly selected. Any fragment of it appearing as the
# answer to the questions below is the defect.
NOTICE_TEMPLATE_MARKERS = ("설치일 전날", "알림톡은 설치일 전날")


def rule_answer(question: str, product: str = INSTALL_PRODUCT):
    return AnswerEngine().answer(product, question, "")


def notice_template_selected(question: str, product: str = INSTALL_PRODUCT) -> bool:
    answer = rule_answer(question, product).answer or ""
    return any(marker in answer for marker in NOTICE_TEMPLATE_MARKERS)


def analyse(
    question: str,
    *,
    order_id: str = "",
    source_type: str = "PRODUCT_INQUIRY",
    product: str = INSTALL_PRODUCT,
):
    return InquiryAnalysisService().analyze(
        answer_request_from_inquiry(
            {
                "id": 1,
                "source_type": source_type,
                "inquiry_type": source_type,
                "source_question_id": "gdp",
                "external_inquiry_id": "gdp",
                "title": "상품 문의",
                "content": question,
                "product_name": product,
                "order_id": order_id,
                "product_order_id": "",
                "raw_json": {},
                "source_answered": 0,
                "post_status": "NOT_POSTED",
            }
        )
    )


# ==========================================================================
# 1. The production inquiry itself
# ==========================================================================


def test_the_production_inquiry_is_not_answered_with_the_notice_template() -> None:
    assert not notice_template_selected(FAILING_QUESTION)


def test_the_production_inquiry_needs_neither_order_nor_dps() -> None:
    analysis = analyse(FAILING_QUESTION)

    assert analysis.requires_order_lookup is False
    assert analysis.requires_dps_lookup is False
    assert analysis.order_id_status is not OrderIdStatus.VALIDATED


def test_the_production_inquiry_invents_no_delivery_duration() -> None:
    """No basis exists for a Saturday rule, so no number may be asserted."""

    answer = rule_answer(FAILING_QUESTION).answer or ""

    # Either the engine declines (empty answer) or it answers without
    # committing to a figure it cannot support.
    for forbidden in ("토요일에도 배송", "토요일 배송이 가능", "주말에도 배송이 가능"):
        assert forbidden not in answer


def test_both_subquestions_survive_decomposition() -> None:
    """One template must not swallow a two-part inquiry."""

    result = rule_answer(FAILING_QUESTION)

    assert result.question_count == 2
    breakdown = result.question_breakdown or ""
    assert "토요일" in breakdown
    assert "소요" in breakdown


# ==========================================================================
# 2. General delivery duration -- CASE B
# ==========================================================================


@pytest.mark.parametrize(
    "question",
    [
        "주문하면 보통 며칠 걸리나요?",
        "배송기간이 어떻게 되나요?",
        "배송은 보통 며칠 걸리나요?",
        "구매 후 배송까지 며칠 정도 걸리나요?",
        "주문시 며칠 소요되나요",
        "배송 얼마나 걸려요?",
    ],
)
def test_general_duration_questions_avoid_the_notice_template(
    question: str,
) -> None:
    assert not notice_template_selected(question)


@pytest.mark.parametrize(
    "question",
    ["주문하면 보통 며칠 걸리나요?", "배송기간이 어떻게 되나요?"],
)
def test_general_duration_questions_need_no_order_or_dps(question: str) -> None:
    analysis = analyse(question)

    assert analysis.requires_order_lookup is False
    assert analysis.requires_dps_lookup is False


# ==========================================================================
# 3. Weekend / holiday policy -- CASE C
# ==========================================================================


@pytest.mark.parametrize(
    "question",
    [
        "토요일에도 배송되나요?",
        "주말에도 받을 수 있나요?",
        "토요일 배송 가능한가요?",
        "일요일에도 배달 되나요?",
        "공휴일에도 배송하나요?",
    ],
)
def test_weekend_policy_questions_avoid_the_notice_template(
    question: str,
) -> None:
    assert not notice_template_selected(question)


@pytest.mark.parametrize(
    "question", ["토요일에도 배송되나요?", "주말에도 받을 수 있나요?"]
)
def test_weekend_policy_questions_assert_no_unverified_rule(
    question: str,
) -> None:
    """The shipping config holds no weekend rule. None may be invented."""

    answer = rule_answer(question).answer or ""

    for forbidden in ("토요일에도 배송이 가능", "주말에도 배송이 가능", "주말 배송이 가능"):
        assert forbidden not in answer


@pytest.mark.parametrize(
    "question", ["토요일에도 배송되나요?", "주말에도 받을 수 있나요?"]
)
def test_weekend_policy_questions_need_no_order_or_dps(question: str) -> None:
    analysis = analyse(question)

    assert analysis.requires_order_lookup is False
    assert analysis.requires_dps_lookup is False


# ==========================================================================
# 4. The question the notice template *does* answer -- CASE D, unchanged
# ==========================================================================


@pytest.mark.parametrize(
    "question",
    [
        "설치일 알림톡 언제 오나요?",
        "설치 알림톡은 언제 발송되나요?",
        "설치 예정일 알림톡은 언제 발송되나요?",
        "배송 알림톡은 언제 오나요?",
        "배송 안내 문자는 언제 오나요?",
        "기사 방문 전 알림톡은 언제 오나요?",
    ],
)
def test_notice_questions_keep_the_notice_template(question: str) -> None:
    assert notice_template_selected(question), question


def test_happycall_question_keeps_its_own_answer() -> None:
    answer = rule_answer("해피콜은 누가 하나요?").answer or ""

    assert "해피콜" in answer


# ==========================================================================
# 5. Order-specific schedule -- CASE E / CASE F, unchanged
# ==========================================================================


@pytest.mark.parametrize(
    "question", ["언제 배송되나요?", "제 주문 언제 배송되나요?", "언제설치가능한가요?"]
)
def test_case_e_valid_order_still_requires_dps(question: str) -> None:
    analysis = analyse(
        question, order_id=ORDER_NUMBER, source_type="CUSTOMER_INQUIRY"
    )

    assert analysis.requires_order_lookup is True
    assert analysis.requires_dps_lookup is True


@pytest.mark.parametrize(
    "question",
    ["제가 주문한 상품 언제 배송되나요?", "어제 주문했는데 언제 발송되나요?"],
)
def test_case_f_missing_order_number_still_asks_for_it(question: str) -> None:
    # 두 번째 문의는 구매 사실을 밝혀야 주문번호 요청 경로에 도달한다.
    # 아무것도 밝히지 않은 문의는 현재 정책상 보류된다.
    analysis = analyse(question, source_type="CUSTOMER_INQUIRY")

    assert analysis.order_id_status is OrderIdStatus.MISSING
    assert analysis.answer_strategy is AnswerStrategy.REQUEST_ORDER_ID


# ==========================================================================
# 6. Schedule change -- CASE G, unchanged
# ==========================================================================


@pytest.mark.parametrize(
    "question",
    [
        "토요일로 배송일 변경해주세요.",
        "배송일을 다음 주 금요일로 변경해주세요.",
        "설치일을 토요일로 옮겨주세요.",
    ],
)
def test_case_g_schedule_change_keeps_its_safety_policy(question: str) -> None:
    analysis = analyse(question, source_type="CUSTOMER_INQUIRY")

    assert analysis.manual_review_required is True
    assert analysis.answer_strategy is AnswerStrategy.MANUAL_REVIEW


def test_bare_weekend_install_request_is_still_not_a_policy_question() -> None:
    """"이번 주말에 설치해주세요" names a weekend but asks us to act.

    The classifier files it as GENERAL_INSTALLATION_GUIDANCE rather than a
    schedule change -- behaviour that predates this fix and is left alone.
    What matters here is that it never becomes a weekend *policy* question and
    so never picks up the general-policy handling below.
    """

    assert not is_weekend_delivery_policy_question("이번 주말에 설치해주세요.")


@pytest.mark.parametrize(
    "question", ["토요일로 배송일 변경해주세요.", "이번 주말에 설치해주세요."]
)
def test_schedule_change_is_never_read_as_a_weekend_policy_question(
    question: str,
) -> None:
    """Asking us to move it to Saturday is not asking whether we ship on Saturday."""

    assert not is_weekend_delivery_policy_question(question)


# ==========================================================================
# 7. Unrelated classes must not be dragged in
# ==========================================================================


@pytest.mark.parametrize(
    "question",
    [
        "보증기간이 얼마나 되나요?",
        "제품 보증기간은 어떻게 되나요?",
        "삼성센터AS무상기간알려주세요",
        "A/S 기간이 얼마나 되나요?",
    ],
)
def test_warranty_duration_is_not_a_delivery_duration(question: str) -> None:
    assert not is_general_delivery_policy_question(question)


@pytest.mark.parametrize(
    "question",
    ["HDMI 단자가 몇 개 있나요?", "스탠드 분리 후 다시 장착할 수 있나요?"],
)
def test_product_spec_questions_need_no_order_or_dps(question: str) -> None:
    analysis = analyse(question)

    assert analysis.requires_order_lookup is False
    assert analysis.requires_dps_lookup is False


@pytest.mark.parametrize(
    "question", ["주문 취소해주세요.", "환불 처리 부탁드립니다.", "반품 신청 부탁드립니다.", "교환 요청드립니다."]
)
def test_cancel_and_refund_keep_their_safety_policy(question: str) -> None:
    analysis = analyse(
        question, order_id=ORDER_NUMBER, source_type="CUSTOMER_INQUIRY"
    )

    assert analysis.manual_review_required is True
    assert analysis.answer_strategy is AnswerStrategy.MANUAL_REVIEW


# ==========================================================================
# 8. The predicates themselves
# ==========================================================================


@pytest.mark.parametrize(
    "question",
    [
        "주문하면 보통 며칠 걸리나요?",
        "배송기간이 어떻게 되나요?",
        "주문시 며칠 소요되나요",
        "배송까지 얼마나 걸리나요?",
        "배송 기한이 어떻게 되나요?",
    ],
)
def test_duration_predicate_matches(question: str) -> None:
    assert GENERAL_DELIVERY_DURATION_QUERY.search(question.replace(" ", ""))
    assert is_general_delivery_policy_question(question)


@pytest.mark.parametrize(
    "question",
    [
        "설치일 알림톡 언제 오나요?",
        "배송 예정일 알려주세요",
        "제 주문 언제 배송되나요?",
        "보증기간이 얼마나 되나요?",
        "캐시백 받을 수 있나요?",
        "HDMI 단자가 몇 개 있나요?",
    ],
)
def test_duration_predicate_does_not_match(question: str) -> None:
    assert not is_general_delivery_policy_question(question)


@pytest.mark.parametrize(
    "question",
    ["토요일에도 배송되나요?", "주말에도 받을 수 있나요?", "공휴일에도 배송하나요?", "일요일 배달 가능한가요?"],
)
def test_weekend_predicate_matches(question: str) -> None:
    assert WEEKEND_DELIVERY_POLICY_QUERY.search(question.replace(" ", ""))
    assert is_weekend_delivery_policy_question(question)


@pytest.mark.parametrize(
    "question",
    [
        "설치일 알림톡 언제 오나요?",
        "배송기간이 어떻게 되나요?",
        "토요일로 배송일 변경해주세요.",
        "주말 사용 후기 남기면 포인트 주나요?",
    ],
)
def test_weekend_predicate_does_not_match(question: str) -> None:
    assert not is_weekend_delivery_policy_question(question)
