from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


class GptMode(str, Enum):
    FAKE = "FAKE"
    SHADOW = "SHADOW"
    CANARY = "CANARY"
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


@dataclass(frozen=True)
class GptProviderSettings:
    provider_name: str = "fake"
    mode: GptMode = GptMode.FAKE
    model: str = "fake-json-v1"
    connect_timeout_seconds: float = 5.0
    # Requests are not streamed, so nothing arrives until generation is
    # finished: this is effectively "how long one model call may take".
    # 30s was below what the configured reasoning model needs even for the
    # small UNDERSTANDING prompt, which is what timed out in production.
    read_timeout_seconds: float = 45.0
    # The budget for one whole "GPT 새 답변 생성", across every provider call
    # it makes -- not per call. This is the operator's real wait.
    total_timeout_seconds: float = 120.0
    max_retries: int = 2
    retry_backoff_seconds: float = 0.5
    requests_per_minute: int = 30
    daily_request_limit: int = 1_000
    per_inquiry_request_limit: int = 5
    regeneration_cooldown_seconds: int = 10
    daily_cost_limit_krw: float = 0.0
    canary_percentage: float = 0.0
    shadow_enabled: bool = False
    enabled: bool = True
    approved_by_company: bool = False
    api_key_present: bool = False
    privacy_protection_enabled: bool = True
    prompt_capture_approved: bool = False
    prompt_version: str = "prompt-v1"
    privacy_policy_version: str = "privacy-v1"
    validator_policy_version: str = "validator-v1"
    company_tone_version: str = "tone-v1"
    policy_version: str = "governance-v1"
    allowed_provider_names: tuple[str, ...] = ("openai",)
    allowed_models: tuple[str, ...] = ()
    allowed_inquiry_types: tuple[str, ...] = (
        "PRODUCT_INQUIRY",
        "CUSTOMER_INQUIRY",
        "상품",
        "배송",
        "설치",
    )

    def __post_init__(self) -> None:
        if not isinstance(self.mode, GptMode):
            object.__setattr__(self, "mode", GptMode(str(self.mode).upper()))
        if not 0 <= self.canary_percentage <= 100:
            raise ValueError("canary_percentage must be between 0 and 100.")
        for name in (
            "connect_timeout_seconds",
            "read_timeout_seconds",
            "total_timeout_seconds",
        ):
            if float(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be positive.")
        if self.max_retries < 0:
            raise ValueError("max_retries must not be negative.")

    @classmethod
    def from_environment(cls) -> GptProviderSettings:
        provider = os.getenv("QNA_GPT_PROVIDER", "fake").strip().lower()
        default_model = "fake-json-v1" if provider == "fake" else ""
        return cls(
            provider_name=provider,
            mode=GptMode(os.getenv("QNA_GPT_MODE", "FAKE").strip().upper()),
            model=os.getenv("QNA_GPT_MODEL", default_model).strip(),
            connect_timeout_seconds=_float_env(
                "QNA_GPT_CONNECT_TIMEOUT_SECONDS", 5.0
            ),
            read_timeout_seconds=_float_env(
                "QNA_GPT_READ_TIMEOUT_SECONDS", 45.0
            ),
            total_timeout_seconds=_float_env(
                "QNA_GPT_TOTAL_TIMEOUT_SECONDS", 120.0
            ),
            max_retries=_int_env("QNA_GPT_MAX_RETRIES", 2),
            retry_backoff_seconds=_float_env(
                "QNA_GPT_RETRY_BACKOFF_SECONDS", 0.5
            ),
            requests_per_minute=_int_env(
                "QNA_GPT_REQUESTS_PER_MINUTE", 30
            ),
            daily_request_limit=_int_env(
                "QNA_GPT_DAILY_REQUEST_LIMIT", 1_000
            ),
            per_inquiry_request_limit=_int_env(
                "QNA_GPT_PER_INQUIRY_LIMIT", 5
            ),
            regeneration_cooldown_seconds=_int_env(
                "QNA_GPT_REGENERATION_COOLDOWN_SECONDS", 10
            ),
            daily_cost_limit_krw=_float_env(
                "QNA_GPT_DAILY_COST_LIMIT_KRW", 0.0
            ),
            canary_percentage=_float_env(
                "QNA_GPT_CANARY_PERCENTAGE", 0.0
            ),
            shadow_enabled=_bool_env("QNA_GPT_SHADOW_ENABLED"),
            enabled=_bool_env("QNA_GPT_ENABLED", True),
            approved_by_company=_bool_env("QNA_GPT_COMPANY_APPROVED"),
            api_key_present=bool(os.getenv("QNA_GPT_API_KEY")),
            privacy_protection_enabled=_bool_env(
                "QNA_GPT_PRIVACY_ENABLED", True
            ),
            prompt_capture_approved=(
                _bool_env("QNA_GPT_PROMPT_CAPTURE_COMPANY_APPROVED")
                and _bool_env("QNA_GPT_PROMPT_CAPTURE_SECURITY_APPROVED")
            ),
            prompt_version=os.getenv(
                "QNA_GPT_PROMPT_VERSION", "prompt-v1"
            ),
            privacy_policy_version=os.getenv(
                "QNA_GPT_PRIVACY_POLICY_VERSION", "privacy-v1"
            ),
            validator_policy_version=os.getenv(
                "QNA_GPT_VALIDATOR_POLICY_VERSION", "validator-v1"
            ),
            company_tone_version=os.getenv(
                "QNA_GPT_COMPANY_TONE_VERSION", "tone-v1"
            ),
            policy_version=os.getenv(
                "QNA_GPT_POLICY_VERSION", "governance-v1"
            ),
            allowed_provider_names=tuple(
                item.strip().lower()
                for item in os.getenv(
                    "QNA_GPT_ALLOWED_PROVIDERS", "openai"
                ).split(",")
                if item.strip()
            ),
            allowed_models=tuple(
                item.strip()
                for item in os.getenv("QNA_GPT_ALLOWED_MODELS", "").split(",")
                if item.strip()
            ),
        )

    @property
    def is_real_provider(self) -> bool:
        return self.provider_name.lower() not in {"fake", "fake_gpt"}

    def validation_issues(self) -> tuple[str, ...]:
        issues: list[str] = []
        if self.mode in {GptMode.DISABLED, GptMode.FAKE}:
            return ()
        if not self.enabled:
            issues.append("GPT provider가 비활성 상태입니다.")
        if self.is_real_provider:
            if self.provider_name.lower() not in self.allowed_provider_names:
                issues.append("승인된 Provider 목록에 포함되지 않았습니다.")
            if not self.approved_by_company:
                issues.append("회사 승인이 확인되지 않았습니다.")
            if not self.api_key_present:
                issues.append("Provider API key가 설정되지 않았습니다.")
            if not self.model:
                issues.append("Provider model이 설정되지 않았습니다.")
            elif self.allowed_models and self.model not in self.allowed_models:
                issues.append("승인된 모델 목록에 포함되지 않았습니다.")
            if not self.privacy_protection_enabled:
                issues.append("개인정보 보호 설정이 완료되지 않았습니다.")
        return tuple(issues)

    def public_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["mode"] = self.mode.value
        result.pop("api_key_present", None)
        result["api_key_configured"] = self.api_key_present
        return result


@dataclass(frozen=True)
class PrivacySanitizationResult:
    sanitized_payload: Any
    removed_fields: tuple[str, ...] = ()
    masked_patterns: tuple[str, ...] = ()
    blocking_issues: tuple[str, ...] = ()
    safe_to_send: bool = True

    def audit_dict(self) -> dict[str, Any]:
        return {
            "removed_count": len(self.removed_fields)
            + len(self.masked_patterns),
            "removed_fields": list(self.removed_fields),
            "masked_patterns": list(self.masked_patterns),
            "blocking_issues": list(self.blocking_issues),
            "safe_to_send": self.safe_to_send,
        }
