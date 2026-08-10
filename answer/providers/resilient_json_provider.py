from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from answer.governance_models import GptProviderSettings
from answer.provider_errors import (
    GptProviderAuthenticationError,
    GptProviderRetryableError,
    GptProviderTimeoutError,
)
from answer.providers.interfaces import JsonGptProvider


class ResilientJsonProvider:
    def __init__(
        self,
        provider: JsonGptProvider,
        settings: GptProviderSettings,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.provider = provider
        self.settings = settings
        self.sleeper = sleeper
        self.clock = clock
        self.name = provider.name
        self.retry_count = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.total_tokens = 0
        self.usage_available = False

    @staticmethod
    def _retryable(error: Exception) -> bool:
        if isinstance(error, GptProviderAuthenticationError):
            return False
        if isinstance(
            error,
            (GptProviderTimeoutError, TimeoutError, ConnectionError),
        ):
            return True
        if isinstance(error, GptProviderRetryableError):
            return error.status_code is None or error.status_code == 429 or (
                500 <= error.status_code <= 599
            )
        return False

    def generate_json(
        self,
        *,
        task: str,
        prompt: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        started = self.clock()
        attempt = 0
        while True:
            try:
                result = self.provider.generate_json(
                    task=task, prompt=prompt, context=context
                )
                usage = result.get("_usage") or result.get("usage")
                if isinstance(usage, dict):
                    input_tokens = usage.get(
                        "input_tokens", usage.get("prompt_tokens")
                    )
                    output_tokens = usage.get(
                        "output_tokens", usage.get("completion_tokens")
                    )
                    if input_tokens is not None and output_tokens is not None:
                        self.input_tokens += max(0, int(input_tokens))
                        self.output_tokens += max(0, int(output_tokens))
                        self.total_tokens += max(
                            0,
                            int(
                                usage.get(
                                    "total_tokens",
                                    int(input_tokens) + int(output_tokens),
                                )
                            ),
                        )
                        self.usage_available = True
                if self.clock() - started > self.settings.total_timeout_seconds:
                    raise GptProviderTimeoutError(
                        "GPT provider total timeout exceeded."
                    )
                return result
            except Exception as error:
                if not self._retryable(error) or attempt >= self.settings.max_retries:
                    if isinstance(error, (TimeoutError,)):
                        raise GptProviderTimeoutError(
                            "GPT provider request timed out."
                        ) from error
                    raise
                attempt += 1
                self.retry_count += 1
                delay = self.settings.retry_backoff_seconds * (2 ** (attempt - 1))
                if self.clock() - started + delay > self.settings.total_timeout_seconds:
                    raise GptProviderTimeoutError(
                        "GPT provider retry would exceed total timeout."
                    ) from error
                self.sleeper(delay)
