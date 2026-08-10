from __future__ import annotations

from typing import Any, Callable

from answer.facts import AnswerFacts
from answer.fact_selection import SelectedFacts
from answer.hybrid_models import DraftResult, IntentResult
from answer.inquiry_analysis import InquiryAnalysis
from answer.prompt_builder import PromptBuilder
from answer.providers.interfaces import JsonGptProvider


class DraftGenerationService:
    def __init__(
        self,
        provider: JsonGptProvider,
        *,
        prompt_builder: PromptBuilder | None = None,
        learning_context_provider: Callable[[AnswerFacts, IntentResult], dict[str, Any]] | None = None,
    ) -> None:
        self.provider = provider
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.learning_context_provider = learning_context_provider

    def generate(
        self,
        facts: AnswerFacts,
        intent: IntentResult,
        *,
        analysis: InquiryAnalysis | None = None,
        selected_facts: SelectedFacts | None = None,
    ) -> DraftResult:
        if selected_facts is None:
            context = {
                "rule": dict(facts.rule),
                "dps": {
                    "delivery_status": facts.delivery.get("status"),
                    "installation_status": facts.installation.get("status"),
                    "installation_date": facts.installation.get("date"),
                    "required_delivery_date": facts.installation.get(
                        "required_delivery_date"
                    ),
                    "installation_date_source": facts.installation.get(
                        "source"
                    ),
                    "date_parse_status": facts.installation.get(
                        "date_parse_status"
                    ),
                    "installation_date_confirmed": facts.installation.get(
                        "installation_date_confirmed"
                    ),
                },
                "intent": intent.to_dict(),
            }
        else:
            context = {
                "allowed_facts": dict(selected_facts.values),
                "answer_strategy": (
                    analysis.answer_strategy.value if analysis else ""
                ),
                "intent": intent.to_dict(),
            }
        try:
            learning_context = (
                self.learning_context_provider(facts, intent)
                if self.learning_context_provider is not None
                else {}
            )
        except Exception:
            # Learning is an optional enrichment and can never block GPT.
            learning_context = {}
        prompt_input = {
            "intent": intent.to_dict(),
            "context_priority": [
                "CURRENT_INQUIRY", "PRODUCT_DB", "POLICY", "FIXED_TEMPLATE",
                "SIMILAR_APPROVED_ANSWERS", "SELLER_STYLE_EXAMPLES",
                "HISTORICAL_CASES_REFERENCE_ONLY", "OJE_STYLE_RULES",
            ],
            **learning_context,
        }
        context.update(learning_context)
        raw = self.provider.generate_json(
            task="DRAFT",
            prompt=self.prompt_builder.build(
                task="DRAFT",
                facts=facts,
                extra=prompt_input,
                analysis=analysis,
                selected_facts=selected_facts,
            ),
            context=self.prompt_builder.safe_payload(context),
        )
        return self.parse(raw)

    @staticmethod
    def parse(raw: dict[str, Any]) -> DraftResult:
        if not isinstance(raw, dict):
            raise ValueError("GPT draft output must be a JSON object.")
        confidence = float(raw.get("confidence", 0))
        if not 0 <= confidence <= 1:
            raise ValueError("GPT draft confidence must be 0..1.")
        list_fields: dict[str, tuple[str, ...]] = {}
        for field in ("used_facts", "missing_information", "warnings"):
            value = raw.get(field, [])
            if not isinstance(value, list):
                raise ValueError(f"GPT draft {field} must be a list.")
            list_fields[field] = tuple(str(item) for item in value)
        return DraftResult(
            answer=str(raw.get("answer") or ""),
            confidence=confidence,
            used_facts=list_fields["used_facts"],
            missing_information=list_fields["missing_information"],
            requires_review=bool(raw.get("requires_review")),
            warnings=list_fields["warnings"],
        )
