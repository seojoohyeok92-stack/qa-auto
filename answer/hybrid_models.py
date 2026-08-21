from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Emotion(str, Enum):
    NORMAL = "NORMAL"
    CONFUSED = "CONFUSED"
    URGENT = "URGENT"
    ANGRY = "ANGRY"
    THANKFUL = "THANKFUL"
    FOLLOW_UP = "FOLLOW_UP"


@dataclass(frozen=True)
class IntentResult:
    category: str
    questions: tuple[str, ...]
    emotion: Emotion
    urgency: str
    confidence: float
    requires_review: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["emotion"] = self.emotion.value
        result["questions"] = list(self.questions)
        return result


@dataclass(frozen=True)
class DraftResult:
    answer: str
    confidence: float
    used_facts: tuple[str, ...] = ()
    missing_information: tuple[str, ...] = ()
    requires_review: bool = False
    warnings: tuple[str, ...] = ()
    learning_usage: tuple[dict[str, Any], ...] = ()
    historical_usage: tuple[dict[str, Any], ...] = ()
    feedback_signal_usage: tuple[dict[str, Any], ...] = ()
    subquestion_results: tuple[dict[str, Any], ...] = ()
    learning_recovery_used: bool = False

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        for key in (
            "used_facts", "missing_information", "warnings",
            "learning_usage", "historical_usage", "feedback_signal_usage",
            "subquestion_results",
        ):
            result[key] = list(result[key])
        return result


@dataclass(frozen=True)
class SelfReviewResult:
    passed: bool
    answered_all_questions: bool
    has_speculation: bool
    facts_consistent: bool
    requires_review: bool
    reason: str
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["warnings"] = list(self.warnings)
        return result


@dataclass(frozen=True)
class ValidationRuleResult:
    code: str
    status: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ValidationResult:
    passed: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    checked_facts: tuple[str, ...] = ()
    status: str = ""
    rules: tuple[ValidationRuleResult, ...] = ()
    # Evidence/rule-derived signals that genuinely require staff review.
    # ``warnings`` also carries free-form advisory notes emitted by the GPT
    # draft/self-review steps; those are shown to staff but must not by
    # themselves force REVIEW_REQUIRED (see AnswerValidator.validate).
    review_signals: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        for key in ("errors", "warnings", "checked_facts", "review_signals"):
            result[key] = list(result[key])
        result["status"] = self.status or (
            "PASS" if self.passed else "BLOCK"
        )
        result["rules"] = [item.to_dict() for item in self.rules]
        return result
