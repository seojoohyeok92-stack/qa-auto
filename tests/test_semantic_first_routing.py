from __future__ import annotations

import pytest

from repositories.database import Database
from repositories.answer_repository import AnswerRepository
from repositories.inquiry_repository import InquiryRepository
from answer.models import AnswerResult, AnswerStatus
from services.inquiry_processing_plan_service import InquiryProcessingPlanService
from services.semantic_action_support import MISMATCH, evaluate
from services.semantic_analysis import parse


def semantic(
    primary: str,
    *secondary: str,
    order: bool = False,
    delivery: bool = False,
    atomic: list[dict] | None = None,
    purchase_state: str = "UNKNOWN",
    asks_schedule: bool = False,
):
    return parse({
        "primary_action": primary,
        "secondary_actions": list(secondary),
        "request_type": "MIXED" if secondary else "QUESTION",
        "objects": [],
        "atomic_questions": atomic or [{"text": "질문", "action": primary}],
        "deadline": None,
        "constraints": [],
        "negation": False,
        "conditional": False,
        "requires_order_context": order,
        "requires_delivery_schedule": delivery,
        "purchase_state": purchase_state,
        "asks_delivery_schedule": asks_schedule,
        "confidence": 0.95,
    })


def inquiry(database: Database, key: str, content: str) -> dict:
    inquiry_id = InquiryRepository(database).upsert_work_item({
        "store_code": "OJE_PLUS", "source_type": "NAVER",
        "source_question_id": key, "inquiry_type": "PRODUCT_INQUIRY",
        "title": "문의", "content": content, "product_name": "삼성 TV",
        "product_id": "p1", "order_id": None, "product_order_id": None,
        "raw_json": {},
    }).inquiry_id
    return InquiryRepository(database).get(inquiry_id)


def test_event_context_order_question_cannot_become_delivery_dps_route(tmp_path) -> None:
    database = Database(tmp_path / "semantic-routing.db")
    database.initialize()
    value = inquiry(
        database, "event-order",
        "행사 신청 중인데 주문번호가 틀렸다고 합니다. 어디서 확인하나요?",
    )
    understanding = semantic("ORDER_IDENTIFICATION", order=True)

    plan = InquiryProcessingPlanService(database).create(
        value, semantic_analysis=understanding,
        semantic_routing={"phase": "PRE_ROUTING", "called": True},
    )

    assert plan.detected_intent == "ORDER_IDENTIFICATION"
    assert plan.requires_order_lookup is True
    assert plan.requires_dps_lookup is False
    assert plan.semantic_routing == {"phase": "PRE_ROUTING", "called": True}


def test_event_notification_timing_does_not_gain_delivery_dps_from_keywords(tmp_path) -> None:
    database = Database(tmp_path / "semantic-notification.db")
    database.initialize()
    value = inquiry(
        database, "event-notification",
        "리뷰 이벤트 결과 문자는 언제 오나요?",
    )

    plan = InquiryProcessingPlanService(database).create(
        value, semantic_analysis=semantic("NOTIFICATION_POLICY"),
    )

    assert plan.requires_order_lookup is False
    assert plan.requires_dps_lookup is False


@pytest.mark.parametrize("action", ["COLLECTION", "BENEFIT"])
def test_order_context_without_external_evidence_does_not_require_lookup(
    tmp_path, action: str,
) -> None:
    database = Database(tmp_path / f"semantic-context-{action}.db")
    database.initialize()
    value = inquiry(database, f"context-{action}", "customer policy question")

    plan = InquiryProcessingPlanService(database).create(
        value,
        semantic_analysis=semantic(action, order=True),
    )

    assert plan.requires_order_lookup is False
    assert plan.requires_dps_lookup is False
    assert plan.order_id_status == "NOT_REQUIRED"
    assert plan.order_lookup_status == "NOT_REQUIRED"


@pytest.mark.parametrize(
    ("content", "action"),
    [
        (
            "\uc124\uce58\uc640 \ud568\uaed8 \ud3d0\uac00\uc804 \uc218\uac70 "
            "\ucd94\uac00\uc694\uccad\ub4dc\ub824\ub3c4 \ub420\uae4c\uc694?",
            "COLLECTION",
        ),
        (
            "\ub9ac\ubdf0 \uc791\uc131 \uc644\ub8cc \ud6c4 \ud3ec\uc778\ud2b8 "
            "\uc9c0\uae09 \uc2dc\uc810\uc774 \uad81\uae08\ud569\ub2c8\ub2e4.",
            "BENEFIT",
        ),
    ],
)
def test_policy_or_collection_question_with_purchase_context_never_creates_order_blocker(
    tmp_path, content: str, action: str,
) -> None:
    database = Database(tmp_path / f"runtime-order-context-{action}.db")
    database.initialize()
    value = inquiry(database, f"runtime-{action}", content)

    plan = InquiryProcessingPlanService(database).create(
        value, semantic_analysis=semantic(action, order=True),
    )

    assert plan.requires_order_lookup is False
    assert plan.order_id_status == "NOT_REQUIRED"
    assert plan.order_lookup_status == "NOT_REQUIRED"


def test_dashboard_uses_active_draft_execution_plan(tmp_path) -> None:
    from ui.review_workspace import _processing_plan_for_inquiry

    database = Database(tmp_path / "persisted-execution-plan.db")
    database.initialize()
    value = inquiry(database, "persisted-plan", "policy question")
    persisted = InquiryProcessingPlanService(database).create(
        value, semantic_analysis=semantic("BENEFIT", order=True),
    )
    AnswerRepository(database).create_program_draft(
        int(value["id"]),
        AnswerResult(
            status=AnswerStatus.GENERATED,
            category="policy",
            reason="test",
            answer="safe answer",
            provider="rules",
            auto_answerable=True,
            needs_review=False,
            metadata={"processing_plan": persisted.to_dict()},
        ),
    )

    displayed = _processing_plan_for_inquiry(database, value)

    assert displayed.to_dict() == persisted.to_dict()


def test_compound_purchase_question_keeps_order_evidence_and_review(tmp_path) -> None:
    database = Database(tmp_path / "semantic-compound.db")
    database.initialize()
    value = inquiry(
        database, "compound-order",
        "스탠드와 모니터를 따로 작성해야 하나요? 그리고 다른 구매상품 모델명도 알려주세요.",
    )
    understanding = semantic(
        "PRODUCT_SPEC", "ORDER_IDENTIFICATION", order=True,
        atomic=[
            {"text": "스탠드와 모니터를 따로 작성해야 하나요?", "action": "PRODUCT_SPEC"},
            {"text": "다른 구매상품 모델명도 알려주세요.", "action": "ORDER_IDENTIFICATION"},
        ],
    )

    plan = InquiryProcessingPlanService(database).create(
        value, semantic_analysis=understanding,
    )

    assert plan.requires_order_lookup is True
    assert plan.requires_dps_lookup is False
    assert plan.needs_staff_review is True
    assert plan.analysis.manual_review_required is True
    assert len(plan.analysis.subquestion_analyses) == 2


def test_current_delivery_semantics_keeps_order_and_dps_requirements(tmp_path) -> None:
    """구매가 확인된 배송문의는 기존 Order/DPS pipeline 그대로다."""

    database = Database(tmp_path / "semantic-delivery.db")
    database.initialize()
    value = inquiry(database, "delivery", "주문했는데 현재 배송 상태가 궁금합니다.")

    plan = InquiryProcessingPlanService(database).create(
        value,
        semantic_analysis=semantic(
            "DELIVERY_STATUS", order=True, delivery=True,
            purchase_state="CURRENT_ORDER", asks_schedule=True,
        ),
    )

    assert plan.requires_order_lookup is True
    assert plan.requires_dps_lookup is True
    assert plan.selected_answer_route == "ORDER_ID_REQUEST"


def test_the_same_question_without_a_stated_purchase_is_held(tmp_path) -> None:
    """같은 문장이라도 구매 여부가 없으면 주문번호를 요구하지 않는다.

    위 테스트와 짝이다. 문의 본문이 구매를 말하지 않으면 시스템은 주문이
    있는지 모르고, 모르는 상태에서 주문번호를 자동 요청하는 것이 확정된
    운영정책이 금지한 동작이다.
    """

    database = Database(tmp_path / "semantic-delivery-unknown.db")
    database.initialize()
    value = inquiry(database, "delivery-unknown", "현재 배송 상태가 궁금합니다.")

    plan = InquiryProcessingPlanService(database).create(
        value,
        semantic_analysis=semantic(
            "DELIVERY_STATUS", order=True, delivery=True,
            purchase_state="UNKNOWN", asks_schedule=True,
        ),
    )

    assert plan.requires_order_lookup is False
    assert plan.requires_dps_lookup is False
    assert plan.analysis.requires_order_id is False
    assert plan.analysis.manual_review_required is True
    assert plan.selected_answer_route != "ORDER_ID_REQUEST"


def test_fixed_event_rule_cannot_answer_order_identification_question() -> None:
    decision = evaluate(
        semantic("ORDER_IDENTIFICATION", order=True),
        route="TEMPLATE", template_id="", match_kind="FIXED_EVENT_ONNURI",
    )

    assert decision.status == MISMATCH
