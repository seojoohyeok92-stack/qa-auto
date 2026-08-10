from __future__ import annotations

from typing import Any

from answer.facts import AnswerFacts
from answer.fact_selection import SelectedFacts
from answer.hybrid_models import Emotion, IntentResult
from answer.inquiry_analysis import InquiryAnalysis
from answer.prompt_builder import PromptBuilder
from answer.providers.interfaces import JsonGptProvider


class GptUnderstandingService:
    def __init__(
        self,
        provider: JsonGptProvider,
        *,
        prompt_builder: PromptBuilder | None = None,
    ) -> None:
        self.provider = provider
        self.prompt_builder = prompt_builder or PromptBuilder()

    def analyze(
        self,
        facts: AnswerFacts,
        *,
        analysis: InquiryAnalysis | None = None,
        selected_facts: SelectedFacts | None = None,
    ) -> IntentResult:
        context = {
            "question": facts.inquiry.get("question"),
            "rule": dict(facts.rule),
            "dps": dict(facts.dps),
        }
        raw = self.provider.generate_json(
            task="UNDERSTANDING",
            prompt=self.prompt_builder.build(
                task="UNDERSTANDING",
                facts=facts,
                analysis=analysis,
                selected_facts=selected_facts,
            ),
            context=self.prompt_builder.safe_payload(context),
        )
        return self.parse(raw)

    @staticmethod
    def parse(raw: dict[str, Any]) -> IntentResult:
        if not isinstance(raw, dict):
            raise ValueError("GPT understanding output must be a JSON object.")
        questions = raw.get("questions")
        if not isinstance(questions, list):
            raise ValueError("GPT understanding questions must be a list.")
        try:
            emotion = Emotion(str(raw.get("emotion") or "NORMAL"))
        except ValueError as error:
            raise ValueError("Invalid GPT emotion.") from error
        confidence = float(raw.get("confidence", 0))
        if not 0 <= confidence <= 1:
            raise ValueError("GPT understanding confidence must be 0..1.")
        return IntentResult(
            category=str(raw.get("category") or "기타/직원확인"),
            questions=tuple(
                str(item).strip()
                for item in questions
                if str(item).strip()
            ),
            emotion=emotion,
            urgency=str(raw.get("urgency") or "NORMAL").upper(),
            confidence=confidence,
            requires_review=bool(raw.get("requires_review")),
            reason=str(raw.get("reason") or ""),
        )
