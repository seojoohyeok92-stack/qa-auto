from __future__ import annotations

from answer.models import AnswerRequest, AnswerResult
from answer.providers.base import AnswerProvider, RuleEvaluator


class RuleProvider(AnswerProvider):
    name = "rules"

    def generate(
        self,
        request: AnswerRequest,
        rule_evaluator: RuleEvaluator,
    ) -> AnswerResult:
        return rule_evaluator(request)
