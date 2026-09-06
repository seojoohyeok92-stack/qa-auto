"""A sentence promising a check is not an answer to the question it names.

Inquiry 687718601 asked two things -- which of two TV lines suits OTT viewing,
and which 2026 stand model to pick -- and a stand rule answered a third thing
nobody asked. The uncovered topic was noticed: ``atomic_completeness_service``
appended "문의하신 스마트TV·인터넷TV 차이 부분은 담당자 확인 후 안내드리겠습니다."
so staff and customer could see what was missed.

Naming the topic put it into the answer, and this module counted a topic in the
answer as a topic addressed. So the sentence written to report the gap closed
it: PARTIAL before the append, PASS after, and the reply auto-posted to Naver
with one of the two questions unanswered and the other answered wrongly.

All eighteen topic labels behaved that way, which means the completeness pass
disabled the coverage gate for precisely the population it exists to catch --
partially answered compound inquiries.

These tests pin the separation. The sentence stays in the reply; it stops
counting as evidence.
"""
from __future__ import annotations

import pytest

from services.atomic_completeness_service import (
    TOPIC_LABELS,
    _DEFERRAL_PREFIX,
    _DEFERRAL_SUFFIX,
)
from services.semantic_coverage_service import (
    SemanticCoverageService,
    answered_topics_of,
    is_completion_deferral,
    topics_of,
)


REVIEW_STATUSES = {"FAIL", "PARTIAL"}

# The reply that actually reached the customer, and the rule answer it grew from.
INQUIRY_687718601 = (
    "안녕하세요:) 집에서 그냥 일반 tv시청이나 셋톱박스 연결되어있는걸로 "
    "ott, 유튜브 볼건데 비즈니스tv와 사이니지tv 중 뭐가 낫나요?? "
    "그리고 스탠드형 비즈니스 tv가 여러 제품이 있던데 "
    "2026년 출시형 모델 제품 추천 부탁드립니다!!"
)
RULE_ANSWER = (
    "스탠드는 오베닉 스탠드 FMS 모델로 출고되고 있습니다. "
    "오베닉 스탠드는 세대 구분이 아닌 디자인으로 모델을 나누고 있습니다.\n\n"
    "더 궁금하신 점은 저희 고객센터로 문의 주세요."
)
POSTED_ANSWER = (
    RULE_ANSWER
    + "\n\n문의하신 스마트TV·인터넷TV 차이 부분은 담당자 확인 후 안내드리겠습니다."
)
# Every route's reply carries this. It defers, and it names no topic, so
# removing it from the evidence must change nothing at all.
STANDARD_FOOTER = (
    "\n\n안내드린 내용이 문의하신 내용과 다른 경우,\n"
    "네이버 톡톡으로 문의 남겨주시면 담당자가 확인 후 안내드리겠습니다.\n\n감사합니다."
)


@pytest.fixture
def coverage() -> SemanticCoverageService:
    return SemanticCoverageService()


# ------------------------------------------------------- 687718601 그 자체
def test_the_rule_answer_alone_was_always_partial(coverage):
    """The gate was right until the deferral was appended."""
    result = coverage.evaluate(
        question=INQUIRY_687718601, answer=RULE_ANSWER, route="SAFE_RULE"
    )
    assert result.status in REVIEW_STATUSES


def test_the_posted_answer_no_longer_passes(coverage):
    """The reply that auto-posted must now go to a person.

    The rule still mismatches -- that is deliberately left alone here. What
    must hold is that a wrong rule answer cannot publish itself by naming the
    question it failed to answer.
    """
    result = coverage.evaluate(
        question=INQUIRY_687718601, answer=POSTED_ANSWER, route="SAFE_RULE"
    )
    assert result.status in REVIEW_STATUSES


def test_appending_the_deferral_cannot_improve_the_verdict(coverage):
    """Before and after the append must not differ in the customer's favour."""
    before = coverage.evaluate(
        question=INQUIRY_687718601, answer=RULE_ANSWER, route="SAFE_RULE"
    )
    after = coverage.evaluate(
        question=INQUIRY_687718601, answer=POSTED_ANSWER, route="SAFE_RULE"
    )
    assert after.status == before.status


def test_the_uncovered_topic_stays_uncovered():
    """PRODUCT_CONCEPT was asked, was not answered, and must not appear."""
    assert "PRODUCT_CONCEPT" in topics_of(INQUIRY_687718601)
    assert "PRODUCT_CONCEPT" not in answered_topics_of(POSTED_ANSWER)


def test_the_deferral_sentence_remains_in_the_reply():
    """Staff and customer still see what was missed; only the scoring changed."""
    assert "담당자 확인 후 안내드리겠습니다" in POSTED_ANSWER


# ------------------------------------------ 18개 라벨 전체 self-fulfillment
@pytest.mark.parametrize("topic", sorted(TOPIC_LABELS))
def test_no_topic_label_can_satisfy_itself(topic):
    """Measured at 18/18 before the fix. Must be 0/18."""
    sentence = _DEFERRAL_PREFIX + TOPIC_LABELS[topic] + _DEFERRAL_SUFFIX
    assert topic not in answered_topics_of(sentence)


def test_the_whole_label_set_is_covered_by_this_invariant():
    """A label added later is caught by the parametrize above, not missed."""
    offenders = [
        topic
        for topic, label in TOPIC_LABELS.items()
        if topic in answered_topics_of(
            _DEFERRAL_PREFIX + label + _DEFERRAL_SUFFIX
        )
    ]
    assert offenders == []


# ----------------------------------------------------------- 복합문의 계약
def test_a_compound_inquiry_answered_throughout_still_passes(coverage):
    """The fix must not send every compound inquiry to review."""
    result = coverage.evaluate(
        question="배송은 언제 오나요? 브라켓도 같이 오나요?",
        answer=(
            "배송 일정은 결제 확인 후 1~2영업일이며 도착 예정일은 주문 화면에서 "
            "확인하실 수 있습니다. "
            "벽걸이 브라켓은 구성품에 포함되어 함께 발송됩니다."
        ),
        route="GPT_HYBRID",
    )
    assert result.status not in REVIEW_STATUSES


def test_a_compound_inquiry_half_deferred_does_not_pass(coverage):
    """One real answer and one promise is not two answers."""
    result = coverage.evaluate(
        question="배송은 언제 오나요? 브라켓도 같이 오나요?",
        answer=(
            "배송은 보통 1~2영업일 소요됩니다.\n\n"
            "문의하신 브라켓 관련 사항 부분은 담당자 확인 후 안내드리겠습니다."
        ),
        route="GPT_HYBRID",
    )
    assert result.status in REVIEW_STATUSES


# ------------------------------------------------- 판별기 정밀도 (과탐 금지)
@pytest.mark.parametrize("sentence", [
    "문의하신 배송비 부분은 담당자 확인 후 안내드리겠습니다.",
    "문의하신 브라켓 관련 사항 부분은 담당자 확인 후 안내드리겠습니다.",
])
def test_the_generated_gap_report_is_recognised(sentence):
    assert is_completion_deferral(sentence) is True


@pytest.mark.parametrize("sentence", [
    # Facts.
    "스탠드는 오베닉 스탠드 FMS 모델로 출고되고 있습니다.",
    "배송은 보통 1~2영업일 소요됩니다.",
    "설치 전날 저녁 기사님이 연락드립니다.",
    "벽걸이 브라켓은 구성품에 포함되어 있습니다.",
    # Written limitations. This evaluator has always counted these as
    # responses -- admitting a limit is a response -- and narrowing that would
    # be a separate decision from the one this fix makes.
    "설치 일정 변경은 담당자 확인이 필요합니다.",
    "브라켓 규격 정보가 확인되지 않아 정확한 호환 여부는 확인이 필요합니다.",
    "토요일 및 공휴일 배송 가능 여부는 주문별 일정에 따라 확인이 필요합니다.",
    "50인치 이상 제품은 모델별 무게와 베사 규격 확인이 필요합니다.",
    # The standard closing every route carries.
    "네이버 톡톡으로 문의 남겨주시면 담당자가 확인 후 안내드리겠습니다.",
])
def test_anything_a_person_or_the_model_wrote_is_left_alone(sentence):
    """Only the generated frame is excluded; over-matching would send working
    answers to review and withdraw a contract this evaluator already holds."""
    assert is_completion_deferral(sentence) is False


def test_the_written_limitation_contract_still_holds(coverage):
    """The existing behaviour these tests must not disturb, restated here.

    ``test_semantic_coverage_soft_gate`` owns this contract; it is repeated at
    this fix's own boundary so a later widening of the filter fails here too,
    beside the reason it must not.
    """
    assert coverage.evaluate(
        question="토요일로 설치일 변경해주세요.",
        answer="설치 일정 변경은 담당자 확인이 필요합니다.",
    ).status == "PASS"


# ------------------------------------------------------ 기존 동작 불변 보장
@pytest.mark.parametrize("answer", [
    "택배배송 상품은 오후 3시 이전 결제 시 당일 발송되며, 배송은 1~2영업일 소요됩니다.",
    "택배배송 상품은 오후 3시 이전 결제 시 당일 발송되며, 배송은 1~2영업일 소요됩니다."
    + STANDARD_FOOTER,
    "배송은 1~2영업일 소요됩니다. 벽걸이 브라켓은 구성품에 포함되어 함께 발송됩니다.",
    "삼성 설치 기사님이 방문하여 배송 후 설치까지 진행해드립니다." + STANDARD_FOOTER,
])
def test_an_answer_without_a_topic_deferral_is_scored_exactly_as_before(answer):
    """The standard footer defers but names nothing, so nothing may change.

    This is what separates the fix from a blanket penalty on polite closings:
    only a sentence that *names a topic* while deferring it loses its evidence.
    """
    assert answered_topics_of(answer) == topics_of(answer)


def test_the_question_side_is_untouched():
    """A question defers nothing; filtering it would delete what was asked."""
    assert "PRODUCT_CONCEPT" in topics_of(INQUIRY_687718601)
