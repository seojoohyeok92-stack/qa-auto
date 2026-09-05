"""The two holds that need nothing from the evidence side.

This module once compared a property label on the question against a property
label on the stored answer, in code. That design was measured and abandoned.
Labelling 12 approved answers showed why: "사다리차 사용 여부는 설치 기사님께서
판단하시며 유상으로 알고 있습니다" genuinely names an actor -- the technician who
decides whether a ladder truck is needed -- so an ACTOR check passes while the
customer's question, who bears the cost, stays unanswered. The relation is what
differs between question and answer, and a label drops the relation.

Judging that comparison is now ``evidence_verification_service``, which puts the
customer's question and one candidate to the model together.

Two conclusions survived, because neither ever needed the evidence side:

    ACTION_EXECUTION  a stored answer describes how something is done; it is
                      never proof that it was done for this customer.
    UNKNOWN           the understanding could not name what was asked, which is
                      not a licence to answer from stored Learning.

Both apply only where stored Learning is the grounds. An exact Template, a
confirmed RULE and the product catalogue keep the behaviour they had.

This module decides nothing about publication. The safety gate, the validator
and auto-post eligibility still have the last word.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from services.semantic_analysis import REQUESTED_ATTRIBUTES, UNKNOWN_ATTRIBUTE

# A request to *do* something is never settled by a stored answer describing how
# it is normally done. The procedure may be quoted; the action has not happened.
ACTION_EXECUTION = "ACTION_EXECUTION"

COVERED = "COVERED"
ATTRIBUTE_MISMATCH = "ATTRIBUTE_MISMATCH"
ATTRIBUTE_UNKNOWN = "ATTRIBUTE_UNKNOWN"
ACTION_NOT_PERFORMED = "ACTION_NOT_PERFORMED"

# What eligibility appends when this gate holds an answer back. It is a hold and
# only a hold: nothing here can make an otherwise-blocked answer publishable.
REASON_CODE = "REQUESTED_ATTRIBUTE_NOT_COVERED"

# The metadata key generation writes and eligibility reads. Eligibility runs
# long after generation and has no provider of its own, so it consumes what was
# recorded rather than re-deriving it.
METADATA_KEY = "requested_attribute_coverage"


@dataclass(frozen=True)
class AttributeCoverage:
    """One atomic question, and whether any evidence states the asked property."""

    covered: bool
    reason: str
    requested_attribute: str
    covering_evidence: tuple[Any, ...] = ()
    mismatched_evidence: tuple[Any, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "covered": self.covered,
            "reason": self.reason,
            "requested_attribute": self.requested_attribute,
            "covering_evidence": list(self.covering_evidence),
            "mismatched_evidence": list(self.mismatched_evidence),
        }


def _normalise(value: object) -> str:
    text = str(value or "").strip().upper()
    return text if text in REQUESTED_ATTRIBUTES else UNKNOWN_ATTRIBUTE


def evaluate(
    requested_attribute: object,
    evidence: Iterable[Mapping[str, Any]],
) -> AttributeCoverage:
    """Report whether any piece of evidence states the property that was asked.

    ``evidence`` entries carry an identifier and the attribute that piece
    establishes, e.g. ``{"id": 42, "establishes_attribute": "ACTOR"}``.
    Entries the verifier did not mark as supporting are the caller's to filter;
    everything passed in here is treated as offered evidence.
    """

    asked = _normalise(requested_attribute)
    covering: list[Any] = []
    mismatched: list[Any] = []

    for item in evidence or ():
        if not isinstance(item, Mapping):
            continue
        identifier = item.get("id")
        establishes = _normalise(item.get("establishes_attribute"))
        if asked != UNKNOWN_ATTRIBUTE and establishes == asked:
            covering.append(identifier)
        else:
            mismatched.append(identifier)

    if asked == ACTION_EXECUTION:
        # The customer asked us to carry something out. Stored procedure may be
        # quoted back to them, but nothing here says it was done for them.
        return AttributeCoverage(
            False, ACTION_NOT_PERFORMED, asked, (), tuple(mismatched))
    if asked == UNKNOWN_ATTRIBUTE:
        # Not knowing which property was asked is not a licence to answer.
        return AttributeCoverage(
            False, ATTRIBUTE_UNKNOWN, asked, (), tuple(mismatched))
    if covering:
        return AttributeCoverage(
            True, COVERED, asked, tuple(covering), tuple(mismatched))
    return AttributeCoverage(
        False, ATTRIBUTE_MISMATCH, asked, (), tuple(mismatched))


def evaluate_atoms(
    atoms: Iterable[Mapping[str, Any]],
) -> dict[str, AttributeCoverage]:
    """Coverage for each unresolved atomic question, keyed by its identifier.

    ``atoms`` entries look like
    ``{"atom_id": "a1", "requested_attribute": "ACTOR", "evidence": [...]}``.
    An atom already settled by an exact Template or RULE carries
    ``resolved_by_template`` and is reported as covered without evidence --
    the deterministic systems keep their precedence.
    """

    out: dict[str, AttributeCoverage] = {}
    for atom in atoms or ():
        if not isinstance(atom, Mapping):
            continue
        atom_id = str(atom.get("atom_id") or len(out))
        if atom.get("resolved_by_template"):
            out[atom_id] = AttributeCoverage(
                True, "RESOLVED_BY_TEMPLATE",
                _normalise(atom.get("requested_attribute")))
            continue
        out[atom_id] = evaluate(
            atom.get("requested_attribute"), atom.get("evidence") or ())
    return out


def all_covered(coverage: Mapping[str, AttributeCoverage]) -> bool:
    """Every atomic question settled. A compound inquiry is not partly answered."""

    return bool(coverage) and all(item.covered for item in coverage.values())


def uncovered_reasons(
    coverage: Mapping[str, AttributeCoverage],
) -> dict[str, str]:
    """Why each unsettled atom is unsettled, for the review queue to show."""

    return {key: item.reason for key, item in coverage.items() if not item.covered}


# The deterministic routes. An exact Template, a confirmed RULE and the product
# catalogue each answer from a source that was settled before this gate existed,
# and their precedence is not this gate's to revisit. Holding them would trade a
# measured pair of bad Learning answers for every deterministic reply in the
# store -- which is the shape of regression this work exists to avoid.
DETERMINISTIC_ROUTES = frozenset({"TEMPLATE", "SAFE_RULE", "PRODUCT_DB"})


def record(
    atoms: Iterable[Mapping[str, Any]],
    *,
    evidence_attributes_available: bool,
    uses_learning_evidence: bool = True,
) -> dict[str, Any]:
    """The verdict generation writes down for eligibility to read later.

    ``evidence_attributes_available`` says whether anything upstream actually
    established what each piece of evidence supports. When it is false the
    mismatch test has no input and must not pretend otherwise -- only the two
    conclusions that need no evidence at all are drawn: a request to *act*, and
    a question whose asked property the understanding could not name.

    ``uses_learning_evidence`` says whether the answer rests on stored Learning
    at all. It does not for an exact Template, a confirmed RULE or the product
    catalogue, and those keep the behaviour they had: this gate exists for the
    case where a *stored answer about the right subject* is offered as grounds,
    and it has nothing to say about a deterministic source.
    """

    coverage = evaluate_atoms(atoms)
    reasons = uncovered_reasons(coverage)
    if not uses_learning_evidence:
        holding: dict[str, str] = {}
    else:
        # ATTRIBUTE_MISMATCH is deliberately not here. Deciding it from labels
        # was measured and abandoned; ``evidence_verification_service`` decides
        # it now, with the question and the candidate in front of the model.
        holding = {
            key: reason for key, reason in reasons.items()
            if reason in (ACTION_NOT_PERFORMED, ATTRIBUTE_UNKNOWN)
        }
    return {
        "evidence_attributes_available": bool(evidence_attributes_available),
        "uses_learning_evidence": bool(uses_learning_evidence),
        "atoms": {key: item.to_dict() for key, item in coverage.items()},
        "uncovered": reasons,
        "holding": holding,
        "holds_auto_post": bool(holding),
    }


def decision_from_metadata(metadata: Mapping[str, Any] | None) -> tuple[bool, str]:
    """(hold, why). A draft with nothing recorded holds nothing."""

    metadata = metadata or {}
    value = metadata.get(METADATA_KEY)
    if not isinstance(value, Mapping):
        return False, "NOT_RECORDED"
    if not value.get("holds_auto_post"):
        return False, "NO_HOLD"
    holding = value.get("holding")
    if isinstance(holding, Mapping) and holding:
        first = sorted(holding.items())[0]
        return True, "%s:%s" % (first[0], first[1])
    return True, "UNCOVERED"
