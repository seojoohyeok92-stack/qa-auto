"""Coupang inquiry payload normalization for collection only.

No model resolution, Product Knowledge lookup, DPS lookup, answer generation,
or posting happens in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from repositories.log_repository import mask_sensitive_data


KST = timezone(timedelta(hours=9))
COUPANG_STORE_CODE = "COUPANG"
COUPANG_ONLINE_INQUIRY = "COUPANG_ONLINE_INQUIRY"
COUPANG_CONTACT_CENTER_INQUIRY = "COUPANG_CONTACT_CENTER_INQUIRY"


def _source_time(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).strip().replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed.astimezone(KST).isoformat(timespec="seconds")


def _value(payload: dict[str, Any], field: str) -> str | None:
    value = payload.get(field)
    return str(value) if value not in (None, "") else None


def _safe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep source provenance while masking Contact Center PII recursively."""

    return mask_sensitive_data(dict(payload))


@dataclass(frozen=True)
class CoupangNormalizedInquiry:
    source_type: str
    external_inquiry_id: str
    title: str
    content: str
    product_id: str | None
    product_name: str | None
    order_id: str | None
    answered: bool
    source_status: str | None
    source_created_at: str | None
    raw_payload: dict[str, Any]

    def to_work_item(self) -> dict[str, Any]:
        return {
            "store_id": COUPANG_STORE_CODE,
            "store_code": COUPANG_STORE_CODE,
            "source": self.source_type,
            "source_type": self.source_type,
            "inquiry_id": self.external_inquiry_id,
            "source_question_id": self.external_inquiry_id,
            "external_inquiry_id": self.external_inquiry_id,
            "category": self.source_type,
            "inquiry_type": self.source_type,
            "title": self.title,
            "content": self.content,
            "product_id": self.product_id,
            "product_name": self.product_name,
            "order_id": self.order_id,
            "product_order_id": None,
            "masked_writer_id": None,
            "answered": self.answered,
            "source_status": self.source_status,
            "source_created_at": self.source_created_at,
            "source_updated_at": self.source_created_at,
            "registered_at": self.source_created_at,
            "raw_payload": dict(self.raw_payload),
            "original_data": dict(self.raw_payload),
            # Collection-only: do not import a remote answer into Learning or
            # invoke answer/post paths through this work item.
            "seller_answer": None,
            "posted_answer": None,
            "lookup_result": None,
            "orders": [],
        }


class CoupangInquiryNormalizer:
    """Map the two documented Coupang inquiry response schemas."""

    def online(self, payload: dict[str, Any]) -> CoupangNormalizedInquiry:
        inquiry_id = _value(payload, "inquiryId")
        if not inquiry_id:
            raise ValueError("Coupang online inquiry is missing inquiryId")
        order_ids = payload.get("orderIds")
        single_order_id = (
            str(order_ids[0])
            if isinstance(order_ids, list) and len(order_ids) == 1
            and order_ids[0] not in (None, "")
            else None
        )
        comments = payload.get("commentDtoList")
        return CoupangNormalizedInquiry(
            source_type=COUPANG_ONLINE_INQUIRY,
            external_inquiry_id=inquiry_id,
            title="Coupang 상품문의",
            content=str(payload.get("content") or ""),
            product_id=_value(payload, "sellerProductId"),
            product_name=None,
            order_id=single_order_id,
            answered=bool(comments) if isinstance(comments, list) else False,
            source_status=None,
            source_created_at=_source_time(payload.get("inquiryAt")),
            raw_payload=_safe_payload(payload),
        )

    def contact_center(self, payload: dict[str, Any]) -> CoupangNormalizedInquiry:
        inquiry_id = _value(payload, "inquiryId")
        if not inquiry_id:
            raise ValueError("Coupang contact center inquiry is missing inquiryId")
        counseling_status = _value(payload, "csPartnerCounselingStatus")
        inquiry_status = _value(payload, "inquiryStatus")
        source_status = ":".join(
            part for part in (inquiry_status, counseling_status) if part
        ) or None
        return CoupangNormalizedInquiry(
            source_type=COUPANG_CONTACT_CENTER_INQUIRY,
            external_inquiry_id=inquiry_id,
            title="Coupang Contact Center 문의",
            content=str(payload.get("content") or ""),
            product_id=None,
            product_name=_value(payload, "itemName"),
            order_id=_value(payload, "orderId"),
            answered=str(counseling_status or "").lower() == "answered",
            source_status=source_status,
            source_created_at=_source_time(payload.get("inquiryAt")),
            raw_payload=_safe_payload(payload),
        )
