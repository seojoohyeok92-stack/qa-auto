"""Regression tests for the three causes of the 2026-08-25 production holds.

Each test pins a *mechanism* rather than an inquiry id, because all three were
invisible to the existing suite for the same reason: the suite exercised each
layer with the shape the layer expected, and the defects lived in what one
layer handed the next.

1. ``subquestion_evidence`` was built without consulting ``product_facts.db``,
   so a specification question the Product DB answers exactly was marked
   NO_RELIABLE_SOURCE -- which both told the model not to answer it and made
   the validator treat the answer as an unsupported claim.
2. Any non-200 from the Naver order API became a generic lookup failure, so a
   number Naver explicitly reports as unknown was indistinguishable from an
   outage.
3. A test-only synthetic order snapshot was selected by inspecting lazily
   populated attributes, which a production ``AnswerService`` satisfies on its
   first delivery inquiry after start-up.
"""
from __future__ import annotations

from typing import Any

import pytest
import requests

from api.order import OrderNotFoundError, raise_order_api_error
from services.answer_service import AnswerService
from services.hybrid_answer_service import HybridAnswerService
from services.product_knowledge_service import (
    ProductFact,
    ProductKnowledgeResult,
    required_fact_groups,
)


class _Request:
    def __init__(self, metadata: dict[str, Any]) -> None:
        self.metadata = metadata


def _fact(field_key: str, value: Any, unit: str | None = None) -> ProductFact:
    return ProductFact(
        product_id="13239109816",
        listing_id="listing_13239109816",
        model_code=None,
        field_key=field_key,
        value=value,
        raw_value=value,
        unit=unit,
        scope="PRODUCT",
        scope_key="13239109816",
        component_scope="BASE",
        volatility="STABLE",
        verification_status="VERIFIED",
        resolution_status="RESOLVED",
        lifecycle_status="ACTIVE",
        canonical_fact_id=f"fact-{field_key}",
        value_id=f"value-{field_key}",
        safe_for_answer=True,
    )


def _knowledge(*facts: ProductFact) -> ProductKnowledgeResult:
    return ProductKnowledgeResult(
        product_id="13239109816",
        listing_id="listing_13239109816",
        matched=True,
        requested_fields=tuple(item.field_key for item in facts),
        safe_facts=facts,
    )


def _evidence(subquestion: str, **overrides: Any) -> dict[str, Any]:
    item = {
        "subquestion": subquestion,
        "status": "NO_RELIABLE_SOURCE",
        "source": None,
        "learning_ids": [],
        "historical_case_ids": [],
        "feedback_signal_ids": [],
        "answer_required": False,
        "evidence_coverage": "UNSUPPORTED",
    }
    item.update(overrides)
    return item


def _promote(knowledge: ProductKnowledgeResult, *items: dict[str, Any]) -> list[dict]:
    context = HybridAnswerService._apply_product_fact_evidence(
        _Request({"product_knowledge": knowledge}),
        {"subquestion_evidence": list(items)},
    )
    return context["subquestion_evidence"]


# --------------------------------------------------------------------------
# 1. A verified product fact is evidence for the sub-question it answers.
# --------------------------------------------------------------------------


def test_verified_product_fact_answers_its_own_subquestion() -> None:
    question = "이 제품 화면 크기랑 해상도가 어떻게 되나요?"
    knowledge = _knowledge(
        _fact("screen_size", {"inch": 43}),
        _fact("display_size_cm", 107.9),
        _fact("resolution", {"width": 3840, "height": 2160}),
        _fact("resolution_class", "4K UHD"),
    )

    (item,) = _promote(knowledge, _evidence(question))

    assert item["status"] == "ANSWERABLE"
    assert item["source"] == "VERIFIED_PRODUCT_FACT"
    assert item["evidence_coverage"] == "SUPPORTED"
    assert item["answer_required"] is True
    assert set(item["product_fact_fields"]) == {
        "screen_size", "display_size_cm", "resolution", "resolution_class",
    }


def test_verified_fact_settles_coverage_for_partially_supported_learning() -> None:
    """A partial answer-support score must not outweigh a proven fact.

    The weight question retrieved an approved Learning answer that merely
    shared wording, scoring PARTIALLY_SUPPORTED -- enough to keep
    ``_evidence_fully_supported`` false and hold an answer the Product DB
    proves outright.
    """

    question = "이 제품 스탠드 제외하고 본체 무게가 몇 kg인가요?"
    knowledge = _knowledge(_fact("weight_without_stand_kg", 6.5, "kg"))

    (item,) = _promote(
        knowledge,
        _evidence(
            question,
            status="ANSWERABLE",
            source="ACTIVE_POSITIVE_LEARNING",
            evidence_coverage="PARTIALLY_SUPPORTED",
            answer_required=True,
        ),
    )

    assert item["evidence_coverage"] == "SUPPORTED"
    # The retrieval source it earned is preserved, not overwritten.
    assert item["source"] == "ACTIVE_POSITIVE_LEARNING"
    assert item["product_fact_fields"] == ["weight_without_stand_kg"]


def test_missing_fact_is_left_unsupported() -> None:
    """No verified VESA fact means the hold stays, product match or not."""

    knowledge = _knowledge(_fact("screen_size", {"inch": 43}))

    (item,) = _promote(knowledge, _evidence("이 제품 베사홀 규격이 어떻게 되나요?"))

    assert item["status"] == "NO_RELIABLE_SOURCE"
    assert item["evidence_coverage"] == "UNSUPPORTED"
    assert "product_fact_fields" not in item


def test_unrelated_facts_never_support_a_non_product_question() -> None:
    """A catalogued specification is not evidence about a delivery date."""

    knowledge = _knowledge(
        _fact("screen_size", {"inch": 43}),
        _fact("resolution_class", "4K UHD"),
    )

    (item,) = _promote(knowledge, _evidence("배송 언제 되나요?"))

    assert item["status"] == "NO_RELIABLE_SOURCE"


def test_conflict_and_dps_statuses_are_never_overruled() -> None:
    knowledge = _knowledge(_fact("screen_size", {"inch": 43}))

    conflict, needs_dps = _promote(
        knowledge,
        _evidence("이 제품 화면 크기가 어떻게 되나요?", status="CONFLICT"),
        _evidence("설치 예정일이 언제인가요?", status="NEEDS_DPS"),
    )

    assert conflict["status"] == "CONFLICT"
    assert needs_dps["status"] == "NEEDS_DPS"


def test_unmatched_product_contributes_no_evidence() -> None:
    """Facts belonging to another product may never vouch for this one."""

    knowledge = ProductKnowledgeResult(
        product_id=None, listing_id=None, matched=False
    )

    (item,) = _promote(knowledge, _evidence("이 제품 화면 크기가 어떻게 되나요?"))

    assert item["status"] == "NO_RELIABLE_SOURCE"


# --------------------------------------------------------------------------
# 1b. Scope: the question names which of a topic's facts may answer it.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("이 제품 스탠드 제외하고 본체 무게가 몇 kg인가요?", {"weight_without_stand_kg"}),
        ("스탠드 포함 무게가 어떻게 되나요?", {"weight_with_stand_kg"}),
        (
            "이 제품 무게가 몇 kg인가요?",
                {"weight_with_stand_kg", "weight_without_stand_kg", "weight_catalog"},
        ),
        ("해상도가 어떻게 되나요?", {"resolution", "resolution_class"}),
        ("몇 인치인가요?", {"screen_size", "display_size_cm"}),
    ],
)
def test_weight_and_display_scope_is_explicit(
    question: str, expected: set[str]
) -> None:
    groups = required_fact_groups(question)

    assert set().union(*groups) == expected


def test_accessory_package_weight_cannot_answer_body_weight() -> None:
    """The stand's shipping carton is not the panel, whatever it weighs."""

    knowledge = _knowledge(_fact("accessory_package_weight_kg", 25.3, "kg"))

    (item,) = _promote(
        knowledge, _evidence("이 제품 스탠드 제외하고 본체 무게가 몇 kg인가요?")
    )

    assert item["status"] == "NO_RELIABLE_SOURCE"


# --------------------------------------------------------------------------
# 2. "No such order" and "the lookup failed" are different findings.
# --------------------------------------------------------------------------


class _Response:
    def __init__(self, status_code: int, payload: Any, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_order_not_found_is_raised_distinctly() -> None:
    response = _Response(
        400, {"code": "100003", "message": "주문을 찾을 수 없음"}
    )

    with pytest.raises(OrderNotFoundError):
        raise_order_api_error(response, "상품주문번호 조회")


def test_server_error_stays_a_generic_failure() -> None:
    response = _Response(500, ValueError("not json"), text="upstream error")

    with pytest.raises(RuntimeError) as raised:
        raise_order_api_error(response, "상품주문번호 조회")

    assert not isinstance(raised.value, OrderNotFoundError)


def test_lookup_reports_order_not_found_without_claiming_an_outage() -> None:
    from services import order_service

    def _raise(**_: Any) -> Any:
        raise OrderNotFoundError("no such order")

    original = order_service.get_orders_by_order_id
    order_service.get_orders_by_order_id = _raise
    try:
        result = order_service.lookup_general_order_id(
            "token", "2026082351391541", force_refresh=True
        )
    finally:
        order_service.get_orders_by_order_id = original

    assert result["success"] is False
    assert result["error_code"] == "ORDER_NOT_FOUND"
    assert "원활하지" not in (result["error_message"] or "")


def test_transport_failure_still_reports_a_lookup_failure() -> None:
    from services import order_service

    def _raise(**_: Any) -> Any:
        raise requests.ConnectionError("boom")

    original = order_service.get_orders_by_order_id
    order_service.get_orders_by_order_id = _raise
    try:
        result = order_service.lookup_general_order_id(
            "token", "2026082351391542", force_refresh=True
        )
    finally:
        order_service.get_orders_by_order_id = original

    assert result["error_code"] == "ORDER_LOOKUP_FAILED"


def test_plan_separates_not_found_from_failed() -> None:
    from services.inquiry_processing_plan_service import (
        InquiryProcessingPlanService,
    )

    not_found = {"success": False, "orders": [], "error_code": "ORDER_NOT_FOUND"}
    failed = {"success": False, "orders": [], "error_code": "ORDER_LOOKUP_FAILED"}

    assert InquiryProcessingPlanService._order_result_status(not_found) == "NOT_FOUND"
    assert InquiryProcessingPlanService._order_result_status(failed) == "FAILED"


# --------------------------------------------------------------------------
# 3. The synthetic order snapshot is a test affordance, not a runtime one.
# --------------------------------------------------------------------------


def test_production_answer_service_never_uses_a_synthetic_order_snapshot(
    tmp_path: Any,
) -> None:
    from repositories.database import Database

    service = AnswerService(Database(tmp_path / "probe.db"))

    assert service._use_synthetic_order_snapshot() is False

    # Touching the lazy properties is what production does on its first
    # delivery inquiry, and is exactly what used to flip the decision.
    _ = service.dps_enrichment
    _ = service.order_lookup_service

    assert service._use_synthetic_order_snapshot() is False


def test_injected_dps_double_without_order_service_keeps_the_affordance(
    tmp_path: Any,
) -> None:
    from repositories.database import Database

    service = AnswerService(
        Database(tmp_path / "probe.db"), dps_enrichment=object()
    )

    assert service._use_synthetic_order_snapshot() is True


def test_an_injected_order_service_always_wins(tmp_path: Any) -> None:
    from repositories.database import Database

    service = AnswerService(
        Database(tmp_path / "probe.db"),
        dps_enrichment=object(),
        order_lookup_service=object(),
    )

    assert service._use_synthetic_order_snapshot() is False


# --------------------------------------------------------------------------
# 4. A measurement is a commitment, and two measurements can disagree.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        "HDMI 단자는 3개입니다.",
        "스탠드를 제외한 본체 무게는 6.5kg입니다.",
        "화면 크기는 43인치입니다.",
        "이 제품은 미러링을 지원합니다.",
        "이 제품은 에어플레이를 지원하지 않습니다.",
    ],
)
def test_definite_answers_are_not_hedged(answer: str) -> None:
    from services.learning_evidence_policy import is_hedged

    assert is_hedged(answer) is False


@pytest.mark.parametrize(
    "answer",
    [
        "HDMI 단자는 3개인 것 같습니다.",
        "정확한 수량은 확인이 필요합니다.",
        "담당자가 확인 후 안내드리겠습니다.",
        "아마 3개일 겁니다.",
        "",
    ],
)
def test_hedged_answers_stay_hedged(answer: str) -> None:
    from services.learning_evidence_policy import is_hedged

    assert is_hedged(answer) is True


def test_two_counts_that_disagree_are_a_conflict() -> None:
    """Polarity cannot see this; without it one of the two gets published."""

    from services.learning_evidence_policy import quantities_conflict

    assert quantities_conflict("HDMI 단자는 3개입니다.", "HDMI 단자는 2개입니다.")
    assert not quantities_conflict("HDMI 단자는 3개입니다.", "HDMI는 3개 있습니다.")
    assert not quantities_conflict("지원합니다.", "HDMI 단자는 3개입니다.")


def test_verified_count_contradicting_learning_is_reported() -> None:
    from services.learning_evidence_policy import evaluate

    approved = [
        {
            "learning_example_id": 1,
            "authority": "APPROVED",
            "answer": "HDMI 단자는 2개입니다.",
            "answer_support": 0.9,
            "matched_subquestion": "HDMI 단자가 몇 개인가요?",
            "compatibility": {"product_match": "EXACT_PRODUCT"},
        }
    ]
    decision = evaluate(
        learning_context={"similar_approved_answers": approved},
        safe_facts=[_fact("hdmi_port_count", 3, "개")],
    )

    assert decision.usable is False
    assert decision.conflict is True
    assert decision.reason == "PRODUCT_FACT_VS_LEARNING_CONFLICT"


def test_verified_count_agreeing_with_learning_is_not_a_conflict() -> None:
    from services.learning_evidence_policy import evaluate

    question = "HDMI 단자가 몇 개인가요?"
    approved = [
        {
            "learning_example_id": 1,
            "authority": "APPROVED",
            "answer": "HDMI 단자는 3개입니다.",
            "answer_support": 0.9,
            "matched_subquestion": question,
            "compatibility": {"product_match": "EXACT_PRODUCT"},
        }
    ]
    decision = evaluate(
        learning_context={
            "similar_approved_answers": approved,
            "subquestion_evidence": [
                {
                    "subquestion": question,
                    "status": "ANSWERABLE",
                    "evidence_coverage": "SUPPORTED",
                    "source": "ACTIVE_POSITIVE_LEARNING",
                }
            ],
        },
        safe_facts=[_fact("hdmi_port_count", 3, "개")],
    )

    assert decision.conflict is False
    assert decision.usable is True


# --------------------------------------------------------------------------
# 5. "It was moved, when is it now?" is a lookup, whole or split.
# --------------------------------------------------------------------------


def _classify(question: str) -> dict[str, Any]:
    from answer.source_adapter import answer_request_from_inquiry
    from services.inquiry_analysis_service import InquiryAnalysisService

    inquiry = {
        "id": 1,
        "source_type": "PRODUCT_INQUIRY",
        "inquiry_type": "PRODUCT_INQUIRY",
        "source_question_id": "fixture",
        "external_inquiry_id": "fixture",
        "title": "상품 문의",
        "content": question,
        "product_name": "삼성 43인치(107.9cm)TV",
        "order_id": "",
        "product_order_id": "",
        "raw_json": {},
        "source_answered": 0,
        "post_status": "NOT_POSTED",
    }
    return (
        InquiryAnalysisService()
        .analyze(answer_request_from_inquiry(inquiry))
        .to_dict()
    )


def _is_schedule_change(question: str) -> bool:
    analysis = _classify(question)
    return (
        str(analysis.get("detected_intent") or "") == "SCHEDULE_CHANGE"
        or str(analysis.get("inquiry_subtype") or "") == "SCHEDULE_CHANGE_REQUEST"
    )


@pytest.mark.parametrize(
    "question",
    [
        "설치 예정일이 다음 주인데 이번 주 안으로 좀 당겨주실 수 있나요?",
        "설치일을 다음 주로 좀 미뤄주실 수 있나요?",
        "설치 날짜를 이번 주로 바꿔주세요",
        "배송일 변경해주세요",
        "설치 예정일 앞당겨 주세요",
    ],
)
def test_rescheduling_requests_stay_with_staff(question: str) -> None:
    assert _is_schedule_change(question) is True


@pytest.mark.parametrize(
    "question",
    [
        # Split across two sentences, the report and the question each look
        # like something they are not; the undivided message decides.
        "설치가 미뤄졌다고 들었는데 언제 오나요?",
        "일정이 미뤄졌는데 언제 오나요?",
        "설치일이 변경됐는데 언제인가요?",
        "설치일이 변경됐다는데 언제인가요?",
    ],
)
def test_schedule_status_questions_are_lookups(question: str) -> None:
    assert _is_schedule_change(question) is False
