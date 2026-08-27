"""Per-task request tuning, so one slow default does not have to fit every job.

Drafting a customer answer and classifying a question are not the same work.
The answer needs the model to think; the semantic classifier returns a small
JSON object from a closed vocabulary, and measured against the live provider it
was spending a mean of 5.5 seconds and 353 output tokens to do it.

Only tasks named here are tuned. Every other task -- ``DRAFT`` above all --
resolves to the configured model with no extra request fields at all, which is
byte-for-byte the request that was being sent before this module existed.

Nothing is guessed: an unset variable means "leave it alone", and a model name
outside the deployment's own allow-list is refused rather than sent.
"""
from __future__ import annotations

import os
from typing import Any, Mapping


SEMANTIC_TASK = "SEMANTIC_ANALYSIS"

MODEL_ENV = "QNA_GPT_SEMANTIC_MODEL"
EFFORT_ENV = "QNA_GPT_SEMANTIC_REASONING_EFFORT"
MAX_OUTPUT_ENV = "QNA_GPT_SEMANTIC_MAX_OUTPUT_TOKENS"

VALID_EFFORTS = frozenset({"minimal", "low", "medium", "high"})


def _text(name: str) -> str:
    return str(os.getenv(name) or "").strip()


def task_request_profile(
    task: object,
    default_model: str,
    *,
    allowed_models: tuple[str, ...] = (),
) -> tuple[str, dict[str, Any]]:
    """The model and extra request fields to use for one task."""

    if str(task or "").upper() != SEMANTIC_TASK:
        return default_model, {}

    model = _text(MODEL_ENV) or default_model
    if (
        model != default_model
        and allowed_models
        and model not in allowed_models
    ):
        # A typo in an environment variable must not quietly send traffic to a
        # model this deployment never approved. Fall back to the configured
        # one, which is always allowed by definition.
        model = default_model
    options: dict[str, Any] = {}

    effort = _text(EFFORT_ENV).lower()
    if effort in VALID_EFFORTS:
        options["reasoning"] = {"effort": effort}

    raw_limit = _text(MAX_OUTPUT_ENV)
    if raw_limit:
        try:
            limit = int(raw_limit)
        except ValueError:
            limit = 0
        if limit > 0:
            options["max_output_tokens"] = limit

    return model, options


def semantic_profile_summary() -> Mapping[str, Any]:
    """What the semantic task is currently configured to send. For reports."""

    model, options = task_request_profile(SEMANTIC_TASK, "")
    return {
        "model_override": model or None,
        "request_options": dict(options),
    }
