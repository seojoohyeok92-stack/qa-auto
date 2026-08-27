"""The answer has to address the request, not merely share its subject.

"고장난 기존 tv 수거 요청드려요" was published with the Samsung service-centre
number. The validator passed it -- the sentence is true. Semantic coverage
passed it -- question and answer both anchor on 고장. Eligibility found no
blocking reason. Three gates, each correct by its own definition, and not one
of them asks whether the answer answers the question.

The store already held the right answer: ``_old_appliance_pickup`` matches
"수거" and returns the collection guidance. It never ran, because
``_install_common_info`` sits earlier in the engine and fires on 고장. A word
describing the *object* outranked the customer's *action*.

So this gate compares an action against an action. The question's action comes
from the semantic analyzer; the answer's comes from the label the rule engine
stamps on its own output. That asymmetry is deliberate -- a table over our own
templates does not have to grow every time a customer phrases something a new
way, which is precisely what the anchor tables were forced to do.

The gate is block-only and silent by default. An unlabelled answer, a draft
with nothing recorded, or an understanding the model could not supply all leave
it undetermined, and undetermined changes no decision. It can add a hold; it can
never remove one, and it never decides that anything may be published.
"""
from __future__ import annotations

import pytest

from answer.hold_reasons import describe_reason
from services.auto_processing_eligibility_service import (
    SOFT_REASONS,
    AutoProcessingEligibilityService,
)
from services.semantic_action_support import (
    COMPATIBLE,
    MISMATCH,
    REASON_CODE,
    UNDETERMINED,
    answer_support,
    decision_from_metadata,
    evaluate,
)
from services.semantic_analysis import SemanticAnalysis, parse


def understanding(primary: str, *secondary: str, confidence: float = 0.95):
    return parse({
        "primary_action": primary,
        "secondary_actions": list(secondary),
        "request_type": "ACTION_REQUEST",
        "objects": [{"type": "TV", "states": ["BROKEN"]}],
        "atomic_questions": [{"text": "q", "action": primary}],
        "deadline": None, "constraints": [], "negation": False,
        "conditional": False, "requires_order_context": False,
        "requires_delivery_schedule": False, "confidence": confidence,
    })


# ==========================================================================
# 1. The reported failure, and the answer that should have been chosen
# ==========================================================================


def test_the_as_answer_does_not_address_a_collection_request() -> None:
    decision = evaluate(
        understanding("COLLECTION"),
        route="TEMPLATE", template_id="설치상품/공통안내",
    )

    assert decision.status == MISMATCH
    assert decision.question_action == "COLLECTION"
    assert "REPAIR" in decision.answer_actions
    assert "COLLECTION" not in decision.answer_actions


def test_the_collection_answer_does_address_it() -> None:
    decision = evaluate(
        understanding("COLLECTION"),
        route="TEMPLATE", template_id="폐가전수거",
    )

    assert decision.status == COMPATIBLE


def test_a_repair_request_is_compatible_with_the_as_answer() -> None:
    """The same answer, a different question -- and no hold on this account."""

    decision = evaluate(
        understanding("REPAIR"),
        route="TEMPLATE", template_id="설치상품/공통안내",
    )

    assert decision.status == COMPATIBLE
    assert decision.mismatched is False


@pytest.mark.parametrize("primary,template,expected", [
    # §11 regression list, as action against answer label.
    ("COLLECTION", "설치상품/공통안내", MISMATCH),      # 고장난 TV 수거해주세요
    ("COLLECTION", "폐가전수거", COMPATIBLE),           # 기존 TV 가져가주시나요
    ("COLLECTION", "배송/설치신규+폐가전", COMPATIBLE),  # 폐가전 수거 가능한가요
    ("REPAIR", "설치상품/공통안내", COMPATIBLE),        # TV가 고장났어요
    ("DELIVERY_DEADLINE_CONFIRMATION", "배송/설치신규", MISMATCH),
    ("DELIVERY_POLICY", "배송/설치신규", COMPATIBLE),   # 토요일에도 배송하나요
    ("SCHEDULE_REQUEST", "배송/설치일조율", COMPATIBLE),
    ("SCHEDULE_CHANGE", "배송/설치일변경요청", COMPATIBLE),
    ("INSTALLATION_METHOD", "설치상품/공통안내", COMPATIBLE),
    ("PACKAGE_CONTENTS", "상품구성", COMPATIBLE),
    ("STORE_PICKUP", "방문수령/설치상품", COMPATIBLE),
])
def test_the_regression_matrix(primary, template, expected) -> None:
    decision = evaluate(
        understanding(primary), route="TEMPLATE", template_id=template,
    )

    assert decision.status == expected


def test_a_compound_request_is_held_when_its_main_action_is_dropped() -> None:
    """Delivery and collection were asked; only delivery was answered."""

    decision = evaluate(
        understanding("COLLECTION", "DELIVERY_POLICY"),
        route="TEMPLATE", template_id="배송/설치신규",
    )

    assert decision.status == MISMATCH
    assert "PRIMARY_ACTION_UNADDRESSED_COLLECTION" == decision.reason


def test_a_dps_schedule_answer_addresses_a_status_question() -> None:
    decision = evaluate(
        understanding("DELIVERY_STATUS"),
        route="DELIVERY_WITH_INSTALLATION_DATE", template_id=None,
    )

    assert decision.status == COMPATIBLE


# ==========================================================================
# 2. Silence wherever either side is unknown
# ==========================================================================


def test_no_semantic_analysis_means_no_verdict() -> None:
    assert evaluate(None, route="TEMPLATE", template_id="폐가전수거").status == UNDETERMINED


def test_an_unusable_understanding_means_no_verdict() -> None:
    fallback = SemanticAnalysis(source="UNAVAILABLE", reason="PROVIDER_ERROR")

    decision = evaluate(fallback, route="TEMPLATE", template_id="설치상품/공통안내")

    assert decision.status == UNDETERMINED
    assert decision.reason == "NO_SEMANTIC_ANALYSIS"


def test_a_model_that_could_not_tell_is_not_a_disagreement() -> None:
    decision = evaluate(
        understanding("OTHER"), route="TEMPLATE", template_id="폐가전수거",
    )

    assert decision.status == UNDETERMINED
    assert decision.reason == "QUESTION_ACTION_UNKNOWN"


def test_an_unlabelled_answer_yields_no_verdict() -> None:
    """Model-composed prose carries no label we own, so nothing is claimed."""

    decision = evaluate(
        understanding("COLLECTION"), route="GPT_FALLBACK", template_id=None,
    )

    assert decision.status == UNDETERMINED
    assert decision.reason == "ANSWER_ACTION_UNKNOWN"


def test_an_unmapped_template_yields_no_verdict() -> None:
    decision = evaluate(
        understanding("COLLECTION"), route="TEMPLATE",
        template_id="행사/신청방법",
    )

    assert decision.status == UNDETERMINED


def test_asking_for_the_order_number_is_never_a_mismatch() -> None:
    """It asserts nothing about the request; blocking it leaves no reply."""

    supported, label = answer_support(
        route="ORDER_ID_REQUEST", template_id="설치상품/공통안내",
    )

    assert supported is None
    assert evaluate(
        understanding("COLLECTION"), route="ORDER_ID_REQUEST",
        template_id="설치상품/공통안내",
    ).status == UNDETERMINED


# ==========================================================================
# 3. Eligibility consumes a record; it never derives one
# ==========================================================================


def inquiry(content: str = "고장난 기존 tv 수거 요청드려요") -> dict:
    return {
        "id": 1, "title": "문의", "content": content,
        "source_answered": False, "post_status": "NONE",
    }


def draft(metadata: dict | None = None) -> dict:
    return {
        "original_answer": (
            "제품 사용 중 고장이나 불량이 의심되는 경우 삼성전자 고객센터 "
            "1588-3366으로 문의해 A/S 접수해 주시면 됩니다."
        ),
        "validation_status": "PASS",
        "validator_result_json": {"passed": True},
        "review_status": "PENDING",
        "posted": False,
        "metadata_json": {
            "processing_plan": {"analysis": {}}, **(metadata or {}),
        },
    }


def evaluate_eligibility(drafted, route="TEMPLATE"):
    return AutoProcessingEligibilityService().evaluate(
        inquiry=inquiry(), draft=drafted, route=route,
    )


def test_a_recorded_mismatch_blocks_auto_post() -> None:
    decision = evaluate_eligibility(draft({
        "semantic_action_support": evaluate(
            understanding("COLLECTION"),
            route="TEMPLATE", template_id="설치상품/공통안내",
        ).to_dict(),
    }))

    assert decision.decision == "REVIEW_REQUIRED"
    assert REASON_CODE in decision.reasons
    assert decision.safe is False


def test_the_reason_is_hard_not_soft() -> None:
    assert REASON_CODE not in SOFT_REASONS


def test_the_reason_has_an_operator_sentence() -> None:
    assert describe_reason(REASON_CODE) != REASON_CODE


def test_a_recorded_compatible_verdict_changes_nothing() -> None:
    decision = evaluate_eligibility(draft({
        "semantic_action_support": evaluate(
            understanding("REPAIR"),
            route="TEMPLATE", template_id="설치상품/공통안내",
        ).to_dict(),
    }))

    assert REASON_CODE not in decision.reasons


def test_a_draft_with_no_record_is_unaffected() -> None:
    """Every draft written before this existed must decide exactly as before."""

    before = evaluate_eligibility(draft())

    assert REASON_CODE not in before.reasons
    assert decision_from_metadata({}).status == UNDETERMINED
    assert decision_from_metadata(None).status == UNDETERMINED


def test_an_unreadable_record_is_ignored_rather_than_trusted() -> None:
    for value in ("mismatch", 42, [], {"status": "PUBLISH"}):
        assert decision_from_metadata(
            {"semantic_action_support": value}
        ).status == UNDETERMINED


def test_the_gate_can_only_add_a_hold() -> None:
    """No recorded value may turn a held answer into a publishable one."""

    held = draft({"semantic_action_support": {"status": COMPATIBLE}})
    held["validation_status"] = "FAIL"

    decision = evaluate_eligibility(held)

    assert decision.decision != "SAFE"
    assert "VALIDATOR_NOT_PASS" in decision.reasons


def test_eligibility_holds_no_provider() -> None:
    """This stage must not acquire a network dependency."""

    import inspect

    import services.auto_processing_eligibility_service as module

    source = inspect.getsource(module)
    assert "generate_json" not in source
    assert "GptSemanticAnalyzerService" not in source


def test_the_existing_deadline_gate_is_untouched() -> None:
    """Both gates hold the deadline case, independently."""

    decision = evaluate_eligibility(
        draft(), route="TEMPLATE",
    )
    assert "DELIVERY_DEADLINE_NOT_CONFIRMABLE" not in decision.reasons

    deadline = AutoProcessingEligibilityService().evaluate(
        inquiry={
            "id": 2, "title": "문의",
            "content": "혹시 오늘 주문하면 9일까지 받아볼 수 있을까요?",
            "source_answered": False, "post_status": "NONE",
        },
        draft=draft({
            "semantic_action_support": evaluate(
                understanding("DELIVERY_DEADLINE_CONFIRMATION"),
                route="TEMPLATE", template_id="배송/설치신규",
            ).to_dict(),
        }),
        route="TEMPLATE",
    )

    assert "DELIVERY_DEADLINE_NOT_CONFIRMABLE" in deadline.reasons
    assert REASON_CODE in deadline.reasons
    assert deadline.decision == "REVIEW_REQUIRED"
