# -*- coding: utf-8 -*-
"""Legacy answer routes: readable as history, never choosable as authority.

Three route names -- ``TEMPLATE``, ``SAFE_RULE`` and ``PRODUCT_DB`` -- were
once final-answer routes. A keyword template, a safe rule, or a catalogue fact
answered the customer directly, and the route name was how everything
downstream knew that. Nothing produces them now: the same material reaches
GPT (2) as evidence instead.

The names survive in two places and for one reason. Rows already written to the
operational database carry them, and the review workspace re-evaluates those
rows to show staff why each one was or was not posted; dropping the names would
relabel settled history. What must not survive is the other half -- the ability
for an answer written today to *acquire* one of those routes and with it the
permissions the route used to imply.

So these tests assert the separation rather than the values: the route
producers enumerate their whole output and none of it is a legacy name, and the
validator -- which only ever runs at generation time -- no longer knows the
legacy names at all.
"""

from __future__ import annotations

import ast
import io

import pytest

from answer.answer_validator import AnswerValidator
from services.auto_processing_eligibility_service import (
    AUTO_POSTABLE_ROUTES,
    CURRENT_AUTO_POSTABLE_ROUTES,
    HISTORICAL_AUTO_POSTABLE_ROUTES,
    _EVIDENCE_EXEMPT_ROUTES,
)


LEGACY_NAMES = frozenset({"TEMPLATE", "SAFE_RULE", "PRODUCT_DB"})


def _string_literals(path: str, *, inside: str) -> set[str]:
    """Every string constant appearing in one function of one module."""

    tree = ast.parse(io.open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == inside:
            return {
                child.value
                for child in ast.walk(node)
                if isinstance(child, ast.Constant)
                and isinstance(child.value, str)
            }
    raise AssertionError(f"{inside} not found in {path}")


# ------------------------------------------------- the names are still readable


def test_the_legacy_names_are_still_recognised_as_auto_postable() -> None:
    """A stored row on a legacy route keeps the verdict it was given."""

    assert LEGACY_NAMES == HISTORICAL_AUTO_POSTABLE_ROUTES
    assert LEGACY_NAMES <= AUTO_POSTABLE_ROUTES


def test_the_two_halves_are_declared_separately_and_do_not_overlap() -> None:
    """Reading history and making a decision are different lists."""

    assert not (CURRENT_AUTO_POSTABLE_ROUTES & HISTORICAL_AUTO_POSTABLE_ROUTES)
    assert AUTO_POSTABLE_ROUTES == (
        CURRENT_AUTO_POSTABLE_ROUTES | HISTORICAL_AUTO_POSTABLE_ROUTES
    )


def test_template_history_cannot_bypass_the_gpt2_evidence_decision() -> None:
    """Read compatibility must not turn TEMPLATE into an evidence shortcut."""

    # TEMPLATE is readable as a historical route, but unlike a mechanical DPS
    # reply it is generic candidate material.  It must not evade the persisted
    # GPT② evidence verdict merely because an old row carries that route name.
    assert _EVIDENCE_EXEMPT_ROUTES & LEGACY_NAMES == {
        "SAFE_RULE", "PRODUCT_DB",
    }
    assert "TEMPLATE" not in _EVIDENCE_EXEMPT_ROUTES
    assert _EVIDENCE_EXEMPT_ROUTES - HISTORICAL_AUTO_POSTABLE_ROUTES == {
        "ORDER_ID_REQUEST",
        "DELIVERY_WITH_INSTALLATION_DATE",
    }


# --------------------------------------------- but nothing can choose them now


def test_the_plan_service_cannot_emit_a_legacy_route() -> None:
    """Every route the plan builder can produce, read off its own source.

    An enumeration rather than a scenario: the assertion holds for inputs
    nobody has thought to write a case for.
    """

    literals = _string_literals(
        "services/inquiry_processing_plan_service.py", inside="create",
    )
    assert not (literals & LEGACY_NAMES), sorted(literals & LEGACY_NAMES)


def test_the_validator_no_longer_knows_the_legacy_routes() -> None:
    """Validation runs at generation time, so it needs no historical reader.

    ``SAFE_RULE`` is the exception and is deliberately still dispatched: the
    safe review draft is generated today, and it is a current route under a
    legacy-sounding name. See the stale-telemetry section of the report.
    """

    validator = AnswerValidator()
    for route in ("TEMPLATE", "PRODUCT_DB"):
        result = validator.validate_route("어떤 답변 본문입니다.", route=route)
        assert result.passed is False
        assert result.status == "FAILED_SYSTEM_ERROR"
        assert any("지원되지 않는" in message for message in result.errors)


def test_the_product_db_answer_validator_is_gone() -> None:
    """Its only caller was the dispatch branch removed above."""

    assert not hasattr(AnswerValidator, "validate_product_db_text")


@pytest.mark.parametrize("route", sorted(LEGACY_NAMES - {"SAFE_RULE"}))
def test_a_legacy_route_is_never_a_generation_target(route: str) -> None:
    """No production module assigns one of these to a route field."""

    assignments = []
    for path in (
        "services/answer_service.py",
        "services/inquiry_processing_plan_service.py",
        "services/hybrid_answer_service.py",
        "services/automatic_draft_service.py",
        "answer/safe_draft.py",
    ):
        tree = ast.parse(io.open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            # ``{"selected_answer_route": "TEMPLATE"}`` and
            # ``selected_answer_route=route`` are the two shapes that matter.
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value in (
                            "selected_answer_route", "generation_mode",
                        )
                        and isinstance(value, ast.Constant)
                        and value.value == route
                    ):
                        assignments.append((path, node.lineno))
            if isinstance(node, ast.keyword) and node.arg in (
                "selected_answer_route", "generation_mode",
            ):
                if (
                    isinstance(node.value, ast.Constant)
                    and node.value.value == route
                ):
                    assignments.append((path, node.value.lineno))
    assert assignments == [], assignments


# ------------------------------------------- what the operator's screen claims


def test_the_two_findings_that_looked_contradictory_now_read_as_two() -> None:
    """"Source: SUFFICIENT" beside "GPT②: UNRESOLVED" is not a contradiction.

    Evidence was retrieved for every atomic question *and* the model still
    could not answer part of the inquiry from it. Both codes are correct and
    the row showed them bare, so staff read the pair as a bug in the pipeline.
    The codes are unchanged -- the export and the trace tests depend on them --
    and the screen now spells them out.
    """

    from ui.answer_status_presenter import trace_stage_label

    sufficient = trace_stage_label("source", "SUFFICIENT")
    unresolved = trace_stage_label("gpt2", "UNRESOLVED")

    assert sufficient.startswith("SUFFICIENT — ")
    assert "모든 질문" in sufficient and "검색" in sufficient
    assert unresolved.startswith("UNRESOLVED — ")
    assert "답할 수 없다" in unresolved
    # An unknown or future code is shown as itself, never blanked or guessed.
    assert trace_stage_label("source", "WHATEVER") == "WHATEVER"
    assert trace_stage_label("nosuchstage", "SUFFICIENT") == "SUFFICIENT"
    assert trace_stage_label("source", "") == ""


def test_the_learning_caption_no_longer_speaks_for_every_evidence_channel(
) -> None:
    """"답변 근거 사용 N건" counted Learning and Historical rows only.

    An answer resting on a verified Product Fact and nothing else therefore
    read as having used no evidence at all. The count was never wrong about
    what it measured; the label claimed more than it measured.
    """

    source = io.open("ui/learning_performance.py", encoding="utf-8").read()

    assert "답변 근거 사용 " not in source
    assert "답변에 사용된 Learning/Historical " in source
    assert "Product Fact {product_fact_count}건" in source


def test_the_safe_review_draft_cannot_inherit_the_legacy_safe_rule_permissions(
) -> None:
    """The one live hazard in keeping ``SAFE_RULE`` in those two sets.

    The safe review draft still records ``generation_mode="SAFE_RULE"``, which
    is accurate -- its body is a fixed safe sentence, not a composed answer --
    and the name collides with the legacy route that was auto-postable and
    exempt from the evidence decision. It cannot inherit either permission,
    because the draft always carries ``selected_answer_route`` as well, and the
    route is what both sets are matched against. Pinned here because the
    collision is in the names, so only a test keeps them from meeting.
    """

    from answer.safe_draft import review_required_safe_result
    from services.auto_processing_eligibility_service import REVIEW_ROUTES

    from answer.source_adapter import answer_request_from_inquiry

    request = answer_request_from_inquiry({
        "id": 1, "inquiry_type": "배송", "content": "배송 언제 오나요?",
        "product_name": "일반 상품", "raw_json": {},
    })
    metadata = review_required_safe_result(
        request, template_preferred=True, failure_code="PROVIDER_FAILED",
    ).metadata

    assert metadata["generation_mode"] == "SAFE_RULE"
    route = metadata["selected_answer_route"]
    assert route == "REVIEW_REQUIRED_SAFE_DRAFT"
    assert route in REVIEW_ROUTES
    assert route not in AUTO_POSTABLE_ROUTES
    assert route not in _EVIDENCE_EXEMPT_ROUTES
