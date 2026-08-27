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

import re
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Iterable, Mapping

from answer.answer_format import extract_answer_body
from answer.evidence_support import SUPPORTED_THRESHOLD
from answer.learning_signal import detect_polarity, facts_conflict
from services.auto_post_validation_service import INTERNAL_PLACEHOLDER

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
#
# A literal-substring list is the wrong shape for Korean: the same hedge
# appears as 보입니다 / 보여집니다 / 보이며 / 보이는데, and listing every
# inflection is how "것으로 보입니다" came to be missing while "것 같" was
# present -- which let an approved answer reading "사용 가능하실 것으로
# 보입니다" stand as a definite fact. The patterns below match the *stem* of
# each hedge and let the ending vary.
HEDGE_MARKERS = (
    "것 같", "것같", "듯 합니다", "듯합니다", "아마", "추정", "예상됩니다",
    "확인이 필요", "확인 후", "확인해 보", "알아보",
)

_HEDGE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # ...것으로 보입니다 / 보여집니다 / 보이며 — the one that got through.
    re.compile(r"보입니다|보여집니다|보이며|보이는데|보여요"),
    # 예상 / 추정 / 추측 / 판단 / 사료 in any ending.
    re.compile(r"예상[되돼]|예상됩니다|추정[되됩]|추측|판단[되됩]|사료[되됩]"),
    # "...할 것으로", "...인 것으로" — a projection, not a statement.
    re.compile(r"[을ㄹ] 것으로|인 것으로|것으로 [보예판사]"),
    # 가능성 / ~일 수 있 / ~수도 있 — possibility, not fact.
    re.compile(r"가능성이|[울ㄹ] 수(?:도)? 있|있을 수 있|다를 수 있|상이할 수 있"),
    # Deferral: the writer is telling the customer to check elsewhere.
    re.compile(r"확인이 (?:필요|되어야)|확인 후|확인해 ?[보주]|확인 바랍|"
               r"확인하시기|확인해주시기|문의(?:해|하시어) ?보|알아보"),
    # Explicit uncertainty about their own statement.
    re.compile(r"정확하[지진] ?않|정확도가|불확실|명확하지 ?않|장담"),
    # Hearsay.
    re.compile(r"[로으로] 알고 ?있|알려져 ?있|듣기로"),
    # Approximation used as the answer itself.
    re.compile(r"대략|어느 ?정도|약간|보통은|일반적으로는"),
)


def claim_body(answer: object) -> str:
    """The part of the answer that states something, without the frame.

    Every answer this system writes is wrapped in the company template, and
    that template's closing reads "...담당자가 확인 후 안내드리겠습니다".
    Scanning the whole answer for hedges therefore found "확인 후" in the
    boilerplate of *every* answer, so no answer the pipeline had ever
    produced could serve as evidence -- including "LS27D400 모델은 스피커가
    내장되어 있지 않습니다", which is as definite as a claim gets.

    The frame says nothing about the product, so it is removed before the
    uncertainty check. A deferral written in the body ("주문·배송 상태 확인이
    필요합니다") still reads as a deferral.
    """

    body = extract_answer_body(str(answer or ""))
    return _text(body or answer)


# The subset of the markers above that means *the writer is guessing*, as
# opposed to the writer deferring. Both belong in ``hedge_reason`` -- neither
# kind of sentence commits to a fact, which is what the validator's conflict
# checks need to know -- but they are not the same finding when deciding what
# may be shown to the model as an approved answer.
#
# "결제 확인 후 설치 기사님 일정에 맞춰 배송이 진행됩니다" is the store's actual
# new-order policy, and it carries "확인 후" as a *sequence*, not a deferral.
# Rejecting it as a guess would have thrown away one of the few genuinely
# reusable delivery policies in the corpus, which is the false-rejection this
# work is meant to avoid rather than cause.
_ESTIMATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"보입니다|보여집니다|보이며|보이는데|보여요"),
    re.compile(r"예상[되돼]|예상됩니다|추정[되됩]|추측|판단[되됩]|사료[되됩]"),
    re.compile(r"[을ㄹ] 것으로|인 것으로|것으로 [보예판사]"),
    re.compile(r"가능성이|[울ㄹ] 수(?:도)? 있|있을 수 있|다를 수 있|상이할 수 있"),
    re.compile(r"정확하[지진] ?않|정확도가|불확실|명확하지 ?않|장담"),
    re.compile(r"[로으로] 알고 ?있|알려져 ?있|듣기로"),
    re.compile(r"대략|어느 ?정도|약간|보통은|일반적으로는"),
)
_ESTIMATION_MARKERS = ("것 같", "것같", "듯 합니다", "듯합니다", "아마", "추정", "예상됩니다")


def estimation_reason(answer: object) -> str | None:
    """Which *guess* marker this answer carries, or None.

    Narrower than ``hedge_reason`` on purpose: an answer that says a person
    will check is honest and reusable, while an answer that guesses at the
    fact is the one that must not be offered as grounds for a definite claim.
    """

    text = claim_body(answer)
    if not text:
        return "EMPTY"
    for marker in _ESTIMATION_MARKERS:
        if marker in text:
            return marker
    for pattern in _ESTIMATION_PATTERNS:
        found = pattern.search(text)
        if found:
            return found.group(0)
    return None


def hedge_reason(answer: object) -> str | None:
    """Which uncertainty marker this answer carries, or None."""

    text = claim_body(answer)
    if not text:
        return "EMPTY"
    for marker in HEDGE_MARKERS:
        if marker in text:
            return marker
    for pattern in _HEDGE_PATTERNS:
        found = pattern.search(text)
        if found:
            return found.group(0)
    return None

# A stated quantity in a declarative sentence: "HDMI 단자는 3개입니다",
# "본체 무게는 6.5kg입니다", "해상도는 3840x2160입니다".
#
# ``detect_polarity`` exists to catch two yes/no statements that flatly
# disagree, and returns UNCERTAIN for anything without a 가능/불가 style
# marker -- which is every measurement there is. Reading that UNCERTAIN as
# "the writer declined to commit" disqualified precisely the approved answers
# that state a number, so Learning could settle "지원하나요?" but never
# "몇 개인가요?" -- the questions it is most often approved for.
_QUANTITY_STATEMENT = re.compile(
    r"\d+(?:[.,]\d+)?\s*"
    r"(?:개|대|장|인치|cm|mm|m|kg|g|w|hz|ms|년|월|일|시간|분|x|×)?"
    r"[^.!?\n]*"
    r"(?:입니다|이에요|예요|됩니다|습니다|이며|이고|입니다\.)"
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
    """True when the answer declines to commit to what it is being read for.

    A hedge marker settles it outright, and a clear yes/no polarity settles the
    opposite. What remains is everything the polarity detector has no opinion
    about, which includes every measurement -- so a stated quantity in a
    declarative sentence counts as committing, and anything else stays hedged.
    """

    text = claim_body(answer)
    if not text:
        return True
    if hedge_reason(answer) is not None:
        return True
    if detect_polarity(text) != "UNCERTAIN":
        return False
    return not _QUANTITY_STATEMENT.search(text)


# A number with its middle blanked out -- "주문번호 2026****2541". Eleven live
# rows carry one, and one of those is factual-eligible. It is the same thing a
# ``<masked-order>`` token is: a record that something was removed. Copied into
# a new answer it would hand a customer an order number that does not exist.
#
# Digits are required on both sides so a decorative "***" separator, or a
# seller writing "★★★", is not mistaken for a redaction. Scoped to the Learning
# evidence policy rather than added to INTERNAL_PLACEHOLDER, which the
# validator also reads -- widening a published answer's blocking rule is a
# separate decision from deciding what may be reused as evidence.
_PARTIALLY_MASKED_NUMBER = re.compile(r"\d{2,}\*{2,}\d{2,}")


# --- provenance ------------------------------------------------------------
#
# "A person approved this" is one bit, and it was being asked to carry a
# distinction it cannot hold. 299 of the 306 human_verified rows are seller
# answers that were already posted on Naver and later marked verified in bulk;
# 7 are answers a member of staff actually edited or reviewed in this system.
# Ranking read the bit alone, so the 299 sat at or above the 7.
#
# The store already records the difference -- ``answer_provenance`` says who
# wrote the text, ``facts_authority`` says what it was accepted as -- so the
# class is read, never inferred. Nothing here guesses from the answer body.
#
# Staff who compose an answer from scratch (DIRECT_HUMAN) are deliberately
# absent: no column distinguishes them from an edited draft today, and
# inventing the class would mean guessing. They fall to UNKNOWN_PROVENANCE,
# which sits at the bottom of the ladder rather than the top.
APPROVED_EDITED = "APPROVED_EDITED"
APPROVED_UNEDITED = "APPROVED_UNEDITED"
SELLER_ANSWER_VERIFIED = "SELLER_ANSWER_VERIFIED"
SELLER_ANSWER = "SELLER_ANSWER"
HISTORICAL_PROMOTED = "HISTORICAL_PROMOTED"
UNKNOWN_PROVENANCE = "UNKNOWN_PROVENANCE"

# Authority *within* Learning, used only to order candidates relevance and the
# compatibility gate have already accepted as equally applicable. It is not a
# licence to admit anything: a class absent from this table keeps whatever the
# existing source table gives it.
LEARNING_AUTHORITY: Mapping[str, int] = {
    APPROVED_EDITED: 8,
    APPROVED_UNEDITED: 6,
    SELLER_ANSWER_VERIFIED: 4,
}


def classify_provenance(item: Mapping[str, Any]) -> str:
    """Which kind of answer a Learning row actually is."""

    metadata = item.get("metadata_json")
    metadata = metadata if isinstance(metadata, dict) else {}
    source = str(item.get("learning_source") or "").upper()
    origin = str(metadata.get("source_origin") or "").upper()
    provenance = str(metadata.get("answer_provenance") or "").upper()

    # Checked first: a promoted historical case is historical whatever else is
    # stamped on it, and must not reach a staff tier through its source name.
    if origin == HISTORICAL_PROMOTED:
        return HISTORICAL_PROMOTED
    if metadata.get("human_verified") is not True:
        return SELLER_ANSWER if source == SELLER_ANSWER else UNKNOWN_PROVENANCE
    if provenance == "STAFF_EDITED" or source == APPROVED_EDITED:
        return APPROVED_EDITED
    if provenance == "PROGRAM_GENERATED" or source == APPROVED_UNEDITED:
        return APPROVED_UNEDITED
    if provenance == "NAVER_POSTED":
        return SELLER_ANSWER_VERIFIED
    return UNKNOWN_PROVENANCE


def contamination_reason(value: object) -> str | None:
    """The redaction token this text carries, or None.

    ``<masked-phone>`` and its siblings are written when the pipeline removes
    something. A stored answer containing one is a record of a removal, not a
    statement about the product, and the token itself must never reach a
    customer -- so such an answer can neither prove a claim nor be shown to
    the model as an example worth copying.
    """

    text = _text(value)
    found = INTERNAL_PLACEHOLDER.search(text)
    if found:
        return found.group(0)
    partial = _PARTIALLY_MASKED_NUMBER.search(text)
    return partial.group(0) if partial else None


def usable_as_factual_evidence(item: Mapping[str, Any] | Any) -> bool:
    """Whether one retrieved item's text may ground a factual claim.

    Deliberately shallow: identity, approval and topic scope are judged by
    the compatibility gate and ``_qualifying`` below. This answers only the
    question those two never ask -- is the *text itself* the kind of thing
    that can prove something?
    """

    if not isinstance(item, Mapping):
        return False
    if item.get("style_only"):
        return False
    # Learning items carry ``answer``; historical cases are raw rows and carry
    # ``seller_answer``. Reading only one of them would silently drop a whole
    # evidence class from the grounding corpus.
    text = _text(
        item.get("answer")
        or item.get("final_answer")
        or item.get("seller_answer")
    )
    if not text:
        return False
    if contamination_reason(text) is not None:
        return False
    return not is_hedged(text)


_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def quantities(value: object) -> frozenset[float]:
    """Every number stated in ``value``.

    Admitting measurements as definite (see ``is_hedged``) means the conflict
    checks have to be able to see a measurement disagree. Polarity cannot:
    "3개입니다" and "2개입니다" are both UNCERTAIN to it, so without this the
    two would have been treated as agreeing, and one of them published.

    Bare numbers, deliberately: a unit-aware comparison would have to decide
    that 6.5kg and 6500g are the same claim and that 3개 and 3.0 are too, and
    getting that wrong in either direction is worse than the small cost of
    treating an incidental number as a claim. Disagreement here only ever
    routes an inquiry to a person.
    """

    return frozenset(
        float(match.group()) for match in _NUMBER.finditer(_text(value))
    )


def quantities_conflict(left: object, right: object) -> bool:
    """True when both state numbers and share none of them."""

    left_values = quantities(left)
    right_values = quantities(right)
    if not left_values or not right_values:
        return False
    return not (left_values & right_values)


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
            if not facts_conflict(
                left.get("answer"), right.get("answer")
            ) and not quantities_conflict(
                left.get("answer"), right.get("answer")
            ):
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


def _fact_quantity_text(value: object) -> str:
    """The numbers a fact value states, whatever shape it is stored in.

    Values arrive as scalars (``3``), mappings (``{"inch": 43}``) and lists
    (``[450, 500, 550]``); flattening to text lets one number comparison read
    all three without teaching it each field's schema.
    """

    if isinstance(value, Mapping):
        return " ".join(str(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(str(item) for item in value)
    return str(value or "")


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
        for fact in facts:
            value = getattr(fact, "value", None)
            polarity = value_polarity(value)
            opposed_polarity = (
                answer_polarity != "UNCERTAIN"
                and polarity not in {"UNCERTAIN", answer_polarity}
            )
            # A verified count must also be able to contradict an approved
            # answer that states a different count; neither text carries a
            # polarity, so polarity alone would call them compatible.
            opposed_quantity = quantities_conflict(
                item.get("answer"), _fact_quantity_text(value)
            )
            if not opposed_polarity and not opposed_quantity:
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
