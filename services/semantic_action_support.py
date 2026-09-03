"""Does the answer we produced address the action the customer asked for?

Three gates looked at "고장난 기존 tv 수거 요청드려요" and each said fine.

    validator   PASS      -- the A/S sentence is true and carries no risk
    coverage    PASS      -- question and answer both anchor on 고장
    eligibility SAFE      -- see below

and it was auto-posted with the Samsung service-centre number, to a customer
asking us to take their old television away. Every gate was right by its own
definition. None of them asks whether the answer answers the question.

The store was not even missing the answer. ``_old_appliance_pickup`` matches
"수거" and returns the collection guidance -- but ``_install_common_info`` runs
first in the engine and fires on 고장, so the branch that had the right answer
never ran. A word describing the *object* outranked the customer's *action*.

So the comparison here is action against action:

    question action     from the semantic analyzer -- what was asked for
    answer support      from the identity of the answer we produced

The answer side is deliberately not read out of the answer text. It comes from
the label the rule engine already stamps on its own output ("폐가전수거",
"설치상품/공통안내"), which is a name *we* chose for *our* template. That is the
whole point: a table over our own templates does not grow when a customer
invents a new phrasing, which is exactly what the anchor tables had to do.

An answer whose label is not in the table yields no verdict at all. Silence is
the only safe default -- a gate that guessed would block real answers, and this
one can only ever add a hold, never remove one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from services.semantic_analysis import (
    ACTIONS,
    BENEFIT,
    COLLECTION,
    DELIVERY_POLICY,
    DELIVERY_STATUS,
    FORM_FIELD_GUIDANCE,
    INSTALLATION_METHOD,
    INSTALLATION_SCHEDULE,
    NOTIFICATION_POLICY,
    ORDER_IDENTIFICATION,
    PACKAGE_CONTENTS,
    PRODUCT_CONCEPT,
    PRODUCT_SPEC,
    REPAIR,
    SCHEDULE_CHANGE,
    SCHEDULE_QUESTION_ACTIONS,
    SCHEDULE_REQUEST,
    STORE_PICKUP,
    SemanticAnalysis,
)


MISMATCH = "MISMATCH"
COMPATIBLE = "COMPATIBLE"
UNDETERMINED = "UNDETERMINED"

REASON_CODE = "SEMANTIC_ACTION_MISMATCH"


# What each of our own answers is able to address.
#
# Keyed on the rule engine's category, which reaches the draft as
# ``template_id``. Only labels whose content is unambiguous are listed; the
# rest are absent on purpose and produce UNDETERMINED.
ANSWER_ACTION_SUPPORT: dict[str, frozenset[str]] = {
    # The collection answer, which is what the reported inquiry should have
    # received.
    "폐가전수거": frozenset({COLLECTION}),
    "배송/설치신규+폐가전": frozenset({COLLECTION, DELIVERY_POLICY,
                                        INSTALLATION_SCHEDULE}),
    # 설치기사 / 정품 / A/S 안내. It answers a repair question and an
    # installation-method question. It does not answer a collection request,
    # which is the substitution that reached a customer.
    "설치상품/공통안내": frozenset({REPAIR, INSTALLATION_METHOD,
                                    PRODUCT_CONCEPT}),
    "배송/설치신규": frozenset({DELIVERY_POLICY, INSTALLATION_SCHEDULE}),
    "배송/택배": frozenset({DELIVERY_POLICY}),
    "배송/설치기존+해피콜": frozenset({NOTIFICATION_POLICY,
                                       INSTALLATION_SCHEDULE}),
    "배송/설치일조율": frozenset({SCHEDULE_REQUEST, SCHEDULE_CHANGE,
                                  INSTALLATION_SCHEDULE}),
    "배송/설치일변경요청": frozenset({SCHEDULE_CHANGE, SCHEDULE_REQUEST}),
    "방문수령/설치상품": frozenset({STORE_PICKUP}),
    "방문수령/배송유형확인": frozenset({STORE_PICKUP}),
    "상품구성": frozenset({PACKAGE_CONTENTS}),
    "제품정보/구성품확인": frozenset({PACKAGE_CONTENTS}),
    "제품사용": frozenset({PRODUCT_CONCEPT, PRODUCT_SPEC}),
    "방송시청": frozenset({PRODUCT_CONCEPT}),
    # Which model the stand is. It answers a question about the product and
    # says nothing about whether one arrived, which is how it came to answer
    # "오베닉 스마트마운트 스탠드가 안왔어요".
    "스탠드모델": frozenset({PRODUCT_SPEC, PRODUCT_CONCEPT}),
    "스탠드호환": frozenset({INSTALLATION_METHOD, PRODUCT_SPEC}),
    "스탠드사용법": frozenset({INSTALLATION_METHOD, PRODUCT_CONCEPT}),
    "배터리호환": frozenset({PRODUCT_SPEC, PACKAGE_CONTENTS}),
}

# Routes that identify what an answer is about even when no template label
# does. A DPS schedule answer states a date; an order-id request states
# nothing at all and asks the customer for a number.
ROUTE_ACTION_SUPPORT: dict[str, frozenset[str]] = {
    # A confirmed date answers every way of asking when, so this reads the
    # shared definition rather than repeating a subset of it. Listing only
    # DELIVERY_STATUS and INSTALLATION_SCHEDULE here made a correct DPS answer
    # look like a mismatch whenever the analyzer labelled the question
    # SCHEDULE_REQUEST, which is how "언제설치가능한가요?" came back.
    "DELIVERY_WITH_INSTALLATION_DATE": SCHEDULE_QUESTION_ACTIONS,
    "PRODUCT_DB": frozenset({PRODUCT_SPEC, PRODUCT_CONCEPT}),
}

# ``template_id`` is a human-facing category and legacy templates do not all
# have a stable identifier.  The rule engine does, however, stamp every fixed
# candidate with an ASCII match kind.  This is deliberately a small allow-list:
# it lets semantic routing reject a known wrong candidate without guessing what
# an unlabelled rule means.
MATCH_KIND_ACTION_SUPPORT: dict[str, frozenset[str]] = {
    "FIXED_EVENT_ONNURI": frozenset({BENEFIT}),
    "FIXED_EVENT_REVIEW": frozenset({BENEFIT, NOTIFICATION_POLICY}),
    "FIXED_POLICY_SHIPPING": frozenset({
        DELIVERY_POLICY, DELIVERY_STATUS, INSTALLATION_SCHEDULE,
        SCHEDULE_REQUEST,
    }),
    "FIXED_POLICY_INSTALL": frozenset({
        INSTALLATION_METHOD, PRODUCT_CONCEPT, REPAIR,
    }),
    "FIXED_POLICY_PICKUP": frozenset({COLLECTION}),
    "FIXED_POLICY_STORE_PICKUP": frozenset({STORE_PICKUP}),
    "FIXED_PRODUCT_ACCESSORY": frozenset({
        PRODUCT_SPEC, PRODUCT_CONCEPT, PACKAGE_CONTENTS,
    }),
    "FIXED_PACKAGE_CODE": frozenset({PRODUCT_SPEC, PACKAGE_CONTENTS}),
    "PRODUCT_DB_MODEL_CODE": frozenset({PRODUCT_SPEC}),
}

# An answer that asserts nothing about the customer's request cannot
# contradict it. Asking for the order number is the pipeline's safe reply to a
# missing order id, and blocking it would leave the customer with no reply.
NO_VERDICT_ROUTES = frozenset({"ORDER_ID_REQUEST"})

# Model-composed prose carries no label we control, so nothing here can say
# what it addresses. Left undetermined rather than guessed at; the deadline
# case is held by its own hard reason regardless of route.
UNLABELLED_ROUTES = frozenset({"GPT_FALLBACK", "GPT_DIRECT"})


@dataclass(frozen=True)
class ActionSupportDecision:
    status: str
    question_action: str | None = None
    answer_actions: tuple[str, ...] = ()
    answer_label: str | None = None
    reason: str = ""

    @property
    def mismatched(self) -> bool:
        return self.status == MISMATCH

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "question_action": self.question_action,
            "answer_actions": list(self.answer_actions),
            "answer_label": self.answer_label,
            "reason": self.reason,
        }


def answer_support(
    *, route: object, template_id: object, match_kind: object = None,
) -> tuple[frozenset[str] | None, str | None]:
    """The actions the produced answer can address, if we can name it."""

    normalized_route = str(route or "").upper()
    if normalized_route in NO_VERDICT_ROUTES:
        return None, normalized_route
    label = str(template_id or "").strip()
    if label and label in ANSWER_ACTION_SUPPORT:
        return ANSWER_ACTION_SUPPORT[label], label
    kind = str(match_kind or "").strip().upper()
    if kind in MATCH_KIND_ACTION_SUPPORT:
        return MATCH_KIND_ACTION_SUPPORT[kind], kind
    supported = ROUTE_ACTION_SUPPORT.get(normalized_route)
    if supported is not None:
        return supported, normalized_route
    return None, (label or normalized_route or None)


def evaluate(
    semantic: SemanticAnalysis | None,
    *,
    route: object,
    template_id: object,
    match_kind: object = None,
) -> ActionSupportDecision:
    """Compare what was asked against what the answer can address.

    Returns UNDETERMINED whenever either side is unknown. Only a positive
    disagreement -- a known question action that a known answer cannot address
    -- produces MISMATCH.
    """

    if semantic is None or not semantic.usable:
        return ActionSupportDecision(
            UNDETERMINED, reason="NO_SEMANTIC_ANALYSIS",
        )
    asked = semantic.primary_action
    if asked not in ACTIONS or asked == "OTHER":
        # "I could not tell" is not a disagreement.
        return ActionSupportDecision(
            UNDETERMINED, question_action=asked, reason="QUESTION_ACTION_UNKNOWN",
        )

    supported, label = answer_support(
        route=route, template_id=template_id, match_kind=match_kind,
    )
    if supported is None:
        return ActionSupportDecision(
            UNDETERMINED, question_action=asked, answer_label=label,
            reason="ANSWER_ACTION_UNKNOWN",
        )

    # Every action the customer raised, not only the first: an answer that
    # covers one half of a compound request still leaves the other half
    # unanswered, and the pipeline should hear about that from a person.
    asked_actions = {asked, *semantic.secondary_actions}
    unaddressed = asked_actions - supported
    if not unaddressed:
        return ActionSupportDecision(
            COMPATIBLE, question_action=asked,
            answer_actions=tuple(sorted(supported)), answer_label=label,
            reason="ANSWER_ADDRESSES_ALL_REQUESTED_ACTIONS",
        )
    if asked in supported:
        # The primary action is covered, but a compound inquiry still has an
        # unaddressed core action.  A partial fixed rule must not be treated as
        # an answer to the whole inquiry.
        return ActionSupportDecision(
            MISMATCH, question_action=asked,
            answer_actions=tuple(sorted(supported)), answer_label=label,
            reason=(
                "SECONDARY_ACTION_UNADDRESSED_"
                + "/".join(sorted(unaddressed))
            ),
        )
    return ActionSupportDecision(
        MISMATCH, question_action=asked,
        answer_actions=tuple(sorted(supported)), answer_label=label,
        reason=f"{asked}_VS_{'/'.join(sorted(supported)) or 'NONE'}",
    )


def decision_from_metadata(
    metadata: Mapping[str, Any] | None,
) -> ActionSupportDecision:
    """Read a verdict the generation stage already recorded, if any.

    Eligibility runs long after generation and has no provider of its own, so
    it consumes what was written down rather than re-deriving it. A draft with
    nothing recorded is undetermined, which blocks nothing.
    """

    metadata = metadata or {}
    value = metadata.get("semantic_action_support")
    if not isinstance(value, Mapping):
        return ActionSupportDecision(UNDETERMINED, reason="NOT_RECORDED")
    status = str(value.get("status") or UNDETERMINED).upper()
    if status not in {MISMATCH, COMPATIBLE, UNDETERMINED}:
        return ActionSupportDecision(UNDETERMINED, reason="UNREADABLE_RECORD")
    return ActionSupportDecision(
        status,
        question_action=value.get("question_action"),
        answer_actions=tuple(
            str(item) for item in (value.get("answer_actions") or [])
        ),
        answer_label=value.get("answer_label"),
        reason=str(value.get("reason") or ""),
    )
