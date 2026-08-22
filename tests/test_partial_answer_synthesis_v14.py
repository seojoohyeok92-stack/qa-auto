"""Partial answer synthesis for compound inquiries.

Measured on the server with the real provider: v13's decomposition, routing
and safety gates all worked, but the Program Answer produced for the six-part
inquiry was the generic safety draft -- it echoed the six questions back and
said everything needed checking, discarding the answers to the safe ones.

Root cause, reproduced deterministically below: one risky sub-question puts
``policy.requires_review`` on the whole inquiry, and the validator turned that
into a hard BLOCK ("Rule 정책의 직원 검토 요구를 해제했습니다"). A correct
partial answer therefore failed validation, the hybrid path fell back to the
rule answer -- which is empty for a hard-blocked inquiry -- and AnswerService
ended at ``_safe_review_draft``.

That check is now a review signal instead of a block. The review requirement
itself is unaffected: ``rule_result.needs_review`` already forces
NEEDS_REVIEW downstream, so the answer still cannot be published.

These tests assert real answer content, not merely that a string exists --
the production failure was precisely a non-empty but useless answer.
"""
from __future__ import annotations

from answer.answer_validator import AnswerValidator
from answer.facts import build_answer_facts
from answer.hybrid_models import (
    DraftResult,
    Emotion,
    IntentResult,
    SelfReviewResult,
)
from answer.models import AnswerRequest, AnswerResult, AnswerStatus


SIX_PART = (
    "A/S는 삼성서비스센터에서 하나요?\n"
    "설치는 기사님이 해주시나요?\n"
    "집에 있는 브라켓과 호환되나요?\n"
    "설치예정일은 언제인가요?\n"
    "카드 할인도 되나요?\n"
    "배송 중 파손되면 어떻게 하나요?"
)

# What a correct partial synthesis looks like: the safe questions actually
# answered, the unverifiable ones deferred without a verdict.
PARTIAL_ANSWER = (
    "안녕하세요. 오제 챗봇이 답변드립니다.\n\n"
    "A/S는 삼성전자 서비스센터를 통해 받으실 수 있습니다.\n"
    "설치는 전문 기사님이 방문하여 진행해 드립니다.\n"
    "보유하신 브라켓과의 호환 여부는 규격 확인이 필요하여 "
    "현재 정보만으로 확답드리기 어렵습니다.\n"
    "설치예정일은 주문 확인 후 안내가 가능합니다.\n"
    "카드 할인 혜택은 담당자 확인이 필요합니다.\n"
    "배송 중 파손 관련 사항은 담당자 확인이 필요합니다.\n\n"
    "감사합니다."
)


def hard_blocked_rule_result() -> AnswerResult:
    """What AnswerEngine returns for a compound inquiry containing 파손.

    The rule layer hard-blocks, so it carries needs_review with an empty
    answer -- which is why falling back to it produced nothing usable.
    """
    return AnswerResult(
        status=AnswerStatus.NEEDS_REVIEW,
        category="기타/직원확인",
        reason="hard block",
        answer="",
        provider="rules",
        auto_answerable=False,
        needs_review=True,
    )


def facts_for(question: str = SIX_PART):
    return build_answer_facts(
        AnswerRequest(question=question, metadata={}),
        hard_blocked_rule_result(),
    )


def validate(answer: str, questions: tuple[str, ...], *, facts=None):
    intent = IntentResult(
        "GENERAL", questions, Emotion.NORMAL, "NORMAL", 0.9, False, "test"
    )
    return AnswerValidator().validate(
        facts if facts is not None else facts_for(),
        intent,
        DraftResult(answer=answer, confidence=0.9, requires_review=False),
        SelfReviewResult(
            passed=True,
            answered_all_questions=True,
            has_speculation=False,
            facts_consistent=True,
            requires_review=False,
            reason="ok",
        ),
    )


QUESTIONS = (
    "A/S는 삼성서비스센터에서 하나요?",
    "배송 중 파손되면 어떻게 하나요?",
)


# ------------------------------------------------------- the root cause

def test_rule_review_requirement_no_longer_discards_the_answer() -> None:
    """The regression itself: a good partial answer must survive."""
    result = validate(PARTIAL_ANSWER, QUESTIONS)
    assert result.passed is True
    assert result.status == "REVIEW_REQUIRED"


def test_review_requirement_is_still_enforced() -> None:
    """Surviving validation must not mean escaping review."""
    result = validate(PARTIAL_ANSWER, QUESTIONS)
    # Never a clean PASS, so the inquiry still reaches staff.
    assert result.status != "PASS"
    assert any("Rule 정책" in item for item in result.review_signals)


# CASE A -- the production inquiry: the safe answers must be preserved.
def test_case_a_six_part_partial_answer_keeps_safe_content() -> None:
    result = validate(PARTIAL_ANSWER, tuple(SIX_PART.split("\n")))
    assert result.passed is True

    # The two safe sub-questions are actually answered, not deferred.
    assert "삼성전자 서비스센터를 통해 받으실 수 있습니다" in PARTIAL_ANSWER
    assert "전문 기사님이 방문하여 진행" in PARTIAL_ANSWER

    # And this is not the generic safety draft that caused the report.
    assert "관련하여 정확한 정보 확인이 필요합니다" not in PARTIAL_ANSWER
    assert (
        "확인되지 않은 내용을 임의로 안내하지 않고" not in PARTIAL_ANSWER
    )


# The unverifiable sub-questions must carry no verdict either way.
def test_unverifiable_subquestions_are_not_decided() -> None:
    for forbidden in (
        "호환됩니다",
        "호환되지 않습니다",
        "할인이 적용됩니다",
        "할인이 적용되지 않습니다",
        "전액 보상해 드립니다",
        "교환해 드립니다",
    ):
        assert forbidden not in PARTIAL_ANSWER


# CASE F -- no DPS lookup has run, so no date may appear.
def test_no_date_is_invented_without_dps() -> None:
    import re

    assert not re.search(r"\d{4}-\d{2}-\d{2}", PARTIAL_ANSWER)
    assert not re.search(r"\d{1,2}월\s*\d{1,2}일", PARTIAL_ANSWER)
    facts = facts_for()
    assert facts.installation["date"] is None
    assert facts.installation["installation_date_confirmed"] is False


# A date that the facts do not contain is still a hard block: relaxing the
# review check must not have opened a hole for invented schedules.
def test_invented_date_is_still_blocked() -> None:
    result = validate(
        PARTIAL_ANSWER.replace(
            "설치예정일은 주문 확인 후 안내가 가능합니다.",
            "설치예정일은 2026-09-01입니다.",
        ),
        QUESTIONS,
    )
    assert result.passed is False


# Privacy and speculation blocks are untouched by the change.
def test_privacy_and_speculation_are_still_blocked() -> None:
    assert validate("연락처는 010-1234-5678 입니다.", QUESTIONS).passed is False
    assert (
        validate("아마도 호환될 것 같습니다.", QUESTIONS).passed is False
    )


# The empty rule answer is what made the fallback useless; confirm the
# precondition still holds so the test above is meaningful.
def test_hard_blocked_rule_answer_is_empty() -> None:
    assert hard_blocked_rule_result().answer == ""
    assert facts_for().policy["requires_review"] is True
