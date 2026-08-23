"""Provider timeout policy and provenance for inquiry 686058300.

Production reproduction: "GPT 새 답변 생성" produced no draft.

    GPT_BUTTON_CLICKED       2026-08-23T04:36:22.401Z
    GPT_FALLBACK_RULE        2026-08-23T04:37:21.013Z  GPTPROVIDERTIMEOUTERROR
    ANSWER_GENERATION_FAILED 2026-08-23T04:37:21.079Z

58.6 seconds, and no GPT_ANALYSIS_COMPLETED in between -- the very first
provider call (UNDERSTANDING) never returned. Requests are not streamed, so
read_timeout is in practice "how long one model call may take", and the
30s default was under what the configured model needs. The retry then sent
the same slow request again, doubling the wait to no purpose, and the
configured total timeout could not restrain either attempt because it was
only compared after a read timeout had already elapsed.

The policy these tests pin down:

  * one deadline for the whole generation, not per provider call;
  * that deadline bounds the in-flight request, not just the gaps between;
  * a response timeout is terminal, while connect failures, 429 and 5xx retry.

Fake clock and fake providers only -- no network, no real provider, no key.
"""
from __future__ import annotations

import pytest

from answer.governance_models import GptProviderSettings
from answer.provider_errors import (
    GptProviderAuthenticationError,
    GptProviderRetryableError,
    GptProviderTimeoutError,
)
from answer.providers.resilient_json_provider import ResilientJsonProvider


READ_TIMEOUT = 45.0
TOTAL_BUDGET = 120.0
SECRET_PROMPT = "고객 010-1234-5678 님의 문의 내용입니다. sk-test-not-a-real-key"


def settings(**overrides) -> GptProviderSettings:
    values = {
        "model": "gpt-5.6-sol",
        "connect_timeout_seconds": 5.0,
        "read_timeout_seconds": READ_TIMEOUT,
        "total_timeout_seconds": TOTAL_BUDGET,
        "max_retries": 2,
        "retry_backoff_seconds": 0.5,
    }
    values.update(overrides)
    return GptProviderSettings(**values)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class ReadTimeoutProvider:
    """Behaves like a request whose generation outran the read budget."""

    name = "openai"

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.attempts = 0
        self.budgets: list[float] = []

    def set_attempt_budget(self, seconds: float | None) -> None:
        self.budgets.append(seconds)

    def generate_json(self, *, task, prompt, context):
        self.attempts += 1
        # Burn whatever the caller allowed this attempt to take.
        allowed = self.budgets[-1] if self.budgets else READ_TIMEOUT
        self.clock.now += min(READ_TIMEOUT, max(0.0, allowed))
        raise GptProviderTimeoutError("OpenAI Provider 응답 시간이 초과되었습니다.")


class FlakyConnectionProvider:
    """Fails to connect once, then answers."""

    name = "openai"

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.attempts = 0

    def generate_json(self, *, task, prompt, context):
        self.attempts += 1
        if self.attempts == 1:
            self.clock.now += 5.0
            raise ConnectionError("network")
        self.clock.now += 2.0
        return {"category": "설치/AS", "questions": ["A/S는?"]}


def build(factory, **overrides):
    clock = FakeClock()
    inner = factory(clock)
    provider = ResilientJsonProvider(
        inner, settings(**overrides), sleeper=clock.sleep, clock=clock
    )
    return clock, inner, provider


# ------------------------------------------------------- timeout policy

def test_a_response_timeout_is_not_retried() -> None:
    """One attempt, not two: the wait is no longer doubled for nothing."""

    clock, inner, provider = build(ReadTimeoutProvider)
    with pytest.raises(GptProviderTimeoutError):
        provider.generate_json(task="UNDERSTANDING", prompt="p", context={})

    assert inner.attempts == 1
    assert provider.retry_count == 0
    assert clock.now == pytest.approx(READ_TIMEOUT)


def test_a_transient_connect_failure_still_retries() -> None:
    clock, inner, provider = build(FlakyConnectionProvider)
    result = provider.generate_json(
        task="UNDERSTANDING", prompt="p", context={}
    )
    assert result["category"] == "설치/AS"
    assert inner.attempts == 2
    assert provider.retry_count == 1
    assert provider.last_call["outcome"] == "OK"


def test_the_deadline_is_shared_across_generation_stages() -> None:
    """One generation runs several provider calls; the budget covers them
    all, so the operator's wait cannot be the budget times the stage count."""

    clock, inner, provider = build(ReadTimeoutProvider)
    for stage in ("UNDERSTANDING", "DRAFT", "SELF_REVIEW"):
        with pytest.raises(GptProviderTimeoutError):
            provider.generate_json(task=stage, prompt="p", context={})

    # Three stages of 45s would be 135s with a per-call budget.
    assert clock.now <= TOTAL_BUDGET
    assert inner.attempts == 3


def test_the_deadline_bounds_the_in_flight_request() -> None:
    """The budget handed to each attempt shrinks as the deadline nears."""

    clock, inner, provider = build(ReadTimeoutProvider)
    for stage in ("UNDERSTANDING", "DRAFT", "SELF_REVIEW"):
        with pytest.raises(GptProviderTimeoutError):
            provider.generate_json(task=stage, prompt="p", context={})

    assert inner.budgets == [
        pytest.approx(120.0),
        pytest.approx(75.0),
        pytest.approx(30.0),
    ]
    # The last attempt was cut short by the deadline rather than running a
    # full read timeout past it.
    assert clock.now == pytest.approx(TOTAL_BUDGET)


def test_a_stage_starting_past_the_deadline_gets_no_budget() -> None:
    clock, inner, provider = build(ReadTimeoutProvider, total_timeout_seconds=40.0)
    with pytest.raises(GptProviderTimeoutError):
        provider.generate_json(task="UNDERSTANDING", prompt="p", context={})
    with pytest.raises(GptProviderTimeoutError):
        provider.generate_json(task="DRAFT", prompt="p", context={})
    assert inner.budgets[-1] == pytest.approx(0.0)
    assert clock.now == pytest.approx(40.0)


def test_authentication_failure_is_not_retried() -> None:
    clock = FakeClock()

    class AuthFailure:
        name = "openai"

        def generate_json(self, *, task, prompt, context):
            raise GptProviderAuthenticationError("auth")

    provider = ResilientJsonProvider(
        AuthFailure(), settings(), sleeper=clock.sleep, clock=clock
    )
    with pytest.raises(GptProviderAuthenticationError):
        provider.generate_json(task="UNDERSTANDING", prompt="p", context={})
    assert provider.retry_count == 0
    assert clock.now == 0.0


def test_rate_limit_is_still_retried() -> None:
    clock = FakeClock()

    class RateLimited:
        name = "openai"

        def __init__(self):
            self.attempts = 0

        def generate_json(self, *, task, prompt, context):
            self.attempts += 1
            if self.attempts == 1:
                raise GptProviderRetryableError("429", status_code=429)
            return {"ok": True}

    inner = RateLimited()
    provider = ResilientJsonProvider(
        inner, settings(), sleeper=clock.sleep, clock=clock
    )
    assert provider.generate_json(task="DRAFT", prompt="p", context={})["ok"]
    assert inner.attempts == 2


# ------------------------------------------------------------ provenance

def test_timeout_records_the_stage_and_the_applied_timeouts() -> None:
    _, _, provider = build(ReadTimeoutProvider)
    with pytest.raises(GptProviderTimeoutError):
        provider.generate_json(
            task="UNDERSTANDING",
            prompt=SECRET_PROMPT,
            context={"question": SECRET_PROMPT, "rule": {}},
        )

    call = provider.last_call
    assert call["task"] == "UNDERSTANDING"
    assert call["model"] == "gpt-5.6-sol"
    assert call["read_timeout_seconds"] == READ_TIMEOUT
    assert call["connect_timeout_seconds"] == 5.0
    assert call["total_timeout_seconds"] == TOTAL_BUDGET
    assert call["deadline_budget_seconds"] == pytest.approx(TOTAL_BUDGET)
    assert call["max_retries"] == 2
    assert call["attempts"] == 1
    assert call["prompt_chars"] == len(SECRET_PROMPT)
    assert call["context_keys"] == ["question", "rule"]
    assert call["outcome"] == "GptProviderTimeoutError"
    # How long the attempt actually took must survive a terminal failure --
    # it is the number the server needs to size the timeout.
    assert call["elapsed_seconds"] == pytest.approx(READ_TIMEOUT)
    assert call["last_attempt_seconds"] == pytest.approx(READ_TIMEOUT)


def test_provenance_never_carries_prompt_text_or_secrets() -> None:
    _, _, provider = build(ReadTimeoutProvider)
    with pytest.raises(GptProviderTimeoutError):
        provider.generate_json(
            task="DRAFT",
            prompt=SECRET_PROMPT,
            context={"question": SECRET_PROMPT},
        )

    rendered = repr(provider.last_call)
    for forbidden in ("010-1234-5678", "sk-test", "고객", SECRET_PROMPT):
        assert forbidden not in rendered
    # Only sizes and code-defined key names may appear.
    assert set(provider.last_call) == {
        "task", "model", "connect_timeout_seconds", "read_timeout_seconds",
        "total_timeout_seconds", "max_retries", "prompt_chars",
        "context_keys", "attempts", "outcome", "deadline_budget_seconds",
        "elapsed_seconds", "last_attempt_seconds", "prompt_component_chars",
        "prompt_accounted_chars", "prompt_unaccounted_chars",
    }
