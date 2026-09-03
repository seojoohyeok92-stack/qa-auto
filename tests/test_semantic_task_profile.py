"""Tuning the semantic call, and only the semantic call.

Measured against the deployment's own provider, one semantic classification was
costing a mean of 5.5 seconds and 352 output tokens on the configured model.
That model is chosen for drafting a customer answer, which is a different job:
this one returns a small JSON object from a closed vocabulary and has no prose
to write.

So the request is tuned per task. ``DRAFT`` -- and every other task -- resolves
to the configured model with no extra request fields, which is byte-for-byte the
request that was being sent before this existed. Only ``SEMANTIC_ANALYSIS`` can
be pointed somewhere else, only through the deployment's own allow-list, and
only when a variable says so.

The A/B that motivated it, over the same 50 live inquiries:

    control (configured model)    mean 6217ms  p50 5798  p95 9855  out 352 tok
    experiment (faster model,
      reasoning effort low)       mean 3507ms  p50 3530  p95 5913  out 225 tok

with accuracy 27/34 against 26/34 -- no worse, and all seven required meanings
preserved on both.
"""
from __future__ import annotations

import pytest

from answer.providers.task_profiles import (
    EFFORT_ENV,
    MAX_OUTPUT_ENV,
    MODEL_ENV,
    SEMANTIC_TASK,
    task_request_profile,
)


ALLOWED = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")
DEFAULT = "gpt-5.6-sol"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in (MODEL_ENV, EFFORT_ENV, MAX_OUTPUT_ENV):
        monkeypatch.delenv(name, raising=False)


def profile(task=SEMANTIC_TASK):
    return task_request_profile(task, DEFAULT, allowed_models=ALLOWED)


# ==========================================================================
# The answer path is untouched
# ==========================================================================


@pytest.mark.parametrize("task", ["DRAFT", "UNDERSTANDING", "SELF_REVIEW", ""])
def test_every_other_task_sends_exactly_what_it_always_sent(
    task, monkeypatch,
) -> None:
    """Even with every semantic variable set, no other task changes."""

    monkeypatch.setenv(MODEL_ENV, "gpt-5.6-terra")
    monkeypatch.setenv(EFFORT_ENV, "low")
    monkeypatch.setenv(MAX_OUTPUT_ENV, "800")

    assert profile(task) == (DEFAULT, {})


def test_an_unset_deployment_changes_nothing_at_all() -> None:
    """The default is the configured model and no extra fields."""

    assert profile() == (DEFAULT, {})


# ==========================================================================
# What the semantic task may be tuned to
# ==========================================================================


def test_the_semantic_task_can_use_an_allowed_model(monkeypatch) -> None:
    monkeypatch.setenv(MODEL_ENV, "gpt-5.6-terra")

    assert profile() == ("gpt-5.6-terra", {})


def test_a_model_outside_the_allow_list_is_refused(monkeypatch) -> None:
    """A typo must not quietly send traffic somewhere unapproved."""

    monkeypatch.setenv(MODEL_ENV, "gpt-4o-mini")

    model, options = profile()

    assert model == DEFAULT
    assert options == {}


@pytest.mark.parametrize("effort", ["minimal", "low", "medium", "high"])
def test_a_recognised_effort_is_forwarded(effort, monkeypatch) -> None:
    monkeypatch.setenv(EFFORT_ENV, effort)

    assert profile()[1] == {"reasoning": {"effort": effort}}


@pytest.mark.parametrize("effort", ["", "fastest", "LOWEST", "0", "none"])
def test_an_unrecognised_effort_is_ignored(effort, monkeypatch) -> None:
    monkeypatch.setenv(EFFORT_ENV, effort)

    assert profile()[1] == {}


@pytest.mark.parametrize("raw,expected", [
    ("800", {"max_output_tokens": 800}),
    ("0", {}), ("-5", {}), ("", {}), ("many", {}),
])
def test_only_a_positive_output_cap_is_sent(raw, expected, monkeypatch) -> None:
    monkeypatch.setenv(MAX_OUTPUT_ENV, raw)

    assert profile()[1] == expected


def test_the_measured_configuration_resolves_as_intended(monkeypatch) -> None:
    """The arm the A/B chose, spelled out so it cannot drift silently."""

    monkeypatch.setenv(MODEL_ENV, "gpt-5.6-terra")
    monkeypatch.setenv(EFFORT_ENV, "low")

    assert profile() == (
        "gpt-5.6-terra", {"reasoning": {"effort": "low"}},
    )


# ==========================================================================
# The transport puts the profile where it belongs, and nowhere else
# ==========================================================================


def test_the_transport_body_carries_the_options(monkeypatch) -> None:
    from answer.governance_models import GptProviderSettings
    from answer.providers.openai_json_provider import OpenAIJsonProvider

    monkeypatch.setenv(MODEL_ENV, "gpt-5.6-terra")
    monkeypatch.setenv(EFFORT_ENV, "low")
    seen: list[dict] = []

    def transport(**kwargs):
        seen.append(kwargs)
        return {"primary_action": "OTHER", "confidence": 0.9}

    settings = GptProviderSettings(
        provider_name="openai", mode="ACTIVE", model=DEFAULT,
        allowed_models=ALLOWED, enabled=True, approved_by_company=True,
        api_key_present=True,
    )
    provider = OpenAIJsonProvider(settings, transport=transport)

    provider.generate_json(task=SEMANTIC_TASK, prompt="p", context={})
    provider.generate_json(task="DRAFT", prompt="p", context={})

    assert seen[0]["model"] == "gpt-5.6-terra"
    assert seen[0]["request_options"] == {"reasoning": {"effort": "low"}}
    # The answer call is the one that must not move.
    assert seen[1]["model"] == DEFAULT
    assert seen[1]["request_options"] == {}


# ==========================================================================
# The prompt still asks for everything the pipeline consumes
# ==========================================================================


def test_the_prompt_still_requests_every_field_the_gate_reads() -> None:
    from services.gpt_semantic_analyzer_service import (
        PROMPT_BUDGET,
        GptSemanticAnalyzerService,
    )

    class Provider:
        name = "p"

        def generate_json(self, **kwargs):
            return {}

    prompt = GptSemanticAnalyzerService(Provider()).build_prompt("질문")

    for field in (
        "primary_action", "secondary_actions", "request_type", "objects",
        "atomic_questions", "deadline", "constraints", "confidence",
    ):
        assert field in prompt, field
    # Still no prose budget: this is paid on every semantic call.
    #
    # 1400 was this prompt's own length (1386) before purchase_state,
    # asks_delivery_schedule and asks_delivery_outcome existed -- three output
    # fields the purchase-state safety policy now reads, each needing a rule
    # the model can apply. Measured floor with *every* sentence of policy prose
    # deleted, keeping only the header, the field contract and the inquiry, is
    # 1101; the three fields' rules cannot be stated in the 299 characters that
    # would leave. So the ceiling is set from what the contract actually costs,
    # not from what would make this line pass: 2272 measured, 2400 here. Prose
    # creep still fails -- there is 128 characters of slack, not room to think
    # out loud in.
    assert len(prompt) < PROMPT_BUDGET


def test_the_prompt_separates_asking_for_a_date_from_asking_what_it_is() -> None:
    """Measured: without this line, "이번 토요일에 설치해주세요" came back as
    INSTALLATION_SCHEDULE on both arms -- a request read as a question."""

    from services.gpt_semantic_analyzer_service import GptSemanticAnalyzerService

    class Provider:
        name = "p"

        def generate_json(self, **kwargs):
            return {}

    prompt = GptSemanticAnalyzerService(Provider()).build_prompt("질문")

    assert "SCHEDULE_REQUEST" in prompt
    assert "INSTALLATION_SCHEDULE" in prompt
