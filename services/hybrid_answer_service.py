from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from answer.answer_validator import AnswerValidator
from answer.fact_selection import FactSelectionService, SelectedFacts
from answer.facts import AnswerFacts, build_answer_facts
from answer.hybrid_models import (
    DraftResult,
    IntentResult,
    SelfReviewResult,
    ValidationResult,
)
from answer.models import AnswerRequest, AnswerResult, AnswerStatus
from answer.inquiry_analysis import InquiryAnalysis
from answer.inquiry_analysis import AnswerStrategy
from answer.providers.interfaces import JsonGptProvider
from answer.providers.provider_factory import create_gpt_provider
from services.draft_generation_service import DraftGenerationService
from services.gpt_understanding_service import GptUnderstandingService
from services.self_review_service import SelfReviewService


@dataclass(frozen=True)
class HybridEvent:
    code: str
    message: str
    level: str = "INFO"
    details: dict[str, Any] | None = None


@dataclass(frozen=True)
class HybridAnswerOutcome:
    result: AnswerResult
    facts: AnswerFacts
    intent: IntentResult | None
    draft: DraftResult | None
    self_review: SelfReviewResult | None
    validation: ValidationResult | None
    fallback_used: bool
    events: tuple[HybridEvent, ...]


class HybridAnswerService:
    def __init__(
        self,
        provider: JsonGptProvider | None = None,
        *,
        validator: AnswerValidator | None = None,
        fact_selection: FactSelectionService | None = None,
        learning_context_provider: Callable[[AnswerFacts, IntentResult], dict[str, Any]] | None = None,
    ) -> None:
        self.provider = provider or create_gpt_provider()
        self.understanding = GptUnderstandingService(self.provider)
        self.drafts = DraftGenerationService(
            self.provider,
            learning_context_provider=learning_context_provider,
        )
        self.self_review = SelfReviewService(self.provider)
        self.validator = validator or AnswerValidator()
        self.fact_selection = fact_selection or FactSelectionService()

    @staticmethod
    def _fallback(
        rule_result: AnswerResult,
        facts: AnswerFacts,
        *,
        reason: str,
        provider_name: str,
        events: list[HybridEvent],
        intent: IntentResult | None = None,
        draft: DraftResult | None = None,
        review: SelfReviewResult | None = None,
        validation: ValidationResult | None = None,
    ) -> HybridAnswerOutcome:
        metadata = dict(rule_result.metadata)
        metadata["hybrid"] = {
            "enabled": True,
            "provider": provider_name,
            "fallback_used": True,
            "fallback_reason": reason,
            "facts": {
                "warnings": list(facts.warnings),
                "available": _available_fact_paths(facts),
            },
            "intent": intent.to_dict() if intent else None,
            "draft": draft.to_dict() if draft else None,
            "self_review": review.to_dict() if review else None,
            "validation": validation.to_dict() if validation else None,
            "confirmed_facts": {
                "installation_date": facts.installation.get("date"),
                "required_delivery_date": facts.installation.get(
                    "required_delivery_date"
                ),
                "installation_date_source": facts.installation.get(
                    "source"
                ),
                "installation_date_status": (
                    "CONFIRMED"
                    if facts.installation.get(
                        "installation_date_confirmed"
                    )
                    else "UNCONFIRMED"
                ),
                "dps_lookup_id": facts.installation.get(
                    "dps_lookup_id"
                ),
            },
        }
        fallback = AnswerResult(
            status=(
                AnswerStatus.NEEDS_REVIEW
                if reason == "VALIDATION_FAILED"
                and isinstance(rule_result.metadata.get("phase9"), dict)
                else rule_result.status
            ),
            category=rule_result.category,
            reason=rule_result.reason,
            answer=rule_result.answer,
            provider=rule_result.provider,
            auto_answerable=(
                False
                if reason == "VALIDATION_FAILED"
                and isinstance(rule_result.metadata.get("phase9"), dict)
                else rule_result.auto_answerable
            ),
            needs_review=(
                True
                if reason == "VALIDATION_FAILED"
                and isinstance(rule_result.metadata.get("phase9"), dict)
                else rule_result.needs_review
            ),
            matched_rule=rule_result.matched_rule,
            warnings=tuple(rule_result.warnings),
            metadata=metadata,
        )
        events.append(
            HybridEvent(
                "GPT_FALLBACK_RULE",
                "GPT 결과 대신 검증된 Rule Answer를 사용했습니다.",
                "WARNING",
                {"reason": reason, "provider": rule_result.provider},
            )
        )
        return HybridAnswerOutcome(
            fallback,
            facts,
            intent,
            draft,
            review,
            validation,
            True,
            tuple(events),
        )

    def generate(
        self,
        request: AnswerRequest,
        rule_result: AnswerResult,
    ) -> HybridAnswerOutcome:
        facts = build_answer_facts(request, rule_result)
        analysis_value = request.metadata.get("phase9_analysis")
        analysis = (
            InquiryAnalysis.from_dict(analysis_value)
            if isinstance(analysis_value, dict) and analysis_value
            else None
        )
        selected_facts = (
            self.fact_selection.select(facts, analysis)
            if analysis is not None
            else SelectedFacts(
                values={
                    path: facts.get_fact(path)
                    for path in _available_fact_paths(facts)
                },
                keys=tuple(_available_fact_paths(facts)),
            )
        )
        phase9_metadata = (
            dict(rule_result.metadata.get("phase9"))
            if isinstance(rule_result.metadata.get("phase9"), dict)
            else {}
        )
        phase9_metadata["analysis"] = analysis.to_dict() if analysis else {}
        phase9_metadata["selected_facts"] = selected_facts.to_dict()
        rule_result.metadata["phase9"] = phase9_metadata
        installation_date = facts.installation.get("date")
        events = [
            HybridEvent(
                "PHASE9_FACTS_SELECTED",
                "문의 유형에 필요한 사실만 선택했습니다.",
                details={
                    "answer_strategy": (
                        analysis.answer_strategy.value if analysis else None
                    ),
                    "selected_fact_keys": list(selected_facts.keys),
                },
            ),
            HybridEvent(
                "GPT_FACTS_READY",
                "현재 문의의 AnswerFacts 준비를 완료했습니다.",
                details={
                    "status": (
                        "READY" if installation_date else "NO_DATE"
                    ),
                    "dps_lookup_id": facts.installation.get(
                        "dps_lookup_id"
                    ),
                },
            ),
            HybridEvent(
                "ANSWER_FACTS_INSTALLATION_DATE_INCLUDED",
                "현재 문의의 설치예정일 Facts를 준비했습니다.",
                details={
                    "status": (
                        "CONFIRMED"
                        if facts.installation.get(
                            "installation_date_confirmed"
                        )
                        else "UNCONFIRMED"
                    ),
                    "normalized_date": installation_date,
                    "dps_lookup_id": facts.installation.get(
                        "dps_lookup_id"
                    ),
                },
            ),
            HybridEvent(
                "GPT_PROMPT_FACTS_READY",
                "GPT Prompt용 확정 Facts를 준비했습니다.",
                details={
                    "status": (
                        "READY" if installation_date else "NO_DATE"
                    ),
                    "normalized_date": installation_date,
                    "dps_lookup_id": facts.installation.get(
                        "dps_lookup_id"
                    ),
                },
            ),
            HybridEvent(
                "GPT_PROMPT_READY",
                "현재 문의의 GPT Prompt 준비를 완료했습니다.",
                details={
                    "status": "READY",
                    "normalized_date": installation_date,
                },
            ),
            HybridEvent(
                "GPT_ANALYSIS_STARTED",
                "Facts 기반 GPT 문의 분석을 시작했습니다.",
                details={"provider": self.provider.name},
            )
        ]
        intent: IntentResult | None = None
        draft: DraftResult | None = None
        review: SelfReviewResult | None = None
        validation: ValidationResult | None = None
        try:
            events.append(
                HybridEvent(
                    "GPT_PROVIDER_STARTED",
                    "GPT Provider 호출을 시작했습니다.",
                    details={"provider": self.provider.name},
                )
            )
            intent = self.understanding.analyze(
                facts,
                analysis=analysis,
                selected_facts=selected_facts,
            )
            events.append(
                HybridEvent(
                    "GPT_ANALYSIS_COMPLETED",
                    "GPT 문의 분석을 완료했습니다.",
                    details={
                        "category": intent.category,
                        "emotion": intent.emotion.value,
                        "question_count": len(intent.questions),
                        "confidence": intent.confidence,
                    },
                )
            )
            if (
                analysis is not None
                and analysis.answer_strategy
                is AnswerStrategy.REQUEST_ORDER_ID
            ):
                draft = DraftResult(
                    answer=rule_result.answer,
                    confidence=1.0,
                    used_facts=(),
                    missing_information=(),
                    requires_review=False,
                    warnings=(),
                )
            else:
                draft = self.drafts.generate(
                    facts,
                    intent,
                    analysis=analysis,
                    selected_facts=selected_facts,
                )
            events.append(
                HybridEvent(
                    "GPT_RESPONSE_NORMALIZED",
                    "GPT 응답 본문 정규화를 완료했습니다.",
                    details={
                        "status": (
                            "NON_EMPTY"
                            if draft.answer.strip()
                            else "EMPTY"
                        ),
                        "answer_length": len(draft.answer.strip()),
                    },
                )
            )
            events.append(
                HybridEvent(
                    "GPT_RESPONSE_RECEIVED",
                    "GPT 응답 본문을 수신했습니다.",
                    details={
                        "status": (
                            "RECEIVED"
                            if draft.answer.strip()
                            else "EMPTY"
                        ),
                        "normalized_date": installation_date,
                    },
                )
            )
            events.append(
                HybridEvent(
                    "GPT_DRAFT_CREATED",
                    "Facts 기반 GPT 답변 후보를 생성했습니다.",
                    details={
                        "confidence": draft.confidence,
                        "used_facts": list(draft.used_facts),
                        "missing_information": list(
                            draft.missing_information
                        ),
                    },
                )
            )
            if (
                analysis is not None
                and analysis.answer_strategy
                is AnswerStrategy.REQUEST_ORDER_ID
            ):
                review = SelfReviewResult(
                    passed=True,
                    answered_all_questions=True,
                    has_speculation=False,
                    facts_consistent=True,
                    requires_review=False,
                    reason="안전한 주문번호 요청 템플릿을 사용했습니다.",
                    warnings=(),
                )
            else:
                review = self.self_review.review(
                    facts,
                    intent,
                    draft,
                    analysis=analysis,
                    selected_facts=selected_facts,
                )
            events.append(
                HybridEvent(
                    "GPT_PROVIDER_FINISHED",
                    "GPT Provider 호출을 완료했습니다.",
                    details={
                        "provider": self.provider.name,
                        "status": "COMPLETED",
                    },
                )
            )
            events.append(
                HybridEvent(
                    "GPT_SELF_REVIEW",
                    "GPT 답변 자체 검토를 수행했습니다.",
                    level="INFO" if review.passed else "WARNING",
                    details={
                        "passed": review.passed,
                        "requires_review": review.requires_review,
                    },
                )
            )
            events.append(
                HybridEvent(
                    "GPT_VALIDATOR_STARTED",
                    "GPT 답변 Validator 확인을 시작했습니다.",
                )
            )
            validation = self.validator.validate(
                facts,
                intent,
                draft,
                review,
                analysis=analysis,
                selected_facts=selected_facts,
            )
            events.append(
                HybridEvent(
                    "GPT_VALIDATOR_FINISHED",
                    "GPT 답변 Validator 확인을 완료했습니다.",
                    level="INFO" if validation.passed else "WARNING",
                    details={
                        "status": (
                            "PASSED" if validation.passed else "FAILED"
                        )
                    },
                )
            )
            if (
                facts.installation.get(
                    "installation_date_confirmed"
                )
                and any(
                    (
                        "누락" in error
                        or "확인할 수 없" in error
                    )
                    for error in validation.errors
                )
            ):
                events.append(
                    HybridEvent(
                        "GPT_INSTALLATION_DATE_MISSING_IN_ANSWER",
                        "GPT 답변에 확정 설치예정일이 반영되지 않았습니다.",
                        "WARNING",
                        {
                            "status": "VALIDATION_FAILED",
                            "normalized_date": installation_date,
                            "dps_lookup_id": facts.installation.get(
                                "dps_lookup_id"
                            ),
                        },
                    )
                )
            if facts.dps.get("requires_human_review"):
                events.append(
                    HybridEvent(
                        "GPT_INSTALLATION_DATE_CONFLICT",
                        "복수 설치 일정 충돌로 직원 확인이 필요합니다.",
                        "WARNING",
                        {
                            "status": "CONFLICT",
                            "dps_lookup_id": facts.installation.get(
                                "dps_lookup_id"
                            ),
                        },
                    )
                )
            if not validation.passed:
                events.append(
                    HybridEvent(
                        "GPT_VALIDATION_FAILED",
                        "GPT 답변이 Facts 검증을 통과하지 못했습니다.",
                        "WARNING",
                        {"errors": list(validation.errors)},
                    )
                )
                return self._fallback(
                    rule_result,
                    facts,
                    reason="VALIDATION_FAILED",
                    provider_name=self.provider.name,
                    events=events,
                    intent=intent,
                    draft=draft,
                    review=review,
                    validation=validation,
                )
            requires_review = bool(
                rule_result.needs_review
                or intent.requires_review
                or draft.requires_review
                or review.requires_review
                or draft.missing_information
                or validation.status == "REVIEW_REQUIRED"
            )
            status = (
                AnswerStatus.NEEDS_REVIEW
                if requires_review
                else AnswerStatus.GENERATED
            )
            metadata = dict(rule_result.metadata)
            metadata["phase9"] = {
                "analysis": analysis.to_dict() if analysis else {},
                "selected_facts": selected_facts.to_dict(),
            }
            metadata["hybrid"] = {
                "enabled": True,
                "provider": self.provider.name,
                "fallback_used": False,
                "facts": {
                    "warnings": list(facts.warnings),
                    "available": _available_fact_paths(facts),
                },
                "intent": intent.to_dict(),
                "draft": draft.to_dict(),
                "self_review": review.to_dict(),
                "validation": validation.to_dict(),
                "phase9": metadata["phase9"],
                "confirmed_facts": {
                    "installation_date": installation_date,
                    "required_delivery_date": facts.installation.get(
                        "required_delivery_date"
                    ),
                    "installation_date_source": facts.installation.get(
                        "source"
                    ),
                    "installation_date_status": (
                        "CONFIRMED"
                        if facts.installation.get(
                            "installation_date_confirmed"
                        )
                        else "UNCONFIRMED"
                    ),
                    "dps_lookup_id": facts.installation.get(
                        "dps_lookup_id"
                    ),
                },
            }
            result = AnswerResult(
                status=status,
                category=intent.category or rule_result.category,
                reason=(
                    "Facts 기반 GPT 답변이 자체 검토와 Validator를 통과했습니다."
                ),
                answer=draft.answer,
                provider=f"{self.provider.name}_hybrid",
                auto_answerable=not requires_review,
                needs_review=requires_review,
                matched_rule=rule_result.matched_rule,
                warnings=tuple(
                    dict.fromkeys(
                        [
                            *rule_result.warnings,
                            *draft.warnings,
                            *review.warnings,
                            *validation.warnings,
                        ]
                    )
                ),
                metadata=metadata,
            )
            events.append(
                HybridEvent(
                    "GPT_APPROVED",
                    "GPT 답변이 Validator를 통과해 Program Answer로 채택되었습니다.",
                    details={
                        "confidence": draft.confidence,
                        "requires_review": requires_review,
                    },
                )
            )
            return HybridAnswerOutcome(
                result,
                facts,
                intent,
                draft,
                review,
                validation,
                False,
                tuple(events),
            )
        except Exception as error:
            return self._fallback(
                rule_result,
                facts,
                reason=error.__class__.__name__.upper(),
                provider_name=self.provider.name,
                events=events,
                intent=intent,
                draft=draft,
                review=review,
                validation=validation,
            )


def _available_fact_paths(facts: AnswerFacts) -> list[str]:
    result: list[str] = []
    for section, values in facts.to_prompt_dict().items():
        if isinstance(values, dict):
            for key, value in values.items():
                if value not in (None, "", [], {}, ()):
                    result.append(f"{section}.{key}")
        elif values not in (None, "", [], {}, ()):
            result.append(section)
    return result
