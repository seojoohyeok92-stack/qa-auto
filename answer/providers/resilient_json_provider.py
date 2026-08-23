from __future__ import annotations

import json
import re
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


# Open a branch further only when it is large enough to be the explanation.
_EXPLAIN_COMPONENT_CHARS = 20_000
_MAX_COMPONENT_DEPTH = 3
# Real prompt keys are code-defined identifiers such as "system_policy"
# or "learning_retrieval". Only that shape is recorded.
_SAFE_COMPONENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def _prompt_component_chars(prompt: object) -> dict[str, int]:
    """Size each part of the prompt, so an oversized one can be explained.

    A production DRAFT prompt measured 655,129 characters where the same
    inquiry measured about 15,000 on a database with no learning records, and
    nothing in the record said which part carried the difference. The prompt
    is a JSON document, so its own structure gives the breakdown.

    Sizes only -- keys and character counts. No prompt text, no learning
    bodies, no customer data. Returns {} for a prompt that is not JSON.
    """

    try:
        payload = json.loads(str(prompt or ""))
    except (TypeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}

    def measure(value: Any) -> int:
        return len(json.dumps(value, ensure_ascii=False, default=str))

    sizes: dict[str, int] = {}

    def name(key: object, index: int) -> str:
        # A component name is only ever a code-defined identifier. Anything
        # else -- which would mean a data-derived key -- is recorded by
        # position instead, so a size record can never carry customer text.
        text = str(key)
        return text if _SAFE_COMPONENT_NAME.match(text) else f"key_{index}"

    def walk(prefix: str, value: Any, depth: int) -> None:
        chars = measure(value)
        sizes[prefix] = chars
        if isinstance(value, list):
            sizes[f"{prefix}.count"] = len(value)
        # Descend while a branch is still worth explaining. `input` carries
        # the retrieved evidence and is always broken out; anything else is
        # only opened when it is large enough to be the answer to "what made
        # this prompt 655,129 characters", so an ordinary record stays small.
        if depth >= _MAX_COMPONENT_DEPTH:
            return
        explain = prefix == "input" or chars >= _EXPLAIN_COMPONENT_CHARS
        if not explain:
            return
        if isinstance(value, dict):
            for index, (key, item) in enumerate(value.items()):
                walk(f"{prefix}.{name(key, index)}", item, depth + 1)
        elif isinstance(value, list) and value:
            # For a list, the largest single record locates the weight
            # without naming every element.
            biggest = max(value, key=measure)
            sizes[f"{prefix}.max_record"] = measure(biggest)

    for index, (key, value) in enumerate(payload.items()):
        walk(name(key, index), value, 0)
    return dict(sorted(sizes.items(), key=lambda item: -item[1]))


def _accounted_chars(components: dict[str, int]) -> int:
    """How much of the prompt the top-level components explain.

    The remainder is JSON key names and punctuation. Reporting it keeps an
    unexplained gap from being mistaken for a missing component.
    """

    return sum(
        chars
        for name, chars in components.items()
        if "." not in name and not name.endswith(".count")
    )


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
        # One deadline for the whole generation, not per call. A single
        # "GPT 새 답변 생성" runs UNDERSTANDING, DRAFT and SELF_REVIEW, plus a
        # corrective DRAFT/SELF_REVIEW pair when validation fails -- up to
        # five provider calls. With a per-call budget the operator's worst
        # case was that budget multiplied by five, so the configured total
        # never described what anyone actually waited. This provider is built
        # once per generation, so the instance lifetime is the generation.
        self._deadline: float | None = None
        # Safe diagnostics for the most recent provider call. A timeout
        # previously recorded only the exception class, which left no way to
        # tell a slow single request from prompt growth or from retries
        # burning the wall clock. Metadata only: never the prompt text, the
        # context values, the API key or anything a customer wrote.
        self.last_call: dict[str, Any] = {}
        # Every call this generation made, in order. One instance is built per
        # generation, so this is the per-generation provider ledger: how many
        # round trips a "GPT 새 답변 생성" actually cost and where the time went.
        self.call_records: list[dict[str, Any]] = []

    @staticmethod
    def _retryable(error: Exception) -> bool:
        if isinstance(error, GptProviderAuthenticationError):
            return False
        # A response timeout is not a transient fault. The request reached the
        # provider and generation ran past the budget; sending the identical
        # request again almost always runs past it again, and the only certain
        # effect is doubling the customer's wait. Connect failures, 429s and
        # 5xx are genuinely transient and stay retryable -- a connect timeout
        # arrives as GptProviderRetryableError from the transport.
        if isinstance(error, (GptProviderTimeoutError, TimeoutError)):
            return False
        if isinstance(error, ConnectionError):
            return True
        if isinstance(error, GptProviderRetryableError):
            return error.status_code is None or error.status_code == 429 or (
                500 <= error.status_code <= 599
            )
        return False

    def _apply_attempt_budget(self, attempt_started: float) -> None:
        """Tell the inner provider how long this attempt may take.

        Providers that cannot bound themselves (the fakes, and any transport
        without the hook) simply do not implement it.
        """

        setter = getattr(self.provider, "set_attempt_budget", None)
        if not callable(setter) or self._deadline is None:
            return
        setter(max(0.0, self._deadline - attempt_started))

    def generate_json(
        self,
        *,
        task: str,
        prompt: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        started = self.clock()
        # Parsed once: on an oversized prompt this is the expensive part.
        _components = _prompt_component_chars(prompt)
        _accounted = _accounted_chars(_components)
        _total = len(str(prompt or ""))
        if self._deadline is None:
            # Reuses the clock read above so recording adds no extra reads.
            self._deadline = started + self.settings.total_timeout_seconds
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
            "prompt_component_chars": _components,
            "prompt_accounted_chars": _accounted,
            "prompt_unaccounted_chars": _total - _accounted,
            "context_keys": sorted(str(key) for key in (context or {})),
            "attempts": 0,
            "outcome": "STARTED",
        }
        self.last_call = record
        self.call_records.append(record)
        # Tracked without reading the clock: the first attempt starts with the
        # call, and each later one starts when the backoff finishes. Recording
        # must not add clock reads -- the retry budget arithmetic is what the
        # clock is for.
        attempt_started = started
        record["deadline_budget_seconds"] = round(self._deadline - started, 3)
        while True:
            record["attempts"] = attempt + 1
            # Bound the request itself by what is left of the generation
            # budget, so the deadline restrains an in-flight call instead of
            # only being noticed once it has already overrun.
            self._apply_attempt_budget(attempt_started)
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
                if now > self._deadline:
                    record["outcome"] = "TOTAL_TIMEOUT_AFTER_RESPONSE"
                    raise GptProviderTimeoutError(
                        "GPT provider total timeout exceeded."
                    )
                record["outcome"] = "OK"
                return result
            except Exception as error:
                # Read the clock once and use it for both the terminal and the
                # retry path. How long the attempt actually took is the whole
                # point of the record, so it must survive a terminal failure --
                # that is the number the server needs after a timeout.
                now = self.clock()
                record["last_attempt_seconds"] = round(now - attempt_started, 3)
                record["elapsed_seconds"] = round(now - started, 3)
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
                if now + delay > self._deadline:
                    record["outcome"] = "RETRY_WOULD_EXCEED_TOTAL_TIMEOUT"
                    raise GptProviderTimeoutError(
                        "GPT provider retry would exceed total timeout."
                    ) from error
                self.sleeper(delay)
                attempt_started = now + delay
