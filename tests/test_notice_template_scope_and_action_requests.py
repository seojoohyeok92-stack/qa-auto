"""The notice template answers one thing, and asking is not telling.

Two structural defects, both found by enumerating what the rule engine
actually returns rather than by reading any single inquiry.

**1. The notice template was the shipping block's default.**

``AnswerEngine._shipping()`` ended in a last-resort branch: for an install
product, anything that mentioned a shipping keyword and matched no earlier
branch got ``install_existing_order_answer`` -- "설치 예정일 관련 알림톡은
설치일 전날 수취인의 카카오톡으로 발송됩니다". Because the block is labelled
``FIXED_POLICY_SHIPPING``, and ``answer_service`` lists that in
``EXACT_TEMPLATE_MATCH_KINDS``, the default also outranked GPT and became the
published answer.

Enumerating the test corpus found **75** questions receiving that body, of
which only **7** are about the notice at all. The rest included "보증기간이
얼마나 되나요?", "캐시백 받을 수 있나요?", "삼성전자 서비스센터에서 A/S 받을
수 있나요?" and "배송 중 깨진 것 같은데 어떻게 하나요?" -- a damage report
answered with kakao-notification guidance.

The default is now deny: the template is returned only for a question about
the notice, and everything else is handed back to the rest of the engine.
``_shipping()`` runs *before* ``_install_common_info()``, so handing back is
what lets the A/S and visiting-installer rules answer the questions that were
being intercepted -- 8 of the 68 are picked up that way, and the remainder
reach the evidence pipeline instead of a wrong certainty.

**2. An instruction was read as a question.**

"이번 주말에 설치해주세요." was classified GENERAL_INSTALLATION_GUIDANCE --
ordinary information -- because ``_is_schedule_change_request`` looks for a
change verb (변경/바꿔/미뤄) next to a schedule noun (설치일/배송일), and this
sentence has neither. It is not a change to an existing date; it is the
customer telling us when to come. "주말 설치 가능한가요?" asks whether we ever
do that, and must stay an ordinary policy question.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from answer.engine import AnswerEngine
from answer.inquiry_analysis import AnswerStrategy
from answer.source_adapter import answer_request_from_inquiry
from answer.text_utils import (
    compact,
    is_delivery_notice_question,
    is_operational_schedule_request,
)
from services.inquiry_analysis_service import InquiryAnalysisService


INSTALL_PRODUCT = (
    "삼성 125.7cm(50인치) UHD 4K 1등급 비즈니스TV LH50BEFHLGFXKR 스탠드형"
)
ORDER_NUMBER = "2026082198559811"

# The distinctive sentence of install_existing_order_answer.
NOTICE_BODY_MARKER = "알림톡은 설치일 전날"


def rule_answer(question: str, product: str = INSTALL_PRODUCT):
    return AnswerEngine().answer(product, question, "")


def notice_template_returned(
    question: str, product: str = INSTALL_PRODUCT
) -> bool:
    """Whether the notice template was chosen as the answer in its own right.

    Two other branches deliberately quote the same sentence inside a larger
    composite answer -- the happycall variant (배송/설치기존+해피콜) and the
    Onnuri event answer (배송/행사복합). Those are their own matches, not this
    template standing in for a question it does not answer, so the category is
    what identifies the branch under test.
    """

    result = rule_answer(question, product)
    return (
        result.category == "배송/설치기존"
        and NOTICE_BODY_MARKER in (result.answer or "")
    )


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
                "source_question_id": "nts",
                "external_inquiry_id": "nts",
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
# 1. The template invariant, checked over the whole corpus
# ==========================================================================


def corpus_questions() -> list[str]:
    """Every question-shaped Korean string written anywhere in the tests."""

    found: set[str] = set()
    for path in sorted(pathlib.Path(__file__).parent.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for pattern in (
            r'"([^"\\]{5,80}?(?:나요|까요|가요|세요|을까요|해요|어요|주세요)\??)"',
            r'"([^"\\]{5,80}?\?)"',
        ):
            for match in re.finditer(pattern, text):
                found.add(match.group(1))
    return sorted(found)


def test_notice_template_is_returned_only_for_notice_questions() -> None:
    """The invariant. Any other meaning reaching this template is the defect.

    This enumerates the corpus rather than listing cases, so a question added
    later that slips into the template fails here without anyone remembering
    to write a test for it.
    """

    offenders = [
        question
        for question in corpus_questions()
        if notice_template_returned(question.replace("\\n", "\n"))
        and not is_delivery_notice_question(question.replace("\\n", "\n"))
    ]

    assert offenders == [], (
        f"{len(offenders)} question(s) reached install_existing_order_answer "
        f"without asking about the notice: {offenders[:10]}"
    )


def test_the_corpus_still_exercises_the_notice_template() -> None:
    """Guards the test above from passing because nothing reaches it at all."""

    kept = [
        question
        for question in corpus_questions()
        if notice_template_returned(question.replace("\\n", "\n"))
    ]

    assert len(kept) >= 5


# ==========================================================================
# 2. Positive cases -- the questions the template does answer
# ==========================================================================


@pytest.mark.parametrize(
    "question",
    [
        "설치일 알림톡 언제 오나요?",
        "설치 예정일 문자는 언제 오나요?",
        "설치 알림톡은 언제 발송되나요?",
        "설치 예정일 알림톡은 언제 발송되나요?",
        "배송 알림톡은 언제 오나요?",
        "배송 안내 문자는 언제 오나요?",
        "기사 방문 전 알림톡은 언제 오나요?",
        "설치기사님한테 언제 연락이 오나요?",
    ],
)
def test_notice_questions_keep_the_confirmed_template(question: str) -> None:
    assert is_delivery_notice_question(question), question
    assert notice_template_returned(question), question


def test_advance_contact_question_is_recognised_but_not_forced_in() -> None:
    """"기사님 방문 전에 연락 오나요?" carries no shipping keyword.

    It never enters the shipping block at all, so it reached staff review
    before this change and still does. The predicate recognises its meaning --
    which is what stops it being denied if it ever does reach the block -- but
    widening the block's entry keywords to pull it in would re-open the
    catch-all this change closes. Left as it is, and reported.
    """

    assert is_delivery_notice_question("기사님 방문 전에 연락 오나요?")
    assert not notice_template_returned("기사님 방문 전에 연락 오나요?")


def test_happycall_keeps_its_own_variant() -> None:
    answer = rule_answer("해피콜은 누가 하나요?").answer or ""

    assert "해피콜" in answer


# ==========================================================================
# 3. Meanings that must never reach it again
# ==========================================================================


@pytest.mark.parametrize(
    ("group", "question"),
    [
        ("보증기간", "보증기간이 얼마나 되나요?"),
        ("보증기간", "제품 보증기간은 어떻게 되나요?"),
        ("보증기간", "삼성센터AS무상기간알려주세요"),
        ("A/S", "A/S 기간이 얼마나 되나요?"),
        ("A/S", "삼성전자 서비스센터에서 A/S 받을 수 있나요?"),
        ("혜택", "캐시백 받을 수 있나요?"),
        ("혜택", "지금 올려도 네이버포인트 혜택을 받을 수 있나요?"),
        ("파손", "배송 중 깨진 것 같은데 어떻게 하나요?"),
        ("배송기간", "배송기간이 얼마나 걸리나요?"),
        ("배송기간", "도서산간은 배송이 하루 더 걸리나요?"),
        ("주말배송", "토요일에도 배송되나요?"),
        ("주말배송", "주말에도 받을 수 있나요?"),
        ("배송지역", "제주도 배송 가능한가요?"),
        ("배송지역", "배송 가능한 지역인가요?"),
        ("설치범위", "배송 기사님이 설치까지 해주시나요?"),
        ("설치범위", "설치기사님이 벽걸이 설치도 해주시나요?"),
        ("일정변경", "설치일 바꿔주세요"),
        ("일정변경", "배송 예정일을 이번 주로 바꿔주세요"),
        ("특정주문", "배송 예정일 알려주세요"),
        ("특정주문", "설치일은 언제인가요?"),
    ],
)
def test_unrelated_meanings_never_get_the_notice_template(
    group: str, question: str
) -> None:
    assert not notice_template_returned(question), f"{group}: {question}"


@pytest.mark.parametrize(
    ("question", "expected_fragment"),
    [
        ("A/S 기간이 얼마나 되나요?", "고장이나 불량"),
        ("삼성전자 서비스센터에서 A/S 받을 수 있나요?", "고장이나 불량"),
        ("배송기사님이 설치해주시나요?", "방문하여 설치"),
        ("설치기사님이 벽걸이 설치도 해주시나요?", "방문하여 설치"),
    ],
)
def test_handing_back_lets_the_right_rule_answer(
    question: str, expected_fragment: str
) -> None:
    """Denying the template is not the same as answering nothing.

    ``_shipping()`` runs before the A/S and installation rules, so these were
    intercepted before their own rule could ever be reached.
    """

    assert expected_fragment in (rule_answer(question).answer or "")


def test_damage_report_is_not_answered_by_any_shipping_template() -> None:
    """A breakage report is a dispute, not a schedule question."""

    answer = rule_answer("배송 중 깨진 것 같은데 어떻게 하나요?").answer or ""

    assert NOTICE_BODY_MARKER not in answer
    assert analyse("배송 중 깨진 것 같은데 어떻게 하나요?").manual_review_required is True


# ==========================================================================
# 4. Section 6 -- the same word, six different meanings
# ==========================================================================


@pytest.mark.parametrize(
    "question", ["주말 설치 가능한가요?", "토요일 설치 가능한가요?", "일요일에도 설치되나요?"]
)
def test_case_a_policy_question_stays_a_question(question: str) -> None:
    analysis = analyse(question)

    assert is_operational_schedule_request(question) is False
    assert analysis.answer_strategy is not AnswerStrategy.MANUAL_REVIEW


@pytest.mark.parametrize(
    "question",
    [
        "이번 주말에 설치해주세요.",
        "토요일에 설치해주세요.",
        "다음 주에 설치해주세요.",
        "내일 설치 부탁드립니다.",
        "오늘 배송해주세요.",
    ],
)
def test_case_b_action_request_goes_to_a_person(question: str) -> None:
    analysis = analyse(question, source_type="CUSTOMER_INQUIRY")

    assert is_operational_schedule_request(question) is True
    assert analysis.manual_review_required is True
    assert analysis.answer_strategy is AnswerStrategy.MANUAL_REVIEW


@pytest.mark.parametrize(
    "question",
    [
        "설치일을 토요일로 변경해주세요.",
        "금요일로 설치일 변경해주세요.",
        "배송일 토요일로 바꿔주세요.",
        "기사님 방문일 변경해주세요.",
    ],
)
def test_case_c_change_request_keeps_its_existing_policy(question: str) -> None:
    analysis = analyse(question, source_type="CUSTOMER_INQUIRY")

    assert analysis.manual_review_required is True
    assert analysis.answer_strategy is AnswerStrategy.MANUAL_REVIEW


def test_case_d_notice_policy_is_neither_request_nor_change() -> None:
    question = "설치일 알림톡 언제 오나요?"

    assert is_operational_schedule_request(question) is False
    assert analyse(question).manual_review_required is False
    assert notice_template_returned(question)


@pytest.mark.parametrize(
    "question", ["벽걸이 설치 가능한가요?", "스탠드 설치에 타공이 필요한가요?"]
)
def test_case_e_installation_method_needs_no_order_or_dps(
    question: str,
) -> None:
    analysis = analyse(question)

    assert is_operational_schedule_request(question) is False
    assert analysis.requires_order_lookup is False
    assert analysis.requires_dps_lookup is False


@pytest.mark.parametrize(
    "question", ["제 주문 언제 설치되나요?", "언제 배송되나요?"]
)
def test_case_f_specific_order_schedule_still_needs_dps(question: str) -> None:
    analysis = analyse(
        question, order_id=ORDER_NUMBER, source_type="CUSTOMER_INQUIRY"
    )

    assert is_operational_schedule_request(question) is False
    assert analysis.requires_order_lookup is True
    assert analysis.requires_dps_lookup is True


# ==========================================================================
# 5. The predicates
# ==========================================================================


@pytest.mark.parametrize(
    "question",
    [
        "설치일 알림톡 언제 오나요?",
        "배송 안내 문자는 언제 오나요?",
        "기사님 방문 전에 연락 오나요?",
        "설치기사님한테 언제 연락이 오나요?",
        "설치 예정일 문자는 언제 오나요?",
    ],
)
def test_notice_predicate_matches(question: str) -> None:
    assert is_delivery_notice_question(question)


@pytest.mark.parametrize(
    "question",
    [
        "보증기간이 얼마나 되나요?",
        "캐시백 받을 수 있나요?",
        "배송 예정일 알려주세요",
        "설치일 바꿔주세요",
        "토요일에도 배송되나요?",
        "배송 중 깨진 것 같은데 어떻게 하나요?",
        "제주도 배송 가능한가요?",
    ],
)
def test_notice_predicate_does_not_match(question: str) -> None:
    assert not is_delivery_notice_question(question)


@pytest.mark.parametrize(
    "question",
    [
        "이번 주말에 설치해주세요.",
        "토요일에 설치해주세요.",
        "다음 주에 설치해주세요.",
        "내일 설치 부탁드립니다.",
        "오늘 배송해주세요.",
        "이번 주 금요일에 방문해주세요.",
    ],
)
def test_operational_request_predicate_matches(question: str) -> None:
    assert is_operational_schedule_request(question)


@pytest.mark.parametrize(
    "question",
    [
        "주말 설치 가능한가요?",
        "토요일 설치 가능한가요?",
        "토요일에도 배송되나요?",
        "설치일 알림톡 언제 오나요?",
        "벽걸이 설치 가능한가요?",
        "제 주문 언제 설치되나요?",
        "내일 도착 가능한가요?",
        "배송기간이 어떻게 되나요?",
        "주말에도 받을 수 있나요?",
    ],
)
def test_operational_request_predicate_does_not_match(question: str) -> None:
    assert not is_operational_schedule_request(question)


def test_operational_request_needs_both_halves() -> None:
    """An imperative with no time, and a time with no imperative, are neither."""

    assert not is_operational_schedule_request("벽걸이로 설치해주세요.")
    assert not is_operational_schedule_request("이번 주말 어떤가요?")


def test_compact_is_used_so_spacing_does_not_decide() -> None:
    assert is_operational_schedule_request("이번주말에설치해주세요")
    assert is_delivery_notice_question("설치일알림톡언제오나요")
    assert compact(" 설치 ") == "설치"
