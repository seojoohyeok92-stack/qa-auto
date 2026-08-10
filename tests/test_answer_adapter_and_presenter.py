from __future__ import annotations

from answer.source_adapter import (
    answer_request_from_inquiry,
    answer_request_from_work_item,
)
from ui.answer_presenter import build_answer_display


def test_inquiry_adapter_separates_order_identifiers() -> None:
    request = answer_request_from_inquiry(
        {
            "id": 1,
            "store_code": "OJE_PLUS",
            "source_type": "CUSTOMER_INQUIRY",
            "source_question_id": "Q-1",
            "content": "질문",
            "product_name": "상품",
            "option_name": "옵션",
            "order_id": "ORDER-1",
            "product_order_id": "PRODUCT-ORDER-1",
            "raw_json": {"existing_answer": "기존 답변"},
        }
    )
    assert request.order_id == "ORDER-1"
    assert request.product_order_id == "PRODUCT-ORDER-1"
    assert request.existing_answer == "기존 답변"


def test_work_item_adapter_handles_missing_fields() -> None:
    request = answer_request_from_work_item(
        {
            "store_code": "OJE_PLUS",
            "source": "PRODUCT_INQUIRY",
            "original_data": {"questionId": "ORIGINAL-Q"},
        }
    )
    assert request.question_id == "ORIGINAL-Q"
    assert request.question == ""
    assert request.order_id == ""
    assert request.product_order_id == ""


def test_work_item_adapter_never_turns_product_order_into_order_id() -> None:
    request = answer_request_from_work_item(
        {
            "store_code": "OJE_PLUS",
            "source": "CUSTOMER_INQUIRY",
            "inquiry_id": "Q-2",
            "product_order_ids": ["PRODUCT-ONLY"],
        }
    )
    assert request.order_id == ""
    assert request.product_order_id == "PRODUCT-ONLY"


def test_presenter_builds_generated_display() -> None:
    display = build_answer_display(
        {
            "program_status": "GENERATED",
            "category": "배송/택배",
            "reason": "규칙",
            "provider": "rules",
            "original_answer": "답변",
            "posted": 0,
            "created_at": "2026-07-29T12:00:00Z",
        },
        {"warnings": ["확인"], "auto_answerable": True},
    )
    assert display["status_label"] == "초안 생성 완료"
    assert display["auto_answerable"] is True
    assert display["needs_review"] is False
    assert display["warnings"] == ["확인"]


def test_presenter_marks_unsupported_as_review_required() -> None:
    display = build_answer_display(
        {
            "program_status": "NOT_SUPPORTED",
            "category": "기타/직원확인",
            "reason": "미지원",
            "provider": "rules",
            "original_answer": "",
            "posted": 0,
        }
    )
    assert display["status_label"] == "자동답변 미지원"
    assert display["auto_answerable"] is False
    assert display["needs_review"] is True
