from __future__ import annotations

import os

import pytest

from answer.exceptions import AnswerProviderUnavailableError
from answer.governance_models import GptMode, GptProviderSettings
from answer.providers.openai_json_provider import OpenAIJsonProvider
from services.prompt_privacy_service import PromptPrivacyService


@pytest.mark.parametrize("mode", list(GptMode))
def test_all_provider_modes_are_supported(mode: GptMode) -> None:
    settings = GptProviderSettings(mode=mode)
    assert settings.mode is mode


def test_mode_string_is_normalized() -> None:
    assert GptProviderSettings(mode="shadow").mode is GptMode.SHADOW


def test_default_environment_mode_is_fake(monkeypatch) -> None:
    for name in (
        "QNA_GPT_MODE",
        "QNA_GPT_PROVIDER",
        "QNA_GPT_API_KEY",
        "QNA_GPT_COMPANY_APPROVED",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = GptProviderSettings.from_environment()
    assert settings.mode is GptMode.FAKE
    assert settings.provider_name == "fake"
    assert settings.api_key_present is False


def test_public_settings_never_include_api_key_value(monkeypatch) -> None:
    monkeypatch.setenv("QNA_GPT_API_KEY", "super-secret-value")
    settings = GptProviderSettings.from_environment()
    public = settings.public_dict()
    assert "api_key_present" not in public
    assert "super-secret-value" not in str(public)
    assert public["api_key_configured"] is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"approved_by_company": False, "api_key_present": True, "model": "m"},
        {"approved_by_company": True, "api_key_present": False, "model": "m"},
        {"approved_by_company": True, "api_key_present": True, "model": ""},
        {
            "approved_by_company": True,
            "api_key_present": True,
            "model": "m",
            "privacy_protection_enabled": False,
        },
    ],
)
def test_real_provider_requires_all_company_gates(overrides: dict) -> None:
    settings = GptProviderSettings(
        provider_name="openai", mode=GptMode.ACTIVE, **overrides
    )
    assert settings.validation_issues()


def test_real_provider_gate_passes_when_all_conditions_exist() -> None:
    settings = GptProviderSettings(
        provider_name="openai",
        mode=GptMode.ACTIVE,
        model="approved-model",
        approved_by_company=True,
        api_key_present=True,
        privacy_protection_enabled=True,
    )
    assert settings.validation_issues() == ()


@pytest.mark.parametrize("mode", [GptMode.FAKE, GptMode.DISABLED])
def test_fake_and_disabled_do_not_require_company_gate(mode: GptMode) -> None:
    settings = GptProviderSettings(
        provider_name="fake",
        mode=mode,
        model="fake-json-v1",
        approved_by_company=False,
        api_key_present=False,
    )
    assert settings.validation_issues() == ()


@pytest.mark.parametrize("percentage", [-1, 101])
def test_invalid_canary_percentage_is_rejected(percentage: float) -> None:
    with pytest.raises(ValueError, match="canary"):
        GptProviderSettings(canary_percentage=percentage)


@pytest.mark.parametrize(
    "field",
    [
        "connect_timeout_seconds",
        "read_timeout_seconds",
        "total_timeout_seconds",
    ],
)
def test_non_positive_timeout_is_rejected(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        GptProviderSettings(**{field: 0})


def test_negative_retry_count_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_retries"):
        GptProviderSettings(max_retries=-1)


def test_prompt_capture_requires_two_approval_flags(monkeypatch) -> None:
    monkeypatch.setenv("QNA_GPT_PROMPT_CAPTURE_COMPANY_APPROVED", "true")
    monkeypatch.delenv(
        "QNA_GPT_PROMPT_CAPTURE_SECURITY_APPROVED", raising=False
    )
    assert (
        GptProviderSettings.from_environment().prompt_capture_approved
        is False
    )
    monkeypatch.setenv("QNA_GPT_PROMPT_CAPTURE_SECURITY_APPROVED", "true")
    assert (
        GptProviderSettings.from_environment().prompt_capture_approved
        is True
    )


@pytest.mark.parametrize(
    "field",
    [
        "order_id",
        "product_order_id",
        "inquiry_id",
        "question_id",
        "customer_id",
        "customer_display",
        "customer_name",
        "phone",
        "email",
        "address",
        "sales_number",
    ],
)
def test_privacy_removes_sensitive_fields(field: str) -> None:
    result = PromptPrivacyService().sanitize(
        {"safe": "value", field: "secret"}
    )
    assert field not in result.sanitized_payload
    assert field in result.removed_fields
    assert result.sanitized_payload["safe"] == "value"


@pytest.mark.parametrize(
    ("text", "marker"),
    [
        ("010-1234-5678", "<masked-phone>"),
        ("customer@example.com", "<masked-email>"),
        ("서울시 강남구 테헤란로 123", "<masked-address>"),
        ("2026072912345678", "<masked-order>"),
    ],
)
def test_privacy_masks_patterns(text: str, marker: str) -> None:
    result = PromptPrivacyService().sanitize({"question": text})
    assert text not in str(result.sanitized_payload)
    assert marker in str(result.sanitized_payload)


def test_phone_masking_alone_is_safe_to_send() -> None:
    result = PromptPrivacyService().sanitize(
        {"question": "연락처는 010-1234-5678입니다"}
    )
    assert result.safe_to_send is True
    assert "phone" in result.masked_patterns


@pytest.mark.parametrize(
    "payload",
    [
        {"question": "otp=123456"},
        {"question": "token=secret-value"},
        {"question": "http://10.0.0.1/admin"},
        {"question": r"C:\internal\secret.txt"},
        {"api_key": "secret"},
        {"authorization": "Bearer secret"},
    ],
)
def test_privacy_blocks_auth_or_internal_values(payload: dict) -> None:
    result = PromptPrivacyService().sanitize(payload)
    assert result.safe_to_send is False
    assert result.blocking_issues


def test_nested_sensitive_fields_are_removed_with_path() -> None:
    result = PromptPrivacyService().sanitize(
        {"facts": {"order": {"order_id": "ORDER", "status": "배송중"}}}
    )
    assert result.sanitized_payload["facts"]["order"] == {"status": "배송중"}
    assert "facts.order.order_id" in result.removed_fields


def test_privacy_audit_does_not_contain_original_payload() -> None:
    secret = "token=do-not-store"
    result = PromptPrivacyService().sanitize({"question": secret})
    assert secret not in str(result.audit_dict())


def test_openai_adapter_blocks_unapproved_initialization() -> None:
    with pytest.raises(AnswerProviderUnavailableError):
        OpenAIJsonProvider(
            GptProviderSettings(
                provider_name="openai",
                mode=GptMode.ACTIVE,
                model="model",
                approved_by_company=False,
                api_key_present=True,
            )
        )


def test_openai_adapter_import_and_init_need_no_openai_package() -> None:
    settings = GptProviderSettings(
        provider_name="openai",
        mode=GptMode.ACTIVE,
        model="model",
        approved_by_company=True,
        api_key_present=True,
    )
    provider = OpenAIJsonProvider(
        settings,
        transport=lambda **kwargs: {"answer": "json"},
    )
    assert provider.generate_json(
        task="DRAFT", prompt="{}", context={}
    ) == {"answer": "json"}


def test_openai_adapter_without_api_key_never_calls_network(monkeypatch) -> None:
    monkeypatch.delenv("QNA_GPT_API_KEY", raising=False)
    settings = GptProviderSettings(
        provider_name="openai",
        mode=GptMode.ACTIVE,
        model="model",
        approved_by_company=True,
        api_key_present=True,
    )
    with pytest.raises(AnswerProviderUnavailableError, match="API key"):
        OpenAIJsonProvider(settings).generate_json(
            task="DRAFT", prompt="{}", context={}
        )
