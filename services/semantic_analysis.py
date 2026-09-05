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
# What arrived is not what was ordered -- an item is absent. Distinct from
# DAMAGE_REPORT (it arrived broken) and from PACKAGE_CONTENTS (asking what
# is included). "스탠드가 안왔어요" is neither of those, and with no class to
# put it in it was answered as a question about the stand.
MISSING_ITEM_REPORT = "MISSING_ITEM_REPORT"
NOTIFICATION_POLICY = "NOTIFICATION_POLICY"
ORDER_IDENTIFICATION = "ORDER_IDENTIFICATION"
FORM_FIELD_GUIDANCE = "FORM_FIELD_GUIDANCE"
STORE_PICKUP = "STORE_PICKUP"
OTHER = "OTHER"

ACTIONS: frozenset[str] = frozenset({
    COLLECTION, REPAIR, DELIVERY_STATUS, DELIVERY_DEADLINE_CONFIRMATION,
    DELIVERY_POLICY, SCHEDULE_REQUEST, SCHEDULE_CHANGE, INSTALLATION_METHOD,
    INSTALLATION_SCHEDULE, PRODUCT_SPEC, PRODUCT_CONCEPT, PACKAGE_CONTENTS,
    BENEFIT, CANCEL_RETURN, DAMAGE_REPORT, MISSING_ITEM_REPORT,
    NOTIFICATION_POLICY,
    ORDER_IDENTIFICATION, FORM_FIELD_GUIDANCE, STORE_PICKUP, OTHER,
})

# An object's condition, which is never an action. This is the slot 고장 was
# missing: with it, "고장난 TV 수거" is a BROKEN object with a COLLECTION
# action, and the anchor table's tie between 고장 and 수거 stops existing.
OBJECT_STATES: frozenset[str] = frozenset({
    "BROKEN", "OLD", "EXISTING", "NEW", "DAMAGED", "UNOPENED", "INSTALLED",
    # The object never arrived. A state, not an action: the customer may be
    # reporting it, asking when it will come, or asking to cancel it.
    "NOT_RECEIVED",
})

REQUEST_TYPES: frozenset[str] = frozenset({
    "QUESTION", "ACTION_REQUEST", "COMPLAINT", "MIXED",
})

# Whether the customer already has an order, as an *observation* rather than an
# inference.
#
# The action vocabulary could not carry this. Measured on 40 real inquiries,
# every pre-purchase and every already-ordered delivery question was classified
# correctly -- and all six inquiries where the text genuinely does not say
# ("배송 언제 되나요??", "언제 받을수 있나요?") were still assigned a definite
# action at confidence 0.9 or above. The model was not unsure; it was answering
# a question that has no answer in the text. Raising the confidence threshold
# cannot fix that, because the confidence was never low.
#
# So this asks something different: not "has this customer ordered" but "does
# this message say so". UNKNOWN is the honest answer to most short delivery
# questions, and it is the default here -- a missing or unrecognised value
# reads as UNKNOWN, never as a purchase.
PRE_PURCHASE = "PRE_PURCHASE"
CURRENT_ORDER = "CURRENT_ORDER"
UNKNOWN_PURCHASE_STATE = "UNKNOWN"
PURCHASE_STATES: frozenset[str] = frozenset({
    PRE_PURCHASE, CURRENT_ORDER, UNKNOWN_PURCHASE_STATE,
})

# Which *property* of the subject the customer wants, as distinct from the
# subject itself.
#
# ``requested_information`` already names the missing fact, and that separated
# three gift-certificate questions that share one action. It does not separate
# a question from an answer about the same fact under a different property.
# Two measured cases:
#
#   "사다리차가 필요하면 비용은 누가 내나요?"  answered by "사다리차는 유상입니다"
#   "설치 기사님 안 부르고 받아만 볼 수 있나요?" answered by "기사님이 배송 후 설치합니다"
#
# Both stored answers are about exactly the right subject, and both leave the
# asked property unstated -- who bears the cost, and whether declining is
# allowed. Every selector built against topic or subject accepted them.
#
# Splitting the question into more facts does not help and costs recall: the
# subject is already single. What is missing is the property being asked, so
# evidence stating a *neighbouring* property of the same subject can be told
# apart from evidence stating the asked one. UNKNOWN is the honest default and
# never licenses an answer on its own.
UNKNOWN_ATTRIBUTE = "UNKNOWN"
REQUESTED_ATTRIBUTES: frozenset[str] = frozenset({
    "EXISTENCE_OR_CAPABILITY",   # 있는지 / 되는지 / 지원하는지
    "PERMISSION_OR_OPTION",      # 해도 되는지 / 안 해도 되는지 / 선택 가능한지
    "ACTOR",                     # 누가 하는지 / 누가 부담하는지
    "AMOUNT_OR_COST",            # 얼마인지
    "TIMING",                    # 언제 / 얼마나 걸리는지
    "METHOD_OR_PROCEDURE",       # 어떻게 하는지
    "LOCATION_OR_CONTACT",       # 어디서 / 어디에 문의하는지
    "SPEC_VALUE",                # 규격 / 수치 / 입력값
    "COMPATIBILITY",             # 호환되는지
    "INCLUSION",                 # 포함되는지 / 같이 오는지
    "DIFFERENCE",                # 무엇이 다른지
    "ACTION_EXECUTION",          # 대신 처리해 달라 (정보 요구가 아니다)
    UNKNOWN_ATTRIBUTE,
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
    """One thing the customer asked, and what they want to know about it.

    ``action`` is what they want done; ``requested_information`` is the fact
    they are missing. The two are not the same, and retrieval needs the second
    one. Three questions about a gift certificate are all BENEFIT -- how to
    apply, whether an application went through, when the reward is paid -- and
    an answer to any of them was allowed to stand as evidence for the others
    because the action matched. Naming the requested fact separates them
    without a rule per topic.
    """

    text: str
    action: str
    requested_information: str = ""
    # Which property of the requested fact is being asked. Additive: a model
    # that does not emit it leaves UNKNOWN, and every existing consumer that
    # reads text/action/requested_information is unaffected.
    requested_attribute: str = UNKNOWN_ATTRIBUTE

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "action": self.action,
            "requested_information": self.requested_information,
            "requested_attribute": self.requested_attribute,
        }


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
    # What the inquiry *says* about whether an order exists. Defaults to
    # UNKNOWN so any caller reading an older value, or a provider that omits
    # the field, gets the state that grants nothing.
    purchase_state: str = UNKNOWN_PURCHASE_STATE
    # Whether the customer is asking *when* -- a date, a duration, or whether a
    # particular day is possible -- as opposed to how something works or what
    # it costs. The action vocabulary cannot carry this: "배송비는 얼마인가요?"
    # and "지금 주문하면 배송 얼마나 걸리나요?" are both DELIVERY_POLICY with no
    # deadline, and only one of them is a schedule question.
    asks_delivery_schedule: bool = False
    # The same question one step wider: is the customer asking about *getting
    # it* at all -- when, how long, by a date, or whether it will arrive as
    # they need -- rather than how it is carried out or what it costs.
    #
    # ``asks_delivery_schedule`` names only the timing half, and "지금 구매하면
    # 정상적으로 받을 수 있나요?" is the other half: it asks whether receipt
    # will happen, names no day and no duration, and came back false. Nothing
    # upstream then held it, and a fabricated "약 3~4주" was stopped only by
    # the publishing gate reading the finished answer.
    #
    # A superset, not a rival: ``parse`` forces it true whenever the timing
    # field is, so the two can never disagree and an older provider that omits
    # it keeps the exact behaviour it had.
    asks_delivery_outcome: bool = False
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
            "purchase_state": self.purchase_state,
            "asks_delivery_schedule": self.asks_delivery_schedule,
            "asks_delivery_outcome": self.asks_delivery_outcome,
            "confidence": self.confidence,
            "source": self.source,
            "reason": self.reason,
        }


# Delivery questions divide on one thing: does the customer already have an
# order? "지금 주문하면 며칠 걸려요?" and "제 주문 언제 와요?" share every
# delivery word and are opposite questions -- the first has no order to look
# up and no date that exists yet, the second is a lookup and nothing else.
#
# No new field was needed to tell them apart. Measured over 18 labelled real
# inquiries from the dev database (9 pre-purchase, 9 already ordered), the
# fields already on this dataclass separated them with no misclassification:
# a pre-purchase question carries a policy/deadline/request action with
# ``requires_order_context`` and ``requires_delivery_schedule`` both false,
# and an existing order carries a status/schedule action with them true.
# The model is told "Delivery schedule=current order only", which is what
# makes ``requires_delivery_schedule`` the reliable half of that pair.
PRE_PURCHASE_DELIVERY_ACTIONS: frozenset[str] = frozenset({
    DELIVERY_POLICY, DELIVERY_DEADLINE_CONFIRMATION, SCHEDULE_REQUEST,
})
CURRENT_ORDER_DELIVERY_ACTIONS: frozenset[str] = frozenset({
    DELIVERY_STATUS, INSTALLATION_SCHEDULE, SCHEDULE_CHANGE,
})

# Actions that are about a date whatever else is true of them. "금요일까지 받을
# 수 있나요" and "9월 5일에 배송해 주세요" name a date in the action itself;
# DELIVERY_POLICY does not, which is why it needs the separate observation --
# it covers "배송 얼마나 걸리나요" and "배송비는 얼마인가요" alike.
#
# Listing these keeps the rule working when the provider does not report
# ``asks_delivery_schedule`` at all, so an older understanding still routes a
# deadline question the way it always did.
INHERENT_SCHEDULE_ACTIONS: frozenset[str] = frozenset({
    *CURRENT_ORDER_DELIVERY_ACTIONS,
    DELIVERY_DEADLINE_CONFIRMATION,
    SCHEDULE_REQUEST,
})

# "When do I get it?", in every form the analyzer uses to say it. One meaning,
# named once, so the stages that have to agree about it read the same set
# instead of each keeping its own list.
#
# The four differ in framing, not in what would answer them: an existing date
# settles "제 설치 언제인가요", "언제 설치 가능한가요", "금요일까지 되나요" and
# "9월 5일에 해주세요" alike. The analyzer picks among them by phrasing --
# "언제설치가능한가요?" came back SCHEDULE_REQUEST -- and a stage that lists
# only two of the four then reads a correct DPS answer as addressing the wrong
# question. That is what held one.
#
# SCHEDULE_CHANGE is deliberately absent. It asks us to *move* the date, and
# telling the customer what the date currently is does not do that.
SCHEDULE_QUESTION_ACTIONS: frozenset[str] = frozenset({
    DELIVERY_STATUS, INSTALLATION_SCHEDULE,
    DELIVERY_DEADLINE_CONFIRMATION, SCHEDULE_REQUEST,
})


def delivery_schedule_question(semantic: SemanticAnalysis | None) -> bool:
    """Whether the customer is asking about *getting it* -- when, or whether.

    Not "does this mention delivery". "배송비는 얼마인가요?" mentions delivery and
    asks a price; "배송과 설치는 어떤 방식으로 진행되나요?" asks a procedure.
    Neither is about receiving it, and neither is held.

    The line that matters is outcome versus process, and it is wider than a
    date. "지금 구매하면 정상적으로 받을 수 있나요?" names no day and no
    duration, so the timing observation reported false and nothing upstream
    held it; what it asks is still whether this customer will receive an order
    that does not exist yet, which is the same thing the policy is about.
    ``asks_delivery_outcome`` is that observation, and it is a superset of the
    timing one.

    The remaining clauses keep older understandings working exactly as before:
    a delivery status or installation schedule action has always been a
    schedule question, and a provider that reports neither new field still
    routes as it did.
    """

    if semantic is None or not semantic.usable:
        return False
    return bool(
        semantic.asks_delivery_outcome
        or semantic.asks_delivery_schedule
        or semantic.requires_delivery_schedule
        or (semantic.actions & INHERENT_SCHEDULE_ACTIONS)
    )


def purchase_confirmed(
    semantic: SemanticAnalysis | None, *, order_id_validated: bool = False,
) -> bool:
    """Whether an order is known to exist -- never assumed into existence.

    Two independent kinds of evidence, and the absence of both is not evidence
    of the opposite:

      the message says so   the customer names the purchase, a payment, an
                            order number, or a delivery already under way
      the record carries it a validated order id is attached to this inquiry,
                            which proves the order regardless of the wording

    The second is not "judging by the order id alone" -- it is one of two
    sources, and the first is about meaning. "어제 주문했는데 언제 오나요?"
    carries no order number and is confirmed by the first; a bare "배송 언제
    되나요?" on an inquiry the platform attached to an order is confirmed by the
    second. A bare question with neither stays unconfirmed, which is the whole
    point: the pipeline must not decide that customer has ordered.
    """

    if order_id_validated:
        return True
    if semantic is None or not semantic.usable:
        return False
    return semantic.purchase_state == CURRENT_ORDER


def delivery_schedule_needs_review(
    semantic: SemanticAnalysis | None, *, order_id_validated: bool = False,
) -> bool:
    """A schedule question with no confirmed order behind it.

    Covers both confirmed policies with one rule, because they are one
    situation: there is no schedule to report. A pre-purchase customer has no
    order yet, and an ambiguous one may not either -- and answering the second
    by demanding an order number is the same mistake as inventing a delivery
    period for the first.
    """

    return delivery_schedule_question(semantic) and not purchase_confirmed(
        semantic, order_id_validated=order_id_validated
    )


def is_pre_purchase_delivery(semantic: SemanticAnalysis | None) -> bool:
    """Whether this is a delivery question from a customer who has not ordered.

    Every clause has to hold, and each rules out a different way of getting
    this wrong:

      usable              no understanding is not evidence of anything. An
                          unavailable analysis returns False, so a provider
                          timeout can never route an inquiry into the
                          pre-purchase policy by default
      a delivery action   the question is about delivery at all
      no current action   SCHEDULE_REQUEST beside INSTALLATION_SCHEDULE is a
                          customer moving an existing appointment
      no schedule needed  ``requires_delivery_schedule`` means a date that
                          already exists, which only an order has
      no order context    the customer has not put their own purchase in play

    Deliberately not read from the order id. "어제 주문했는데 언제 오나요?"
    carries no order number and is an existing order; "지금 주문하면 언제
    와요?" carries none because there is nothing to carry.
    """

    if semantic is None or not semantic.usable:
        return False
    actions = semantic.actions
    if not actions & PRE_PURCHASE_DELIVERY_ACTIONS:
        return False
    if actions & CURRENT_ORDER_DELIVERY_ACTIONS:
        return False
    return not (
        semantic.requires_delivery_schedule or semantic.requires_order_context
    )


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


def _purchase_state(value: object) -> str:
    """Read the reported state, defaulting to the one that grants nothing.

    Unrecognised input is UNKNOWN rather than an error: this field was added
    after the fact, and an older provider that omits it must keep working --
    it simply never claims an order exists.
    """

    state = str(value or "").strip().upper()
    return state if state in PURCHASE_STATES else UNKNOWN_PURCHASE_STATE


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
        # Unrecognised or absent reads as UNKNOWN rather than raising: the
        # field is additive and an older model response must keep parsing.
        attribute = str(item.get("requested_attribute") or "").strip().upper()
        if attribute not in REQUESTED_ATTRIBUTES:
            attribute = UNKNOWN_ATTRIBUTE
        atomic.append(AtomicQuestion(
            text=text, action=action,
            requested_information=_text(item.get("requested_information"), 80),
            requested_attribute=attribute,
        ))

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
    asks_schedule = bool(raw.get("asks_delivery_schedule"))

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
        purchase_state=_purchase_state(raw.get("purchase_state")),
        asks_delivery_schedule=asks_schedule,
        # Asking when is one way of asking whether you get it, so the wider
        # field can never be false while the narrower one is true.
        asks_delivery_outcome=(
            asks_schedule or bool(raw.get("asks_delivery_outcome"))
        ),
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
# The customer is not asking about the product -- they are telling us something
# went wrong with their order. The three triggers above all detect a classifier
# that is unsure; this one exists because "오베닉 스마트마운트 스탠드가
# 안왔어요" was classified confidently (0.94) and answered with a description of
# the stand's model line. Confidence was never the problem.
#
# The absence marker has to sit next to something that was ordered, or an
# ordinary usage question ("전원 버튼을 찾을 수 없네요") would look like a
# delivery complaint.
# "안 왔다" says something about delivery and nothing else. Paired with
# anything that was ordered, it is a report.
_DID_NOT_ARRIVE = re.compile(
    r"안왔|안옴|못받|미수령|누락|빠[졌진]|안들어있|들어있지않|안들었"
    r"|동봉안|미배송|미발송|안보내|오지않"
)
# "없어요" is not about delivery at all -- "전원 버튼을 찾을 수 없네요" is a
# usage question. It only reads as a missing item beside a part of the order
# that can be counted, never beside a bare "제품".
_ABSENT = re.compile(r"없어요|없습니다|없네요|없던데|안보[여이]")
_ORDERED_THING = re.compile(
    r"스탠드|스텐드|거치대|리모컨|리모콘|케이블|브라켓|사은품|구성품|부속|부품"
    r"|어댑터|아답터|전원선|받침|나사|볼트|설명서|배터리|마운트|선반|멀티탭"
    r"|셋탑|셋톱|옵션|상품|제품|물건|티비|tv|모니터|본체"
)
_COMPONENT = re.compile(
    r"스탠드|스텐드|거치대|리모컨|리모콘|케이블|브라켓|사은품|구성품|부속|부품"
    r"|어댑터|아답터|전원선|받침|나사|볼트|설명서|배터리|마운트|선반|멀티탭"
    r"|셋탑|셋톱|옵션"
)

TRIGGER_STATE_ACTION_CONFLICT = "STATE_AND_ACTION_COMPETE"
TRIGGER_UNCLASSIFIED = "CLASSIFIER_HAS_NO_ACTION"
TRIGGER_DEADLINE = "DEADLINE_CONSTRAINT"
TRIGGER_ORDER_PROBLEM = "ORDER_PROBLEM_REPORTED"
TRIGGER_CONTEXT_ACTION_CONFLICT = "CONTEXT_AND_QUESTION_COMPETE"
TRIGGER_COMPOUND_EVIDENCE = "COMPOUND_EVIDENCE_REQUIREMENTS"
TRIGGER_SEMANTIC_FIRST = "SEMANTIC_FIRST_ROUTING"

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

    if (_DID_NOT_ARRIVE.search(text) and _ORDERED_THING.search(text)) or (
        _ABSENT.search(text) and _COMPONENT.search(text)
    ):
        reasons.append(TRIGGER_ORDER_PROBLEM)

    # An event name frequently describes why the customer is contacting us,
    # while the actual question is about an order identifier, a correction or
    # where to find it.  A keyword-only event rule cannot distinguish those
    # meanings.  Keep this deliberately structural rather than tied to one
    # campaign name.
    compact_text = re.sub(r"\s+", "", text).lower()
    event_context = any(
        token in compact_text
        for token in ("이벤트", "행사", "페스티벌", "환급", "프로모션", "리뷰")
    )
    order_question = any(
        token in compact_text
        for token in ("주문번호", "상품주문번호", "sh로시작", "어디서확인", "잘못입력", "수정하")
    )
    if event_context and order_question:
        reasons.append(TRIGGER_CONTEXT_ACTION_CONFLICT)

    # A request for another item in the same purchase cannot be answered from
    # the listing's model catalog.  It also commonly appears as the second
    # clause of an otherwise answerable question, so classify its evidence
    # requirement before a single stand/product rule claims the whole inquiry.
    purchased_other = any(
        token in compact_text
        for token in ("다른제품", "다른상품", "다른하나", "나머지상품", "같이주문", "같이구매")
    )
    if purchased_other and any(token in compact_text for token in ("구매", "주문", "샀", "산")):
        reasons.append(TRIGGER_COMPOUND_EVIDENCE)

    # Semantic-first routing is intentionally not a shadow sampler.  When the
    # feature is enabled, every non-empty inquiry earns one structured
    # understanding before order/DPS/RULE decisions.  Specific reasons remain
    # for observability; the common reason makes the precedence contract
    # explicit and prevents a new keyword-only route from silently bypassing
    # semantics later.
    if text:
        reasons.append(TRIGGER_SEMANTIC_FIRST)
    ordered = tuple(dict.fromkeys(reasons))
    return SemanticRouteDecision(use_semantic=bool(ordered), reasons=ordered)
