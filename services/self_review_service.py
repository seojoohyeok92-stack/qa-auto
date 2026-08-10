from __future__ import annotations

from typing import Any

from answer.facts import AnswerFacts
from answer.fact_selection import SelectedFacts
from answer.hybrid_models import (
    DraftResult,
    IntentResult,
    SelfReviewResult,
)
from answer.prompt_builder import PromptBuilder
from answer.inquiry_analysis import InquiryAnalysis
from answer.providers.interfaces import JsonGptProvider


class SelfReviewService:
    def __init__(
        self,
        provider: JsonGptProvider,
        *,
        prompt_builder: PromptBuilder | None = None,
    ) -> None:
        self.provider = provider
        self.prompt_builder = prompt_builder or PromptBuilder()

    def review(
        self,
        facts: AnswerFacts,
        intent: IntentResult,
        draft: DraftResult,
        *,
        analysis: InquiryAnalysis | None = None,
        selected_facts: SelectedFacts | None = None,
    ) -> SelfReviewResult:
        raw = self.provider.generate_json(
            task="SELF_REVIEW",
            prompt=self.prompt_builder.build(
                task="SELF_REVIEW",
                facts=facts,
                extra={
                    "intent": intent.to_dict(),
                    "draft": draft.to_dict(),
                },
                analysis=analysis,
                selected_facts=selected_facts,
            ),
            context=self.prompt_builder.safe_payload(
                {
                    "questions": list(intent.questions),
                    "draft": draft.to_dict(),
                }
            ),
        )
        return self.parse(raw)

    @staticmethod
    def parse(raw: dict[str, Any]) -> SelfReviewResult:
        if not isinstance(raw, dict):
            raise ValueError("GPT self review output must be a JSON object.")
        warnings = raw.get("warnings", [])
        if not isinstance(warnings, list):
            raise ValueError("GPT self review warnings must be a list.")
        return SelfReviewResult(
            passed=bool(raw.get("passed")),
            answered_all_questions=bool(
                raw.get("answered_all_questions")
            ),
            has_speculation=bool(raw.get("has_speculation")),
            facts_consistent=bool(raw.get("facts_consistent")),
            requires_review=bool(raw.get("requires_review")),
            reason=str(raw.get("reason") or ""),
            warnings=tuple(str(item) for item in warnings),
        )
