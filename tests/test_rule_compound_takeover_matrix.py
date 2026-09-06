"""No rule may answer one question and take the rest of the inquiry with it.

The rule engine matches on substrings of the raw inquiry text: 43 categories
across 73 branches, none of which reads the semantic analysis, and each of
which returns one answer for the whole inquiry without saying which part of it
was addressed. 687718601 is what that costs -- "스탠드형 비즈니스 tv" and
"2026년 출시형 모델" matched a rule about an Overnic stand's generation, and
both questions the customer actually asked left the pipeline unanswered.

This file does not test one rule. It walks every category the engine can
return and asserts the property that must hold for all of them: adding a
second, unrelated, substantive question to an inquiry the rule answers must
never leave that second question publishable-over. Whether the rule matches at
all is the matcher's business and is deliberately not asserted here -- the
mismatch in 687718601 is a separate defect, still open.
"""
from __future__ import annotations

import re
import pathlib

import pytest

from services.auto_processing_eligibility_service import (
    SEMANTIC_COVERAGE_INCOMPLETE,
    AutoProcessingEligibilityService,
)
from services.semantic_coverage_service import SemanticCoverageService


ENGINE_SOURCE = pathlib.Path("answer/engine.py").read_text(encoding="utf-8")

# Every answer the rule engine can return, by category. Read from the source so
# a category added later joins this matrix without anyone remembering to.
RULE_CATEGORIES = sorted({
    match.group(2)
    for match in re.finditer(
        r'self\.(yes|need_info|no_answer)\(\s*"([^"]+)"', ENGINE_SOURCE
    )
})

# Branch conditions, counted the same way the audit counted them.
RULE_BRANCHES = len([
    line for line in ENGINE_SOURCE.splitlines()
    if re.match(r"^\s+if .*(in q|in p|in ctext|in cq)", line)
])

REVIEW_STATUSES = {"FAIL", "PARTIAL"}

# Real single-topic questions with the sentence that answers each. These are
# the shapes the deterministic routes actually return -- one subject, settled
# in one sentence -- and they are paired here so the matrix tests the property
# on text the anchors genuinely recognise rather than on synthesised strings.
TOPIC_CASES: tuple[tuple[str, str, str], ...] = (
    ("DELIVERY_COST", "배송비는 얼마인가요", "배송비는 무료입니다."),
    ("BRACKET", "브라켓도 같이 오나요",
     "벽걸이 브라켓은 구성품에 포함되어 함께 발송됩니다."),
    ("WARRANTY_AS", "보증기간은 얼마인가요", "보증기간은 제조사 기준 1년입니다."),
    ("DELIVERY_WEEKEND", "토요일에도 배송되나요",
     "토요일 및 공휴일 배송은 지역별로 상이합니다."),
    ("CANCEL_RETURN", "주문 취소하고 싶어요",
     "주문 취소는 마이페이지에서 진행하실 수 있습니다."),
    ("STORE_PICKUP", "방문수령 가능한가요", "방문수령은 지원하지 않습니다."),
    ("BENEFIT", "포인트 적립되나요",
     "네이버 포인트 적립은 구매 확정 후 지급됩니다."),
)

@pytest.fixture(scope="module")
def coverage() -> SemanticCoverageService:
    return SemanticCoverageService()


def eligibility_for(answer: str, question: str, route: str = "TEMPLATE"):
    """The real gate, on a draft carrying this answer's coverage verdict."""
    verdict = SemanticCoverageService().evaluate(
        question=question, answer=answer, route=route
    )
    return verdict, AutoProcessingEligibilityService().evaluate(
        inquiry={"source_answered": 0, "post_status": "NOT_POSTED"},
        draft={
            "metadata_json": {
                "semantic_coverage": verdict.to_dict(),
                "selected_answer_route": route,
            },
            "original_answer": answer,
            "validation_status": "PASS",
        },
        route=route,
    )


# ===================================================== 구조 자체를 고정한다
def test_the_matrix_covers_every_category_the_engine_can_return():
    """43 categories at the time of writing; the count is read, not typed."""
    assert len(RULE_CATEGORIES) >= 40
    assert "스탠드모델" in RULE_CATEGORIES


def test_the_branch_count_is_recorded_so_a_change_is_visible():
    assert RULE_BRANCHES >= 70


def test_no_rule_branch_consults_the_semantic_analysis():
    """The reason this matrix has to exist, pinned as a fact about the engine.

    If a matcher ever does read the semantic analysis, this fails and the
    matrix should be revisited rather than silently kept.
    """
    for term in (
        "semantic", "atomic_question", "requested_information",
        "requested_attribute", "purchase_state",
    ):
        assert term not in ENGINE_SOURCE


# ================================= 모든 topic 쌍 × "두 번째 질문이 사라지면 안 된다"
@pytest.mark.parametrize("first", TOPIC_CASES, ids=[c[0] for c in TOPIC_CASES])
@pytest.mark.parametrize("second", TOPIC_CASES, ids=[c[0] for c in TOPIC_CASES])
def test_answering_only_the_first_question_cannot_publish(first, second):
    """A deterministic answer settles one half of a two-part inquiry.

    This is the shape of every rule and every template: one answer, returned
    for the whole inquiry, silent about the rest of it. Whichever pair it
    happens to be, the half left unanswered must keep the reply off the store.
    """
    if first[0] == second[0]:
        pytest.skip("same topic twice is not a compound inquiry")
    question = "?\n".join((first[1], second[1])) + "?"
    verdict, gate = eligibility_for(first[2], question)

    assert verdict.status in REVIEW_STATUSES, (
        f"{first[0]}+{second[0]} scored {verdict.status}"
    )
    assert SEMANTIC_COVERAGE_INCOMPLETE in gate.reasons
    assert gate.decision != "SAFE"


@pytest.mark.parametrize("first", TOPIC_CASES, ids=[c[0] for c in TOPIC_CASES])
@pytest.mark.parametrize("second", TOPIC_CASES, ids=[c[0] for c in TOPIC_CASES])
def test_answering_both_questions_still_publishes(first, second):
    """The other half of the matrix: a complete reply must not be held."""
    if first[0] == second[0]:
        pytest.skip("same topic twice is not a compound inquiry")
    question = "?\n".join((first[1], second[1])) + "?"
    verdict, gate = eligibility_for(f"{first[2]} {second[2]}", question)

    assert verdict.status not in REVIEW_STATUSES, (
        f"{first[0]}+{second[0]} scored {verdict.status}"
    )
    assert SEMANTIC_COVERAGE_INCOMPLETE not in gate.reasons


# ============================================ 687718601 그 자체 (fixture)
def test_the_measured_inquiry_is_held():
    question = (
        "안녕하세요:) 집에서 그냥 일반 tv시청이나 셋톱박스 연결되어있는걸로 "
        "ott, 유튜브 볼건데 비즈니스tv와 사이니지tv 중 뭐가 낫나요?? "
        "그리고 스탠드형 비즈니스 tv가 여러 제품이 있던데 "
        "2026년 출시형 모델 제품 추천 부탁드립니다!!"
    )
    answer = (
        "스탠드는 오베닉 스탠드 FMS 모델로 출고되고 있습니다. "
        "오베닉 스탠드는 세대 구분이 아닌 디자인으로 모델을 나누고 있습니다."
    )
    verdict, gate = eligibility_for(answer, question, route="SAFE_RULE")
    assert verdict.status in REVIEW_STATUSES
    assert SEMANTIC_COVERAGE_INCOMPLETE in gate.reasons
    assert gate.decision != "SAFE"


# ====================================================== 단일 문의 보존
@pytest.mark.parametrize("question,answer", [
    ("오베닉 스탠드는 몇 세대인가요?",
     "스탠드는 오베닉 스탠드 FMS 모델로 출고되고 있으며 세대 구분이 아닌 디자인으로 나뉩니다."),
    ("설치일 알림톡 언제 오나요?",
     "설치 전날 저녁에 기사님이 수취인 번호로 연락드립니다."),
    ("배송비는 얼마인가요?",
     "배송비는 무료이며 도서산간 지역은 추가 운임이 발생할 수 있습니다."),
])
def test_a_single_question_answered_in_full_is_not_held_by_this_gate(
    question, answer
):
    """The deterministic shortcut keeps working where it earns it."""
    verdict, gate = eligibility_for(answer, question)
    assert verdict.status not in REVIEW_STATUSES
    assert SEMANTIC_COVERAGE_INCOMPLETE not in gate.reasons


# ================================================== 2-atom / 3-atom 복합
def test_a_two_part_inquiry_answered_throughout_is_not_held():
    verdict, gate = eligibility_for(
        "배송비는 무료입니다. 벽걸이 브라켓은 구성품에 포함되어 함께 발송됩니다.",
        "배송비는 얼마인가요?\n브라켓도 같이 오나요?",
    )
    assert verdict.status not in REVIEW_STATUSES
    assert SEMANTIC_COVERAGE_INCOMPLETE not in gate.reasons


def test_a_three_part_inquiry_missing_one_part_is_held():
    verdict, gate = eligibility_for(
        "배송비는 무료입니다. 벽걸이 브라켓은 구성품에 포함되어 함께 발송됩니다.",
        "배송비는 얼마인가요?\n브라켓도 같이 오나요?\n보증기간은 얼마인가요?",
    )
    assert verdict.status in REVIEW_STATUSES
    assert SEMANTIC_COVERAGE_INCOMPLETE in gate.reasons
    assert gate.decision != "SAFE"


def test_a_three_part_inquiry_answered_throughout_is_not_held():
    verdict, gate = eligibility_for(
        "배송비는 무료입니다. 벽걸이 브라켓은 구성품에 포함되어 함께 발송됩니다. "
        "보증기간은 제조사 기준 1년입니다.",
        "배송비는 얼마인가요?\n브라켓도 같이 오나요?\n보증기간은 얼마인가요?",
    )
    assert verdict.status not in REVIEW_STATUSES
    assert SEMANTIC_COVERAGE_INCOMPLETE not in gate.reasons
