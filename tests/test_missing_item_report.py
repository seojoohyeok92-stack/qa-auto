"""A customer saying something did not arrive is not asking about the product.

Inquiry 325318746: "오베닉 스마트마운트 스탠드가 안왔어요". The reply explained
which model line the stand belongs to -- true, and no help at all to someone
whose stand is missing. Validator PASS, semantic coverage PASS, eligibility
SAFE, auto-post eligible.

The rule that produced it says what went wrong, in its own reason string:

    if any(k in q for k in ["오베닉", "몇세대", "세대"]):
        return self.yes("스탠드모델", ..., "오베닉/FMS 세대 문의 안내입니다.")

It is meant for a question about which generation the stand is. The condition
never asked for one -- the brand name alone was enough, so any sentence
containing 오베닉 received an answer about 오베닉. A brand name is not a
question about the brand, and the fix is to require what the rule already
claimed to require.

Semantic coverage could not catch it either: "스탠드" anchors INSTALLATION_METHOD
on both sides, so question and answer agreed on a topic while disagreeing about
everything that mattered.

The semantic layer had no way to express the meaning at all. An item that never
arrived is not DAMAGE_REPORT (it arrived broken), not PACKAGE_CONTENTS (asking
what is included) and not DELIVERY_STATUS (where is my order). It is a report
that what came is not what was ordered, so the ontology gains one action and one
object state -- the smallest addition that lets the sentence be represented.

The router's three existing triggers all detect a classifier that is unsure.
This inquiry was classified confidently (0.94) and wrongly, so a fourth trigger
was needed: the customer asserting that something about their order went wrong.
Like the others it only decides whether to spend a model call; it never decides
an answer.
"""
from __future__ import annotations

import pytest

from answer.engine import AnswerEngine
from services.semantic_action_support import (
    ANSWER_ACTION_SUPPORT, COMPATIBLE, MISMATCH, UNDETERMINED, evaluate,
)
from services.semantic_analysis import (
    ACTIONS,
    MISSING_ITEM_REPORT,
    OBJECT_STATES,
    TRIGGER_ORDER_PROBLEM,
    parse,
    route,
)


STAND_PRODUCT = (
    "삼성 삼탠바이미 스마트 M5 80cm(32인치)IPTV 모니터 화이트+스탠드 2in1거치대"
)
REPORTED = "오베닉 스마트마운트 스탠드가 안왔어요"
STAND_MODEL_ANSWER = "오베닉 스탠드 FMS 모델로 출고되고 있습니다"


def answer_for(question: str, product: str = STAND_PRODUCT) -> str:
    result = AnswerEngine().answer(product, question)
    return " ".join(str(getattr(result, "answer", "") or "").split())


def understanding(action: str, *, states=("NOT_RECEIVED",)):
    return parse({
        "primary_action": action, "secondary_actions": [],
        "request_type": "COMPLAINT",
        "objects": [{"type": "STAND", "states": list(states)}],
        "atomic_questions": [{"text": REPORTED, "action": action}],
        "deadline": None, "constraints": [], "negation": True,
        "conditional": False, "requires_order_context": True,
        "requires_delivery_schedule": False, "confidence": 0.95,
    })


# ==========================================================================
# 1. The rule now requires what its own reason always claimed
# ==========================================================================


def test_the_reported_inquiry_no_longer_gets_the_model_answer() -> None:
    assert STAND_MODEL_ANSWER not in answer_for(REPORTED)


@pytest.mark.parametrize("question", [
    "리모컨이 안 들어있어요",
    "케이블이 누락됐습니다",
    "본체는 왔는데 스탠드가 없어요",
    "사은품을 못 받았습니다",
    "오베닉 스탠드가 안왔습니다",
])
def test_no_missing_item_report_receives_a_product_description(question) -> None:
    assert STAND_MODEL_ANSWER not in answer_for(question)


@pytest.mark.parametrize("question", [
    "오베닉 스탠드는 어떤 모델인가요?",
    "오베닉 스탠드 몇세대인가요?",
    "스탠드 세대 구분이 어떻게 되나요?",
])
def test_a_genuine_generation_question_is_still_answered(question) -> None:
    """The rule keeps every inquiry it was written for."""

    assert STAND_MODEL_ANSWER in answer_for(question)


def test_a_brand_name_alone_is_not_a_question_about_the_brand() -> None:
    """The whole defect, stated once."""

    assert "오베닉" in REPORTED
    assert STAND_MODEL_ANSWER not in answer_for(REPORTED)
    assert STAND_MODEL_ANSWER in answer_for("오베닉 스탠드 어떤 모델인가요")


# ==========================================================================
# 2. The ontology can now represent the meaning
# ==========================================================================


def test_a_missing_item_is_expressible() -> None:
    result = understanding(MISSING_ITEM_REPORT)

    assert result.usable
    assert result.primary_action == MISSING_ITEM_REPORT
    assert "NOT_RECEIVED" in result.objects[0].states


def test_the_new_state_is_a_state_and_the_new_action_is_an_action() -> None:
    assert "NOT_RECEIVED" in OBJECT_STATES
    assert MISSING_ITEM_REPORT in ACTIONS
    assert not (OBJECT_STATES & ACTIONS)


def test_a_missing_item_is_kept_distinct_from_its_neighbours() -> None:
    """Damage, contents and delivery status each mean something else."""

    for other in ("DAMAGE_REPORT", "PACKAGE_CONTENTS", "DELIVERY_STATUS"):
        assert other in ACTIONS
        assert other != MISSING_ITEM_REPORT


# ==========================================================================
# 3. The mismatch gate recognises the substitution
# ==========================================================================


def test_a_product_description_does_not_address_a_missing_item() -> None:
    decision = evaluate(
        understanding(MISSING_ITEM_REPORT),
        route="TEMPLATE", template_id="스탠드모델",
    )

    assert decision.status == MISMATCH
    assert decision.question_action == MISSING_ITEM_REPORT


def test_a_model_question_is_still_compatible_with_that_answer() -> None:
    decision = evaluate(
        understanding("PRODUCT_SPEC", states=()),
        route="TEMPLATE", template_id="스탠드모델",
    )

    assert decision.status == COMPATIBLE


def test_no_label_still_means_no_verdict() -> None:
    assert evaluate(
        understanding(MISSING_ITEM_REPORT),
        route="GPT_FALLBACK", template_id=None,
    ).status == UNDETERMINED


def test_the_added_labels_describe_answers_we_own() -> None:
    for label in ("스탠드모델", "스탠드호환", "스탠드사용법", "배터리호환"):
        assert label in ANSWER_ACTION_SUPPORT
        assert MISSING_ITEM_REPORT not in ANSWER_ACTION_SUPPORT[label], (
            f"{label} cannot resolve a missing item"
        )


# ==========================================================================
# 4. The router: a confident classifier can still be wrong
# ==========================================================================


@pytest.mark.parametrize("question", [
    REPORTED,
    "리모컨이 안 들어있어요",
    "케이블이 누락됐습니다",
    "본체는 왔는데 스탠드가 없어요",
    "사은품을 못 받았습니다",
    "멀티탭 안왔습니다",
    "선반 체결볼트 2개가 누락되었어요",
    "모니터만 오고 거치대가 안왔습니다",
])
def test_an_order_problem_is_worth_a_semantic_look(question) -> None:
    decision = route(question, analysis={
        "detected_intent": "GENERAL",
        "inquiry_subtype": "GENERAL_INSTALLATION_GUIDANCE",
        "confidence": 0.94,
    })

    assert decision.use_semantic is True
    assert TRIGGER_ORDER_PROBLEM in decision.reasons


@pytest.mark.parametrize("question", [
    # Questions about the product, not reports about an order.
    "스탠드는 기본 구성품인가요?",
    "이 제품에 스탠드가 포함되어 있나요?",
    "오베닉 스탠드는 어떤 모델인가요?",
    "스탠드 호환되나요?",
    "언제 배송되나요?",
    "배송은 보통 며칠 걸리나요?",
    "HDMI 단자 몇 개인가요?",
    "설치 일정은 어떻게 안내받나요?",
])
def test_an_ordinary_product_question_is_not_an_order_problem(question) -> None:
    decision = route(question, analysis={
        "detected_intent": "GENERAL",
        "inquiry_subtype": "GENERAL_INSTALLATION_GUIDANCE",
        "confidence": 0.94,
    })

    assert TRIGGER_ORDER_PROBLEM not in decision.reasons


def test_an_absence_word_alone_is_not_an_order_problem() -> None:
    """"전원 버튼을 찾을 수 없네요" is a usage question, not a delivery complaint."""

    decision = route("이 제품은 전원 버튼을 찾을 수 없네요", analysis={
        "detected_intent": "GENERAL", "inquiry_subtype": "PRODUCT_SPEC_OR_FEATURE",
        "confidence": 0.92,
    })

    assert TRIGGER_ORDER_PROBLEM not in decision.reasons


def test_the_trigger_only_decides_whether_to_look() -> None:
    """It selects a question for analysis. It never selects an answer."""

    decision = route(REPORTED, analysis={})

    assert decision.use_semantic is True
    assert set(decision.reasons) <= {
        TRIGGER_ORDER_PROBLEM, "CLASSIFIER_HAS_NO_ACTION",
        "STATE_AND_ACTION_COMPETE", "DEADLINE_CONSTRAINT",
        "SEMANTIC_FIRST_ROUTING",
    }
    # Nothing in a routing decision resembles a verdict about publishing.
    assert not set(decision.to_dict()) & {
        "answer", "auto_post", "safe", "decision", "eligibility",
    }
