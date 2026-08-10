from __future__ import annotations

import pytest

from answer.facts import AnswerFacts
from answer.hybrid_models import Emotion, IntentResult
from answer.providers.fake_gpt_provider import FakeGptProvider
from services.draft_generation_service import DraftGenerationService
from services.gpt_understanding_service import GptUnderstandingService
from services.self_review_service import SelfReviewService


def facts(question: str = "넷플릭스 되나요?") -> AnswerFacts:
    return AnswerFacts(
        inquiry={"question": question},
        product={"name": "삼성 TV"},
        rule={
            "category": "제품 기능",
            "answer": "넷플릭스 사용이 가능합니다.",
            "needs_review": False,
        },
        dps={"lookup_status": "NOT_REQUIRED"},
        policy={"requires_review": False},
    )


def intent() -> IntentResult:
    return IntentResult(
        category="제품 기능",
        questions=("넷플릭스 되나요",),
        emotion=Emotion.NORMAL,
        urgency="NORMAL",
        confidence=0.95,
        requires_review=False,
        reason="Facts 분석",
    )


def test_understanding_service_returns_intent_model() -> None:
    result = GptUnderstandingService(FakeGptProvider()).analyze(facts())
    assert isinstance(result, IntentResult)
    assert result.emotion is Emotion.NORMAL


@pytest.mark.parametrize(
    ("text", "count"),
    [
        ("하나인가요?", 1),
        ("하나요? 배송은요?", 2),
        ("하나요?\n배송은요?\n설치도 하나요?", 3),
        ("", 0),
    ],
)
def test_understanding_question_split_count(text: str, count: int) -> None:
    result = GptUnderstandingService(FakeGptProvider()).analyze(facts(text))
    assert len(result.questions) == count


def test_understanding_rejects_non_list_questions() -> None:
    with pytest.raises(ValueError, match="list"):
        GptUnderstandingService.parse(
            {"questions": "bad", "emotion": "NORMAL", "confidence": 0.5}
        )


def test_understanding_rejects_invalid_emotion() -> None:
    with pytest.raises(ValueError, match="emotion"):
        GptUnderstandingService.parse(
            {"questions": [], "emotion": "SAD", "confidence": 0.5}
        )


@pytest.mark.parametrize("confidence", [-0.1, 1.1])
def test_understanding_rejects_invalid_confidence(
    confidence: float,
) -> None:
    with pytest.raises(ValueError, match="0..1"):
        GptUnderstandingService.parse(
            {
                "questions": [],
                "emotion": "NORMAL",
                "confidence": confidence,
            }
        )


def test_draft_generation_returns_structured_result() -> None:
    result = DraftGenerationService(FakeGptProvider()).generate(
        facts(), intent()
    )
    assert result.answer == "넷플릭스 사용이 가능합니다."
    assert result.used_facts == ("rule.answer",)
    assert result.confidence == 0.97


@pytest.mark.parametrize(
    "field", ["used_facts", "missing_information", "warnings"]
)
def test_draft_parser_requires_list_fields(field: str) -> None:
    raw = {
        "answer": "답변",
        "confidence": 0.5,
        "used_facts": [],
        "missing_information": [],
        "warnings": [],
    }
    raw[field] = "bad"
    with pytest.raises(ValueError, match="list"):
        DraftGenerationService.parse(raw)


@pytest.mark.parametrize("confidence", [-1, 2])
def test_draft_parser_rejects_invalid_confidence(confidence: float) -> None:
    with pytest.raises(ValueError, match="0..1"):
        DraftGenerationService.parse(
            {
                "answer": "답변",
                "confidence": confidence,
                "used_facts": [],
                "missing_information": [],
                "warnings": [],
            }
        )


def test_empty_rule_answer_marks_missing_information() -> None:
    empty = AnswerFacts(
        inquiry={"question": "모르는 문의"},
        rule={"answer": "", "needs_review": True},
        dps={},
    )
    result = DraftGenerationService(FakeGptProvider()).generate(
        empty, intent()
    )
    assert result.missing_information == ("rule.answer",)
    assert result.requires_review is True


def test_self_review_passes_grounded_answer() -> None:
    draft = DraftGenerationService(FakeGptProvider()).generate(
        facts(), intent()
    )
    review = SelfReviewService(FakeGptProvider()).review(
        facts(), intent(), draft
    )
    assert review.passed is True
    assert review.has_speculation is False


def test_self_review_detects_speculation() -> None:
    provider = FakeGptProvider(
        responses={
            "SELF_REVIEW": {
                "passed": False,
                "answered_all_questions": True,
                "has_speculation": True,
                "facts_consistent": False,
                "requires_review": True,
                "reason": "추측",
                "warnings": ["추측 표현"],
            }
        }
    )
    draft = DraftGenerationService(FakeGptProvider()).generate(
        facts(), intent()
    )
    result = SelfReviewService(provider).review(facts(), intent(), draft)
    assert result.has_speculation is True
    assert result.requires_review is True


def test_self_review_parser_requires_warning_list() -> None:
    with pytest.raises(ValueError, match="warnings"):
        SelfReviewService.parse(
            {
                "passed": True,
                "answered_all_questions": True,
                "has_speculation": False,
                "facts_consistent": True,
                "requires_review": False,
                "warnings": "bad",
            }
        )
