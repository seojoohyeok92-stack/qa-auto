from __future__ import annotations

from answer.answer_validator import AnswerValidator
from answer.facts import AnswerFacts
from answer.hybrid_models import DraftResult, Emotion, IntentResult, SelfReviewResult


def _base_intent(question: str) -> IntentResult:
    return IntentResult(
        "PRODUCT_GENERAL", (question,), Emotion.NORMAL, "NORMAL", 0.9, False, ""
    )


def _passing_review(answer: str) -> SelfReviewResult:
    return SelfReviewResult(
        passed=True,
        answered_all_questions=True,
        has_speculation=False,
        facts_consistent=True,
        requires_review=False,
        reason="ok",
    )


def test_adjacent_topic_answer_without_evidence_is_flagged() -> None:
    """Acceptance Case B: no Learning/Historical evidence exists for the
    sub-question, and the model answers a topically-adjacent question
    instead of the one actually asked ("세로 화면 유튜브" -> "OTT 구독 안내").
    This must be caught generically via topic overlap, not by matching the
    word "유튜브" or "세로" specifically.
    """

    question = "화면을 세로로 돌리면 유튜브 화면이 세로로 꽉 차게 나오나요?"
    tangential_answer = (
        "인터넷 연결 시 OTT 시청이 가능합니다. Wi-Fi 연결이 가능하며 "
        "OTT별 별도 구독이 필요합니다."
    )
    facts = AnswerFacts(inquiry={"question": question})
    draft = DraftResult(answer=tangential_answer, confidence=0.8)
    review = _passing_review(tangential_answer)
    subquestion_evidence = [
        {
            "subquestion": question,
            "status": "NO_RELIABLE_SOURCE",
            "evidence_coverage": "UNSUPPORTED",
            "source": None,
        }
    ]

    result = AnswerValidator().validate(
        facts,
        _base_intent(question),
        draft,
        review,
        subquestion_evidence=subquestion_evidence,
    )

    alignment_rules = [r for r in result.rules if r.code == "QUESTION_ANSWER_ALIGNMENT"]
    assert alignment_rules, "QUESTION_ANSWER_ALIGNMENT rule must run"
    assert alignment_rules[0].status == "REVIEW_REQUIRED"
    assert any("근거 없는" in warning for warning in result.warnings)
    # Adjacent-topic leakage is a review signal, not an automatic block --
    # this must not regress into an all-or-nothing fallback.
    assert result.passed is True
    assert result.status == "REVIEW_REQUIRED"


def test_answer_without_topic_overlap_is_not_flagged() -> None:
    question = "화면을 세로로 돌리면 유튜브 화면이 세로로 꽉 차게 나오나요?"
    unrelated_answer = "확인이 필요한 내용으로 담당자가 별도로 안내드리겠습니다."
    facts = AnswerFacts(inquiry={"question": question})
    draft = DraftResult(answer=unrelated_answer, confidence=0.6)
    review = _passing_review(unrelated_answer)
    subquestion_evidence = [
        {
            "subquestion": question,
            "status": "NO_RELIABLE_SOURCE",
            "evidence_coverage": "UNSUPPORTED",
            "source": None,
        }
    ]

    result = AnswerValidator().validate(
        facts,
        _base_intent(question),
        draft,
        review,
        subquestion_evidence=subquestion_evidence,
    )

    alignment_rules = [r for r in result.rules if r.code == "QUESTION_ANSWER_ALIGNMENT"]
    assert alignment_rules[0].status == "PASS"


def test_supported_subquestion_is_never_flagged_by_alignment_rule() -> None:
    """Case A's fix path: once evidence_coverage is SUPPORTED, the alignment
    check must stay out of the way entirely -- it only polices the
    no-evidence case."""

    question = "온누리 신청 시 구매처를 무엇으로 입력해야 하나요"
    answer = "구매처는 네이버로 선택해주시면 됩니다."
    facts = AnswerFacts(inquiry={"question": question})
    draft = DraftResult(answer=answer, confidence=0.9)
    review = _passing_review(answer)
    subquestion_evidence = [
        {
            "subquestion": question,
            "status": "ANSWERABLE",
            "evidence_coverage": "SUPPORTED",
            "source": "ACTIVE_POSITIVE_LEARNING",
        }
    ]

    result = AnswerValidator().validate(
        facts,
        _base_intent(question),
        draft,
        review,
        subquestion_evidence=subquestion_evidence,
    )
    alignment_rules = [r for r in result.rules if r.code == "QUESTION_ANSWER_ALIGNMENT"]
    assert alignment_rules[0].status == "PASS"


def test_validate_without_subquestion_evidence_does_not_crash() -> None:
    """Backward compatibility: existing callers that never pass
    subquestion_evidence (the default) must keep working unchanged."""

    question = "설치 가능한가요?"
    answer = "네, 설치 가능합니다."
    facts = AnswerFacts(inquiry={"question": question})
    draft = DraftResult(answer=answer, confidence=0.9)
    review = _passing_review(answer)

    result = AnswerValidator().validate(
        facts, _base_intent(question), draft, review,
    )
    assert not any(r.code == "QUESTION_ANSWER_ALIGNMENT" for r in result.rules)
    assert result.passed is True


def test_current_fact_conflict_still_blocks_learning_leakage() -> None:
    """Acceptance Case D: Evidence Authority must be untouched by this
    change -- a confirmed current installation date still wins over any
    answer that invents a different date."""

    facts = AnswerFacts(
        inquiry={"question": "설치 언제 되나요?"},
        installation={
            "date": "2026-08-25",
            "installation_date_confirmed": True,
        },
    )
    conflicting_answer = "2026년 9월 1일에 설치됩니다."
    draft = DraftResult(answer=conflicting_answer, confidence=0.9)
    review = _passing_review(conflicting_answer)

    result = AnswerValidator().validate(
        facts,
        _base_intent("설치 언제 되나요?"),
        draft,
        review,
    )
    assert result.passed is False
    assert any("설치예정일" in error or "다릅니다" in error for error in result.errors)
