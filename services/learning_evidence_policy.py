"""When an approved Learning answer may stand as evidence for a product fact.

The Product Knowledge DB is authoritative but partial.  Treating "the DB has no
row for this field" as "the product does not do this" is the single most
damaging mistake available here: it turns a gap in our records into a negative
claim about the product.  It also throws away the answers staff wrote and a
person explicitly approved -- the most reliable evidence the system has about
anything the DB has not catalogued yet.

So a missing fact stops meaning "unverified" *by itself*.  What replaces it is
not a weaker test but a different one, and every clause below already exists
somewhere in the pipeline; this module only insists that all of them hold at
once before an approved answer may settle a product-fact question:

  approved      a person reviewed and approved it (authority APPROVED, and
                never a seller_style_examples entry -- those are tone, not fact)
  same product  the compatibility gate resolved this candidate to the exact
                model, product id, or distinctive name.  For a strict product
                topic it hard-rejects anything less, so Model A's stand answer
                can never settle Model B's stand question
  on point      the approved answer actually supports *this* question, by the
                same answer-support measure retrieval uses -- not string
                similarity between the two questions
  definite      the answer states something.  A hedged answer is a person
                declining to commit, and inheriting that as a verified fact
                would publish a guess wearing a verified label
  undisputed    no other approved answer in scope contradicts it, and no
                VERIFIED product fact contradicts it

Conflicts are never resolved here -- not by recency, not by score.  Two
approved answers that disagree are a record-keeping problem a person has to
settle, and picking one would publish a coin flip.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any, Iterable, Mapping

from answer.evidence_support import SUPPORTED_THRESHOLD
from answer.learning_signal import detect_polarity, facts_conflict

# Product identity verdicts the compatibility gate issues when it resolved the
# candidate to this exact product. Anything else it returns is either a hard
# reject or a policy-level (product-independent) match, neither of which may
# settle a fact about one specific model.
EXACT_PRODUCT_MATCHES = frozenset({
    "EXACT_MODEL", "EXACT_PRODUCT", "EXACT_NAME",
})

# A person hedging. The polarity detector already reads most hedged phrasings
# as UNCERTAIN; these catch the ones that pair a hedge with a definite verb
# ("가능할 것 같습니다"), where the affirmative marker would otherwise win.
HEDGE_MARKERS = (
    "것 같", "것같", "듯 합니다", "듯합니다", "아마", "추정", "예상됩니다",
    "확인이 필요", "확인 후", "확인해 보", "알아보",
)

# Fact values that carry a polarity of their own. A boolean-ish product fact
# ("stand_detachable: false") is a claim about the product, and an approved
# answer saying the opposite contradicts it even though neither text contains
# Korean polarity grammar.
_NEGATIVE_VALUES = frozenset({
    "false", "no", "n", "0", "none", "unsupported", "미지원", "불가", "불가능",
    "없음", "아니오", "비지원", "미제공",
})
_AFFIRMATIVE_VALUES = frozenset({
    "true", "yes", "y", "1", "supported", "지원", "가능", "있음", "예", "제공",
})


@dataclass(frozen=True)
class LearningEvidenceDecision:
    """Whether approved Learning settles this question, and why or why not."""

    usable: bool
    reason: str
    conflict: bool = False
    learning_ids: tuple[int, ...] = ()
    subquestions: tuple[str, ...] = ()
    conflicts: tuple[dict[str, Any], ...] = dataclass_field(default=())

    def to_dict(self) -> dict[str, Any]:
        return {
            "usable": self.usable,
            "reason": self.reason,
            "conflict": self.conflict,
            "learning_ids": list(self.learning_ids),
            "subquestions": list(self.subquestions),
            "conflicts": [dict(item) for item in self.conflicts],
        }


def _text(value: object) -> str:
    return " ".join(str(value or "").split())


def is_hedged(answer: object) -> bool:
    """True when the answer declines to commit to what it is being read for."""

    text = _text(answer)
    if not text:
        return True
    if any(marker in text for marker in HEDGE_MARKERS):
        return True
    return detect_polarity(text) == "UNCERTAIN"


def value_polarity(value: object) -> str:
    """AFFIRMATIVE/NEGATIVE for a boolean-ish fact value, else UNCERTAIN."""

    text = _text(value).lower().rstrip(".")
    if not text:
        return "UNCERTAIN"
    if text in _NEGATIVE_VALUES:
        return "NEGATIVE"
    if text in _AFFIRMATIVE_VALUES:
        return "AFFIRMATIVE"
    return detect_polarity(text)


def _qualifying(items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Approved, exact-product, on-point, definite candidates."""

    kept: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("authority") or "").upper() != "APPROVED":
            continue
        compatibility = item.get("compatibility")
        compatibility = (
            compatibility if isinstance(compatibility, Mapping) else {}
        )
        if (
            str(compatibility.get("product_match") or "")
            not in EXACT_PRODUCT_MATCHES
        ):
            continue
        try:
            support = float(item.get("answer_support") or 0.0)
        except (TypeError, ValueError):
            continue
        if support < SUPPORTED_THRESHOLD:
            continue
        if is_hedged(item.get("answer")):
            continue
        kept.append(dict(item))
    return kept


def approved_conflicts(
    items: Iterable[Mapping[str, Any]]
) -> tuple[dict[str, Any], ...]:
    """Approved answers for the same sub-question that flatly disagree.

    Uses the same polarity comparison the verified-signal path already uses, so
    "가능합니다" against "불가능합니다" is a conflict and two differently worded
    agreeing answers are not.  Only candidates that passed the identity and
    support checks are compared -- a rejected candidate is not evidence, so it
    cannot create a conflict either.
    """

    usable = _qualifying(items)
    found: list[dict[str, Any]] = []
    for index, left in enumerate(usable):
        for right in usable[index + 1:]:
            if left.get("matched_subquestion") != right.get(
                "matched_subquestion"
            ):
                continue
            if not facts_conflict(left.get("answer"), right.get("answer")):
                continue
            found.append({
                "left_learning_id": left.get("learning_example_id"),
                "right_learning_id": right.get("learning_example_id"),
                "left_text": _text(left.get("answer"))[:200],
                "right_text": _text(right.get("answer"))[:200],
                "subquestion": left.get("matched_subquestion"),
                "kind": "APPROVED_LEARNING_VS_APPROVED_LEARNING",
            })
    return tuple(found)


def fact_conflicts(
    items: Iterable[Mapping[str, Any]], safe_facts: Iterable[Any]
) -> tuple[dict[str, Any], ...]:
    """Approved answers contradicted by a VERIFIED product fact.

    A verified fact is never silently overwritten by Learning.  When the two
    disagree the answer is not "trust the DB" either -- both are evidence a
    person produced, and which of them is stale is not something this code can
    know.  It is reported as a conflict so a person decides.
    """

    usable = _qualifying(items)
    facts = [fact for fact in safe_facts if fact is not None]
    found: list[dict[str, Any]] = []
    for item in usable:
        answer_polarity = detect_polarity(item.get("answer"))
        if answer_polarity == "UNCERTAIN":
            continue
        for fact in facts:
            polarity = value_polarity(getattr(fact, "value", None))
            if polarity in {"UNCERTAIN", answer_polarity}:
                continue
            found.append({
                "learning_id": item.get("learning_example_id"),
                "learning_text": _text(item.get("answer"))[:200],
                "field_key": getattr(fact, "field_key", ""),
                "fact_value": _text(getattr(fact, "value", "")),
                "subquestion": item.get("matched_subquestion"),
                "kind": "PRODUCT_FACT_VS_APPROVED_LEARNING",
            })
    return tuple(found)


def _supported_subquestions(context: Mapping[str, Any]) -> frozenset[str]:
    """Sub-questions retrieval itself resolved from approved Learning.

    Retrieval's verdict is the last word on whether the approved answer answers
    this particular sub-question: an item can clear every check above and still
    sit beside a sub-question that needs a live DPS date.
    """

    evidence = context.get("subquestion_evidence")
    if not isinstance(evidence, list):
        return frozenset()
    return frozenset(
        str(item.get("subquestion") or "")
        for item in evidence
        if isinstance(item, Mapping)
        and str(item.get("status") or "").upper() == "ANSWERABLE"
        and str(item.get("evidence_coverage") or "").upper() == "SUPPORTED"
        and str(item.get("source") or "").upper() == "ACTIVE_POSITIVE_LEARNING"
    )


def evaluate(
    *,
    learning_context: Mapping[str, Any] | None,
    safe_facts: Iterable[Any] = (),
) -> LearningEvidenceDecision:
    """Whether approved Learning may settle this inquiry's product facts."""

    context = learning_context if isinstance(learning_context, Mapping) else {}
    approved = context.get("similar_approved_answers")
    approved = approved if isinstance(approved, list) else []
    if not approved:
        return LearningEvidenceDecision(False, "NO_APPROVED_LEARNING")

    against_facts = fact_conflicts(approved, safe_facts)
    against_each_other = approved_conflicts(approved)
    if against_facts or against_each_other:
        return LearningEvidenceDecision(
            False,
            (
                "PRODUCT_FACT_VS_LEARNING_CONFLICT" if against_facts
                else "APPROVED_LEARNING_CONFLICT"
            ),
            conflict=True,
            conflicts=(*against_facts, *against_each_other),
        )

    usable = _qualifying(approved)
    if not usable:
        return LearningEvidenceDecision(False, "NO_QUALIFYING_APPROVED_LEARNING")

    supported = _supported_subquestions(context)
    covered = tuple(
        str(item.get("matched_subquestion") or "")
        for item in usable
        if str(item.get("matched_subquestion") or "") in supported
    )
    if not covered:
        return LearningEvidenceDecision(False, "LEARNING_NOT_MAPPED_TO_EVIDENCE")
    return LearningEvidenceDecision(
        True,
        "APPROVED_LEARNING_SUPPORTED",
        learning_ids=tuple(
            int(item["learning_example_id"])
            for item in usable
            if item.get("learning_example_id") is not None
        ),
        subquestions=tuple(dict.fromkeys(covered)),
    )
