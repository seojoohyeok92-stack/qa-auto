"""A compound inquiry answered in part goes to a person, not to the customer.

An exact Template that settles one atomic question returns from
``answer_service`` before the Learning pipeline runs for the others. That is a
loss of automation -- the second question could often have been answered -- but
it must never be a loss of safety: the reply that leaves must not read as
though every question was addressed.

The coverage stage already carries that invariant, and these tests pin it
there. They exist because the wiring work beside them deliberately did *not*
refactor the early exit: with the safety property held, rewriting the exit is a
change that buys automation and risks the single-Template path that most of the
store depends on.
"""
from __future__ import annotations

from services.requested_attribute_coverage import (
    all_covered,
    evaluate_atoms,
    record,
)
from services.semantic_analysis import UNKNOWN_ATTRIBUTE
from services.semantic_coverage_service import FAIL, PARTIAL


def test_partial_and_fail_are_the_statuses_that_must_convert_to_review():
    """The two coverage verdicts answer_service turns into manual review."""
    assert PARTIAL == "PARTIAL"
    assert FAIL == "FAIL"


def test_one_settled_atom_does_not_settle_a_two_atom_inquiry():
    coverage = evaluate_atoms([
        {"atom_id": "a1", "resolved_by_template": True,
         "requested_attribute": "METHOD_OR_PROCEDURE"},
        {"atom_id": "a2", "requested_attribute": "INCLUSION", "evidence": ()}])
    assert coverage["a1"].covered is True
    assert all_covered(coverage) is False


def test_a_template_atom_beside_an_unverified_learning_atom_holds():
    """"설치는 어떻게 하나요, 브라켓도 같이 오나요" with nothing verified for the second.

    Whether a candidate settles the second half is the verifier's judgement now.
    What this file pins is the compound property: the Template half never
    settles the inquiry on its own.
    """
    payload = record(
        [{"atom_id": "a1", "resolved_by_template": True,
          "requested_attribute": "METHOD_OR_PROCEDURE"},
         {"atom_id": "a2", "requested_attribute": UNKNOWN_ATTRIBUTE,
          "evidence": ()}],
        evidence_attributes_available=False)
    assert payload["holds_auto_post"] is True


def test_a_single_template_inquiry_is_untouched_by_this_gate():
    """One atom, settled by Template. No Learning, no hold, no change."""
    payload = record(
        [{"atom_id": "a1", "resolved_by_template": True,
          "requested_attribute": "METHOD_OR_PROCEDURE"}],
        evidence_attributes_available=False)
    assert payload["holds_auto_post"] is False
    assert payload["uncovered"] == {}
