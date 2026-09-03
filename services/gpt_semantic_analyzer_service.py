"""Ask the model what the customer wants before routing, never blindly.

Semantic-first routing runs one bounded understanding call for each non-empty
inquiry while its feature flag is enabled.  The result is reused for the plan,
RULE compatibility and final coverage; retries and reruns use the small
in-process cache rather than paying for a second interpretation.

Merging this into an existing call was investigated and is not available.
Instrumenting the pipeline showed exactly one GPT task is ever issued today --
``DRAFT`` -- and it runs *after* routing, order lookup and DPS have already been
decided. A question's meaning has to be known before those choices, so folding
it into ``DRAFT`` would deliver the understanding one stage too late. (The
repository still contains an ``UNDERSTANDING`` task; it is constructed and never
called, and the read-timeout comment in the provider settings records that it
timed out in production when it was.) So this is a separate call, kept small
enough to be worth its own round trip: a bare question, a closed vocabulary and
a JSON object with no prose in it.

Failure is ordinary here, not exceptional. Timeout, transport error, malformed
JSON, an action outside the closed set, a confidence the model is not sure of --
each returns an unusable value, and every caller is expected to carry on down
the path it would have taken anyway. Nothing in the pipeline may wait on this or
stop because of it.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from typing import Any, Mapping

from answer.providers.interfaces import JsonGptProvider
from services.semantic_analysis import (
    ACTIONS,
    OBJECT_STATES,
    PURCHASE_STATES,
    REQUEST_TYPES,
    SemanticAnalysis,
    SemanticAnalysisError,
    parse,
    unavailable,
)


TASK = "SEMANTIC_ANALYSIS"

# A model that is unsure has told us the one thing we needed: do not act on it.
MINIMUM_CONFIDENCE = 0.7

# The ceiling on the prompt, in characters, paid on every semantic call.
#
# Derived, not chosen. The header, the field contract and the inquiry slot cost
# 1101 with every sentence of policy prose removed; the rules for
# purchase_state, asks_delivery_schedule and asks_delivery_outcome -- the three
# fields the purchase-state safety policy reads -- cannot be stated in what is
# left under a smaller number. Measured length is 2272, so this leaves 128
# characters: enough for another ACTION or two, not enough to explain anything
# twice. Raise it only with the same kind of measurement.
PROMPT_BUDGET = 2400

# The prompt is the contract. No examples, no prose, no "explain your
# reasoning" -- every token here is paid on every semantic call, and the output
# is consumed by a parser, not read by a person.
_PROMPT = """Classify the customer inquiry. Reply with one JSON object only.

ACTION={actions}
STATE={states}
primary_action: one ACTION; secondary_actions: ACTION list, may be []
request_type: {request_types}
objects: [{{"type": noun, "states": STATE subset}}]
atomic_questions: [{{"text": str, "action": ACTION,
  "requested_information": noun phrase naming the fact wanted}}], keep every one
deadline: date/period the customer requires, else null
constraints: str list, may be []
negation, conditional, requires_order_context, requires_delivery_schedule: bool
purchase_state: {purchase_states}
asks_delivery_schedule, asks_delivery_outcome: bool
confidence: 0..1

purchase_state is what the message SAYS, never inferred. CURRENT_ORDER only if
it states a purchase made (ordered/paid/bought, an order number, an awaited
delivery); PRE_PURCHASE only if it states none yet or asks conditionally; else
UNKNOWN -- "배송 언제 되나요?" is UNKNOWN.
asks_delivery_outcome: asks about RECEIVING it -- when, how long, by a date, or
at all. asks_delivery_schedule: the subset asking WHEN (date, duration, whether
a given day works); schedule true implies outcome true. Both false for how
delivery or installation is performed, its cost, or what is included.
"지금 구매하면 정상적으로 받을 수 있나요?" outcome only; "설치는 기사님이
해주시나요?" both false.

requested_information is the missing fact, not the topic ("신청 방법" vs
"지급 시점"). Action is what customer wants, not object state: broken-TV
pickup=COLLECTION. Requesting a date=SCHEDULE_REQUEST; asking an existing
date=INSTALLATION_SCHEDULE or DELIVERY_STATUS. Campaign is context. Order
ID/other item=ORDER_IDENTIFICATION (requires_order_context=true). A
registration/application field value or entry is FORM_FIELD_GUIDANCE, never
BENEFIT. Delivery schedule=current order only. Use OTHER if no ACTION fits.
Never invent one.

INQUIRY:
{question}"""


def _fingerprint(question: str) -> str:
    return hashlib.sha256(
        " ".join(str(question or "").split()).lower().encode("utf-8")
    ).hexdigest()


class GptSemanticAnalyzerService:
    """One small GPT call, only when the trigger says it is worth making."""

    def __init__(
        self,
        provider: JsonGptProvider,
        *,
        cache_size: int = 256,
        minimum_confidence: float = MINIMUM_CONFIDENCE,
    ) -> None:
        self.provider = provider
        self.minimum_confidence = float(minimum_confidence)
        self._cache: OrderedDict[str, SemanticAnalysis] = OrderedDict()
        self._cache_size = max(0, int(cache_size))
        # Observability for the shadow report: how often the pipeline paid for
        # a call, and how often it did not have to.
        self.call_count = 0
        self.cache_hits = 0
        self.last_trace: dict[str, Any] = {}

    def build_prompt(self, question: object) -> str:
        return _PROMPT.format(
            actions="|".join(sorted(ACTIONS)),
            request_types="|".join(sorted(REQUEST_TYPES)),
            states="|".join(sorted(OBJECT_STATES)),
            purchase_states="|".join(sorted(PURCHASE_STATES)),
            question=" ".join(str(question or "").split())[:1200],
        )

    def analyze(self, question: object) -> SemanticAnalysis:
        """Understand the question, or return something explicitly unusable."""

        text = " ".join(str(question or "").split())
        if not text:
            self.last_trace = {"cache_hit": False, "latency_ms": 0.0,
                               "outcome": "EMPTY_QUESTION"}
            return unavailable("EMPTY_QUESTION")

        key = _fingerprint(text)
        cached = self._cache.get(key)
        if cached is not None:
            # A regeneration, a Streamlit rerun and a retry all ask the same
            # question again. None of them is a new question.
            self._cache.move_to_end(key)
            self.cache_hits += 1
            self.last_trace = {
                "cache_hit": True, "latency_ms": 0.0, "outcome": "CACHE_HIT",
                "fingerprint": key[:16],
            }
            return cached

        started = time.monotonic()
        outcome = "OK"
        try:
            raw = self.provider.generate_json(
                task=TASK, prompt=self.build_prompt(text), context={},
            )
            result = parse(raw)
            if result.confidence < self.minimum_confidence:
                outcome = "LOW_CONFIDENCE"
                result = unavailable(
                    f"LOW_CONFIDENCE:{result.confidence:.2f}"
                )
        except SemanticAnalysisError as error:
            outcome = "INVALID_OUTPUT"
            result = unavailable(f"INVALID_OUTPUT:{error}")
        except Exception as error:  # noqa: BLE001 - transport faults vary
            # Timeout, connection reset, auth, rate limit, a provider raising
            # something new. The pipeline must not care which: an inquiry is
            # never left unanswered because this stage failed.
            outcome = "PROVIDER_ERROR"
            result = unavailable(f"PROVIDER_ERROR:{type(error).__name__}")
        latency_ms = round((time.monotonic() - started) * 1000, 1)

        self.call_count += 1
        self.last_trace = {
            "cache_hit": False,
            "latency_ms": latency_ms,
            "outcome": outcome,
            "fingerprint": key[:16],
            "confidence": result.confidence if result.usable else None,
        }
        # Only a real understanding is worth remembering. Caching a failure
        # would turn one timeout into a permanently wrong answer for that
        # question.
        if result.usable and self._cache_size:
            self._cache[key] = result
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return result

    def prompt_size(self, question: object) -> int:
        return len(self.build_prompt(question))


def shadow_record(
    *,
    question: object,
    decision: Mapping[str, Any],
    analysis: Mapping[str, Any] | None,
    semantic: SemanticAnalysis,
    trace: Mapping[str, Any],
) -> dict[str, Any]:
    """What the shadow run writes down, for comparison against the classifier.

    Deliberately a plain dict placed in the draft's existing metadata JSON.
    No table, no column, no migration: this is a diagnostic that has to be
    cheap to add and cheap to remove once it has answered its question.
    """

    analysis = analysis or {}
    existing_action = str(
        analysis.get("detected_intent") or "UNKNOWN"
    ).upper()
    semantic_action = semantic.primary_action if semantic.usable else None
    return {
        "existing_intent": existing_action,
        "existing_subtype": str(analysis.get("inquiry_subtype") or "").upper(),
        "existing_confidence": analysis.get("confidence"),
        "semantic_action": semantic_action,
        "semantic_secondary": list(semantic.secondary_actions),
        "semantic_request_type": semantic.request_type if semantic.usable else None,
        "semantic_objects": [item.to_dict() for item in semantic.objects],
        "semantic_atomic_questions": [
            item.to_dict() for item in semantic.atomic_questions
        ],
        "semantic_deadline": semantic.deadline,
        "semantic_confidence": semantic.confidence if semantic.usable else None,
        "semantic_source": semantic.source,
        "semantic_reason": semantic.reason,
        "semantic_used": bool(decision.get("use_semantic")),
        "semantic_trigger_reasons": list(decision.get("reasons") or []),
        "semantic_latency_ms": trace.get("latency_ms"),
        "semantic_cache_hit": bool(trace.get("cache_hit")),
        "semantic_outcome": trace.get("outcome"),
        # Agreement is only meaningful when both sides produced something.
        "agreement": (
            None if semantic_action is None
            else _agrees(existing_action, semantic_action)
        ),
    }


# How the classifier's intent vocabulary lines up with the action vocabulary.
# Only used to score the shadow report -- nothing routes on it.
_EQUIVALENT: dict[str, frozenset[str]] = {
    "DELIVERY_DATE": frozenset({"DELIVERY_STATUS", "INSTALLATION_SCHEDULE"}),
    "DELIVERY_STATUS": frozenset({"DELIVERY_STATUS"}),
    "INSTALLATION_DATE": frozenset({"INSTALLATION_SCHEDULE"}),
    "SCHEDULE_CHANGE": frozenset({"SCHEDULE_CHANGE", "SCHEDULE_REQUEST"}),
    "PRE_PURCHASE_DELIVERY": frozenset({
        "DELIVERY_POLICY", "DELIVERY_DEADLINE_CONFIRMATION",
    }),
    "NOTIFICATION_POLICY": frozenset({"NOTIFICATION_POLICY"}),
    "PRODUCT_COMPATIBILITY": frozenset({"INSTALLATION_METHOD", "PRODUCT_SPEC"}),
}


def _agrees(existing_intent: str, semantic_action: str) -> bool:
    return semantic_action in _EQUIVALENT.get(existing_intent, frozenset())
