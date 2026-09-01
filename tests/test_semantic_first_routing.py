from __future__ import annotations

from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.inquiry_processing_plan_service import InquiryProcessingPlanService
from services.semantic_action_support import MISMATCH, evaluate
from services.semantic_analysis import parse


def semantic(
    primary: str,
    *secondary: str,
    order: bool = False,
    delivery: bool = False,
    atomic: list[dict] | None = None,
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
    database = Database(tmp_path / "semantic-delivery.db")
    database.initialize()
    value = inquiry(database, "delivery", "현재 배송 상태가 궁금합니다.")

    plan = InquiryProcessingPlanService(database).create(
        value,
        semantic_analysis=semantic(
            "DELIVERY_STATUS", order=True, delivery=True,
        ),
    )

    assert plan.requires_order_lookup is True
    assert plan.requires_dps_lookup is True
    assert plan.selected_answer_route == "ORDER_ID_REQUEST"


def test_fixed_event_rule_cannot_answer_order_identification_question() -> None:
    decision = evaluate(
        semantic("ORDER_IDENTIFICATION", order=True),
        route="TEMPLATE", template_id="", match_kind="FIXED_EVENT_ONNURI",
    )

    assert decision.status == MISMATCH
