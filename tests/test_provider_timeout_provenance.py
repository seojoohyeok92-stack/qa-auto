"""Provider timeout provenance for inquiry 686058300.

Production reproduction: "GPT 새 답변 생성" produced no draft.

    GPT_BUTTON_CLICKED       2026-08-23T04:36:22.401Z
    GPT_FALLBACK_RULE        2026-08-23T04:37:21.013Z  GPTPROVIDERTIMEOUTERROR
    ANSWER_GENERATION_FAILED 2026-08-23T04:37:21.079Z

58.6 seconds, and no GPT_ANALYSIS_COMPLETED in between -- so the very first
provider call (UNDERSTANDING) never returned. The recorded evidence was the
exception class and nothing else: no stage, no attempt count, no timings, no
prompt size, so there was no way to tell a single slow request from prompt
growth or from retries consuming the clock.

These tests pin the wall-clock arithmetic that produces ~59s and the safe
diagnostics now attached to the failure. Fake clock and fake provider only --
no network, no real provider, no API key.
"""
from __future__ import annotations

import pytest

from answer.governance_models import GptProviderSettings
from answer.provider_errors import (
    GptProviderAuthenticationError,
    GptProviderTimeoutError,
)
from answer.providers.resilient_json_provider import ResilientJsonProvider


READ_TIMEOUT = 30.0
SECRET_PROMPT = "고객 010-1234-5678 님의 문의 내용입니다. sk-test-not-a-real-key"


def settings(**overrides) -> GptProviderSettings:
    values = {
        "model": "gpt-5.6-sol",
        "connect_timeout_seconds": 5.0,
        "read_timeout_seconds": READ_TIMEOUT,
        "total_timeout_seconds": 40.0,
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
    """Behaves like requests hitting its read timeout on every attempt."""

    name = "openai"

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.attempts = 0

    def generate_json(self, *, task, prompt, context):
        self.attempts += 1
        self.clock.now += READ_TIMEOUT
        raise GptProviderTimeoutError("OpenAI Provider 응답 시간이 초과되었습니다.")


class SlowThenOkProvider:
    """Times out once, then answers -- the retry must still be allowed."""

    name = "openai"

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.attempts = 0

    def generate_json(self, *, task, prompt, context):
        self.attempts += 1
        if self.attempts == 1:
            self.clock.now += READ_TIMEOUT
            raise GptProviderTimeoutError("timeout")
        self.clock.now += 2.0
        return {"category": "설치/AS", "questions": ["A/S는?"]}


def build(inner_factory, **overrides):
    clock = FakeClock()
    inner = inner_factory(clock)
    provider = ResilientJsonProvider(
        inner, settings(**overrides), sleeper=clock.sleep, clock=clock
    )
    return clock, inner, provider


# ------------------------------------------------- wall-clock arithmetic

def test_two_read_timeouts_consume_about_sixty_seconds() -> None:
    """Reproduces the observed ~59s: two HTTP attempts of read_timeout each."""

    clock, inner, provider = build(ReadTimeoutProvider)
    with pytest.raises(GptProviderTimeoutError):
        provider.generate_json(task="UNDERSTANDING", prompt="p", context={})

    assert inner.attempts == 2
    assert clock.now == pytest.approx(2 * READ_TIMEOUT + 0.5)
    assert provider.retry_count == 2


def test_total_timeout_does_not_cap_an_in_flight_request() -> None:
    """total_timeout_seconds=40 is only consulted between attempts, so the
    call still runs past it. Recorded so a future change is deliberate."""

    clock, _, provider = build(ReadTimeoutProvider)
    with pytest.raises(GptProviderTimeoutError):
        provider.generate_json(task="UNDERSTANDING", prompt="p", context={})

    assert clock.now > provider.settings.total_timeout_seconds


def test_each_stage_gets_its_own_budget() -> None:
    """UNDERSTANDING, DRAFT and SELF_REVIEW do not share one clock."""

    clock, _, provider = build(ReadTimeoutProvider)
    for stage in ("UNDERSTANDING", "DRAFT"):
        with pytest.raises(GptProviderTimeoutError):
            provider.generate_json(task=stage, prompt="p", context={})
    assert clock.now == pytest.approx(2 * (2 * READ_TIMEOUT + 0.5))


def test_a_retry_that_succeeds_is_still_returned() -> None:
    clock, inner, provider = build(SlowThenOkProvider)
    result = provider.generate_json(
        task="UNDERSTANDING", prompt="p", context={}
    )
    assert result["category"] == "설치/AS"
    assert inner.attempts == 2
    assert provider.last_call["outcome"] == "OK"


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
    assert call["total_timeout_seconds"] == 40.0
    assert call["max_retries"] == 2
    assert call["attempts"] == 2
    assert call["prompt_chars"] == len(SECRET_PROMPT)
    assert call["context_keys"] == ["question", "rule"]
    assert call["elapsed_seconds"] == pytest.approx(2 * READ_TIMEOUT + 0.5)
    assert call["last_attempt_seconds"] == pytest.approx(READ_TIMEOUT)
    assert call["outcome"] == "RETRY_WOULD_EXCEED_TOTAL_TIMEOUT"


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
        "context_keys", "attempts", "outcome", "elapsed_seconds",
        "last_attempt_seconds",
    }


def test_authentication_failure_is_not_retried() -> None:
    """A non-retryable error must not spend the wall clock."""

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
