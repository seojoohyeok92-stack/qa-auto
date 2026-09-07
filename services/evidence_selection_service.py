"""Which retrieved candidates are worth verifying for this question.

Retrieval now returns roughly twenty candidates per atomic question, and the
stage that decides which of them the verifier ever sees is a deterministic
lexical answer-support score. Measured on inquiry 325584049, the atom "TV 무료
설치인가요" retrieved sixteen candidates, narrowed to one, and that one scored
0.33 against a 0.5 floor -- so nothing reached the verifier and the question was
answered from no evidence at all. The floor is a word-overlap number; whether a
stored answer speaks to what was asked is not.

So the narrowing is asked of the model instead, once per atom, over the
candidates retrieval already found. The selector reads each candidate's own
approved question and answer beside the customer's question and says which of
them could state the fact being asked for.

Two boundaries, both deliberate:

* **It only widens.** Whatever the deterministic ladder already marks
  ANSWERABLE still reaches the verifier; this adds to that set and never
  subtracts from it. A selector fault, an unparsable reply or a missing
  provider therefore leaves the pipeline exactly as it was.
* **It licenses nothing.** Selecting a candidate means "worth one verifier
  call", not "usable as grounds". ``evidence_verification_service`` remains the
  only thing that can turn a candidate into factual evidence, and only on
  SUPPORTED. Merging the two would put candidate plausibility and evidence
  sufficiency behind one judgement, which is the failure this pipeline already
  measured once.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from answer.providers.interfaces import JsonGptProvider

TASK = "EVIDENCE_SELECTION"
METADATA_KEY = "evidence_selection"

# How many candidates one selector call may weigh. Retrieval's own limit is
# twenty; beyond that the prompt stops being a judgement and becomes a list.
MAX_CANDIDATES = 20

# How much of a stored answer the selector reads. It is deciding whether the
# answer is on the right subject at all -- the verifier reads it properly.
ANSWER_EXCERPT = 320

SYSTEM = """You are shortlisting stored answers that might answer one customer question.

You are not deciding whether an answer is sufficient. That is a later step. You
are deciding which of these stored answers are worth reading closely, because
they could state the fact this customer is missing.

Include a candidate when its answer speaks to the property being asked about --
not merely to the same product or the same general topic. "설치는 기사님이
방문합니다" speaks to how installation happens; it does not speak to what it
costs. A candidate about another customer's own order, schedule or refund
states a fact about that order and not about this one.

Selecting nothing is a correct answer. Do not fill a quota."""

PROMPT = """CUSTOMER QUESTION: {atom}
INFORMATION THE CUSTOMER NEEDS: {requested_information}
KIND OF PROPERTY ASKED: {requested_attribute}
PRODUCT: {product}
PURCHASE STATE: {purchase_state}

CANDIDATES:
{candidates}

Reply as JSON:
{{"selected": [{{"id": <candidate id>, "why": "<one short clause>"}}]}}
Return only candidates that could state the information needed. Return an empty
list if none of them can."""

CANDIDATE_TEMPLATE = """- id: {id}
  kind: {kind}
  stored question: {question}
  stored answer: {answer}"""


@dataclass(frozen=True)
class Selection:
    """One selector verdict, per candidate."""

    candidate_id: Any
    selected: bool
    why: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "selected": self.selected,
            "why": self.why,
        }


@dataclass
class SelectionOutcome:
    """What one atom's selection produced, for the record."""

    atom: str
    considered: int = 0
    selected_ids: tuple[Any, ...] = ()
    verdicts: tuple[Selection, ...] = ()
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "atom": self.atom,
            "considered": self.considered,
            "selected_ids": list(self.selected_ids),
            "verdicts": [item.to_dict() for item in self.verdicts],
            "error": self.error,
        }


class EvidenceSelectionService:
    """One call per atomic question, over the candidates retrieval found."""

    def __init__(self, provider: JsonGptProvider) -> None:
        self.provider = provider
        self.call_count = 0
        self.cache_hits = 0
        self._cache: dict[tuple, SelectionOutcome] = {}

    # ------------------------------------------------------------- 프롬프트
    def build_prompt(self, *, atom: Mapping[str, Any],
                     candidates: Sequence[Mapping[str, Any]]) -> str:
        rendered = "\n".join(
            CANDIDATE_TEMPLATE.format(
                id=item.get("id"),
                kind=str(item.get("kind") or "LEARNING"),
                question=" ".join(str(item.get("question") or "").split())[:200],
                answer=" ".join(
                    str(item.get("answer") or "").split())[:ANSWER_EXCERPT],
            )
            for item in candidates
        )
        return PROMPT.format(
            atom=" ".join(str(atom.get("text") or "").split())[:400],
            requested_information=str(atom.get("requested_information") or "")[:160],
            requested_attribute=str(atom.get("requested_attribute") or "UNKNOWN"),
            product=str(atom.get("product") or "")[:120],
            purchase_state=str(atom.get("purchase_state") or "UNKNOWN"),
            candidates=rendered,
        )

    # ----------------------------------------------------------------- 선택
    def select(self, *, atom: Mapping[str, Any],
               candidates: Sequence[Mapping[str, Any]]) -> SelectionOutcome:
        text = " ".join(str(atom.get("text") or "").split())
        usable = [item for item in candidates if item.get("id") is not None]
        usable = usable[:MAX_CANDIDATES]
        if not usable:
            return SelectionOutcome(atom=text, considered=0)

        key = (text, tuple(sorted(str(item.get("id")) for item in usable)))
        cached = self._cache.get(key)
        if cached is not None:
            self.cache_hits += 1
            return cached

        try:
            payload = self.provider.generate_json(
                task=TASK,
                prompt="\n\n".join((
                    SYSTEM, self.build_prompt(atom=atom, candidates=usable),
                )),
                context={})
            self.call_count += 1
        except Exception as error:  # noqa: BLE001 - a fault selects nothing
            return SelectionOutcome(
                atom=text, considered=len(usable),
                error="PROVIDER_%s" % type(error).__name__)

        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = None
        if not isinstance(payload, Mapping):
            return SelectionOutcome(atom=text, considered=len(usable),
                                    error="UNREADABLE_REPLY")

        offered = {str(item.get("id")): item.get("id") for item in usable}
        verdicts: list[Selection] = []
        chosen: list[Any] = []
        for item in payload.get("selected") or ():
            if not isinstance(item, Mapping):
                continue
            raw = str(item.get("id"))
            if raw not in offered:
                # A candidate the selector was never shown cannot be selected.
                continue
            identifier = offered[raw]
            if identifier in chosen:
                continue
            chosen.append(identifier)
            verdicts.append(Selection(identifier, True,
                                      str(item.get("why") or "")[:200]))
        for item in usable:
            if item.get("id") not in chosen:
                verdicts.append(Selection(item.get("id"), False))

        outcome = SelectionOutcome(
            atom=text, considered=len(usable),
            selected_ids=tuple(chosen), verdicts=tuple(verdicts))
        self._cache[key] = outcome
        return outcome


def record(outcomes: Iterable[SelectionOutcome]) -> dict[str, Any]:
    """What selection did, written down beside the verification record.

    Nothing here decides publication. It exists so an operator can tell a
    candidate that was never considered from one the selector passed over and
    one the verifier rejected.
    """

    items = list(outcomes or ())
    return {
        "atoms": [item.to_dict() for item in items],
        "considered": sum(item.considered for item in items),
        "selected": sum(len(item.selected_ids) for item in items),
        "errors": [item.error for item in items if item.error],
    }
