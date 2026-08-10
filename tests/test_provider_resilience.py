from __future__ import annotations

import pytest

from answer.governance_models import GptProviderSettings
from answer.provider_errors import (
    GptProviderAuthenticationError,
    GptProviderRetryableError,
    GptProviderTimeoutError,
)
from answer.providers.resilient_json_provider import ResilientJsonProvider


class ScriptedProvider:
    name = "scripted"

    def __init__(self, outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def generate_json(self, **kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def settings(**overrides) -> GptProviderSettings:
    values = {
        "max_retries": 2,
        "retry_backoff_seconds": 0.1,
        "total_timeout_seconds": 40,
    }
    values.update(overrides)
    return GptProviderSettings(**values)


def test_success_does_not_retry() -> None:
    provider = ScriptedProvider([{"answer": "ok"}])
    wrapped = ResilientJsonProvider(provider, settings(), sleeper=lambda _: None)
    assert wrapped.generate_json(task="DRAFT", prompt="{}", context={}) == {
        "answer": "ok"
    }
    assert wrapped.retry_count == 0
    assert provider.calls == 1


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("timeout"),
        ConnectionError("network"),
        GptProviderTimeoutError("read timeout"),
        GptProviderRetryableError("429", status_code=429),
        GptProviderRetryableError("500", status_code=500),
        GptProviderRetryableError("503", status_code=503),
    ],
)
def test_retryable_errors_retry_and_succeed(error: Exception) -> None:
    provider = ScriptedProvider([error, {"answer": "ok"}])
    wrapped = ResilientJsonProvider(provider, settings(), sleeper=lambda _: None)
    assert wrapped.generate_json(
        task="DRAFT", prompt="{}", context={}
    )["answer"] == "ok"
    assert wrapped.retry_count == 1


@pytest.mark.parametrize(
    "error",
    [
        GptProviderAuthenticationError("401"),
        GptProviderRetryableError("400", status_code=400),
        ValueError("invalid JSON"),
        PermissionError("403"),
    ],
)
def test_non_retryable_errors_are_not_retried(error: Exception) -> None:
    provider = ScriptedProvider([error])
    wrapped = ResilientJsonProvider(provider, settings(), sleeper=lambda _: None)
    with pytest.raises(type(error)):
        wrapped.generate_json(task="DRAFT", prompt="{}", context={})
    assert wrapped.retry_count == 0
    assert provider.calls == 1


def test_retry_stops_at_configured_maximum() -> None:
    provider = ScriptedProvider(
        [
            ConnectionError("one"),
            ConnectionError("two"),
            ConnectionError("three"),
        ]
    )
    wrapped = ResilientJsonProvider(
        provider, settings(max_retries=2), sleeper=lambda _: None
    )
    with pytest.raises(ConnectionError):
        wrapped.generate_json(task="DRAFT", prompt="{}", context={})
    assert wrapped.retry_count == 2
    assert provider.calls == 3


def test_exponential_backoff_is_injectable() -> None:
    delays: list[float] = []
    provider = ScriptedProvider(
        [ConnectionError("one"), ConnectionError("two"), {"ok": True}]
    )
    wrapped = ResilientJsonProvider(
        provider,
        settings(retry_backoff_seconds=0.5),
        sleeper=delays.append,
    )
    wrapped.generate_json(task="DRAFT", prompt="{}", context={})
    assert delays == [0.5, 1.0]


def test_timeout_error_is_normalized_after_retry_exhaustion() -> None:
    provider = ScriptedProvider([TimeoutError("one")])
    wrapped = ResilientJsonProvider(
        provider, settings(max_retries=0), sleeper=lambda _: None
    )
    with pytest.raises(GptProviderTimeoutError):
        wrapped.generate_json(task="DRAFT", prompt="{}", context={})


def test_retry_is_blocked_when_total_timeout_would_be_exceeded() -> None:
    times = iter([0.0, 39.9])
    provider = ScriptedProvider([ConnectionError("network")])
    wrapped = ResilientJsonProvider(
        provider,
        settings(
            total_timeout_seconds=40,
            retry_backoff_seconds=1,
        ),
        sleeper=lambda _: None,
        clock=lambda: next(times),
    )
    with pytest.raises(GptProviderTimeoutError, match="retry"):
        wrapped.generate_json(task="DRAFT", prompt="{}", context={})


def test_completed_response_over_total_timeout_is_rejected() -> None:
    times = iter([0.0, 41.0, 41.0])
    provider = ScriptedProvider([{"answer": "late"}])
    wrapped = ResilientJsonProvider(
        provider,
        settings(total_timeout_seconds=40, max_retries=0),
        sleeper=lambda _: None,
        clock=lambda: next(times),
    )
    with pytest.raises(GptProviderTimeoutError):
        wrapped.generate_json(task="DRAFT", prompt="{}", context={})


def test_provider_usage_is_accumulated_when_available() -> None:
    provider = ScriptedProvider(
        [
            {
                "answer": "ok",
                "_usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 120,
                },
            }
        ]
    )
    wrapped = ResilientJsonProvider(provider, settings(), sleeper=lambda _: None)
    wrapped.generate_json(task="DRAFT", prompt="{}", context={})
    assert wrapped.usage_available is True
    assert (wrapped.input_tokens, wrapped.output_tokens, wrapped.total_tokens) == (
        100,
        20,
        120,
    )


def test_provider_usage_remains_unknown_when_absent() -> None:
    provider = ScriptedProvider([{"answer": "ok"}])
    wrapped = ResilientJsonProvider(provider, settings(), sleeper=lambda _: None)
    wrapped.generate_json(task="DRAFT", prompt="{}", context={})
    assert wrapped.usage_available is False
