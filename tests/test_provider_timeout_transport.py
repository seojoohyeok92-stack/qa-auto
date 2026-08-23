"""Transport-level timeout behaviour for the OpenAI Responses adapter.

Two properties matter for the production timeout, and neither is visible from
the retry wrapper alone:

  * a connect timeout and a response timeout are different faults, because
    only the first is worth retrying;
  * the generation deadline must shorten the socket read of the request that
    is actually in flight, not merely be compared once it has overrun.

A stub transport stands in for the network -- no sockets, no API key.
"""
from __future__ import annotations

import pytest
import requests

from answer.governance_models import GptMode, GptProviderSettings
from answer.provider_errors import (
    GptProviderRetryableError,
    GptProviderTimeoutError,
)
from answer.providers.openai_json_provider import (
    OpenAIJsonProvider,
    OpenAIResponsesTransport,
)
from answer.providers.resilient_json_provider import ResilientJsonProvider


def approved_settings(**overrides) -> GptProviderSettings:
    values = {
        "provider_name": "openai",
        "mode": GptMode.ACTIVE,
        "model": "gpt-5.6-sol",
        "approved_by_company": True,
        "api_key_present": True,
        "allowed_models": ("gpt-5.6-sol",),
        "connect_timeout_seconds": 5.0,
        "read_timeout_seconds": 45.0,
        "total_timeout_seconds": 120.0,
    }
    values.update(overrides)
    return GptProviderSettings(**values)


class RecordingTransport:
    """Captures the timeouts it was handed and returns a canned payload."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"answer": "ok"}


# ------------------------------------------- connect vs response timeout

class StubSession:
    """Stands in for requests.Session; raises whatever it is given."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.posts = 0

    def post(self, *args, **kwargs):
        self.posts += 1
        raise self.error


def real_transport(error: Exception, monkeypatch) -> OpenAIResponsesTransport:
    monkeypatch.setenv("QNA_GPT_API_KEY", "sk-test-not-a-real-key")
    return OpenAIResponsesTransport(
        approved_settings(), session=StubSession(error)
    )


def call(transport: OpenAIResponsesTransport):
    return transport(
        task="DRAFT", prompt="p", context={}, model="gpt-5.6-sol",
        connect_timeout=5.0, read_timeout=45.0, total_timeout=120.0,
    )


def test_connect_timeout_is_reported_as_retryable(monkeypatch) -> None:
    """Never reaching the server is transient, so it must stay retryable."""

    transport = real_transport(requests.ConnectTimeout("connect"), monkeypatch)
    with pytest.raises(GptProviderRetryableError):
        call(transport)


def test_read_timeout_is_reported_as_a_response_timeout(monkeypatch) -> None:
    transport = real_transport(requests.ReadTimeout("read"), monkeypatch)
    with pytest.raises(GptProviderTimeoutError):
        call(transport)


def test_the_two_timeouts_get_opposite_retry_treatment(monkeypatch) -> None:
    """End to end through the retry wrapper: connect retries, read does not."""

    monkeypatch.setenv("QNA_GPT_API_KEY", "sk-test-not-a-real-key")
    for error, expected_posts in (
        (requests.ConnectTimeout("connect"), 3),
        (requests.ReadTimeout("read"), 1),
    ):
        session = StubSession(error)
        adapter = OpenAIJsonProvider(
            approved_settings(),
            transport=OpenAIResponsesTransport(
                approved_settings(), session=session
            ),
        )
        provider = ResilientJsonProvider(
            adapter, approved_settings(max_retries=2), sleeper=lambda _: None
        )
        with pytest.raises(Exception):
            provider.generate_json(task="DRAFT", prompt="p", context={})
        assert session.posts == expected_posts, error


# ------------------------------------------------- deadline bounds a call

def test_read_timeout_is_capped_by_the_remaining_budget() -> None:
    transport = RecordingTransport()
    provider = OpenAIJsonProvider(approved_settings(), transport=transport)

    provider.set_attempt_budget(None)
    provider.generate_json(task="DRAFT", prompt="p", context={})
    assert transport.calls[-1]["read_timeout"] == 45.0

    # 20s left, 5s of it reserved for connecting.
    provider.set_attempt_budget(20.0)
    provider.generate_json(task="DRAFT", prompt="p", context={})
    assert transport.calls[-1]["read_timeout"] == 15.0

    # A generous budget never exceeds the configured read timeout.
    provider.set_attempt_budget(600.0)
    provider.generate_json(task="DRAFT", prompt="p", context={})
    assert transport.calls[-1]["read_timeout"] == 45.0

    # An exhausted budget still sends a request rather than a zero-second one.
    provider.set_attempt_budget(0.0)
    provider.generate_json(task="DRAFT", prompt="p", context={})
    assert transport.calls[-1]["read_timeout"] == 1.0


def test_wrapper_hands_the_remaining_budget_to_the_adapter() -> None:
    transport = RecordingTransport()
    adapter = OpenAIJsonProvider(approved_settings(), transport=transport)
    clock = iter([0.0, 100.0, 100.0, 100.0])
    provider = ResilientJsonProvider(
        adapter,
        approved_settings(total_timeout_seconds=120.0),
        sleeper=lambda _: None,
        clock=lambda: next(clock),
    )
    provider.generate_json(task="UNDERSTANDING", prompt="p", context={})
    # First call: the whole budget, so the configured read timeout applies.
    assert transport.calls[0]["read_timeout"] == 45.0

    provider.generate_json(task="DRAFT", prompt="p", context={})
    # Second call starts at t=100 with 20s left, minus the connect reserve.
    assert transport.calls[1]["read_timeout"] == 15.0


def test_transport_still_receives_the_configured_connect_timeout() -> None:
    transport = RecordingTransport()
    provider = OpenAIJsonProvider(approved_settings(), transport=transport)
    provider.set_attempt_budget(20.0)
    provider.generate_json(task="DRAFT", prompt="p", context={})
    assert transport.calls[-1]["connect_timeout"] == 5.0
    assert transport.calls[-1]["model"] == "gpt-5.6-sol"


def test_transport_endpoint_is_unchanged() -> None:
    assert (
        OpenAIResponsesTransport.endpoint
        == "https://api.openai.com/v1/responses"
    )
