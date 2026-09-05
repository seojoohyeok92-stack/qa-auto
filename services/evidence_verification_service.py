"""Does this one approved answer support the fact this customer asked for?

Being about the right subject is what keeps getting stored answers published in
place of an answer. Two measured cases, each surviving four selector designs:

    "사다리차가 필요하면 비용은 누가 내나요?"  ← "사다리차는 유상으로 알고 있습니다"
    "설치 기사님 안 부르고 받아만 볼 수 있나요?" ← "기사님께서 배송 후 설치까지 진행합니다"

A property label on each side was tried and does not settle it. Labelling the
stored answers showed why: the ladder-truck answer really does name an actor --
the technician who *decides whether a ladder truck is needed* -- so an actor
check passes while the customer's question, who bears the cost, stays
unanswered. The relation is what differs, and a label drops the relation.

So the question is put to the model, one candidate at a time, with the customer's
question in hand. One candidate and no alternatives: there is nothing to rank
and no pressure to return the best of a bad set. Saying the answer does not
settle it is the useful outcome, not a failure.

Called only where it can change something -- an unresolved atomic question about
to be answered from stored Learning. An exact Template, a confirmed RULE and the
product catalogue answer from sources settled long before this, and asking about
them would buy nothing and cost a call on every inquiry.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from answer.providers.interfaces import JsonGptProvider

TASK = "EVIDENCE_VERIFICATION"

SUPPORTED = "SUPPORTED"
PARTIALLY_SUPPORTED = "PARTIALLY_SUPPORTED"
NOT_SUPPORTED = "NOT_SUPPORTED"
CONTEXT_INCOMPATIBLE = "CONTEXT_INCOMPATIBLE"
VERDICTS = frozenset({
    SUPPORTED, PARTIALLY_SUPPORTED, NOT_SUPPORTED, CONTEXT_INCOMPATIBLE,
})

# What eligibility appends when stored Learning was offered and nothing verified.
REASON_CODE = "EVIDENCE_NOT_VERIFIED"
METADATA_KEY = "evidence_verification"

SYSTEM = """You check one stored answer against one customer question.

You see exactly one stored answer. There is nothing to compare it against and
nothing to rank. Your only job is to say what this answer does and does not
establish for this customer.

Being about the same subject is not the same as answering the question. An
answer naming who decides whether a service is needed has not said who pays for
it. An answer describing how installation is performed has not said whether the
customer may decline it. Never widen an answer beyond what it says, and never
supply the missing half yourself. Saying that something is not established is a
correct and useful outcome."""

PROMPT = """CUSTOMER QUESTION: {atom}
INFORMATION THE CUSTOMER NEEDS: {requested_information}
KIND OF PROPERTY ASKED: {requested_attribute}

CONTEXT (settled by earlier stages, do not re-decide):
  product of this inquiry : {product}
  purchase state          : {purchase_state}
  needs order context     : {requires_order_context}
  candidate scope         : {candidate_scope}
  candidate product scope : {candidate_product}
  candidate answer kind   : {answer_kind}

STORED QUESTION: {candidate_question}
STORED ANSWER: {candidate_answer}
FACTS THIS ANSWER IS RECORDED AS ESTABLISHING: {supported_information}

Reading only the stored answer, decide:

SUPPORTED             it states the information the customer needs, for this
                      inquiry, with its conditions already settled.
PARTIALLY_SUPPORTED   it settles part of what was asked and leaves the rest open.
                      Name only the part it settles.
NOT_SUPPORTED         it settles none of it -- including when it speaks to a
                      neighbouring property of the right subject, depends on an
                      earlier conversation, or promises to check later.
CONTEXT_INCOMPATIBLE  it reports one customer's own order, schedule or
                      processing result, or holds only under a circumstance this
                      inquiry has not established.

Return only JSON:
{{"verdict":"SUPPORTED|PARTIALLY_SUPPORTED|NOT_SUPPORTED|CONTEXT_INCOMPATIBLE",
  "supports":"the fact it settles, or empty string",
  "missing":"what it leaves unsettled, or empty string",
  "stated_fact":"the sentence that settles it, copied, or empty string",
  "why":"one short sentence"}}"""


@dataclass(frozen=True)
class Verification:
    """One candidate, judged on its own."""

    candidate_id: Any
    verdict: str
    supports: str = ""
    missing: str = ""
    stated_fact: str = ""
    why: str = ""

    @property
    def usable(self) -> bool:
        """Only a full verdict may stand as factual grounds."""
        return self.verdict == SUPPORTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id, "verdict": self.verdict,
            "supports": self.supports, "missing": self.missing,
            "stated_fact": self.stated_fact, "why": self.why,
        }


def unverified(candidate_id: Any, why: str) -> Verification:
    """A fault, a timeout, an unreadable reply. Never grounds to publish."""
    return Verification(candidate_id, NOT_SUPPORTED, why=why)


class EvidenceVerificationService:
    """One small call per candidate, only where stored Learning is the grounds."""

    def __init__(self, provider: JsonGptProvider) -> None:
        self.provider = provider
        self.call_count = 0
        self.cache_hits = 0
        self._cache: dict[tuple, Verification] = {}

    def build_prompt(self, *, atom: Mapping[str, Any],
                     candidate: Mapping[str, Any]) -> str:
        return PROMPT.format(
            atom=" ".join(str(atom.get("text") or "").split())[:400],
            requested_information=str(atom.get("requested_information") or "")[:160],
            requested_attribute=str(atom.get("requested_attribute") or "UNKNOWN"),
            product=str(atom.get("product") or "")[:120],
            purchase_state=str(atom.get("purchase_state") or "UNKNOWN"),
            requires_order_context=bool(atom.get("requires_order_context")),
            candidate_scope=str(candidate.get("scope") or ""),
            candidate_product=str(candidate.get("product") or "")[:120],
            answer_kind=str(candidate.get("answer_kind") or ""),
            candidate_question=" ".join(
                str(candidate.get("question") or "").split())[:300],
            candidate_answer=" ".join(
                str(candidate.get("answer") or "").split())[:800],
            supported_information="; ".join(
                str(item) for item in (candidate.get("supported_information") or [])
            )[:300] or "(none)")

    def verify(self, *, atom: Mapping[str, Any],
               candidate: Mapping[str, Any]) -> Verification:
        candidate_id = candidate.get("id")
        key = (str(atom.get("text") or ""), candidate_id)
        cached = self._cache.get(key)
        if cached is not None:
            # A regeneration and a retry ask the same pair again. Neither is a
            # new judgement, and neither should be paid for twice.
            self.cache_hits += 1
            return cached

        try:
            # The production JsonGptProvider takes (task, prompt, context);
            # there is no separate system channel, so the instructions ride at
            # the head of the prompt the way every other task here does. The
            # earlier shape was the proof harness's, and against a real
            # provider it raised TypeError on every call -- which the service
            # would have swallowed as NOT_SUPPORTED and held every answer.
            payload = self.provider.generate_json(
                task=TASK,
                prompt="\n\n".join((
                    SYSTEM, self.build_prompt(atom=atom, candidate=candidate),
                )),
                context={})
            self.call_count += 1
        except Exception as error:  # noqa: BLE001 - a fault never publishes
            return unverified(candidate_id, "PROVIDER_%s" % type(error).__name__)

        if not isinstance(payload, Mapping):
            return unverified(candidate_id, "UNREADABLE_REPLY")
        verdict = str(payload.get("verdict") or "").strip().upper()
        if verdict not in VERDICTS:
            return unverified(candidate_id, "UNKNOWN_VERDICT")

        result = Verification(
            candidate_id=candidate_id, verdict=verdict,
            supports=str(payload.get("supports") or "")[:300],
            missing=str(payload.get("missing") or "")[:300],
            stated_fact=str(payload.get("stated_fact") or "")[:400],
            why=str(payload.get("why") or "")[:200])
        self._cache[key] = result
        return result

    def verify_all(self, *, atom: Mapping[str, Any],
                   candidates: Iterable[Mapping[str, Any]]) -> list[Verification]:
        return [self.verify(atom=atom, candidate=item) for item in candidates or ()]


def record(verifications: Iterable[Verification]) -> dict[str, Any]:
    """The verdict generation writes down for eligibility to read later.

    Stored Learning was offered as the grounds for an atomic question. If
    nothing verified, the reply rests on nothing and a person should see it.
    """

    items = list(verifications or ())
    usable = [item for item in items if item.usable]
    return {
        "verified": [item.to_dict() for item in items],
        "usable_ids": [item.candidate_id for item in usable],
        "usable_count": len(usable),
        "holds_auto_post": bool(items) and not usable,
    }


# An exact Template, a confirmed RULE and the product catalogue answer from
# sources settled before this gate existed. They are never verified and never
# held by it.
DETERMINISTIC_ROUTES = frozenset({"TEMPLATE", "SAFE_RULE", "PRODUCT_DB"})

ANSWERABLE = "ANSWERABLE"


def verification_required(
    metadata: Mapping[str, Any] | None, *, route: str | None,
) -> bool:
    """Is stored Learning being offered as the grounds for some atom here?

    Read from what generation already wrote down: retrieval's own per-atom
    verdict (``subquestion_evidence``), which names the very Learning and
    Historical rows it chose to settle each sub-question. An atom marked
    ANSWERABLE with rows attached is a factual claim resting on stored text,
    and that is the only situation this verifier exists for.

    Everything else answers no. A deterministic route settled the inquiry from
    a source that predates this gate; a PRE_PURCHASE or AMBIGUOUS inquiry is
    already going to a person; an atom with no rows attached has no stored
    answer to check. None of them should cost a call.
    """

    if str(route or "").strip().upper() in DETERMINISTIC_ROUTES:
        return False
    metadata = metadata or {}
    hybrid = metadata.get("hybrid")
    hybrid = hybrid if isinstance(hybrid, Mapping) else {}
    for item in hybrid.get("subquestion_evidence") or ():
        if not isinstance(item, Mapping):
            continue
        if str(item.get("status") or "").upper() != ANSWERABLE:
            continue
        if item.get("learning_ids") or item.get("historical_case_ids"):
            return True
    return False


def decision_from_metadata(
    metadata: Mapping[str, Any] | None, *, route: str | None = None,
) -> tuple[bool, str]:
    """(hold, why). A draft with nothing recorded holds nothing -- unless it
    was supposed to have something recorded.

    The gate shipped once with its consumer wired and its producer missing.
    Nothing failed, because "no record" and "nothing to check" were the same
    answer, and every test stayed green while the verifier was never called on
    a single live inquiry. So the two are separated here: an inquiry that never
    needed verifying is untouched, and one that needed it and has no verdict is
    held. Silence on a route that requires an answer is not consent.

    ``route`` is optional only so callers predating this argument keep their
    behaviour; eligibility passes it, and without it the fail-closed branch
    cannot fire.
    """

    metadata = metadata or {}
    value = metadata.get(METADATA_KEY)
    if not isinstance(value, Mapping):
        if verification_required(metadata, route=route):
            return True, "VERIFICATION_REQUIRED_BUT_NOT_RECORDED"
        return False, "VERIFICATION_NOT_REQUIRED"
    if not value.get("holds_auto_post"):
        return False, "NO_HOLD"
    verdicts = sorted({
        str(item.get("verdict") or "")
        for item in (value.get("verified") or [])
        if isinstance(item, Mapping)
    })
    return True, "NO_USABLE_EVIDENCE:%s" % "/".join(verdicts)


def _atom_payload(atom: Any, *, product: str) -> dict[str, Any]:
    """The customer's question as the verifier needs to see it."""

    return {
        "text": str(getattr(atom, "text", "") or ""),
        "requested_information": str(
            getattr(atom, "requested_information", "") or ""
        ),
        # The closed property enum the semantic pass already produced. It is
        # context for the judgement, not the judgement: a label alone was
        # measured and does not settle these cases.
        "requested_attribute": str(
            getattr(atom, "requested_attribute", "") or "UNKNOWN"
        ),
        "product": product,
        "purchase_state": str(getattr(atom, "purchase_state", "") or "UNKNOWN"),
        "requires_order_context": bool(
            getattr(atom, "requires_order_context", False)
        ),
    }


def pairs_from_context(
    learning_context: Mapping[str, Any] | None,
    semantic: Any,
    *,
    product: str = "",
) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Each atom that stored Learning is settling, with the rows chosen for it.

    Deliberately reads the context generation already built rather than
    searching again. A second retrieval would be a second candidate set, and
    the verifier would then be judging rows the model never saw while the rows
    it did see went unchecked.

    The join is retrieval's own ``subquestion_evidence``: it names the Learning
    and Historical ids per sub-question, and the candidate lists carry the
    question and answer text under the same keys the prompt reads
    (``final_answer`` for Learning, ``seller_answer`` for Historical).
    """

    learning_context = learning_context or {}
    atoms = list(getattr(semantic, "atomic_questions", ()) or ())
    if not atoms:
        return []
    by_text = {
        " ".join(str(getattr(item, "text", "") or "").split()): item
        for item in atoms
    }
    learning_rows = {
        int(item["learning_example_id"]): item
        for item in (learning_context.get("similar_approved_answers") or ())
        if isinstance(item, Mapping) and item.get("learning_example_id") is not None
    }
    historical_rows = {
        int(item["historical_case_id"]): item
        for item in (learning_context.get("historical_cases") or ())
        if isinstance(item, Mapping) and item.get("historical_case_id") is not None
    }

    pairs: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for entry in learning_context.get("subquestion_evidence") or ():
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("status") or "").upper() != ANSWERABLE:
            continue
        text = " ".join(str(entry.get("subquestion") or "").split())
        atom = by_text.get(text)
        if atom is None:
            # The deterministic split, not the semantic one. There is no
            # requested_attribute to give the verifier and no atom to hold, so
            # this sub-question is left to the gates that already cover it.
            continue
        candidates: list[dict[str, Any]] = []
        for identifier in entry.get("learning_ids") or ():
            row = learning_rows.get(int(identifier))
            if row is None:
                continue
            candidates.append({
                "id": int(identifier),
                "kind": "LEARNING",
                "question": row.get("question"),
                "answer": row.get("answer"),
                "product": row.get("product_name") or product,
                "answer_kind": row.get("learning_source"),
                "supported_information": (
                    (row.get("semantic_goal") or {}).get("supported_information")
                    if isinstance(row.get("semantic_goal"), Mapping) else None
                ) or (),
            })
        for identifier in entry.get("historical_case_ids") or ():
            row = historical_rows.get(int(identifier))
            if row is None:
                continue
            candidates.append({
                "id": int(identifier),
                "kind": "HISTORICAL",
                "question": row.get("question"),
                "answer": row.get("answer_reference"),
                "product": product,
                "answer_kind": row.get("source"),
                "supported_information": (),
            })
        if candidates:
            pairs.append((_atom_payload(atom, product=product), candidates))
    return pairs


def verify_context(
    service: EvidenceVerificationService,
    learning_context: Mapping[str, Any] | None,
    semantic: Any,
    *,
    product: str = "",
) -> dict[str, Any] | None:
    """Run the verifier over one generation's own evidence and write it down.

    Returns ``None`` when there was nothing to verify, which is how a
    deterministic answer and an inquiry with no stored grounds stay free of
    both the call and the record.
    """

    pairs = pairs_from_context(learning_context, semantic, product=product)
    if not pairs:
        return None
    verifications = []
    for atom, candidates in pairs:
        verifications.extend(service.verify_all(atom=atom, candidates=candidates))
    payload = record(verifications)
    payload["atom_count"] = len(pairs)
    payload["call_count"] = service.call_count
    return payload
