"""One question misread should not cost the customer the other three.

Four operational inquiries, four different ways the pipeline lost the
customer's actual meaning:

* "주문하면 바로 배송되나요" and "혹시 토요일에도 배달 가능하나요? / 주문시
  며칠 소요되나요" are ordinary delivery-policy questions. The classifier kept
  its own word list for that concept -- one that had no 배달 and no "며칠 소요"
  -- while ``answer/text_utils`` already held predicates the rule engine routes
  on. Neither part of the second inquiry reached the pre-purchase path, so both
  classified UNCLASSIFIED, which set ``manual_review_required`` and sent a
  policy question to a person at confidence 0.45.

* "사장님 오늘 주문했는데 해피콜 및 기사님 빠른설치 부탁드릴게요" is a request
  for someone to act. The classifier records exactly that -- SCHEDULE_CHANGE_
  REQUEST, manual review, order id MISSING -- and then the safe tail guard in
  ``phase9_answer_policy`` answered every unrouted delivery inquiry with "주문
  조회가 필요합니다", replying only about the order number and dropping the
  request.

* The numbered four-question installation inquiry was read as *six* questions,
  two of them meaningless: ``split_subquestions`` cut on every newline, so a
  numbered item wrapping onto a second line was severed mid-sentence and the
  dangling clause ("기존 벽에 타공구멍이 있는데") classified as UNCLASSIFIED.

The invariant running through all of it: **a draft that answers what it can is
not the same decision as an inquiry that may be auto-posted.** These tests pin
the split -- per-question verdicts are recorded, and the aggregate review flag
the safety gates read is unchanged.
"""
from __future__ import annotations

import pytest

from answer.inquiry_analysis import AnswerStrategy, OrderIdStatus
from answer.models import AnswerResult, AnswerStatus
from answer.source_adapter import answer_request_from_inquiry
from answer.text_utils import estimate_question_count, split_subquestions
from services.inquiry_analysis_service import InquiryAnalysisService
from services.phase9_answer_policy import apply_phase9_rule_policy


PRODUCT = "삼성 125.7cm(50인치) UHD 4K 1등급 비즈니스TV LH50BEFHLGFXKR 스탠드형"
ORDER_NUMBER = "2026082198559811"

CASE_A = "주문하면 바로 배송되나요"
CASE_B = "혹시 토요일에도 배달 가능하나요?\n주문시 며칠 소요되나요"
CASE_C = "사장님 오늘 주문했는데 해피콜 및 기사님 빠른설치 부탁드릴게요"
# A compound where one part genuinely needs a person: card benefits change
# every promotion and nothing here holds today's terms.
PARTIAL_INQUIRY = "무타공 설치인가요?\n카드 할인도 되나요?"
CASE_D = (
    "1. 무타공 설치인가요?\n"
    "2. 브라켓 별도 구매해야하나요?\n\n"
    "3. 기존 벽에 타공구멍이 있는데\n"
    "같은 곳에 타공 설치 가능한지\n\n"
    "4. 스마트티비는 처음인데\n"
    "인터넷티비랑 다른건가요?"
)


def analyse(
    question: str,
    *,
    order_id: str = "",
    source_type: str = "PRODUCT_INQUIRY",
):
    return InquiryAnalysisService().analyze(
        answer_request_from_inquiry(
            {
                "id": 1,
                "source_type": source_type,
                "inquiry_type": source_type,
                "source_question_id": "atomic",
                "external_inquiry_id": "atomic",
                "title": "문의",
                "content": question,
                "product_name": PRODUCT,
                "order_id": order_id,
                "product_order_id": "",
                "raw_json": {},
                "source_answered": 0,
                "post_status": "NOT_POSTED",
            }
        )
    )


def route(question: str, *, order_id: str = "", source_type: str = "CUSTOMER_INQUIRY"):
    request = answer_request_from_inquiry(
        {
            "id": 1,
            "source_type": source_type,
            "inquiry_type": source_type,
            "source_question_id": "atomic",
            "external_inquiry_id": "atomic",
            "title": "문의",
            "content": question,
            "product_name": PRODUCT,
            "order_id": order_id,
            "product_order_id": "",
            "raw_json": {},
            "source_answered": 0,
            "post_status": "NOT_POSTED",
        }
    )
    analysis = InquiryAnalysisService().analyze(request)
    request.metadata["phase9_analysis"] = analysis.to_dict()
    request.metadata["dps"] = {
        "lookup_required": False,
        "lookup_status": "NOT_STARTED",
        "installation_date": None,
        "installation_date_display": None,
        "change_request": False,
        "general_segments": [],
        "dps_segments": [],
        "warnings": [],
    }
    base = AnswerResult(
        status=AnswerStatus.NEEDS_REVIEW,
        category="배송/설치현황",
        reason="rule fallback",
        answer="",
        provider="rules",
        auto_answerable=False,
        needs_review=True,
        matched_rule="",
        metadata={"answer_source": "rule_engine_fallback"},
    )
    return analysis, apply_phase9_rule_policy(request, base, analysis)


# ==========================================================================
# CASE A -- a general delivery-policy question
# ==========================================================================


def test_case_a_needs_no_order_and_no_dps() -> None:
    analysis = analyse(CASE_A)

    assert analysis.requires_order_lookup is False
    assert analysis.requires_dps_lookup is False
    assert analysis.order_id_status is OrderIdStatus.NOT_REQUIRED


def test_case_a_is_recognised_as_a_policy_question() -> None:
    """분류는 그대로, 결론만 바뀌었다.

    "주문하면 바로 배송되나요" 는 여전히 구매 전 배송 정책 문의로 인식되고
    주문/DPS 조회도 요구하지 않는다. 달라진 것은 자동답변 여부다 -- 확정된
    운영정책상 구매 전 고객에게 배송 시점을 자동으로 답하지 않는다. 안내할
    확정 배송기간이 존재하지 않기 때문이다.
    """

    analysis = analyse(CASE_A)

    assert analysis.inquiry_subtype == "PRE_PURCHASE_DELIVERY_GUIDANCE"
    assert analysis.answer_strategy is AnswerStrategy.GENERAL_GUIDANCE
    assert analysis.manual_review_required is True
    assert analysis.requires_order_lookup is False
    assert analysis.requires_dps_lookup is False


# ==========================================================================
# CASE B -- two policy questions in one message
# ==========================================================================


def test_case_b_splits_into_two_atomic_questions() -> None:
    parts = split_subquestions(CASE_B)

    assert len(parts) == 2
    assert "토요일" in parts[0]
    assert "소요" in parts[1]


def test_case_b_needs_no_order_and_no_dps() -> None:
    analysis = analyse(CASE_B)

    assert analysis.requires_order_lookup is False
    assert analysis.requires_dps_lookup is False


@pytest.mark.parametrize(
    "question",
    [
        CASE_B,
        "혹시 토요일에도 배달 가능하나요",
        "주문시 며칠 소요되나요",
        "배달 며칠이나 걸려요?",
    ],
)
def test_case_b_parts_are_not_unclassified(question: str) -> None:
    """Each part is an ordinary policy question, judged as one.

    분류기가 이 문장들을 이해한다는 것이 이 테스트의 요지이고, 그 부분은
    그대로다. 다만 구매 전 배송 시점 문의라는 결론에 따르는 처리가 자동답변에서
    직원 검토로 바뀌었다 -- 조회는 여전히 필요 없다.
    """

    analysis = analyse(question)

    assert analysis.inquiry_subtype != "UNCLASSIFIED", question
    assert analysis.requires_order_lookup is False, question
    assert analysis.requires_dps_lookup is False, question
    assert analysis.manual_review_required is True, question


def test_the_classifier_and_text_utils_agree_on_policy_questions() -> None:
    """The concept had three definitions; the gaps were where inquiries fell."""

    from answer.text_utils import (
        is_general_delivery_policy_question,
        is_weekend_delivery_policy_question,
    )

    for question in ("주문시 며칠 소요되나요", "혹시 토요일에도 배달 가능하나요"):
        recognised = is_general_delivery_policy_question(
            question
        ) or is_weekend_delivery_policy_question(question)
        assert recognised, question
        assert analyse(question).inquiry_subtype != "UNCLASSIFIED", question


# ==========================================================================
# CASE C -- an action request keeps its meaning
# ==========================================================================


def test_case_c_is_read_as_an_action_request() -> None:
    analysis = analyse(CASE_C, source_type="CUSTOMER_INQUIRY")

    assert analysis.inquiry_subtype == "SCHEDULE_CHANGE_REQUEST"
    assert analysis.manual_review_required is True
    assert analysis.answer_strategy is AnswerStrategy.MANUAL_REVIEW


def test_case_c_keeps_both_the_order_need_and_the_action_review() -> None:
    """Two facts at once: we lack the order number *and* a person must act."""

    analysis = analyse(CASE_C, source_type="CUSTOMER_INQUIRY")

    assert analysis.requires_order_id is True
    assert analysis.order_id_status is OrderIdStatus.MISSING
    assert analysis.manual_review_required is True


def test_case_c_answer_does_not_reduce_to_an_order_number_request() -> None:
    _, result = route(CASE_C)

    assert "일정 변경은 담당자 확인이 필요합니다" in result.answer
    assert "주문 조회가 필요합니다" not in result.answer
    assert result.needs_review is True


def test_an_ordinary_missing_order_schedule_question_is_unchanged() -> None:
    """The order-number request path must keep working."""

    _, result = route("제가 주문한 상품 언제 배송되나요?")

    assert result.metadata["selected_answer_route"] == "ORDER_ID_REQUEST"
    assert "일반 주문번호가 필요합니다" in result.answer


# ==========================================================================
# CASE D -- four questions, read as four
# ==========================================================================


def test_case_d_splits_into_exactly_four_questions() -> None:
    parts = split_subquestions(CASE_D)

    assert len(parts) == 4
    assert parts[0] == "무타공 설치인가요"
    assert parts[1] == "브라켓 별도 구매해야하나요"
    assert "기존 벽에 타공구멍이 있는데" in parts[2]
    assert "같은 곳에 타공 설치 가능한지" in parts[2]
    assert "인터넷티비랑 다른건가요" in parts[3]


def test_wrapped_numbered_items_are_not_cut_at_the_wrap() -> None:
    """The wrap is a line break, not a question boundary.

    Checked as "no part *ends* at the wrap" rather than by equality: before
    the fix the severed clause still carried its "3. " marker, so comparing
    against the bare text would have passed while the inquiry was in fact
    being cut in half.
    """

    parts = split_subquestions(CASE_D)

    for dangling in ("기존 벽에 타공구멍이 있는데", "스마트티비는 처음인데"):
        matching = [part for part in parts if dangling in part]
        assert matching, dangling
        for part in matching:
            assert not part.rstrip().endswith(dangling), (
                f"{part!r} was cut at the line wrap"
            )


def test_a_single_stray_number_is_not_a_list() -> None:
    """One "1." is a sentence; two or more are a list."""

    assert split_subquestions("1번 상품 재고 있나요?") == ("1번 상품 재고 있나요",)


def test_plain_newline_separated_questions_still_split() -> None:
    parts = split_subquestions(
        "인터넷 사용도 가능한가요?\n인터넷 사용시 무선으로 쓸수있나요?"
    )

    assert len(parts) == 2


def test_the_two_decomposers_agree() -> None:
    """Staff-facing count and the classifier's parts come from one splitter."""

    for question in (CASE_A, CASE_B, CASE_C, CASE_D):
        count, _ = estimate_question_count(question)
        assert count == len(split_subquestions(question)), question


def test_case_d_records_a_verdict_for_every_atomic_question() -> None:
    analysis = analyse(CASE_D)

    assert len(analysis.subquestion_analyses) == 4
    for record in analysis.subquestion_analyses:
        for key in (
            "question", "inquiry_subtype", "detected_intent",
            "answer_strategy", "requires_order_lookup",
            "requires_dps_lookup", "manual_review_required",
            "can_generate_answer",
        ):
            assert key in record


def test_case_d_all_four_questions_are_now_classified() -> None:
    """Updated expectation, and the reason it changed.

    When this test was written Q4 ("스마트티비는 처음인데 인터넷티비랑
    다른건가요") classified as UNCLASSIFIED, so CASE D read as three
    answerable questions and one unresolved. That was a taxonomy gap, not a
    property of the inquiry: the product-attribute word list recognised
    questions naming a measurable property and missed one asking what the
    product *is*. With that gap closed all four are ordinary product and
    installation questions, so the old 3/1 split no longer describes CASE D.

    The partial-answerability invariant it was protecting did not go away --
    it is asserted on an inquiry that still has an unresolved part, below and
    in test_atomic_draft_composition.py.
    """

    analysis = analyse(CASE_D)

    assert len(analysis.answerable_subquestions) == 4
    assert len(analysis.unresolved_subquestions) == 0
    answerable = {
        record["question"] for record in analysis.answerable_subquestions
    }
    assert "무타공 설치인가요" in answerable
    assert "브라켓 별도 구매해야하나요" in answerable


def test_one_unresolved_part_does_not_erase_the_answerable_ones() -> None:
    """The invariant, on an inquiry that genuinely has an unresolved part.

    Card benefits change every promotion and nothing here holds today's terms,
    so that half needs a person while the installation half does not.
    """

    analysis = analyse(PARTIAL_INQUIRY)

    assert len(analysis.subquestion_analyses) == 2
    assert len(analysis.answerable_subquestions) == 1
    assert len(analysis.unresolved_subquestions) == 1
    assert "무타공" in analysis.answerable_subquestions[0]["question"]


def test_the_aggregate_is_the_or_of_the_parts() -> None:
    """Draft completeness is not auto-post eligibility.

    The aggregate flag the safety gates read stays the OR across parts: one
    part needing a person holds the whole inquiry, however many of the others
    could be answered.
    """

    for question in (CASE_D, PARTIAL_INQUIRY):
        analysis = analyse(question)
        expected = any(
            record["manual_review_required"]
            for record in analysis.subquestion_analyses
        )
        assert analysis.manual_review_required is expected, question
        assert analysis.auto_answerable is not expected, question


def test_subquestion_records_survive_serialisation() -> None:
    """They travel in existing JSON metadata, so they must round-trip."""

    from answer.inquiry_analysis import InquiryAnalysis

    analysis = analyse(CASE_D)
    restored = InquiryAnalysis.from_dict(analysis.to_dict())

    assert len(restored.subquestion_analyses) == 4
    assert restored.manual_review_required is analysis.manual_review_required
    assert len(restored.answerable_subquestions) == len(
        analysis.answerable_subquestions
    )


def test_records_are_empty_on_an_analysis_built_elsewhere() -> None:
    """A caller that cannot see the breakdown must not infer one question."""

    from answer.inquiry_analysis import InquiryAnalysis

    restored = InquiryAnalysis.from_dict(
        {k: v for k, v in analyse(CASE_D).to_dict().items()
         if k != "subquestion_analyses"}
    )

    assert restored.subquestion_analyses == ()


# ==========================================================================
# Order / DPS invariants -- section 7
# ==========================================================================


@pytest.mark.parametrize(
    "question", ["언제 배송되나요?", "언제설치가능한가요?", "배송 예정일 알려주세요"]
)
def test_invariant_a_real_schedule_with_a_valid_order_needs_dps(
    question: str,
) -> None:
    analysis = analyse(
        question, order_id=ORDER_NUMBER, source_type="CUSTOMER_INQUIRY"
    )

    assert analysis.requires_order_lookup is True
    assert analysis.requires_dps_lookup is True
    assert analysis.order_id_status is OrderIdStatus.VALIDATED


@pytest.mark.parametrize(
    "question",
    ["제가 주문한 상품 언제 배송되나요?", "어제 주문했는데 언제 발송되나요?"],
)
def test_invariant_b_schedule_without_an_order_asks_for_it(
    question: str,
) -> None:
    # 두 번째 문의는 구매 사실을 밝혀야 주문번호 요청 경로에 도달한다.
    # 아무것도 밝히지 않은 문의는 현재 정책상 보류된다.
    analysis = analyse(question, source_type="CUSTOMER_INQUIRY")

    assert analysis.order_id_status is OrderIdStatus.MISSING
    assert analysis.answer_strategy is AnswerStrategy.REQUEST_ORDER_ID


@pytest.mark.parametrize(
    "question",
    [
        "주문하면 바로 배송되나요",
        "배송 며칠 걸리나요",
        "배송기간이 어떻게 되나요?",
        "토요일에도 배송되나요?",
    ],
)
def test_invariant_c_policy_questions_demand_no_order_or_dps(
    question: str,
) -> None:
    analysis = analyse(question)

    assert analysis.requires_order_lookup is False
    assert analysis.requires_dps_lookup is False


@pytest.mark.parametrize(
    "question",
    ["HDMI 단자가 몇 개 있나요?", "벽걸이 설치 가능한가요?", "스탠드 분리 후 다시 장착할 수 있나요?"],
)
def test_invariant_d_product_and_installation_skip_dps(question: str) -> None:
    analysis = analyse(question)

    assert analysis.requires_order_lookup is False
    assert analysis.requires_dps_lookup is False


@pytest.mark.parametrize(
    "question",
    [
        "토요일로 배송일 변경해주세요.",
        "이번 주말에 설치해주세요.",
        CASE_C,
    ],
)
def test_invariant_e_action_requests_go_to_a_person(question: str) -> None:
    analysis = analyse(question, source_type="CUSTOMER_INQUIRY")

    assert analysis.manual_review_required is True
    assert analysis.answer_strategy is AnswerStrategy.MANUAL_REVIEW


@pytest.mark.parametrize(
    "question", ["주문 취소해주세요.", "환불 처리 부탁드립니다.", "반품 신청 부탁드립니다."]
)
def test_cancel_and_refund_keep_their_hold(question: str) -> None:
    analysis = analyse(
        question, order_id=ORDER_NUMBER, source_type="CUSTOMER_INQUIRY"
    )

    assert analysis.manual_review_required is True
