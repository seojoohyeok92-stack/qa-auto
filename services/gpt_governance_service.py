from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from answer.facts import build_answer_facts
from answer.governance_models import GptMode, GptProviderSettings
from answer.gpt_pricing import estimate_cost_krw
from answer.models import AnswerRequest, AnswerResult, AnswerStatus
from answer.provider_errors import (
    GptProviderCostLimitError,
    GptProviderRateLimitError,
    GptProviderTimeoutError,
)
from answer.providers.fake_gpt_provider import FakeGptProvider
from answer.providers.interfaces import JsonGptProvider
from answer.providers.openai_json_provider import OpenAIJsonProvider
from answer.providers.resilient_json_provider import ResilientJsonProvider
from repositories.database import Database
from repositories.gpt_provider_run_repository import GptProviderRunRepository
from services.hybrid_answer_service import (
    HybridAnswerOutcome,
    HybridAnswerService,
    HybridEvent,
)
from services.prompt_privacy_service import PromptPrivacyService
from services.learning_context_service import LearningContextService


HIGH_RISK_PATTERN = re.compile(
    r"(환불|반품|소송|법적|고소|분쟁|보상|손해배상|공정거래)"
)


def canary_selected(inquiry_id: int | str, percentage: float) -> bool:
    value = max(0.0, min(float(percentage), 100.0))
    digest = hashlib.sha256(str(inquiry_id).encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:8], "big") % 10_000
    return bucket < int(value * 100)


class GovernedHybridAnswerService:
    def __init__(
        self,
        database: Database,
        *,
        settings: GptProviderSettings | None = None,
        provider: JsonGptProvider | None = None,
        privacy: PromptPrivacyService | None = None,
        sleeper=time.sleep,
        clock=time.monotonic,
    ) -> None:
        self.database = database
        self.settings = settings or GptProviderSettings.from_environment()
        self.injected_provider = provider
        self.privacy = privacy or PromptPrivacyService()
        self.runs = GptProviderRunRepository(database)
        self.sleeper = sleeper
        self.clock = clock
        self.last_run_id: int | None = None

    def _provider(self) -> JsonGptProvider:
        if self.injected_provider is not None:
            provider = self.injected_provider
        elif not self.settings.is_real_provider:
            provider = FakeGptProvider()
        else:
            provider = OpenAIJsonProvider(self.settings)
        return ResilientJsonProvider(
            provider,
            self.settings,
            sleeper=self.sleeper,
            clock=self.clock,
        )

    def _limit_issue(self, inquiry_id: int | None) -> Exception | None:
        now = datetime.now(UTC)
        minute = (now - timedelta(minutes=1)).isoformat()
        day = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        if self.runs.count_since(minute) >= self.settings.requests_per_minute:
            return GptProviderRateLimitError("분당 GPT 요청 한도를 초과했습니다.")
        if self.runs.count_since(day) >= self.settings.daily_request_limit:
            return GptProviderRateLimitError("일일 GPT 요청 한도를 초과했습니다.")
        if inquiry_id is not None and self.runs.count_since(
            day, inquiry_id=inquiry_id
        ) >= self.settings.per_inquiry_request_limit:
            return GptProviderRateLimitError(
                "문의별 GPT 요청 한도를 초과했습니다."
            )
        recent = self.runs.recent(inquiry_id=inquiry_id, limit=1)
        if inquiry_id is not None and recent:
            try:
                created = datetime.fromisoformat(
                    str(recent[0]["created_at"]).replace("Z", "+00:00")
                )
                if (
                    datetime.now(UTC) - created.astimezone(UTC)
                ).total_seconds() < self.settings.regeneration_cooldown_seconds:
                    return GptProviderRateLimitError(
                        "동일 문의의 연속 재생성 제한 시간입니다."
                    )
            except (TypeError, ValueError):
                pass
        if (
            self.settings.daily_cost_limit_krw > 0
            and self.runs.cost_since(day)
            >= self.settings.daily_cost_limit_krw
        ):
            return GptProviderCostLimitError("일일 GPT 비용 한도를 초과했습니다.")
        return None

    @staticmethod
    def _rule_outcome(
        request: AnswerRequest,
        rule: AnswerResult,
        governance: dict[str, Any],
        events: list[HybridEvent],
    ) -> HybridAnswerOutcome:
        facts = build_answer_facts(request, rule)
        metadata = dict(rule.metadata)
        metadata["governance"] = governance
        result = AnswerResult(
            status=rule.status,
            category=rule.category,
            reason=rule.reason,
            answer=rule.answer,
            provider=rule.provider,
            auto_answerable=rule.auto_answerable,
            needs_review=rule.needs_review,
            matched_rule=rule.matched_rule,
            warnings=rule.warnings,
            metadata=metadata,
        )
        return HybridAnswerOutcome(
            result, facts, None, None, None, None, True, tuple(events)
        )

    def _audit(
        self,
        *,
        request: AnswerRequest,
        correlation_id: str,
        started_at: str,
        start_clock: float,
        outcome: HybridAnswerOutcome,
        privacy_removed_count: int,
        error_type: str | None,
        error_message: str | None,
        canary: bool,
        retry_count: int,
        shadow_comparison: dict[str, Any] | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
        estimated_cost_krw: float | None = None,
    ) -> int:
        completed_at = datetime.now(UTC).isoformat()
        validation = outcome.validation
        audit_inquiry_id = request.inquiry_id
        if audit_inquiry_id is not None:
            with self.database.connection() as connection:
                exists = connection.execute(
                    "SELECT 1 FROM inquiries WHERE id = ?",
                    (audit_inquiry_id,),
                ).fetchone()
            if exists is None:
                audit_inquiry_id = None
        run = self.runs.create_run(
            inquiry_id=audit_inquiry_id,
            correlation_id=correlation_id,
            provider=self.settings.provider_name,
            model=self.settings.model,
            mode=self.settings.mode.value,
            prompt_version=self.settings.prompt_version,
            policy_version=self.settings.policy_version,
            privacy_policy_version=self.settings.privacy_policy_version,
            validator_policy_version=self.settings.validator_policy_version,
            company_tone_version=self.settings.company_tone_version,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=int(max(0, self.clock() - start_clock) * 1_000),
            success=error_type is None,
            error_type=error_type,
            error_message_masked=error_message,
            input_size=len(
                json.dumps(
                    outcome.facts.to_prompt_dict(), ensure_ascii=False
                )
            ),
            output_size=len(outcome.result.answer),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            estimated_cost_krw=estimated_cost_krw,
            privacy_removed_count=privacy_removed_count,
            validator_passed=(
                validation.passed if validation is not None else None
            ),
            fallback_used=outcome.fallback_used,
            retry_count=retry_count,
            canary_selected=canary,
            shadow_comparison=shadow_comparison or {},
        )
        self.last_run_id = int(run["id"])
        return self.last_run_id

    def generate(
        self, request: AnswerRequest, rule_result: AnswerResult
    ) -> HybridAnswerOutcome:
        settings = self.settings
        correlation_id = str(uuid.uuid4())
        started_at = datetime.now(UTC).isoformat()
        start_clock = self.clock()
        facts = build_answer_facts(request, rule_result)
        privacy = self.privacy.sanitize(facts.to_prompt_dict())
        removed_count = len(privacy.removed_fields) + len(
            privacy.masked_patterns
        )
        governance = {
            "mode": settings.mode.value,
            "provider": settings.provider_name,
            "model": settings.model,
            "company_approved": settings.approved_by_company,
            "api_key_configured": settings.api_key_present,
            "prompt_version": settings.prompt_version,
            "privacy_policy_version": settings.privacy_policy_version,
            "validator_policy_version": settings.validator_policy_version,
            "company_tone_version": settings.company_tone_version,
            "policy_version": settings.policy_version,
            "correlation_id": correlation_id,
            "privacy": privacy.audit_dict(),
            "canary_selected": False,
            "shadow": settings.mode is GptMode.SHADOW,
        }
        events = [
            HybridEvent(
                "GPT_PROVIDER_REQUESTED",
                "GPT Provider 실행 정책을 확인했습니다.",
                details={
                    "mode": settings.mode.value,
                    "provider": settings.provider_name,
                    "correlation_id": correlation_id,
                },
            )
        ]
        if settings.mode is GptMode.DISABLED or not settings.enabled:
            governance["fallback_reason"] = "DISABLED"
            events.append(
                HybridEvent(
                    "GPT_RULE_FALLBACK",
                    "GPT 계층이 비활성화되어 Rule Answer를 사용했습니다.",
                    details={"reason": "DISABLED"},
                )
            )
            outcome = self._rule_outcome(
                request, rule_result, governance, events
            )
            self._audit(
                request=request,
                correlation_id=correlation_id,
                started_at=started_at,
                start_clock=start_clock,
                outcome=outcome,
                privacy_removed_count=removed_count,
                error_type=None,
                error_message=None,
                canary=False,
                retry_count=0,
            )
            return outcome
        issues = settings.validation_issues()
        if issues:
            governance["fallback_reason"] = "CONFIGURATION_INVALID"
            events.extend(
                [
                    HybridEvent(
                        "GPT_CONFIGURATION_INVALID",
                        "실제 Provider 설정이 유효하지 않아 호출을 차단했습니다.",
                        "WARNING",
                        {"issues": list(issues)},
                    ),
                    HybridEvent(
                        "GPT_RULE_FALLBACK",
                        "Provider 설정 오류로 Rule Answer를 사용했습니다.",
                        "WARNING",
                        {"reason": "CONFIGURATION_INVALID"},
                    ),
                ]
            )
            outcome = self._rule_outcome(
                request, rule_result, governance, events
            )
            self._audit(
                request=request,
                correlation_id=correlation_id,
                started_at=started_at,
                start_clock=start_clock,
                outcome=outcome,
                privacy_removed_count=removed_count,
                error_type="CONFIGURATION_INVALID",
                error_message=" ".join(issues),
                canary=False,
                retry_count=0,
            )
            return outcome
        if settings.is_real_provider and not privacy.safe_to_send:
            governance["fallback_reason"] = "PRIVACY_BLOCKED"
            events.extend(
                [
                    HybridEvent(
                        "GPT_PRIVACY_BLOCKED",
                        "개인정보 보호 검사로 외부 Provider 호출을 차단했습니다.",
                        "WARNING",
                        {"blocking_issue_count": len(privacy.blocking_issues)},
                    ),
                    HybridEvent(
                        "GPT_RULE_FALLBACK",
                        "Privacy 검사 차단으로 Rule Answer를 사용했습니다.",
                        "WARNING",
                        {"reason": "PRIVACY_BLOCKED"},
                    ),
                ]
            )
            outcome = self._rule_outcome(
                request, rule_result, governance, events
            )
            self._audit(
                request=request,
                correlation_id=correlation_id,
                started_at=started_at,
                start_clock=start_clock,
                outcome=outcome,
                privacy_removed_count=removed_count,
                error_type="PRIVACY_BLOCKED",
                error_message="Privacy policy blocked the request.",
                canary=False,
                retry_count=0,
            )
            return outcome
        limit_error = self._limit_issue(request.inquiry_id)
        if limit_error is not None:
            is_cost = isinstance(limit_error, GptProviderCostLimitError)
            code = (
                "GPT_PROVIDER_COST_LIMITED"
                if is_cost
                else "GPT_PROVIDER_RATE_LIMITED"
            )
            governance["fallback_reason"] = (
                "COST_LIMITED" if is_cost else "RATE_LIMITED"
            )
            events.extend(
                [
                    HybridEvent(code, str(limit_error), "WARNING"),
                    HybridEvent(
                        "GPT_RULE_FALLBACK",
                        "Provider 운영 한도로 Rule Answer를 사용했습니다.",
                        "WARNING",
                        {"reason": governance["fallback_reason"]},
                    ),
                ]
            )
            outcome = self._rule_outcome(
                request, rule_result, governance, events
            )
            self._audit(
                request=request,
                correlation_id=correlation_id,
                started_at=started_at,
                start_clock=start_clock,
                outcome=outcome,
                privacy_removed_count=removed_count,
                error_type=governance["fallback_reason"],
                error_message=str(limit_error),
                canary=False,
                retry_count=0,
            )
            return outcome

        canary = False
        if settings.mode is GptMode.CANARY:
            type_allowed = request.inquiry_type in settings.allowed_inquiry_types
            excluded = bool(
                rule_result.needs_review
                or HIGH_RISK_PATTERN.search(request.question)
                or removed_count
                or not type_allowed
            )
            canary = (
                not excluded
                and canary_selected(
                    request.inquiry_id or request.question_id,
                    settings.canary_percentage,
                )
            )
            governance["canary_selected"] = canary
            events.append(
                HybridEvent(
                    "GPT_CANARY_SELECTED" if canary else "GPT_CANARY_SKIPPED",
                    "Canary 대상에 선정되었습니다."
                    if canary
                    else "Canary 대상에서 제외되었습니다.",
                    details={"selected": canary},
                )
            )
            if excluded:
                governance["fallback_reason"] = "CANARY_EXCLUDED"
                events.append(
                    HybridEvent(
                        "GPT_RULE_FALLBACK",
                        "고위험 또는 Privacy 조건으로 Rule Answer를 사용했습니다.",
                        "WARNING",
                        {"reason": "CANARY_EXCLUDED"},
                    )
                )
                outcome = self._rule_outcome(
                    request, rule_result, governance, events
                )
                self._audit(
                    request=request,
                    correlation_id=correlation_id,
                    started_at=started_at,
                    start_clock=start_clock,
                    outcome=outcome,
                    privacy_removed_count=removed_count,
                    error_type=None,
                    error_message=None,
                    canary=False,
                    retry_count=0,
                )
                return outcome

        provider = self._provider()
        if settings.mode is GptMode.CANARY and not canary:
            provider = ResilientJsonProvider(
                FakeGptProvider(),
                settings,
                sleeper=self.sleeper,
                clock=self.clock,
            )
        try:
            learning_context = LearningContextService(self.database)
            hybrid = HybridAnswerService(
                provider,
                learning_context_provider=learning_context.build,
            ).generate(
                request, rule_result
            )
            retry_count = getattr(provider, "retry_count", 0)
            events.extend(hybrid.events)
            hybrid_metadata = hybrid.result.metadata.get("hybrid", {})
            hybrid_reason = str(
                hybrid_metadata.get("fallback_reason") or ""
            )
            timeout_failure = "TIMEOUT" in hybrid_reason
            provider_failure = bool(
                hybrid.fallback_used
                and hybrid_reason
                and hybrid_reason != "VALIDATION_FAILED"
            )
            if provider_failure:
                events.append(
                    HybridEvent(
                        "GPT_PROVIDER_TIMEOUT"
                        if timeout_failure
                        else "GPT_PROVIDER_FAILED",
                        "GPT Provider 응답 시간이 초과되었습니다."
                        if timeout_failure
                        else "GPT Provider 처리에 실패했습니다.",
                        "WARNING",
                        {"error_type": hybrid_reason},
                    )
                )
            else:
                events.append(
                    HybridEvent(
                        "GPT_PROVIDER_SUCCEEDED",
                        "GPT Provider 처리를 완료했습니다.",
                        details={
                            "mode": settings.mode.value,
                            "retry_count": retry_count,
                        },
                    )
                )
            shadow_comparison: dict[str, Any] = {}
            outcome = hybrid
            if settings.mode is GptMode.SHADOW:
                shadow_comparison = {
                    "validator_passed": bool(
                        hybrid.validation and hybrid.validation.passed
                    ),
                    "rule_length": len(rule_result.answer),
                    "gpt_length": len(hybrid.result.answer),
                    "question_count": (
                        len(hybrid.intent.questions) if hybrid.intent else 0
                    ),
                    "used_facts": (
                        list(hybrid.draft.used_facts)
                        if hybrid.draft
                        else []
                    ),
                    "missing_information": (
                        list(hybrid.draft.missing_information)
                        if hybrid.draft
                        else []
                    ),
                }
                governance["shadow_comparison"] = shadow_comparison
                events.append(
                    HybridEvent(
                        "GPT_SHADOW_COMPLETED",
                        "Shadow 비교를 완료하고 Program Answer는 Rule로 유지했습니다.",
                        details=shadow_comparison,
                    )
                )
                outcome = self._rule_outcome(
                    request, rule_result, governance, events
                )
                outcome = replace(
                    outcome,
                    intent=hybrid.intent,
                    draft=hybrid.draft,
                    self_review=hybrid.self_review,
                    validation=hybrid.validation,
                    fallback_used=hybrid.fallback_used,
                )
            elif settings.mode is GptMode.CANARY and canary:
                metadata = dict(hybrid.result.metadata)
                governance["employee_review_required"] = True
                metadata["governance"] = governance
                result = replace(
                    hybrid.result,
                    status=AnswerStatus.NEEDS_REVIEW,
                    auto_answerable=False,
                    needs_review=True,
                    metadata=metadata,
                )
                outcome = replace(hybrid, result=result)
            else:
                metadata = dict(hybrid.result.metadata)
                metadata["governance"] = governance
                outcome = replace(
                    hybrid,
                    result=replace(hybrid.result, metadata=metadata),
                    events=tuple(events),
                )
            self._audit(
                request=request,
                correlation_id=correlation_id,
                started_at=started_at,
                start_clock=start_clock,
                outcome=outcome,
                privacy_removed_count=removed_count,
                error_type=(
                    "GPT_PROVIDER_TIMEOUT"
                    if timeout_failure
                    else (hybrid_reason if provider_failure else None)
                ),
                error_message=(
                    "Provider processing failed."
                    if provider_failure
                    else None
                ),
                canary=canary,
                retry_count=retry_count,
                shadow_comparison=shadow_comparison,
                input_tokens=(
                    provider.input_tokens
                    if getattr(provider, "usage_available", False)
                    else None
                ),
                output_tokens=(
                    provider.output_tokens
                    if getattr(provider, "usage_available", False)
                    else None
                ),
                total_tokens=(
                    provider.total_tokens
                    if getattr(provider, "usage_available", False)
                    else None
                ),
                estimated_cost_krw=(
                    estimate_cost_krw(
                        settings.model,
                        input_tokens=provider.input_tokens,
                        output_tokens=provider.output_tokens,
                    )
                    if getattr(provider, "usage_available", False)
                    else (
                        0.0 if not settings.is_real_provider else None
                    )
                ),
            )
            return outcome
        except Exception as error:
            timeout = isinstance(error, (GptProviderTimeoutError, TimeoutError))
            # Provenance for the failed call. Without this a timeout recorded
            # only the exception class, so the next investigation could not
            # tell which generation stage stalled, how many HTTP attempts the
            # wall clock actually covered, or whether the prompt had grown.
            # Metadata only -- no prompt text, no context values, no key.
            provider_call = dict(getattr(provider, "last_call", {}) or {})
            provider_call["retry_count"] = getattr(provider, "retry_count", 0)
            events.extend(
                [
                    HybridEvent(
                        "GPT_PROVIDER_TIMEOUT"
                        if timeout
                        else "GPT_PROVIDER_FAILED",
                        "GPT Provider 응답 시간이 초과되었습니다."
                        if timeout
                        else "GPT Provider 처리에 실패했습니다.",
                        "WARNING",
                        {
                            "error_type": error.__class__.__name__,
                            "provider_call": provider_call,
                        },
                    ),
                    HybridEvent(
                        "GPT_RULE_FALLBACK",
                        "Provider 장애로 Rule Answer를 사용했습니다.",
                        "WARNING",
                        {"reason": "TIMEOUT" if timeout else "PROVIDER_FAILED"},
                    ),
                ]
            )
            governance["fallback_reason"] = (
                "TIMEOUT" if timeout else "PROVIDER_FAILED"
            )
            outcome = self._rule_outcome(
                request, rule_result, governance, events
            )
            self._audit(
                request=request,
                correlation_id=correlation_id,
                started_at=started_at,
                start_clock=start_clock,
                outcome=outcome,
                privacy_removed_count=removed_count,
                error_type=(
                    "GPT_PROVIDER_TIMEOUT"
                    if timeout
                    else error.__class__.__name__.upper()
                ),
                error_message=str(error),
                canary=canary,
                retry_count=getattr(locals().get("provider"), "retry_count", 0),
            )
            return outcome
