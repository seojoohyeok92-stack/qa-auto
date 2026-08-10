from __future__ import annotations

import os

from answer.exceptions import AnswerProviderUnavailableError
from answer.governance_models import GptProviderSettings
from answer.providers.fake_gpt_provider import FakeGptProvider
from answer.providers.interfaces import JsonGptProvider
from answer.providers.openai_json_provider import OpenAIJsonProvider


GPT_PROVIDER_ENV = "QNA_GPT_PROVIDER"


def create_gpt_provider(name: str | None = None) -> JsonGptProvider:
    selected = str(name or os.getenv(GPT_PROVIDER_ENV) or "fake").strip().lower()
    if selected in {"fake", "fake_gpt"}:
        return FakeGptProvider()
    if selected == "openai":
        settings = GptProviderSettings.from_environment()
        if settings.provider_name != "openai":
            raise AnswerProviderUnavailableError(
                "QNA_GPT_PROVIDER=openai 설정이 필요합니다."
            )
        return OpenAIJsonProvider(settings)
    if selected in {"azure", "claude", "gemini"}:
        raise AnswerProviderUnavailableError(
            f"{selected} GPT provider transport는 아직 연결되지 않았습니다."
        )
    raise ValueError(f"지원하지 않는 GPT provider입니다: {selected}")
