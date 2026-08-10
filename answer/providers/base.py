from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

from answer.models import AnswerRequest, AnswerResult


RuleEvaluator = Callable[[AnswerRequest], AnswerResult]


class AnswerProvider(ABC):
    name = "base"

    @abstractmethod
    def generate(
        self,
        request: AnswerRequest,
        rule_evaluator: RuleEvaluator,
    ) -> AnswerResult:
        """Generate an answer result without persistence or UI access."""
