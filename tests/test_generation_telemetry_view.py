"""Rendering of the stored prompt-size breakdown.

Server draft 296 for inquiry 686097134 recorded a 655,129-character DRAFT
prompt, and the breakdown was already in the database -- but the viewer
printed only the first fifteen rows, which stopped inside
input.historical_retrieval and hid both its internals and every component
after it. The data was never the problem; the rendering was.

These tests use the shape the server reported. No database, no provider.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_viewer():
    path = Path(__file__).resolve().parents[1] / "scripts" / (
        "show_generation_telemetry.py"
    )
    spec = importlib.util.spec_from_file_location("telemetry_viewer", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


VIEWER = _load_viewer()
TOTAL = 655_129

# The server's own numbers, plus the deeper rows the recorder stores.
SERVER_COMPONENTS = {
    "input": 648_618,
    "input.historical_retrieval": 312_500,
    "input.historical_retrieval.subquestions": 311_900,
    "input.historical_retrieval.subquestions.count": 6,
    "input.historical_retrieval.subquestions.max_record": 60_000,
    "input.historical_retrieval.query": 120,
    "input.historical_retrieval.safe_candidate_count": 4,
    "input.learning_retrieval": 300_000,
    "input.learning_retrieval.subquestions": 299_000,
    "input.learning_retrieval.subquestions.count": 6,
    "input.historical_cases": 7_045,
    "input.historical_cases.count": 3,
    "input.historical_cases.max_record": 2_400,
    "input.subquestion_evidence": 8_400,
    "input.feedback_signals": 3_100,
    "input.similar_approved_answers": 5_600,
    "input.similar_approved_answers.count": 6,
    "input.seller_style_examples": 2_200,
    "input.seller_style_examples.count": 2,
    "input.product_fact_guard": 622,
    "input.historical_case_policy": 364,
    "input.subquestion_answer_policy": 246,
    "input.oje_style_rules": 189,
    "input.intent": 248,
    "input.context_priority": 19,
    "output_schema": 1_159,
    "inquiry_analysis": 1_071,
    "output_contract": 1_038,
    "learning_usage_policy": 696,
    "feedback_signal_policy": 471,
    "prohibited_claims": 344,
    "system_policy": 292,
    "installation_date_instructions": 267,
    "tone_and_length": 188,
    "allowed_facts": 176,
    "customer_inquiry": 122,
    "facts": 190,
    "confirmed_facts": 136,
}


def test_every_component_is_rendered_not_just_the_first_fifteen() -> None:
    lines = VIEWER.render_components(SERVER_COMPONENTS, TOTAL)
    rendered = "\n".join(lines)

    value_names = [
        name
        for name in SERVER_COMPONENTS
        if not name.endswith((".count", ".max_record"))
    ]
    assert len(value_names) > 15, "fixture must exceed the old cap"
    for name in value_names:
        leaf = name.rsplit(".", 1)[-1]
        assert leaf in rendered, f"{name} missing from the breakdown"


def test_largest_branch_is_first_and_its_children_follow_it() -> None:
    lines = VIEWER.render_components(SERVER_COMPONENTS, TOTAL)
    body = [line for line in lines if line != "TOP PROMPT COMPONENTS"]

    # `input` dominates, so it heads the list.
    assert body[0].strip().startswith("input")
    # Its largest child is rendered beneath it, indented, before smaller peers.
    joined = "\n".join(body)
    historical = joined.index("historical_retrieval")
    subquestions = joined.index("subquestions")
    assert historical < subquestions, "children must follow their parent"


def test_counts_and_largest_record_are_attached_to_their_component() -> None:
    lines = VIEWER.render_components(SERVER_COMPONENTS, TOTAL)
    row = next(line for line in lines if "historical_cases" in line)
    assert "records=3" in row
    assert "max_record=2,400" in row
    # A count row is never rendered as a component of its own.
    assert not any(line.strip().startswith("count") for line in lines)


def test_share_of_the_total_is_shown() -> None:
    lines = VIEWER.render_components(SERVER_COMPONENTS, TOTAL)
    row = next(line for line in lines if "historical_retrieval" in line)
    assert "47.7%" in row


def test_render_is_safe_without_a_total() -> None:
    lines = VIEWER.render_components({"input": 10}, None)
    assert any("input" in line for line in lines)


def test_empty_breakdown_renders_nothing() -> None:
    assert VIEWER.render_components({}, TOTAL) == []


@pytest.mark.parametrize("top", [1, 3])
def test_top_limits_only_the_outermost_level(top: int) -> None:
    lines = VIEWER.render_components(SERVER_COMPONENTS, TOTAL, top=top)
    outermost = [
        line for line in lines[1:] if not line.startswith("    ")
    ]
    assert len(outermost) == top
    # Children of the kept branch are still fully expanded.
    assert any("subquestions" in line for line in lines)


def test_renderer_emits_no_content_only_paths_and_sizes() -> None:
    lines = VIEWER.render_components(SERVER_COMPONENTS, TOTAL)
    rendered = "\n".join(lines)
    for forbidden in ("고객", "010-", "http", "sk-"):
        assert forbidden not in rendered
