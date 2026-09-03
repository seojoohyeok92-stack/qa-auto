"""배송 지역 주제가 공백 삭제로 만들어지던 문제.

Topic anchors are matched against ``compact()``, which deletes spaces so that
"배송 예정일" and "배송예정일" are the same subject. For a proper noun that is
wrong: deleting the space builds the name out of two unrelated words. 제주
appears wherever one word ends in 제 and the next starts with 주 -- "어제 주문",
"제 주문", "결제 주문", "실제 주문일", "감사제 주문량".

The customer-visible result: someone who asked only when their order arrives
got "문의하신 배송 가능 지역 부분은 담당자 확인 후 안내드리겠습니다." appended
to an answer that was validated and auto-postable. They had not asked about
delivery coverage at all.

These pin both halves -- the manufactured subject is gone, and the real one is
untouched -- because deleting the anchor would have "fixed" it just as well and
lost every genuine 제주 question with it.
"""
from __future__ import annotations

import pytest

from services.atomic_completeness_service import (
    AtomicCompletenessService,
    TOPIC_LABELS,
)
from services.phase9_answer_policy import ORDER_ID_REQUEST_ANSWER
from services.semantic_coverage_service import topics_of


REGION = "DELIVERY_REGION"
REGION_SENTENCE = f"문의하신 {TOPIC_LABELS[REGION]} 부분은"


# ---------------------------------------------------------------- A, B, F
@pytest.mark.parametrize(
    "question",
    [
        # A -- the reported inquiry.
        "어제 주문했는데 배송 언제 오나요?",
        # B -- the same subject with no order wording at all.
        "배송 예정일이 언제인가요?",
        # The fusion has more sources than 어제: any word ending in 제.
        "제 주문 언제 배송되나요?",
        "제 주문의 설치 예정일을 알려주세요.",
        "오후 3시 이전 결제 주문은 당일 발송됩니다.",
        "삼성 감사제 주문량 증가로 배송이 평소보다 지연되고 있습니다.",
        # Written without a space, the name must not be supplied either.
        "실제주문일 기준으로 알려주세요.",
        # F -- a general procedure question is neither schedule nor region.
        "배송과 설치는 어떤 방식으로 진행되나요?",
    ],
)
def test_a_question_that_never_mentions_a_place_has_no_region_topic(
    question: str,
) -> None:
    assert REGION not in topics_of(question), question


# ---------------------------------------------------------------- C, D
@pytest.mark.parametrize(
    "question",
    [
        # C -- the island, in the forms customers write it.
        "제주도도 배송되나요?",
        "제주 지역도 배송 설치 가능한가요?",
        "제주까지 배송 되나요?",
        "울릉도 배송 가능한가요?",
        # D -- asked without naming a place.
        "이 지역도 배송 가능한가요?",
        "배송 가능한 지역인가요?",
        # A shipping category rather than one place; unaffected by all this.
        "도서산간은 배송이 하루 더 걸리나요?",
    ],
)
def test_a_real_region_question_keeps_its_topic(question: str) -> None:
    assert REGION in topics_of(question), question


def test_an_answer_about_the_island_still_carries_the_region_topic() -> None:
    """Coverage reads answers too -- a region reply must stay recognisable."""

    assert REGION in topics_of(
        "운영 확인 결과 제주도 배송 및 설치가 가능합니다."
    )


# ---------------------------------------------------------------- E
def test_a_compound_keeps_both_the_schedule_and_the_region() -> None:
    """Neither subject may be dropped to make the other one work."""

    topics = topics_of("배송은 언제 오고 제주도도 배송되나요?")

    assert REGION in topics
    assert "DELIVERY_SCHEDULE" in topics


# ------------------------------------------------- the customer-facing end
def test_a_schedule_only_inquiry_gets_no_region_deferral_sentence() -> None:
    """The sentence the customer actually saw."""

    result = AtomicCompletenessService().evaluate(
        question="어제 주문했는데 배송 언제 오나요?",
        answer=ORDER_ID_REQUEST_ANSWER,
    )

    assert REGION not in result.uncovered_topics
    assert REGION_SENTENCE not in result.deferral_sentence
    assert result.deferral_sentence == ""
    completed = AtomicCompletenessService.complete(
        ORDER_ID_REQUEST_ANSWER, result.deferral_sentence
    )
    assert TOPIC_LABELS[REGION] not in completed


def test_a_real_region_question_still_gets_its_deferral_sentence() -> None:
    """The completeness check itself is untouched: an unanswered subject is
    still reported, so removing the sentence above was not achieved by
    weakening the check."""

    result = AtomicCompletenessService().evaluate(
        question="배송은 언제 오나요? 제주도도 배송되나요?",
        answer=ORDER_ID_REQUEST_ANSWER,
        subquestions=[
            {"question": "배송은 언제 오나요?"},
            {"question": "제주도도 배송되나요?"},
        ],
    )

    assert REGION in result.uncovered_topics
    assert REGION_SENTENCE in result.deferral_sentence
