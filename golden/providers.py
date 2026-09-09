"""The two provider stand-ins a deterministic replay needs, and the recorders.

Layer 1 (FAST) has to answer one question: *what did the code put in front of
GPT ②, and what did the deterministic layers do afterwards.* Any variability in
the model itself would show up as movement in those numbers and be
indistinguishable from a code change, so both provider roles are made
deterministic here.

``ReplaySemanticProvider``
    GPT ① replayed from the contract the production run stored. The full
    semantic payload is not persisted -- only the compacted understanding --
    so the missing fields are reconstructed conservatively and the
    reconstruction is *checked* against the stored retrieval trace by
    ``runner``. A case whose replay does not reproduce its own production
    numbers is reported as DRIFT rather than scored.

``PolicyDraftProvider``
    GPT ② replaced by a fixed, legible policy: adopt every candidate the
    pipeline attached to a sub-question, and report as unresolved every
    sub-question it attached nothing to. This is deliberately *not* a model of
    good judgement -- it is a constant, so that a change in what it adopts can
    only have come from a change in what retrieval delivered.

Everything that could reach the outside world is a recorder that counts and
raises, so "no side effects" is enforced rather than asserted.
"""
from __future__ import annotations

import json
from typing import Any

# Actions that mean the customer is asking *when*, used to rebuild the two
# schedule flags the stored contract does not carry. Kept in sync with
# ``semantic_analysis`` by the infrastructure test rather than by hand.
_SCHEDULE_ACTIONS = frozenset({
    "DELIVERY_STATUS", "INSTALLATION_SCHEDULE", "SCHEDULE_CHANGE",
    "SCHEDULE_REQUEST", "DELIVERY_DEADLINE_CONFIRMATION",
})


class ReplaySemanticProvider:
    """GPT ① replayed from the stored understanding contract."""

    name = "golden_semantic_replay"

    def __init__(self, understanding: dict[str, Any]) -> None:
        self.understanding = dict(understanding or {})
        self.calls = 0

    def reconstruction_notes(self) -> list[str]:
        """Where the replay had to fill a gap the storage does not record."""

        notes: list[str] = []
        if self.understanding.get("purchase_state") in (None, "", "UNKNOWN"):
            notes.append("PURCHASE_STATE_UNKNOWN_IN_STORAGE")
        notes.append("REQUIRES_ORDER_CONTEXT_DERIVED_FROM_NEED_ORDER")
        notes.append("ASKS_DELIVERY_SCHEDULE_DERIVED_FROM_ACTIONS")
        return notes

    def generate_json(self, *, task, prompt, context):
        self.calls += 1
        questions = [
            item for item in (self.understanding.get("questions") or [])
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ]
        actions = [str(item.get("action") or "OTHER").upper() for item in questions]
        schedule = bool(_SCHEDULE_ACTIONS & set(actions))
        need_dps = bool(self.understanding.get("need_dps"))
        need_order = bool(self.understanding.get("need_order"))
        return {
            "primary_action": actions[0] if actions else "OTHER",
            "secondary_actions": list(dict.fromkeys(actions[1:])),
            "request_type": "QUESTION",
            "objects": [],
            "atomic_questions": [
                {
                    "text": str(item.get("text") or ""),
                    "action": str(item.get("action") or "OTHER").upper(),
                    "requested_information": str(
                        item.get("requested_information") or ""
                    ),
                    "requested_attribute": str(
                        item.get("requested_attribute") or "UNKNOWN"
                    ).upper(),
                }
                for item in questions
            ],
            "deadline": None,
            "constraints": [],
            "negation": False,
            "conditional": False,
            # Not persisted. ``need_order``/``need_dps`` are what the contract
            # derived *from* them, so this inverts the derivation for the only
            # case where it is invertible and is conservative otherwise.
            "requires_order_context": need_order,
            "requires_delivery_schedule": need_dps,
            "asks_delivery_schedule": schedule or need_dps,
            "asks_delivery_outcome": schedule or need_dps,
            "purchase_state": str(
                self.understanding.get("purchase_state") or "UNKNOWN"
            ).upper(),
            "confidence": 0.95,
        }


class PolicyDraftProvider:
    """GPT ② as a constant policy, so retrieval changes are the only variable.

    It also captures the prompt, which is the actual measurement: the prompt is
    the complete record of what the code was willing to let the model see.
    """

    name = "golden_policy_draft"

    def __init__(self) -> None:
        self.captured: list[dict[str, Any]] = []
        self.calls = 0

    @property
    def drafts(self) -> list[dict[str, Any]]:
        return [
            item for item in self.captured
            if str(item.get("task", "")).upper() == "DRAFT"
        ]

    def generate_json(self, *, task, prompt, context):
        self.calls += 1
        self.captured.append({"task": task, "prompt": prompt})
        try:
            payload = json.loads(prompt)
        except (TypeError, ValueError):
            payload = {}
        evidence = ((payload.get("input") or {}).get("subquestion_evidence")) or []

        adopted_learning: list[int] = []
        adopted_historical: list[int] = []
        unresolved: list[str] = []
        results: list[dict[str, Any]] = []
        decisions: list[dict[str, Any]] = []
        for item in evidence:
            if not isinstance(item, dict):
                continue
            question = str(item.get("subquestion") or "")
            learning = [int(v) for v in (item.get("learning_ids") or [])]
            historical = [int(v) for v in (item.get("historical_case_ids") or [])]
            status = str(item.get("status") or "").upper()
            # The policy: evidence the pipeline attached is evidence the policy
            # uses. Everything else is unresolved. No judgement, by design.
            answerable = status in {"ANSWERABLE", "CANDIDATE"} and (
                learning or historical
            )
            if answerable:
                adopted_learning.extend(learning)
                adopted_historical.extend(historical)
                decisions.extend(
                    {"decision": "USED", "kind": kind, "id": value,
                     "matched_subquestion": question,
                     "reason": "POLICY_ADOPTS_ATTACHED_EVIDENCE"}
                    for kind, values in (("LEARNING", learning),
                                         ("HISTORICAL", historical))
                    for value in values
                )
            else:
                unresolved.append(question)
            results.append({
                "subquestion": question,
                "answered": bool(answerable),
                "status": "ANSWERABLE" if answerable else (status or "NO_RELIABLE_SOURCE"),
                "learning_ids": learning if answerable else [],
            })

        if not evidence:
            unresolved = []
            results = []

        answered = [item for item in results if item["answered"]]
        # A legible, non-asserted body. Golden never matches answer text, so
        # this exists to be a valid draft, not to be compared.
        answer = (
            "제공된 근거에 따라 안내드립니다."
            if answered
            else "확인이 필요한 사항이 있어 담당자 확인 후 안내드리겠습니다."
        )
        return {
            "answer": answer,
            "confidence": 0.9,
            "used_facts": [],
            "missing_information": list(unresolved),
            "requires_review": bool(unresolved),
            "warnings": [],
            "learning_usage": [
                {"learning_id": value, "answer_supported": True,
                 "matched_subquestion": next(
                     (str(i.get("subquestion") or "") for i in evidence
                      if isinstance(i, dict) and value in (i.get("learning_ids") or [])),
                     "",
                 ),
                 "reason": "POLICY_ADOPTS_ATTACHED_EVIDENCE"}
                for value in adopted_learning
            ],
            "historical_usage": [
                {"historical_case_id": value, "answer_supported": True,
                 "matched_subquestion": next(
                     (str(i.get("subquestion") or "") for i in evidence
                      if isinstance(i, dict)
                      and value in (i.get("historical_case_ids") or [])),
                     "",
                 ),
                 "reason": "POLICY_ADOPTS_ATTACHED_EVIDENCE"}
                for value in adopted_historical
            ],
            "subquestion_results": results,
            "evidence_decisions": decisions,
            "used_template_ids": [],
            "used_product_facts": [],
            "used_learning_ids": sorted(set(adopted_learning)),
            "used_historical_ids": sorted(set(adopted_historical)),
            "ignored_evidence": [],
            "unresolved": unresolved,
            "can_auto_post": not unresolved,
            "reason": "GOLDEN_LAYER1_POLICY",
        }


class CapturingProvider:
    """Wraps a real provider so FULL mode measures the same prompt FAST does."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.name = getattr(inner, "name", "openai")
        self.captured: list[dict[str, Any]] = []
        self.calls = 0

    @property
    def drafts(self) -> list[dict[str, Any]]:
        return [
            item for item in self.captured
            if str(item.get("task", "")).upper() == "DRAFT"
        ]

    def generate_json(self, *, task, prompt, context):
        self.calls += 1
        raw = self.inner.generate_json(task=task, prompt=prompt, context=context)
        self.captured.append({"task": task, "prompt": prompt, "raw": raw})
        return raw


class BlockedOrderLookup:
    """Order lookup must never run: it is a live Naver read."""

    def __init__(self) -> None:
        self.calls = 0

    def lookup_for_inquiry(self, *_args: Any, **_kwargs: Any):
        self.calls += 1
        raise AssertionError("golden replay must not perform an order lookup")


def blocked_dps(database: Any) -> Any:
    """The real DPS service with its one outward call removed.

    Substituting the whole service would also remove ``policy.decide`` and
    ``skip_for_phase9``, which are pure decisions the replay is *measuring* --
    routing correctness is one of the metrics. So the real service is used and
    only ``enrich`` (the part that drives a desktop session) is blocked.
    """

    from services.dps_enrichment_service import DpsEnrichmentService

    def _blocked_client(*_args: Any, **_kwargs: Any):
        raise AssertionError("golden replay must not perform a DPS lookup")

    service = DpsEnrichmentService(database, client=_blocked_client)
    service.golden_calls = 0

    def _enrich(*_args: Any, **_kwargs: Any):
        service.golden_calls += 1
        raise AssertionError("golden replay must not perform a DPS lookup")

    service.enrich = _enrich  # type: ignore[method-assign]
    return service


class NaverPostRecorder:
    """Stands where the HTTP write would be, and counts instead of sending."""

    def __init__(self) -> None:
        self.calls = 0
        self.payloads: list[Any] = []

    def send(self, request: Any, *, access_token: str) -> Any:
        self.calls += 1
        self.payloads.append(request)
        raise AssertionError("golden replay must not post to Naver")
