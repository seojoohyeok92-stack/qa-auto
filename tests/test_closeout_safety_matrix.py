"""마감 검증: 복합문의 안전과 배송/주문 정책이 함께 성립하는가.

개별 gate 는 각자의 파일에서 이미 고정되어 있다. 여기서 확인하는 것은 그것들이
한 판정 안에서 같이 작동하는가 -- deterministic 하게 해결된 atom 이 근거 없는
atom 을 데리고 나가지 못하는가, 그리고 배송/주문 정책이 그 위에서 그대로
유지되는가 -- 이다.

실제 provider 호출은 없다. 판정에 쓰이는 것은 파이프라인이 이미 기록한
metadata 뿐이므로, 그 metadata 를 그대로 주고 실제 gate 를 부른다.
"""
from __future__ import annotations

import pytest

from services.auto_processing_eligibility_service import (
    EVIDENCE_NOT_SUFFICIENT,
    SEMANTIC_COVERAGE_INCOMPLETE,
    AutoProcessingEligibilityService,
)

GROUNDED = "기사님이 방문하여 설치해드리며 폐가전 수거도 함께 진행됩니다."


def entry(status: str, index: int = 1) -> dict:
    resolved = status == "ANSWERABLE"
    return {
        "subquestion": "질문 %d" % index,
        "status": status,
        "source": "ACTIVE_POSITIVE_LEARNING" if resolved else None,
        "learning_ids": [100 + index] if resolved else [],
        "historical_case_ids": [],
    }


def decide(*, route: str, statuses, answer: str = GROUNDED,
           coverage: str = "PASS", metadata_extra: dict | None = None,
           inquiry_extra: dict | None = None):
    metadata = {
        "selected_answer_route": route,
        "semantic_coverage": {"status": coverage},
        "hybrid": {
            "subquestion_evidence": [
                entry(status, index) for index, status in enumerate(statuses, start=1)
            ],
            "draft": {"learning_usage": [], "requires_review": False,
                      "missing_information": []},
            "self_review": {"requires_review": False},
        },
    }
    metadata.update(metadata_extra or {})
    inquiry = {"source_answered": 0, "post_status": "NOT_POSTED"}
    inquiry.update(inquiry_extra or {})
    return AutoProcessingEligibilityService().evaluate(
        inquiry=inquiry,
        draft={"metadata_json": metadata, "original_answer": answer,
               "validation_status": "PASS"},
        route=route,
    )


# ==================================================== 복합문의 안전 (종료조건 5)
def test_a_template_only_inquiry_publishes_as_before():
    """A. Template 이 문의 전체를 해결한 경우. 기존 동작 그대로."""
    result = decide(route="TEMPLATE", statuses=["ANSWERABLE"])
    assert EVIDENCE_NOT_SUFFICIENT not in result.reasons


def test_a_template_beside_verified_learning_publishes():
    """B. Template + Learning. 두 atom 모두 근거가 있다."""
    result = decide(route="TEMPLATE", statuses=["ANSWERABLE", "ANSWERABLE"])
    assert EVIDENCE_NOT_SUFFICIENT not in result.reasons


def test_a_product_catalogue_route_publishes():
    """C. Product Catalog 가 해결한 경우."""
    result = decide(route="PRODUCT_DB", statuses=["ANSWERABLE"])
    assert EVIDENCE_NOT_SUFFICIENT not in result.reasons


def test_an_unsupported_atom_beside_a_resolved_one_blocks_the_inquiry():
    """D. 이 파일이 존재하는 이유.

    Template 이 한 atom 을 해결했다는 사실이 근거 없는 다른 atom 을 데리고
    나가지 못한다. 부분 해결은 전체 해결이 아니다.
    """
    result = decide(route="GPT_HYBRID",
                    statuses=["ANSWERABLE", "NO_RELIABLE_SOURCE"])

    assert EVIDENCE_NOT_SUFFICIENT in result.reasons
    assert result.decision != "SAFE"


def test_three_atoms_with_one_unsupported_block():
    result = decide(route="GPT_HYBRID",
                    statuses=["ANSWERABLE", "ANSWERABLE", "NO_RELIABLE_SOURCE"])
    assert result.decision != "SAFE"


def test_three_atoms_all_supported_do_not_block():
    result = decide(route="GPT_HYBRID",
                    statuses=["ANSWERABLE"] * 3)
    assert EVIDENCE_NOT_SUFFICIENT not in result.reasons


# ================================================ 배송/주문 정책 (종료조건 6)
def test_a_pre_purchase_delivery_question_is_held():
    """PRE_PURCHASE 배송 질문은 자동답변되지 않는다."""
    result = decide(
        route="GPT_HYBRID", statuses=["DELIVERY_SCHEDULE_REVIEW"],
        metadata_extra={"requires_manual_review": True,
                        "processing_plan": {"analysis": {}, "needs_staff_review": True}})
    assert result.decision != "SAFE"


def test_an_ambiguous_delivery_question_is_held():
    result = decide(
        route="GPT_HYBRID", statuses=["DELIVERY_SCHEDULE_REVIEW"],
        coverage="PARTIAL",
        metadata_extra={"requires_manual_review": True})
    assert result.decision != "SAFE"


def test_a_current_order_question_awaiting_dps_is_held():
    """CURRENT_ORDER 에서 필요한 조회가 끝나지 않았으면 게시되지 않는다."""
    result = decide(route="GPT_HYBRID", statuses=["NEEDS_DPS"],
                    metadata_extra={"requires_manual_review": True})
    assert result.decision != "SAFE"


def test_a_generic_procedure_answer_needs_no_order_lookup():
    """일반 절차 문의는 근거만 있으면 주문 조회 없이 게시된다.

    안전을 위해 모든 문의를 조회로 보내지 않는다는 쪽의 계약이다.
    """
    result = decide(route="GPT_HYBRID", statuses=["ANSWERABLE"])
    assert EVIDENCE_NOT_SUFFICIENT not in result.reasons
    assert SEMANTIC_COVERAGE_INCOMPLETE not in result.reasons


# ================================================== 근거 없는 답변은 게시 불가
def test_an_inquiry_with_no_source_at_all_is_held():
    result = decide(route="GPT_HYBRID",
                    statuses=["NO_RELIABLE_SOURCE"] * 4,
                    answer="확인 후 안내드리겠습니다.")
    assert EVIDENCE_NOT_SUFFICIENT in result.reasons
    assert result.decision != "SAFE"


def test_the_safe_order_number_request_still_publishes():
    """주문번호를 요청하는 답변은 어떤 사실도 주장하지 않는다."""
    result = decide(route="ORDER_ID_REQUEST",
                    statuses=["NO_RELIABLE_SOURCE"],
                    answer="주문번호를 알려주시면 확인해 드리겠습니다.")
    assert EVIDENCE_NOT_SUFFICIENT not in result.reasons
