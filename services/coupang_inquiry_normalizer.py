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


def coupang_store_code(account_code: object | None = None) -> str:
    """Return an account-scoped common-Inquiry store identity.

    The legacy single-account collector continues to use ``COUPANG``.  A
    historical import, however, must never let account-local inquiry IDs from
    two seller accounts share an Inquiry unique key.
    """

    account = str(account_code or "").strip().upper()
    return f"COUPANG_{account}" if account else COUPANG_STORE_CODE


def _single_comment_answer(
    comments: object,
) -> tuple[str | None, str | None, str | None, str]:
    """Extract only an unambiguous seller-answer candidate.

    The documented response has no observed author/role field.  One comment
    can therefore be retained as a candidate; multiple comments are preserved
    in provenance but are deliberately not selected or merged.
    """

    if not isinstance(comments, list) or not comments:
        return None, None, None, "NO_COMMENT"
    if len(comments) != 1 or not isinstance(comments[0], dict):
        return None, None, None, "MULTI_COMMENT_REVIEW"
    comment = comments[0]
    answer = str(comment.get("content") or "").strip() or None
    return (
        answer,
        _source_time(comment.get("inquiryCommentAt")),
        _value(comment, "inquiryCommentId"),
        "SINGLE_COMMENT" if answer else "EMPTY_COMMENT",
    )


@dataclass(frozen=True)
class CoupangNormalizedInquiry:
    account_code: str | None
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
    seller_answer: str | None = None
    answer_created_at: str | None = None
    inquiry_comment_id: str | None = None
    answer_selection_status: str = "NO_COMMENT"

    def to_work_item(self) -> dict[str, Any]:
        return {
            "store_id": coupang_store_code(self.account_code),
            "store_code": coupang_store_code(self.account_code),
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
            "source_metadata_json": {
                "market": "COUPANG",
                "account_code": self.account_code,
                "seller_answer_selection": self.answer_selection_status,
                "inquiry_comment_id": self.inquiry_comment_id,
                "answer_created_at": self.answer_created_at,
            },
            # Collection-only: do not import a remote answer into Learning or
            # invoke answer/post paths through this work item.
            "seller_answer": None,
            "posted_answer": None,
            "lookup_result": None,
            "orders": [],
        }


class CoupangInquiryNormalizer:
    """Map the two documented Coupang inquiry response schemas."""

    def online(
        self, payload: dict[str, Any], *, account_code: str | None = None
    ) -> CoupangNormalizedInquiry:
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
        seller_answer, answer_created_at, comment_id, selection_status = (
            _single_comment_answer(comments)
        )
        return CoupangNormalizedInquiry(
            account_code=str(account_code or "").strip().upper() or None,
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
            seller_answer=seller_answer,
            answer_created_at=answer_created_at,
            inquiry_comment_id=comment_id,
            answer_selection_status=selection_status,
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
            account_code=None,
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
