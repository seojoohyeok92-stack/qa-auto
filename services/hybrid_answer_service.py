from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from answer.answer_validator import AnswerValidator
from answer.fact_selection import FactSelectionService, SelectedFacts
from answer.facts import AnswerFacts, build_answer_facts
from answer.hybrid_models import (
    DraftResult,
    Emotion,
    IntentResult,
    SelfReviewResult,
    ValidationResult,
)
from answer.models import AnswerRequest, AnswerResult, AnswerStatus
from answer.inquiry_analysis import InquiryAnalysis
from answer.inquiry_analysis import AnswerStrategy
from answer.providers.interfaces import JsonGptProvider
from answer.text_utils import split_subquestions
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
        self._learning_context_provider = learning_context_provider
        self._stage_seconds: dict[str, float] = {}

    def _provider_telemetry(
        self, *, started: float | None = None
    ) -> dict[str, Any]:
        """How many provider round trips this generation cost, and where.

        When a generation failed the safe draft replaced the GPT draft and the
        evidence went with it, so nobody could tell afterwards whether the
        pipeline had made one call or five. Sizes and timings only -- the
        records carry no prompt text, context values or customer data.
        """

        records = list(getattr(self.provider, "call_records", []) or [])
        budget = getattr(self.drafts, "last_prompt_budget", None)
        telemetry: dict[str, Any] = {
            "prompt_budget": budget or {},
            "stage_seconds": dict(self._stage_seconds),
            "provider_call_count": len(records),
            "tasks": [str(item.get("task") or "") for item in records],
            "calls": records,
        }
        if started is not None:
            telemetry["total_elapsed_seconds"] = round(
                time.monotonic() - started, 3
            )
        return telemetry

    @staticmethod
    def _evidence_texts(learning_context: dict[str, Any]) -> str:
        """Every text the model was actually given, for grounding checks.

        The validator can see the facts but not the retrieved answers, so
        without this a claim taken straight from an approved learning example
        would look unsupported.
        """

        parts: list[str] = []
        for key in ("similar_approved_answers", "seller_style_examples",
                    "historical_cases"):
            for item in learning_context.get(key) or []:
                if isinstance(item, dict):
                    parts.extend(
                        str(value) for value in item.values()
                        if isinstance(value, str)
                    )
        signals = learning_context.get("feedback_signals")
        if isinstance(signals, dict):
            for group in signals.values():
                for item in group or []:
                    if isinstance(item, dict):
                        parts.append(str(item.get("content") or ""))
        return " ".join(part for part in parts if part)


    @staticmethod
    def _deterministic_intent(
        facts: AnswerFacts,
        analysis: InquiryAnalysis | None,
        rule_result: AnswerResult,
    ) -> IntentResult:
        """Derive the intent without spending a provider call on it.

        The UNDERSTANDING round trip re-derived what the Python analysis had
        already decided, and the only field anything downstream depends on is
        `questions` -- used for coverage, topic relevance and per-sub-question
        evidence. split_subquestions produces that deterministically, so the
        call bought a second opinion on a decision that was already made while
        adding a network round trip to every generation. Emotion, urgency and
        confidence were only ever written to a diagnostic event.
        """

        questions = split_subquestions(facts.inquiry.get("question"))
        return IntentResult(
            category=(
                (analysis.inquiry_subtype if analysis else "")
                or rule_result.category
                or "기타/직원확인"
            ),
            questions=questions,
            emotion=Emotion.NORMAL,
            urgency="NORMAL",
            confidence=float(analysis.confidence) if analysis else 1.0,
            requires_review=bool(
                analysis.manual_review_required
                if analysis is not None
                else rule_result.needs_review
            ),
            reason="결정적 분석으로 문의를 분해했습니다.",
        )

    @staticmethod
    def _neutral_self_review(questions: tuple[str, ...]) -> SelfReviewResult:
        """Stand in for the provider's self review.

        Every field the self review reported is checked deterministically by
        AnswerValidator -- speculation by pattern, fact existence against the
        resolved facts, coverage against the sub-questions, dates against DPS.
        Asking the model to grade its own answer added a third provider call
        whose opinion could veto a draft the validator would have passed, and
        on inquiry 686097134 that is exactly what happened: the model reported
        a fact inconsistency it could not point at, and a correct partial
        answer was replaced by the generic safe draft. Grading now belongs to
        the validator alone; this neutral result keeps its signature intact.
        """

        return SelfReviewResult(
            passed=True,
            answered_all_questions=True,
            has_speculation=False,
            facts_consistent=True,
            requires_review=False,
            reason="Validator가 결정적으로 검증합니다.",
            warnings=(),
        )

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
        telemetry: dict[str, Any] | None = None,
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
            "provider_telemetry": telemetry or {},
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
        generation_started = time.monotonic()
        # Wall clock per stage. The event log records when a row was written,
        # not when the work happened -- hybrid events are flushed together
        # after generate() returns -- so the log alone cannot say where the
        # time went. These are measured in place. Durations only.
        stage_seconds: dict[str, float] = {}
        self._stage_seconds = stage_seconds

        def _stage(name: str, since: float) -> float:
            now = time.monotonic()
            stage_seconds[name] = round(now - since, 3)
            return now

        _mark = time.monotonic()
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
        _mark = _stage("facts_and_selection", _mark)
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
            intent = self._deterministic_intent(facts, analysis, rule_result)
            _mark = _stage("intent", _mark)
            events.append(
                HybridEvent(
                    "GPT_ANALYSIS_COMPLETED",
                    "문의 분석을 완료했습니다.",
                    details={
                        "category": intent.category,
                        "emotion": intent.emotion.value,
                        "question_count": len(intent.questions),
                        "confidence": intent.confidence,
                        "source": "DETERMINISTIC",
                    },
                )
            )
            learning_context: dict[str, Any] = {}
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
                try:
                    learning_context = (
                        self._learning_context_provider(facts, intent)
                        if self._learning_context_provider is not None
                        else {}
                    )
                except Exception:
                    # Learning is an optional enrichment and can never block
                    # GPT.  Computed once here (instead of inside
                    # DraftGenerationService) so a bounded corrective
                    # regeneration below can reuse it without a second
                    # Learning/Historical DB lookup.
                    learning_context = {}
                draft = self.drafts.generate(
                    facts,
                    intent,
                    analysis=analysis,
                    selected_facts=selected_facts,
                    learning_context=learning_context,
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
            events.append(
                HybridEvent(
                    "LEARNING_ANSWER_USAGE_EVALUATED",
                    "선택 Learning의 실제 답변 근거 사용 여부를 확인했습니다.",
                    level=(
                        "WARNING"
                        if draft.learning_recovery_used
                        else "INFO"
                    ),
                    details={
                        "used_count": sum(
                            1
                            for item in draft.learning_usage
                            if item.get("answer_supported")
                        ),
                        "learning_usage": [
                            {
                                "learning_id": item.get("learning_id"),
                                "matched_subquestion": item.get(
                                    "matched_subquestion"
                                ),
                                "answer_supported": bool(
                                    item.get("answer_supported")
                                ),
                                "reason": item.get("reason"),
                            }
                            for item in draft.learning_usage
                        ],
                        "subquestion_results": [
                            dict(item)
                            for item in draft.subquestion_results
                        ],
                        "learning_recovery_used": (
                            draft.learning_recovery_used
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
                review = self._neutral_self_review(intent.questions)
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
            _mark = _stage("draft_provider_call", _mark)
            validation = self.validator.validate(
                facts,
                intent,
                draft,
                review,
                analysis=analysis,
                selected_facts=selected_facts,
                subquestion_evidence=learning_context.get("subquestion_evidence"),
                evidence_texts=self._evidence_texts(learning_context),
            )
            _mark = _stage("validation", _mark)
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
            can_regenerate = not (
                analysis is not None
                and analysis.answer_strategy
                is AnswerStrategy.REQUEST_ORDER_ID
            )
            if not validation.passed and can_regenerate:
                # Bounded, single corrective regeneration: a rejected draft is
                # often a fixable blanket-uncertainty or speculation problem,
                # not proof that no grounded answer exists.  Reuse the same
                # pre-computed learning_context (no extra Learning/Historical
                # query) and give the provider the concrete rejection reasons
                # so it can answer the supported parts and only ask for
                # confirmation on the parts that actually lack evidence.
                # Exactly one retry: no loop, no extra DPS call, no repeated
                # abuse of the provider.
                events.append(
                    HybridEvent(
                        "GPT_CORRECTIVE_REGENERATION_STARTED",
                        "검증 실패 답변에 대해 1회 보정 재생성을 시도합니다.",
                        "WARNING",
                        {"previous_errors": list(validation.errors)},
                    )
                )
                retry_feedback = {
                    "previous_attempt_rejected": True,
                    "previous_validation_errors": list(validation.errors),
                    "instruction": (
                        "이전 답변은 검증에 실패했습니다. 근거가 있는 "
                        "sub-question만 사실에 기반해 답하고, 근거가 없는 "
                        "부분은 추측하지 말고 확인이 필요하다고 안내하세요. "
                        "질문과 관련 없는 내용을 답변에 포함하지 마세요."
                    ),
                }
                retry_draft = self.drafts.generate(
                    facts,
                    intent,
                    analysis=analysis,
                    selected_facts=selected_facts,
                    learning_context=learning_context,
                    retry_feedback=retry_feedback,
                )
                retry_review = self._neutral_self_review(intent.questions)
                retry_validation = self.validator.validate(
                    facts,
                    intent,
                    retry_draft,
                    retry_review,
                    analysis=analysis,
                    selected_facts=selected_facts,
                    subquestion_evidence=learning_context.get("subquestion_evidence"),
                    evidence_texts=self._evidence_texts(learning_context),
                )
                events.append(
                    HybridEvent(
                        "GPT_CORRECTIVE_REGENERATION_COMPLETED",
                        (
                            "보정 재생성 답변이 Validator를 통과했습니다."
                            if retry_validation.passed
                            else "보정 재생성 답변도 Validator를 통과하지 "
                            "못했습니다."
                        ),
                        "INFO" if retry_validation.passed else "WARNING",
                        {
                            "passed": retry_validation.passed,
                            "errors": list(retry_validation.errors),
                        },
                    )
                )
                if retry_validation.passed:
                    draft, review, validation = (
                        retry_draft,
                        retry_review,
                        retry_validation,
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
                    telemetry=self._provider_telemetry(
                        started=generation_started
                    ),
                )
            requires_review = bool(
                rule_result.needs_review
                or intent.requires_review
                or draft.requires_review
                or review.requires_review
                or draft.has_required_missing_information
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
                "provider_telemetry": self._provider_telemetry(
                    started=generation_started
                ),
                "facts": {
                    "warnings": list(facts.warnings),
                    "available": _available_fact_paths(facts),
                },
                "intent": intent.to_dict(),
                "draft": draft.to_dict(),
                "self_review": review.to_dict(),
                "validation": validation.to_dict(),
                "phase9": metadata["phase9"],
                # Persisted so the downstream auto-post eligibility gate can
                # judge on Evidence/Authority instead of a bare confidence
                # number.  Retrieval already computed this; nothing is
                # recomputed and no extra provider call is made.
                "subquestion_evidence": [
                    dict(item)
                    for item in (
                        learning_context.get("subquestion_evidence") or []
                    )
                ],
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
                telemetry=self._provider_telemetry(
                    started=generation_started
                ),
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
