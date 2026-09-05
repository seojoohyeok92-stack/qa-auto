"""The property gate as it is actually wired: generation records, eligibility reads.

The unit tests beside this one prove the comparison. These prove the wiring --
that a verdict written during generation reaches the gate that holds an answer,
that a draft with nothing recorded is held by nothing, and that the two
conclusions needing no evidence still fire while the evidence side is absent.
"""
from __future__ import annotations

import pytest

from services.requested_attribute_coverage import (
    DETERMINISTIC_ROUTES,
    ACTION_NOT_PERFORMED,
    ATTRIBUTE_MISMATCH,
    ATTRIBUTE_UNKNOWN,
    METADATA_KEY,
    REASON_CODE,
    decision_from_metadata,
    record,
)
from services.semantic_analysis import UNKNOWN_ATTRIBUTE


# --------------------------------------------------------------- 기록 계약
def test_nothing_recorded_holds_nothing():
    """A draft produced before this gate existed must publish exactly as before."""
    hold, why = decision_from_metadata({})
    assert hold is False
    assert why == "NOT_RECORDED"


def test_an_unreadable_record_holds_nothing():
    hold, _why = decision_from_metadata({METADATA_KEY: "not a mapping"})
    assert hold is False


def test_a_covered_atom_records_no_hold():
    payload = record(
        [{"atom_id": "a1", "requested_attribute": "EXISTENCE_OR_CAPABILITY",
          "evidence": [{"id": 1, "establishes_attribute": "EXISTENCE_OR_CAPABILITY"}]}],
        evidence_attributes_available=True)
    assert payload["holds_auto_post"] is False
    assert decision_from_metadata({METADATA_KEY: payload})[0] is False


# --------------------------------------------------------------- 폐기된 설계
def test_a_label_comparison_cannot_hold_cm18_and_no_longer_tries():
    """Why CM01/CM18 are the verifier's to judge, not this module's.

    Labelling the ladder-truck answer produced ACTOR, correctly: it names the
    technician who *decides whether a ladder truck is needed*. The customer
    asked who bears the cost. Same label, different relation, so the comparison
    passes an answer that settles nothing -- measured, not hypothesised.

    This module no longer draws that conclusion. The assertion is kept so the
    label-equality design cannot quietly return.
    """
    payload = record(
        [{"atom_id": "a1", "requested_attribute": "ACTOR",
          "evidence": [{"id": 317203, "establishes_attribute": "ACTOR"}]}],
        evidence_attributes_available=True)
    assert payload["holds_auto_post"] is False
    hold, _why = decision_from_metadata({METADATA_KEY: payload})
    assert hold is False


# --------------------------------------------------------------- 정상 보존
@pytest.mark.parametrize("attribute", [
    "EXISTENCE_OR_CAPABILITY", "DIFFERENCE", "LOCATION_OR_CONTACT",
    "METHOD_OR_PROCEDURE", "INCLUSION", "COMPATIBILITY", "SPEC_VALUE",
])
def test_a_single_matching_evidence_is_never_held_for_being_single(attribute):
    """Eight of thirteen measured correct auto-answers rest on one evidence."""
    payload = record(
        [{"atom_id": "a1", "requested_attribute": attribute,
          "evidence": [{"id": 1, "establishes_attribute": attribute}]}],
        evidence_attributes_available=True)
    assert payload["holds_auto_post"] is False


def test_paraphrase_is_a_matter_of_the_attribute_not_the_wording():
    """"오토피봇 가능" and "오토 피벗 지원 여부" differ in spacing and spelling.

    The comparison is between attributes, so neither the spacing nor the
    language of the two texts reaches it.
    """
    payload = record(
        [{"atom_id": "a1", "requested_attribute": "EXISTENCE_OR_CAPABILITY",
          "evidence": [{"id": 158169,
                        "establishes_attribute": "EXISTENCE_OR_CAPABILITY"}]}],
        evidence_attributes_available=True)
    assert payload["holds_auto_post"] is False


# --------------------------------------------------------------- 증거 속성 부재
def test_without_evidence_attributes_only_the_two_evidence_free_holds_fire():
    """The mismatch test has no input yet and must not pretend otherwise."""
    payload = record(
        [{"atom_id": "a1", "requested_attribute": "ACTOR", "evidence": ()}],
        evidence_attributes_available=False)
    # Uncovered, but not held: with no evidence attribute recorded anywhere,
    # calling this a mismatch would hold every Learning answer in the store.
    assert payload["uncovered"]["a1"] == ATTRIBUTE_MISMATCH
    assert payload["holds_auto_post"] is False


def test_action_execution_holds_even_without_evidence_attributes():
    payload = record(
        [{"atom_id": "a1", "requested_attribute": "ACTION_EXECUTION",
          "evidence": ()}],
        evidence_attributes_available=False)
    assert payload["uncovered"]["a1"] == ACTION_NOT_PERFORMED
    assert payload["holds_auto_post"] is True


def test_unknown_attribute_holds_even_without_evidence_attributes():
    payload = record(
        [{"atom_id": "a1", "requested_attribute": UNKNOWN_ATTRIBUTE,
          "evidence": ()}],
        evidence_attributes_available=False)
    assert payload["uncovered"]["a1"] == ATTRIBUTE_UNKNOWN
    assert payload["holds_auto_post"] is True


# --------------------------------------------------------------- Template 우선순위
def test_a_template_resolved_atom_is_never_held_by_this_gate():
    payload = record(
        [{"atom_id": "a1", "resolved_by_template": True,
          "requested_attribute": UNKNOWN_ATTRIBUTE}],
        evidence_attributes_available=False)
    assert payload["holds_auto_post"] is False


def test_compound_template_plus_learning_still_holds_on_an_unnamed_property():
    """A Template atom beside a Learning atom nobody could name still holds."""
    payload = record(
        [{"atom_id": "a1", "resolved_by_template": True,
          "requested_attribute": "METHOD_OR_PROCEDURE"},
         {"atom_id": "a2", "requested_attribute": UNKNOWN_ATTRIBUTE,
          "evidence": ()}],
        evidence_attributes_available=False)
    assert payload["holds_auto_post"] is True
    assert payload["uncovered"] == {"a2": ATTRIBUTE_UNKNOWN}


# --------------------------------------------------------------- 게이트 성질
def test_the_gate_is_a_hold_and_never_a_licence():
    """Nothing this module returns can make a blocked answer publishable."""
    covered = record(
        [{"atom_id": "a1", "requested_attribute": "ACTOR",
          "evidence": [{"id": 1, "establishes_attribute": "ACTOR"}]}],
        evidence_attributes_available=True)
    hold, why = decision_from_metadata({METADATA_KEY: covered})
    assert (hold, why) == (False, "NO_HOLD")


def test_reason_code_is_stable():
    """Operators and stored review reasons depend on this string."""
    assert REASON_CODE == "REQUESTED_ATTRIBUTE_NOT_COVERED"


# --------------------------------------------------------------- 결정적 경로 보호
@pytest.mark.parametrize("route", sorted(DETERMINISTIC_ROUTES))
def test_a_deterministic_route_is_never_held_by_this_gate(route):
    """Exact Template, confirmed RULE and the product catalogue keep their path.

    The first wiring of this gate held on UNKNOWN regardless of route, and five
    pipeline tests turned SAFE into REVIEW_REQUIRED: every draft whose semantic
    response predates the field parses as UNKNOWN, which is most of them. The
    gate exists for a stored Learning answer offered as grounds and has nothing
    to say about a source that was settled before it.
    """
    payload = record(
        [{"atom_id": "a1", "requested_attribute": UNKNOWN_ATTRIBUTE,
          "evidence": ()}],
        evidence_attributes_available=False,
        uses_learning_evidence=route not in DETERMINISTIC_ROUTES)
    assert payload["holds_auto_post"] is False


def test_an_action_request_answered_deterministically_is_not_held():
    """A Template that explains the procedure keeps answering as it did."""
    payload = record(
        [{"atom_id": "a1", "requested_attribute": "ACTION_EXECUTION",
          "evidence": ()}],
        evidence_attributes_available=False,
        uses_learning_evidence=False)
    assert payload["holds_auto_post"] is False


def test_a_learning_route_still_holds_on_unknown():
    """The narrowing must not switch the gate off where it was needed."""
    payload = record(
        [{"atom_id": "a1", "requested_attribute": UNKNOWN_ATTRIBUTE,
          "evidence": ()}],
        evidence_attributes_available=False,
        uses_learning_evidence=True)
    assert payload["holds_auto_post"] is True
    assert payload["uncovered"]["a1"] == ATTRIBUTE_UNKNOWN
