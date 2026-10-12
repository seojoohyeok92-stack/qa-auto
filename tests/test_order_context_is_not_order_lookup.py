"""Mentioning a purchase is not asking us to look the order up.

Two answered inquiries were held by the auto-post gate on a missing order
number neither of them needed (691701421, 691575305 on the server backup of
2026-10-12), and both were then posted by hand unchanged. The hold was real
policy applied to a false premise: ``InquiryAnalysisService._with_semantic``
had decided no order lookup was required, and ``InquiryProcessingPlanService``
overwrote that with GPT ①'s ``need_order`` -- a flag about whether this is a
current-order workflow at all, raised by ``requires_order_context``, the field
whose own definition says it describes conversational relevance and "is not
authority to fetch their external order". The plan now reads
``need_order_lookup``, which answers the question the gate actually acts on.

These tests run the real plan service and the real eligibility gate, with the
understanding built by the production ``_understanding_contract``, so the
override path is the one under test rather than the analysis alone.
"""
from __future__ import annotations

import pytest

from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.answer_service import AnswerService
from services.auto_processing_eligibility_service import (
    AutoProcessingEligibilityService,
)
from services.inquiry_processing_plan_service import (
    InquiryProcessingPlanService,
)
from services.semantic_analysis import parse

ORDER_HOLD_CODES = frozenset({
    "REQUIRED_ORDER_ID_MISSING_OR_INVALID",
    "ORDER_LOOKUP_NOT_TRUSTED",
})


def understanding_of(
    primary: str,
    *,
    atomic: list[dict],
    order_context: bool,
    delivery_schedule: bool = False,
    purchase_state: str = "CURRENT_ORDER",
):
    """One GPT ① understanding, parsed and contracted by production code."""

    semantic = parse({
        "primary_action": primary,
        "secondary_actions": [],
        "request_type": "ACTION_REQUEST",
        "objects": [],
        "atomic_questions": atomic,
        "deadline": None,
        "constraints": [],
        "negation": False,
        "conditional": False,
        "requires_order_context": order_context,
        "requires_delivery_schedule": delivery_schedule,
        "purchase_state": purchase_state,
        "asks_delivery_schedule": delivery_schedule,
        "confidence": 0.98,
    })
    assert semantic.usable, "the fixture must be a usable understanding"
    return semantic, AnswerService._understanding_contract(semantic)


def plan_for(tmp_path, name: str, content: str, semantic, understanding):
    database = Database(tmp_path / f"{name}.db")
    database.initialize()
    inquiry_id = InquiryRepository(database).upsert_work_item({
        "store_code": "OJE_PLUS", "source_type": "PRODUCT_INQUIRY",
        "source_question_id": name, "inquiry_type": "PRODUCT_INQUIRY",
        "title": "상품 문의", "content": content,
        "product_name": "삼성 125.7cm(50인치) 비즈니스 TV LH50BED-H",
        "product_id": "10914781557", "order_id": None,
        "product_order_id": None, "raw_json": {},
    }).inquiry_id
    inquiry = InquiryRepository(database).get(inquiry_id)
    plan = InquiryProcessingPlanService(database).create(
        inquiry,
        semantic_analysis=semantic,
        # The shape the pipeline passes: the contract travels under
        # "understanding", which is what makes the override fire at all.
        semantic_routing={
            "phase": "PRE_ROUTING", "called": True, "usable": True,
            "semantic": semantic.to_dict(), "understanding": understanding,
        },
    )
    return database, inquiry, plan


# -- The two production cases ------------------------------------------------


def test_691701421_collection_request_needs_no_order_lookup(tmp_path) -> None:
    """"구매했는데 폐가전수거 요청을 체크못한거 같아요 / 폐가전수거 해주세요"."""

    semantic, understanding = understanding_of(
        "COLLECTION",
        atomic=[{"text": "폐가전수거를 해주세요", "action": "COLLECTION"}],
        order_context=True,
    )
    # The field that used to decide this is still true -- the customer did
    # mention their purchase. It simply no longer grants an order lookup.
    assert semantic.requires_order_context is True
    assert semantic.purchase_state == "CURRENT_ORDER"
    assert understanding["need_order_lookup"] is False
    assert understanding["need_dps"] is False
    # The context field is still raised -- Phase9 reads that, and it is a
    # different question from whether we must fetch the order.
    assert understanding["need_order"] is True

    _, _, plan = plan_for(
        tmp_path, "691701421",
        "구매했는데 폐가전수거 요청을 체크못한거 같아요\n폐가전수거 해주세요",
        semantic, understanding,
    )
    assert plan.requires_order_lookup is False
    assert plan.requires_dps_lookup is False
    # Every order-state field has to agree, or a downstream gate reads an
    # unrelated blank as an order failure. This record said
    # requires_order_lookup=True beside order_id_status=NOT_REQUIRED.
    assert plan.order_id_status == "NOT_REQUIRED"


def test_691575305_warranty_and_repair_question_needs_no_order_lookup(
    tmp_path,
) -> None:
    """"25년 12월 구매 했어요 / 제품보증기간 및 As수리 가능여부 부탁 드려요!"."""

    semantic, understanding = understanding_of(
        "REPAIR",
        atomic=[
            {"text": "제품보증기간이 어떻게 되나요", "action": "BENEFIT"},
            {"text": "AS 수리가 가능한가요", "action": "REPAIR"},
        ],
        order_context=True,
    )
    assert understanding["need_order_lookup"] is False

    _, _, plan = plan_for(
        tmp_path, "691575305",
        "25년 12월 구매 했어요\n제품보증기간 및\nAs수리 가능여부 부탁 드려요!",
        semantic, understanding,
    )
    assert plan.requires_order_lookup is False
    assert plan.requires_dps_lookup is False


@pytest.mark.parametrize(
    "name, content, primary, atomic",
    [
        (
            "collection", "구매했는데 폐가전수거 해주세요", "COLLECTION",
            [{"text": "폐가전수거를 해주세요", "action": "COLLECTION"}],
        ),
        (
            "warranty", "구매했어요 보증기간 알려주세요", "BENEFIT",
            [{"text": "보증기간이 어떻게 되나요", "action": "BENEFIT"}],
        ),
    ],
)
def test_the_order_pair_no_longer_holds_an_answered_inquiry(
    tmp_path, name, content, primary, atomic,
) -> None:
    """The gate itself, not just the plan: these two codes must not fire."""

    semantic, understanding = understanding_of(
        primary, atomic=atomic, order_context=True,
    )
    _, inquiry, plan = plan_for(
        tmp_path, f"gate-{name}", content, semantic, understanding,
    )
    draft = {
        "id": 1092,
        "original_answer": (
            "구매 시 폐가전 수거를 체크하지 못하셨더라도 설치 기사님의 "
            "해피콜 또는 방문 시 요청해 주시면 수거해 드립니다."
        ),
        "review_status": "PENDING",
        "validation_status": "PASS",
        "posted": False,
        "metadata_json": {
            "selected_answer_route": "GPT_FALLBACK",
            "generation_mode": "GPT_FALLBACK",
            "processing_plan": plan.to_dict(),
        },
    }
    decision = AutoProcessingEligibilityService().evaluate(
        inquiry=inquiry, draft=draft, route="GPT_FALLBACK",
    )
    # Scoped deliberately to the two codes this change is about. The rest of
    # the gate reads an evidence payload this harness does not reproduce, and
    # those holds are not this fix's business -- on the production cases the
    # order pair was the entire recorded hold.
    assert not (ORDER_HOLD_CODES & set(decision.reasons)), decision.reasons


# -- What must not change ----------------------------------------------------


def test_current_delivery_schedule_still_requires_order_and_dps(
    tmp_path,
) -> None:
    """A real "when does my order arrive" keeps the whole Order/DPS policy."""

    semantic, understanding = understanding_of(
        "DELIVERY_STATUS",
        atomic=[{"text": "배송 언제 오나요", "action": "DELIVERY_STATUS"}],
        order_context=True, delivery_schedule=True,
    )
    assert understanding["need_order_lookup"] is True
    assert understanding["need_dps"] is True

    _, inquiry, plan = plan_for(
        tmp_path, "delivery-status", "주문한 제품 배송 언제 오나요?",
        semantic, understanding,
    )
    assert plan.requires_order_lookup is True
    assert plan.requires_dps_lookup is True
    assert plan.order_id_status == "MISSING"


def test_installation_schedule_still_requires_order_and_dps(tmp_path) -> None:
    semantic, understanding = understanding_of(
        "INSTALLATION_SCHEDULE",
        atomic=[{"text": "설치는 언제 오나요", "action": "INSTALLATION_SCHEDULE"}],
        order_context=True, delivery_schedule=True,
    )
    assert understanding["need_order_lookup"] is True

    _, _, plan = plan_for(
        tmp_path, "install-schedule", "설치 언제 와요?", semantic, understanding,
    )
    assert plan.requires_order_lookup is True
    assert plan.requires_dps_lookup is True


def test_order_identification_still_requires_the_order_lookup(tmp_path) -> None:
    """The authoritative condition the analysis names, reached by itself."""

    semantic, understanding = understanding_of(
        "ORDER_IDENTIFICATION",
        atomic=[{
            "text": "주문번호가 어디서 확인되나요",
            "action": "ORDER_IDENTIFICATION",
        }],
        # Deliberately false: the lookup is earned by the question being about
        # identifying the order, never by the mention of a purchase.
        order_context=False,
    )
    assert understanding["need_order_lookup"] is True

    _, _, plan = plan_for(
        tmp_path, "order-identification",
        "주문번호가 틀렸다고 하는데 어디서 확인하나요?", semantic, understanding,
    )
    # The lookup is granted by the action alone. The ORDER_INFO_REQUIRED
    # retag is a separate, unchanged rule that keys on
    # ``requires_order_context``, so the intent stays as classified here --
    # see the companion test below for the retag itself.
    assert plan.requires_order_lookup is True


def test_order_identification_still_requires_it_with_order_context_too(
    tmp_path,
) -> None:
    semantic, understanding = understanding_of(
        "ORDER_IDENTIFICATION",
        atomic=[{
            "text": "제 주문 확인해 주세요",
            "action": "ORDER_IDENTIFICATION",
        }],
        order_context=True,
    )
    assert understanding["need_order_lookup"] is True

    _, _, plan = plan_for(
        tmp_path, "order-identification-context",
        "구매했는데 제 주문 확인해 주세요", semantic, understanding,
    )
    assert plan.requires_order_lookup is True
    assert plan.detected_intent == "ORDER_IDENTIFICATION"


def test_a_customer_with_no_order_never_gains_an_order_lookup(tmp_path) -> None:
    """The ``current_order`` guard this change preserved, kept under test."""

    semantic, understanding = understanding_of(
        "DELIVERY_POLICY",
        atomic=[{"text": "배송 얼마나 걸려요", "action": "DELIVERY_POLICY"}],
        order_context=True, delivery_schedule=True,
        purchase_state="PRE_PURCHASE",
    )
    assert understanding["need_order_lookup"] is False
    assert understanding["need_dps"] is False

    _, _, plan = plan_for(
        tmp_path, "pre-purchase", "지금 주문하면 배송 얼마나 걸려요?",
        semantic, understanding,
    )
    assert plan.requires_order_lookup is False
    assert plan.requires_dps_lookup is False
