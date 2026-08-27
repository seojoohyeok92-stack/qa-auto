"""What the customer is asking for, as a structure rather than a keyword hit.

The classifier this sits beside matches anchors: a table of regexes, one per
topic, and whichever fires decides the route. "고장난 기존 tv 수거 요청드려요"
shows what that costs. ``WARRANTY_AS`` anchors on 고장, there is no topic for
collection at all, so the inquiry became an A/S question and was auto-posted
with the Samsung service-centre number. The customer had asked us to take their
old television away. Semantic coverage agreed it was answered, because the
answer anchors on 고장 too -- question and answer were consistently wrong.

The missing distinction is not another anchor. 고장 describes the *object*; 수거
is the *action*. A model with only topics has nowhere to put that difference, so
every new phrasing needs another rule, and the rules compete. This module gives
the pipeline the two slots it never had:

    object      TV, and its state: BROKEN
    action      COLLECTION

Actions are a closed set. An open one would let the model invent a label that
no downstream policy knows how to route, which is the same failure in a new
costume -- an unrecognised action is ``OTHER``, and ``OTHER`` is never a licence
to act.

Nothing here decides whether an answer may be published. This is an
understanding of the question; the validator, coverage, eligibility and auto
post gates are unchanged and still have the last word.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from answer.text_utils import compact


# Off unless a deployment says otherwise. The analyzer needs a real provider to
# produce anything at all, and a stage that reaches a model is not something to
# switch on for every environment by default -- tests, local runs and the fake
# provider all stay exactly as they were.
ENABLED_ENV = "OJE_SEMANTIC_ANALYZER_ENABLED"


def is_enabled() -> bool:
    raw = str(os.environ.get(ENABLED_ENV, "0")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


# --- actions ---------------------------------------------------------------
#
# Deliberately closed, and deliberately about what the customer wants done --
# not about which words appear. Each maps onto a decision the pipeline already
# knows how to make.
COLLECTION = "COLLECTION"
REPAIR = "REPAIR"
DELIVERY_STATUS = "DELIVERY_STATUS"
DELIVERY_DEADLINE_CONFIRMATION = "DELIVERY_DEADLINE_CONFIRMATION"
DELIVERY_POLICY = "DELIVERY_POLICY"
SCHEDULE_REQUEST = "SCHEDULE_REQUEST"
SCHEDULE_CHANGE = "SCHEDULE_CHANGE"
INSTALLATION_METHOD = "INSTALLATION_METHOD"
INSTALLATION_SCHEDULE = "INSTALLATION_SCHEDULE"
PRODUCT_SPEC = "PRODUCT_SPEC"
PRODUCT_CONCEPT = "PRODUCT_CONCEPT"
PACKAGE_CONTENTS = "PACKAGE_CONTENTS"
BENEFIT = "BENEFIT"
CANCEL_RETURN = "CANCEL_RETURN"
DAMAGE_REPORT = "DAMAGE_REPORT"
NOTIFICATION_POLICY = "NOTIFICATION_POLICY"
ORDER_IDENTIFICATION = "ORDER_IDENTIFICATION"
STORE_PICKUP = "STORE_PICKUP"
OTHER = "OTHER"

ACTIONS: frozenset[str] = frozenset({
    COLLECTION, REPAIR, DELIVERY_STATUS, DELIVERY_DEADLINE_CONFIRMATION,
    DELIVERY_POLICY, SCHEDULE_REQUEST, SCHEDULE_CHANGE, INSTALLATION_METHOD,
    INSTALLATION_SCHEDULE, PRODUCT_SPEC, PRODUCT_CONCEPT, PACKAGE_CONTENTS,
    BENEFIT, CANCEL_RETURN, DAMAGE_REPORT, NOTIFICATION_POLICY,
    ORDER_IDENTIFICATION, STORE_PICKUP, OTHER,
})

# An object's condition, which is never an action. This is the slot 고장 was
# missing: with it, "고장난 TV 수거" is a BROKEN object with a COLLECTION
# action, and the anchor table's tie between 고장 and 수거 stops existing.
OBJECT_STATES: frozenset[str] = frozenset({
    "BROKEN", "OLD", "EXISTING", "NEW", "DAMAGED", "UNOPENED", "INSTALLED",
})

REQUEST_TYPES: frozenset[str] = frozenset({
    "QUESTION", "ACTION_REQUEST", "COMPLAINT", "MIXED",
})


class SemanticAnalysisError(ValueError):
    """The model returned something this pipeline cannot safely act on."""


@dataclass(frozen=True)
class SemanticObject:
    type: str
    states: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "states": list(self.states)}


@dataclass(frozen=True)
class AtomicQuestion:
    text: str
    action: str

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "action": self.action}


@dataclass(frozen=True)
class SemanticAnalysis:
    """One inquiry, understood. Never a decision -- only an understanding."""

    primary_action: str = OTHER
    secondary_actions: tuple[str, ...] = ()
    request_type: str = "QUESTION"
    objects: tuple[SemanticObject, ...] = ()
    atomic_questions: tuple[AtomicQuestion, ...] = ()
    deadline: str | None = None
    constraints: tuple[str, ...] = ()
    negation: bool = False
    conditional: bool = False
    requires_order_context: bool = False
    requires_delivery_schedule: bool = False
    confidence: float = 0.0
    # Provenance of this understanding, so nothing downstream can mistake a
    # fallback for a model result.
    source: str = "UNAVAILABLE"
    reason: str = ""

    @property
    def usable(self) -> bool:
        """Whether anything may read this as an understanding of the question.

        A fallback carries no understanding at all. It exists so callers have a
        value to hold, never so they can act on it.
        """

        return self.source == "GPT" and self.primary_action in ACTIONS

    @property
    def actions(self) -> frozenset[str]:
        return frozenset({self.primary_action, *self.secondary_actions})

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary_action": self.primary_action,
            "secondary_actions": list(self.secondary_actions),
            "request_type": self.request_type,
            "objects": [item.to_dict() for item in self.objects],
            "atomic_questions": [
                item.to_dict() for item in self.atomic_questions
            ],
            "deadline": self.deadline,
            "constraints": list(self.constraints),
            "negation": self.negation,
            "conditional": self.conditional,
            "requires_order_context": self.requires_order_context,
            "requires_delivery_schedule": self.requires_delivery_schedule,
            "confidence": self.confidence,
            "source": self.source,
            "reason": self.reason,
        }


def unavailable(reason: str) -> SemanticAnalysis:
    """The value callers hold when there is no understanding to be had."""

    return SemanticAnalysis(source="UNAVAILABLE", reason=str(reason)[:120])


def _text(value: object, limit: int = 200) -> str:
    return " ".join(str(value or "").split())[:limit]


def _states(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        dict.fromkeys(
            str(item).strip().upper()
            for item in value
            if str(item).strip().upper() in OBJECT_STATES
        )
    )


def parse(raw: object) -> SemanticAnalysis:
    """Turn the model's JSON into a value, or refuse it.

    Strict on purpose. An action outside the closed set, a confidence that is
    not a number, a malformed atomic question -- each means the caller cannot
    tell what the customer wanted, and guessing is what this module exists to
    stop. Every rejection raises, and every caller falls back to the existing
    deterministic path.
    """

    if not isinstance(raw, Mapping):
        raise SemanticAnalysisError("semantic output must be a JSON object")

    primary = str(raw.get("primary_action") or "").strip().upper()
    if primary not in ACTIONS:
        raise SemanticAnalysisError(f"unknown primary_action: {primary!r}")

    secondary_value = raw.get("secondary_actions")
    if secondary_value in (None, ""):
        secondary_value = []
    if not isinstance(secondary_value, list):
        raise SemanticAnalysisError("secondary_actions must be a list")
    secondary = tuple(
        dict.fromkeys(
            str(item).strip().upper()
            for item in secondary_value
            if str(item).strip().upper() in ACTIONS
            and str(item).strip().upper() != primary
        )
    )

    request_type = str(raw.get("request_type") or "QUESTION").strip().upper()
    if request_type not in REQUEST_TYPES:
        raise SemanticAnalysisError(f"unknown request_type: {request_type!r}")

    objects_value = raw.get("objects") or []
    if not isinstance(objects_value, list):
        raise SemanticAnalysisError("objects must be a list")
    objects = tuple(
        SemanticObject(
            type=str(item.get("type") or "OTHER").strip().upper()[:24],
            states=_states(item.get("states")),
        )
        for item in objects_value
        if isinstance(item, Mapping)
    )

    atomic_value = raw.get("atomic_questions") or []
    if not isinstance(atomic_value, list):
        raise SemanticAnalysisError("atomic_questions must be a list")
    atomic: list[AtomicQuestion] = []
    for item in atomic_value:
        if not isinstance(item, Mapping):
            raise SemanticAnalysisError("atomic_questions entries must be objects")
        action = str(item.get("action") or "").strip().upper()
        if action not in ACTIONS:
            raise SemanticAnalysisError(f"unknown atomic action: {action!r}")
        text = _text(item.get("text"), 160)
        if not text:
            continue
        atomic.append(AtomicQuestion(text=text, action=action))

    try:
        confidence = float(raw.get("confidence"))
    except (TypeError, ValueError) as error:
        raise SemanticAnalysisError("confidence must be a number") from error
    if not 0.0 <= confidence <= 1.0:
        raise SemanticAnalysisError("confidence must be within 0..1")

    constraints_value = raw.get("constraints") or []
    if not isinstance(constraints_value, list):
        raise SemanticAnalysisError("constraints must be a list")

    deadline = _text(raw.get("deadline"), 40) or None

    return SemanticAnalysis(
        primary_action=primary,
        secondary_actions=secondary,
        request_type=request_type,
        objects=objects,
        atomic_questions=tuple(atomic),
        deadline=deadline,
        constraints=tuple(
            _text(item, 60) for item in constraints_value if _text(item, 60)
        )[:5],
        negation=bool(raw.get("negation")),
        conditional=bool(raw.get("conditional")),
        requires_order_context=bool(raw.get("requires_order_context")),
        requires_delivery_schedule=bool(raw.get("requires_delivery_schedule")),
        confidence=confidence,
        source="GPT",
        reason=_text(raw.get("reason"), 120),
    )


# --- when the model is worth asking ----------------------------------------
#
# 44% of a 36-inquiry replay reaches an answer without any GPT call at all, and
# those are the inquiries the deterministic classifier already handles well.
# Sending them to a model would buy nothing and cost a round trip each, so the
# question is not "is semantics useful" but "does this inquiry need it".
#
# Every trigger below is a shape the anchor table is known to get wrong, and
# each is decided from text and the existing analysis alone -- no network, no
# model, no cost.

# The object's condition, which the anchor table reads as a topic.
_STATE_WORD = re.compile(r"고장|불량|파손|망가|안나와|안켜|깨[졌진져]|하자|오래된|기존")
# What the customer wants done with it.
_COLLECTION_WORD = re.compile(r"수거|가져가|가져가시|회수|처분|폐기|버려|철거")
_REPAIR_WORD = re.compile(r"수리|고쳐|a/?s|에이에스|서비스센터|점검")
_REQUEST_SHAPE = re.compile(
    r"해주세요|해주시|주세요|부탁|요청|가능한가요|가능할까요|되나요|될까요"
)

TRIGGER_STATE_ACTION_CONFLICT = "STATE_AND_ACTION_COMPETE"
TRIGGER_UNCLASSIFIED = "CLASSIFIER_HAS_NO_ACTION"
TRIGGER_DEADLINE = "DEADLINE_CONSTRAINT"

# Recorded when the router would have called, and the answer was already
# being held for some other reason. See AnswerService for why that makes
# the call worthless rather than merely redundant.
SKIP_NO_DECISION_VALUE = "NO_DECISION_VALUE"

# Two triggers were measured and dropped rather than shipped.
#
# Raw compound count fired on 96.1% of live inquiries: the sub-question
# splitter breaks on newlines, so an ordinary multi-line message counts as
# several questions. Being compound is not being unclear, and the pipeline
# already analyses each part and tracks them through Atomic Completeness.
#
# Classifier confidence fired on 64.9%. The distribution is not a gradient but
# a switch -- 54.4% of traffic sits at exactly 0.45 and the rest at 0.72 or
# above -- so it is a restatement of "unclassified or compound", not an
# independent signal. Eligibility already treats a low confidence number as a
# soft reason for the same reason: it is not a finding about the answer.


@dataclass(frozen=True)
class SemanticRouteDecision:
    """Whether this inquiry earns a semantic call, and why."""

    use_semantic: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "use_semantic": self.use_semantic,
            "reasons": list(self.reasons),
        }


def route(
    question: object,
    *,
    analysis: Mapping[str, Any] | None = None,
) -> SemanticRouteDecision:
    """Decide, without calling anything, whether semantics are needed here."""

    from answer.text_utils import is_delivery_deadline_question

    text = compact(question)
    analysis = analysis or {}
    reasons: list[str] = []

    # The exact shape of "고장난 TV 수거해주세요": a word describing the object
    # and a word naming the action, where the anchor table only knows the
    # first. Whichever it picks, it is choosing between them blind.
    if _STATE_WORD.search(text) and (
        _COLLECTION_WORD.search(text) or _REPAIR_WORD.search(text)
    ):
        reasons.append(TRIGGER_STATE_ACTION_CONFLICT)

    subtype = str(analysis.get("inquiry_subtype") or "").upper()
    intent = str(analysis.get("detected_intent") or "").upper()
    if subtype in {"UNCLASSIFIED", ""} and intent in {"GENERAL", "UNKNOWN", ""}:
        # No action was identified at all. If the sentence is also asking for
        # something to be done, the route is being chosen with nothing behind
        # it -- which is how the A/S template answered a collection request.
        if _REQUEST_SHAPE.search(text):
            reasons.append(TRIGGER_UNCLASSIFIED)

    if is_delivery_deadline_question(question):
        reasons.append(TRIGGER_DEADLINE)

    # "manual_review_required is already set, so a person will see it" was
    # tried here as a way to skip the call, and measurement rejected it: the
    # collection request carries that flag and was auto-posted anyway.
    # Eligibility downgrades a review flag to a soft reason when the classifier
    # simply found no rule (INTENT_UNCLASSIFIED_VALIDATOR_CLEAR), so the
    # inquiries most in need of an understanding are exactly the ones that flag
    # does not hold back. There is no cheap skip to be had here.
    ordered = tuple(dict.fromkeys(reasons))
    return SemanticRouteDecision(use_semantic=bool(ordered), reasons=ordered)
