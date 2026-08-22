from __future__ import annotations

import json

import pytest

from answer.answer_validator import AnswerValidator
from answer.facts import AnswerFacts
from answer.hybrid_models import (
    DraftResult,
    Emotion,
    IntentResult,
    SelfReviewResult,
)
from answer.prompt_builder import PromptBuilder


def facts(**overrides) -> AnswerFacts:
    values = {
        "inquiry": {"question": "배송은 언제 오나요?"},
        "product": {"name": "삼성 TV"},
        "order": {"shipping_due_date": "2026-08-03"},
        "delivery": {"status": "배송 준비 중"},
        "installation": {"date": "2026-08-03", "status": "설치 예정"},
        "dps": {"lookup_status": "SUCCESS"},
        "rule": {
            "answer": "배송 준비 중이며 설치일은 2026-08-03입니다.",
            "needs_review": False,
        },
        "policy": {"requires_review": False},
    }
    values.update(overrides)
    return AnswerFacts(**values)


def intent(**overrides) -> IntentResult:
    values = {
        "category": "배송/설치현황",
        "questions": ("배송은 언제 오나요",),
        "emotion": Emotion.NORMAL,
        "urgency": "NORMAL",
        "confidence": 0.9,
        "requires_review": False,
        "reason": "Facts",
    }
    values.update(overrides)
    return IntentResult(**values)


def draft(answer: str, **overrides) -> DraftResult:
    values = {
        "answer": answer,
        "confidence": 0.9,
        "used_facts": ("rule.answer",),
        "missing_information": (),
        "requires_review": False,
        "warnings": (),
    }
    values.update(overrides)
    return DraftResult(**values)


def review(**overrides) -> SelfReviewResult:
    values = {
        "passed": True,
        "answered_all_questions": True,
        "has_speculation": False,
        "facts_consistent": True,
        "requires_review": False,
        "reason": "통과",
        "warnings": (),
    }
    values.update(overrides)
    return SelfReviewResult(**values)


@pytest.mark.parametrize(
    ("unsafe", "masked"),
    [
        ("010-1234-5678", "<masked-phone>"),
        ("test@example.com", "<masked-email>"),
        ("api_key=super-secret", "<masked-secret>"),
        ("서울시 강남구 테헤란로 123", "<masked-address>"),
    ],
)
def test_prompt_masks_forbidden_values(unsafe: str, masked: str) -> None:
    prompt = PromptBuilder().build(
        task="DRAFT",
        facts=facts(inquiry={"question": f"문의 {unsafe}"}),
    )
    assert unsafe not in prompt
    assert masked in prompt


@pytest.mark.parametrize(
    "key", ["phone", "address", "otp", "token", "cookie", "session", "api_key"]
)
def test_prompt_removes_forbidden_keys(key: str) -> None:
    safe = PromptBuilder().safe_payload({key: "secret", "question": "문의"})
    assert key not in safe
    assert safe["question"] == "문의"


def test_prompt_is_json_and_facts_only() -> None:
    payload = json.loads(PromptBuilder().build(task="DRAFT", facts=facts()))
    assert payload["output_contract"]["format"] == "JSON object only"
    assert payload["output_contract"]["facts_only"] is True
    assert "facts" in payload


def test_prompt_does_not_expose_internal_implementation() -> None:
    prompt = PromptBuilder().build(task="DRAFT", facts=facts())
    assert "SELECT " not in prompt
    assert "UIA" not in prompt
    assert "DPS 화면" not in prompt


def test_validator_accepts_grounded_answer() -> None:
    result = AnswerValidator().validate(
        facts(),
        intent(),
        draft("배송 준비 중이며 설치일은 2026-08-03입니다."),
        review(),
    )
    assert result.passed is True
    assert result.checked_facts == ("rule.answer",)


def test_validator_rejects_missing_fact_reference() -> None:
    result = AnswerValidator().validate(
        facts(),
        intent(),
        draft("답변", used_facts=("installation.time_text",)),
        review(),
    )
    assert result.passed is False
    assert "존재하지 않는 Fact" in result.errors[0]


def test_validator_rejects_empty_answer() -> None:
    result = AnswerValidator().validate(
        facts(), intent(), draft(""), review()
    )
    assert result.passed is False


@pytest.mark.parametrize(
    "answer",
    ["010-1234-5678로 연락드립니다.", "test@example.com으로 보냈습니다."],
)
def test_validator_rejects_personal_information(answer: str) -> None:
    result = AnswerValidator().validate(
        facts(), intent(), draft(answer), review()
    )
    assert any("개인정보" in error for error in result.errors)


def test_validator_rejects_secret_language() -> None:
    result = AnswerValidator().validate(
        facts(), intent(), draft("token 값을 확인했습니다."), review()
    )
    assert any("인증정보" in error for error in result.errors)


@pytest.mark.parametrize(
    "phrase", ["아마 내일 옵니다.", "배송될 것 같습니다.", "추측하면 오늘입니다."]
)
def test_validator_rejects_speculation(phrase: str) -> None:
    result = AnswerValidator().validate(
        facts(), intent(), draft(phrase), review()
    )
    assert any("추측" in error for error in result.errors)


def test_validator_rejects_unknown_date() -> None:
    result = AnswerValidator().validate(
        facts(), intent(), draft("설치일은 2026-09-01입니다."), review()
    )
    assert any("Facts에 없는 날짜" in error for error in result.errors)


def test_validator_cannot_override_rule_review_policy() -> None:
    """An answer may not release a review the Rule layer demanded.

    Previously asserted by looking for a blocking error. The guarantee is
    the same but is now carried as a review signal: blocking discarded the
    whole answer, which on a compound inquiry threw away correct answers to
    the safe sub-questions. What must hold is that the result never comes
    back as a clean PASS, so the inquiry still reaches staff.
    """

    result = AnswerValidator().validate(
        facts(policy={"requires_review": True}),
        intent(requires_review=False),
        draft("확인 답변", requires_review=False),
        review(),
    )
    assert result.status == "REVIEW_REQUIRED"
    assert any("Rule 정책" in signal for signal in result.review_signals)
    assert any("Rule 정책" in warning for warning in result.warnings)


@pytest.mark.parametrize(
    "claim",
    [
        "배송 완료되었습니다",
        "설치 완료되었습니다",
        "반품 가능합니다",
        "기사님이 방문합니다",
    ],
)
def test_validator_rejects_claim_without_status_fact(claim: str) -> None:
    result = AnswerValidator().validate(
        facts(delivery={}, installation={}),
        intent(),
        draft(claim),
        review(),
    )
    assert any("근거 없는 확정" in error for error in result.errors)


def test_validator_rejects_failed_self_review() -> None:
    result = AnswerValidator().validate(
        facts(), intent(), draft("답변"), review(passed=False)
    )
    assert any("자체 검토" in error for error in result.errors)


def test_validator_warns_for_unanswered_compound_question() -> None:
    result = AnswerValidator().validate(
        facts(),
        intent(questions=("질문1", "질문2")),
        draft("답변"),
        review(answered_all_questions=False),
    )
    assert any("복합 질문" in warning for warning in result.warnings)
