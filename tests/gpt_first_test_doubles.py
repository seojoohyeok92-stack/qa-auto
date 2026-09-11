"""Small non-network GPT② doubles for execution-contract tests."""
from __future__ import annotations

from types import SimpleNamespace

from answer.models import AnswerResult, AnswerStatus


class ScriptedGpt2:
    def __init__(self, answer: str, *, needs_review: bool = False) -> None:
        self.answer = answer
        self.needs_review = needs_review
        self.requests = []

    def generate(self, request, rule_context):
        self.requests.append((request, rule_context))
        result = AnswerResult(
            status=AnswerStatus.NEEDS_REVIEW if self.needs_review else AnswerStatus.GENERATED,
            category="test",
            reason="SCRIPTED_GPT2",
            answer=self.answer,
            provider="scripted-gpt2",
            auto_answerable=not self.needs_review,
            needs_review=self.needs_review,
        )
        return SimpleNamespace(result=result, validation=SimpleNamespace(passed=True), fallback_used=False, events=())
