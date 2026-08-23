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
        # Safe diagnostics for the most recent provider call. A timeout
        # previously recorded only the exception class, which left no way to
        # tell a slow single request from prompt growth or from retries
        # burning the wall clock. Metadata only: never the prompt text, the
        # context values, the API key or anything a customer wrote.
        self.last_call: dict[str, Any] = {}

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
        record: dict[str, Any] = {
            # Which generation stage this was: UNDERSTANDING, DRAFT,
            # SELF_REVIEW. Each stage gets its own budget -- `started` is
            # per call -- so the stage name is what identifies where the
            # wall clock went.
            "task": str(task).upper(),
            "model": self.settings.model,
            "connect_timeout_seconds": self.settings.connect_timeout_seconds,
            "read_timeout_seconds": self.settings.read_timeout_seconds,
            "total_timeout_seconds": self.settings.total_timeout_seconds,
            "max_retries": self.settings.max_retries,
            # Sizes only. The prompt itself is never recorded.
            "prompt_chars": len(str(prompt or "")),
            "context_keys": sorted(str(key) for key in (context or {})),
            "attempts": 0,
            "outcome": "STARTED",
        }
        self.last_call = record
        # Tracked without reading the clock: the first attempt starts with the
        # call, and each later one starts when the backoff finishes. Recording
        # must not add clock reads -- the retry budget arithmetic is what the
        # clock is for.
        attempt_started = started
        while True:
            record["attempts"] = attempt + 1
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
                now = self.clock()
                record["last_attempt_seconds"] = round(now - attempt_started, 3)
                record["elapsed_seconds"] = round(now - started, 3)
                if now - started > self.settings.total_timeout_seconds:
                    record["outcome"] = "TOTAL_TIMEOUT_AFTER_RESPONSE"
                    raise GptProviderTimeoutError(
                        "GPT provider total timeout exceeded."
                    )
                record["outcome"] = "OK"
                return result
            except Exception as error:
                record["outcome"] = error.__class__.__name__
                if not self._retryable(error) or attempt >= self.settings.max_retries:
                    if isinstance(error, (TimeoutError,)):
                        raise GptProviderTimeoutError(
                            "GPT provider request timed out."
                        ) from error
                    raise
                attempt += 1
                self.retry_count += 1
                delay = self.settings.retry_backoff_seconds * (2 ** (attempt - 1))
                now = self.clock()
                record["last_attempt_seconds"] = round(now - attempt_started, 3)
                record["elapsed_seconds"] = round(now - started, 3)
                if now - started + delay > self.settings.total_timeout_seconds:
                    record["outcome"] = "RETRY_WOULD_EXCEED_TOTAL_TIMEOUT"
                    raise GptProviderTimeoutError(
                        "GPT provider retry would exceed total timeout."
                    ) from error
                self.sleeper(delay)
                attempt_started = now + delay
