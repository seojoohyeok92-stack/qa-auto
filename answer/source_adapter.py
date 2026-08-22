from __future__ import annotations

import json
from typing import Any

from answer.models import AnswerRequest
from answer.text_utils import (
    normalize_option_name,
    normalize_product_name,
    normalize_question_text,
    normalize_space,
)


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _first(*values: Any) -> Any:
    return next((value for value in values if value not in (None, "")), "")


def _combined_inquiry_text(title: Any, content: Any) -> str:
    normalized_title = normalize_question_text(title)
    normalized_content = normalize_question_text(content)
    if not normalized_title:
        return normalized_content
    if not normalized_content or normalized_content == normalized_title:
        return normalized_title
    return f"{normalized_title}\n{normalized_content}"


def answer_request_from_inquiry(row: dict[str, Any]) -> AnswerRequest:
    raw = _mapping(row.get("raw_json"))
    order_lookup = _mapping(raw.get("order_lookup"))
    original = _mapping(raw.get("original_data"))
    return AnswerRequest(
        inquiry_id=row.get("id"),
        question_id=normalize_space(row.get("source_question_id")),
        store_code=normalize_space(row.get("store_code")),
        inquiry_type=normalize_space(
            _first(row.get("inquiry_type"), row.get("source_type"))
        ),
        question=_combined_inquiry_text(
            row.get("title"),
            row.get("content"),
        ),
        product_name=normalize_product_name(row.get("product_name")),
        option_name=normalize_option_name(row.get("option_name")),
        customer_display=normalize_space(row.get("customer_display")),
        order_id=normalize_space(
            _first(
                row.get("order_id"),
                order_lookup.get("order_id"),
                raw.get("order_id"),
                raw.get("orderId"),
                original.get("orderId"),
            )
        ),
        product_order_id=normalize_space(
            _first(
                row.get("product_order_id"),
                order_lookup.get("product_order_id"),
                raw.get("product_order_id"),
                raw.get("productOrderId"),
                original.get("productOrderId"),
            )
        ),
        existing_answer=normalize_question_text(
            _first(raw.get("existing_answer"), raw.get("answer"))
        ),
        metadata={
            "product_id": row.get("product_id"),
            "source_type": row.get("source_type"),
            "inquiry_title": row.get("title"),
            "inquiry_content": row.get("content"),
            "question_source_fields": ["title", "content"],
            "registered_at": row.get("registered_at"),
            # Fallback reference for judging whether a DPS schedule had
            # already passed when this inquiry arrived.
            "created_at": row.get("created_at"),
            "order_date": _first(
                row.get("order_date"),
                raw.get("order_date"),
                raw.get("orderDate"),
            ),
            "order_created_at": _first(
                raw.get("order_created_at"), raw.get("orderCreatedAt")
            ),
            "payment_date": _first(
                raw.get("payment_date"), raw.get("paymentDate")
            ),
            "payment_completed_at": _first(
                raw.get("payment_completed_at"),
                raw.get("paymentCompletedAt"),
            ),
            "place_order_date": _first(
                raw.get("place_order_date"), raw.get("placeOrderDate")
            ),
            "shipping_due_date": _first(
                raw.get("shipping_due_date"), raw.get("shippingDueDate")
            ),
            "order_status": row.get("order_status"),
            "is_private": row.get("is_private"),
            "source_metadata": _mapping(
                row.get("source_metadata_json")
            ),
        },
    )


def answer_request_from_work_item(
    work_item: dict[str, Any],
) -> AnswerRequest:
    original = _mapping(work_item.get("original_data"))
    product_order_ids = work_item.get("product_order_ids")
    product_order_id = (
        product_order_ids[0]
        if isinstance(product_order_ids, list) and product_order_ids
        else work_item.get("product_order_id")
    )
    return AnswerRequest(
        question_id=normalize_space(
            _first(
                work_item.get("inquiry_id"),
                work_item.get("source_question_id"),
                original.get("questionId"),
                original.get("inquiryNo"),
            )
        ),
        store_code=normalize_space(work_item.get("store_code")),
        inquiry_type=normalize_space(
            _first(work_item.get("category"), work_item.get("source"))
        ),
        question=_combined_inquiry_text(
            work_item.get("title"),
            work_item.get("content"),
        ),
        product_name=normalize_product_name(work_item.get("product_name")),
        option_name=normalize_option_name(
            _first(
                work_item.get("product_option"),
                work_item.get("option_name"),
            )
        ),
        customer_display=normalize_space(
            _first(
                work_item.get("customer_name"),
                work_item.get("customer_id"),
                work_item.get("writer_id"),
            )
        ),
        order_id=normalize_space(work_item.get("order_id")),
        product_order_id=normalize_space(product_order_id),
        existing_answer=normalize_question_text(
            work_item.get("existing_answer")
        ),
        metadata={
            "product_id": work_item.get("product_id"),
            "source_type": work_item.get("source"),
            "inquiry_title": work_item.get("title"),
            "inquiry_content": work_item.get("content"),
            "question_source_fields": ["title", "content"],
            "registered_at": work_item.get("registered_at"),
            "created_at": work_item.get("created_at"),
        },
    )
