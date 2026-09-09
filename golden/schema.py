"""What a Golden case is, and the deliberate separation inside it.

A case carries three things that must never be conflated:

``stratum``
    How the case was selected. Derived from stored production data, so it is a
    fact about the corpus and can be recomputed at any time.

``label`` (:class:`GoldenLabel`)
    What a *correct* pipeline would do. Sourced from a human, from an explicit
    company policy, or from the seller answer actually posted to Naver -- never
    from what this program currently outputs. Anything not yet established is
    ``None``, which reads as "not yet labelled" and is reported as such.

``observed`` (:class:`CaseObservation`)
    What the pipeline actually did on this run. Rewritten every run.

Writing an observation into a label is the one mistake that would make this
whole exercise worthless: the current bug becomes the expected answer, and
every later comparison confirms it. ``GoldenLabel.from_observation`` therefore
does not exist, and ``label_source`` records where each label came from.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any


# Where a label's authority comes from. ``PRODUCTION_ANSWER`` means a human's
# reply was actually posted to the customer, which is evidence about the right
# answer that is independent of this program.
LABEL_SOURCES = (
    "HUMAN_REVIEW",
    "PRODUCTION_ANSWER",
    "COMPANY_POLICY",
    "FORENSIC_ANALYSIS",
    "UNLABELLED",
)

ANSWER_QUALITY_VALUES = (
    "CORRECT",
    "PARTIALLY_CORRECT",
    "INCORRECT",
    "INSUFFICIENT_EVIDENCE",
    "UNKNOWN",
)


@dataclass
class GoldenLabel:
    """The expected behaviour, from a source other than this program.

    Every field is optional. ``None`` means "nobody has established this yet"
    and every metric that would need it reports NOT_YET_LABELED rather than
    guessing. That is the point: a missing label must cost us a measurement,
    never produce a fabricated one.
    """

    label_source: str = "UNLABELLED"
    # Should the pipeline have been able to answer this from evidence?
    expected_answerability: str | None = None      # ANSWERABLE | NEEDS_HUMAN | NEEDS_CURRENT_ORDER_FACT
    expected_review_required: bool | None = None
    expected_order_lookup: bool | None = None
    expected_dps_lookup: bool | None = None
    # Evidence ids that a correct run must put in front of GPT ②.
    expected_evidence_learning_ids: list[int] = field(default_factory=list)
    expected_evidence_historical_ids: list[int] = field(default_factory=list)
    # Evidence that would be wrong to use as fact here (other product, other
    # order, expired promotion...). Using one of these is a MAJOR failure.
    forbidden_evidence_learning_ids: list[int] = field(default_factory=list)
    # Sub-questions the answer must actually address, in the customer's terms.
    required_subquestions: list[str] = field(default_factory=list)
    # Claims the answer must not make without a current-order fact.
    forbidden_claims: list[str] = field(default_factory=list)
    expected_answer_quality: str | None = None
    # The seller answer actually posted to the customer, when there is one.
    production_answer: str | None = None
    notes: str = ""

    def is_labelled(self) -> bool:
        return self.label_source != "UNLABELLED"


@dataclass
class CaseObservation:
    """What the pipeline did. Overwritten on every run; never a label."""

    ran: bool = False
    error: str | None = None
    # --- GPT (1) / routing
    semantic_usable: bool | None = None
    semantic_actions: list[str] = field(default_factory=list)
    atom_count: int | None = None
    atoms: list[str] = field(default_factory=list)
    need_template: bool | None = None
    need_product: bool | None = None
    need_learning: bool | None = None
    need_order: bool | None = None
    need_dps: bool | None = None
    # --- retrieval
    learning_pool: int | None = None
    learning_hard_valid: int | None = None
    learning_selected: int | None = None
    product_identity_status: str | None = None
    verified_product_facts: int | None = None
    template_candidates: int | None = None
    subquestion_evidence: list[dict[str, Any]] = field(default_factory=list)
    # --- what actually reached GPT (2)
    prompt_fact_keys: list[str] = field(default_factory=list)
    prompt_learning_ids: list[int] = field(default_factory=list)
    prompt_historical_ids: list[int] = field(default_factory=list)
    prompt_chars: int | None = None
    # --- GPT (2) verdicts
    used_learning_ids: list[int] = field(default_factory=list)
    used_historical_ids: list[int] = field(default_factory=list)
    used_template_ids: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    requires_review: bool | None = None
    can_auto_post: bool | None = None
    answer: str = ""
    # --- deterministic layers
    validator_status: str | None = None
    eligibility_decision: str | None = None
    eligibility_reasons: list[str] = field(default_factory=list)
    eligibility_soft_reasons: list[str] = field(default_factory=list)
    # --- side-effect proof
    naver_post_calls: int = 0
    dps_calls: int = 0
    order_lookup_calls: int = 0
    gpt_calls: int = 0
    duration_seconds: float | None = None
    # --- replay fidelity against the stored production run, when one exists
    fidelity: str | None = None                    # OK | DRIFT | NO_REFERENCE
    fidelity_notes: list[str] = field(default_factory=list)


@dataclass
class GoldenCase:
    """One production inquiry, its stratum, its label and its last observation."""

    case_id: str
    source_question_id: str
    inquiry_id: int
    question: str
    product_name: str
    tier: str = "CORE"                             # ANCHOR | CORE | FULL
    stratum: dict[str, Any] = field(default_factory=dict)
    label: GoldenLabel = field(default_factory=GoldenLabel)
    # Recorded once at selection time so a later run can tell whether the
    # production run it is being compared against is the same one.
    baseline_reference: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        payload = asdict(self)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "GoldenCase":
        label = GoldenLabel(**(payload.get("label") or {}))
        data = {k: v for k, v in payload.items() if k != "label"}
        return cls(label=label, **data)


@dataclass
class CaseResult:
    """A case plus what one run observed, ready to be scored."""

    case: GoldenCase
    observed: CaseObservation

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case.case_id,
            "source_question_id": self.case.source_question_id,
            "inquiry_id": self.case.inquiry_id,
            "tier": self.case.tier,
            "stratum": dict(self.case.stratum),
            "label": asdict(self.case.label),
            "observed": asdict(self.observed),
        }
