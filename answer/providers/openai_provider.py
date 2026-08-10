from __future__ import annotations

from answer.exceptions import AnswerProviderUnavailableError
from answer.models import AnswerRequest, AnswerResult
from answer.providers.base import AnswerProvider, RuleEvaluator


class OpenAIProvider(AnswerProvider):
    """Disabled placeholder for a later, explicitly authorized phase."""

    name = "openai_disabled"

    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = False

    def generate(
        self,
        request: AnswerRequest,
        rule_evaluator: RuleEvaluator,
    ) -> AnswerResult:
        raise AnswerProviderUnavailableError(
            "OpenAI provider는 2단계에서 비활성화되어 있습니다."
        )
