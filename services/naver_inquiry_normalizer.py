from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from core.business_engine import classify_inquiry, determine_priority
from core.utils import get_queue_label


KST = timezone(timedelta(hours=9))


def _first(payload: dict[str, Any], *names: str) -> Any:
    return next(
        (payload[name] for name in names if payload.get(name) not in (None, "")),
        None,
    )


def _source_time(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(
                str(value).strip().replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed.astimezone(KST).isoformat(timespec="seconds")


def _bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "y", "yes", "answered", "complete"}:
        return True
    if normalized in {"false", "0", "n", "no", "waiting", "unanswered"}:
        return False
    return None


def derive_operational_metadata(
    title: Any,
    content: Any,
    *,
    order_id: Any = None,
) -> dict[str, Any]:
    """Derive collection-only metadata without order or DPS API calls."""

    text = "\n".join(
        str(part).strip()
        for part in (title, content)
        if str(part or "").strip()
    )
    if not text:
        return {
            "queue": None,
            "queue_label": None,
            "priority": None,
            "analysis": None,
            "is_delivery": None,
        }
    analysis = classify_inquiry(text)
    is_delivery = bool(analysis.get("is_delivery"))
    if not is_delivery:
        queue = "GENERAL_INQUIRY"
    elif order_id not in (None, ""):
        queue = "ORDER_LOOKUP_READY"
    else:
        queue = "CUSTOMER_CONFIRMATION_REQUIRED"
    return {
        "queue": queue,
        "queue_label": get_queue_label(queue),
        "priority": determine_priority(text, is_delivery),
        "analysis": analysis,
        "is_delivery": is_delivery,
    }


@dataclass(frozen=True)
class NormalizedInquiry:
    store_id: str
    store_code: str
    inquiry_type: str
    external_inquiry_id: str
    title: str
    content: str
    product_id: str | None
    product_name: str | None
    order_id: str | None
    product_order_id: str | None
    masked_writer_id: str | None
    answered: bool | None
    source_status: str | None
    source_created_at: str | None
    source_updated_at: str | None
    raw_payload: dict[str, Any]
    seller_answer: str | None = None

    def to_work_item(self) -> dict[str, Any]:
        metadata = derive_operational_metadata(
            self.title,
            self.content,
            order_id=self.order_id,
        )
        return {
            "store_id": self.store_id,
            "store_code": self.store_code,
            "source": self.inquiry_type,
            "source_type": self.inquiry_type,
            "inquiry_id": self.external_inquiry_id,
            "source_question_id": self.external_inquiry_id,
            "external_inquiry_id": self.external_inquiry_id,
            "category": self.inquiry_type,
            "inquiry_type": self.inquiry_type,
            "title": self.title,
            "content": self.content,
            "product_id": self.product_id,
            "product_name": self.product_name,
            "order_id": self.order_id,
            "product_order_id": self.product_order_id,
            "masked_writer_id": self.masked_writer_id,
            "writer_id": self.masked_writer_id,
            "answered": self.answered,
            "source_status": self.source_status,
            "source_created_at": self.source_created_at,
            "source_updated_at": self.source_updated_at,
            # Passed directly to the isolated Learning Layer. It is not
            # copied into inquiries.raw_json.
            "seller_answer": self.seller_answer,
            "registered_at": self.source_created_at,
            "analysis": metadata["analysis"],
            "is_delivery": metadata["is_delivery"],
            "queue": metadata["queue"],
            "queue_label": metadata["queue_label"],
            "priority": metadata["priority"],
            "raw_payload": dict(self.raw_payload),
            "original_data": dict(self.raw_payload),
            # Synchronization is collection-only. It must not run order or DPS
            # lookup and must never infer an order ID from free-form text.
            "lookup_result": None,
            "orders": [],
        }


class InquiryNormalizer:
    """Normalize Naver inquiry payloads without calling order/DPS services."""

    _PRODUCT_RAW_FIELDS = (
        "questionId",
        "productId",
        "answered",
        "createDate",
        "updateDate",
        "lastModifiedDate",
        "status",
    )
    _CUSTOMER_RAW_FIELDS = (
        "inquiryNo",
        "productId",
        "answered",
        "inquiryRegistrationDateTime",
        "inquiryModificationDateTime",
        "answerContentId",
        "inquiryCommentNo",
        "status",
    )

    @staticmethod
    def _safe_raw(
        payload: dict[str, Any], fields: tuple[str, ...]
    ) -> dict[str, Any]:
        return {
            field: payload[field]
            for field in fields
            if payload.get(field) not in (None, "")
        }

    def product(
        self,
        payload: dict[str, Any],
        *,
        store_code: str,
    ) -> NormalizedInquiry:
        external_id = _first(payload, "questionId")
        if external_id in (None, ""):
            raise ValueError("상품 문의 questionId가 없습니다.")
        created = _source_time(_first(payload, "createDate"))
        return NormalizedInquiry(
            store_id=store_code,
            store_code=store_code,
            inquiry_type="PRODUCT_INQUIRY",
            external_inquiry_id=str(external_id),
            title=str(_first(payload, "title") or "상품 문의"),
            content=str(_first(payload, "question", "content") or ""),
            product_id=(
                str(value)
                if (value := _first(payload, "productId")) not in (None, "")
                else None
            ),
            product_name=(
                str(value)
                if (value := _first(payload, "productName")) not in (None, "")
                else None
            ),
            order_id=None,
            product_order_id=None,
            masked_writer_id=(
                str(value)
                if (value := _first(payload, "maskedWriterId"))
                not in (None, "")
                else None
            ),
            answered=_bool(_first(payload, "answered")),
            source_status=(
                str(value)
                if (value := _first(payload, "status")) not in (None, "")
                else None
            ),
            source_created_at=created,
            source_updated_at=(
                _source_time(
                    _first(payload, "updateDate", "lastModifiedDate")
                )
                or created
            ),
            seller_answer=(
                str(value)
                if (value := _first(
                    payload, "sellerAnswer", "answerContent", "commentContent", "answer"
                )) not in (None, "")
                else None
            ),
            raw_payload=self._safe_raw(
                payload, self._PRODUCT_RAW_FIELDS
            ),
        )

    def customer(
        self,
        payload: dict[str, Any],
        *,
        store_code: str,
    ) -> NormalizedInquiry:
        external_id = _first(payload, "inquiryNo", "inquiryId")
        if external_id in (None, ""):
            raise ValueError("고객 문의 inquiryNo가 없습니다.")
        created = _source_time(
            _first(payload, "inquiryRegistrationDateTime", "createDate")
        )
        order_id = _first(payload, "orderId", "orderNo")
        product_order_id = _first(
            payload, "productOrderId", "productOrderNo"
        )
        return NormalizedInquiry(
            store_id=store_code,
            store_code=store_code,
            inquiry_type="CUSTOMER_INQUIRY",
            external_inquiry_id=str(external_id),
            title=str(
                _first(payload, "inquiryTitle", "title") or "고객 문의"
            ),
            content=str(
                _first(payload, "inquiryContent", "content") or ""
            ),
            product_id=(
                str(value)
                if (value := _first(payload, "productId")) not in (None, "")
                else None
            ),
            product_name=(
                str(value)
                if (value := _first(payload, "productName")) not in (None, "")
                else None
            ),
            order_id=str(order_id) if order_id not in (None, "") else None,
            product_order_id=(
                str(product_order_id)
                if product_order_id not in (None, "")
                else None
            ),
            masked_writer_id=(
                str(value)
                if (
                    value := _first(
                        payload, "maskedWriterId", "maskedCustomerId"
                    )
                )
                not in (None, "")
                else None
            ),
            answered=_bool(_first(payload, "answered")),
            source_status=(
                str(value)
                if (value := _first(payload, "status", "inquiryStatus"))
                not in (None, "")
                else None
            ),
            source_created_at=created,
            source_updated_at=(
                _source_time(
                    _first(
                        payload,
                        "inquiryModificationDateTime",
                        "updateDate",
                    )
                )
                or created
            ),
            seller_answer=(
                str(value)
                if (value := _first(
                    payload, "sellerAnswer", "answerContent", "commentContent", "answer"
                )) not in (None, "")
                else None
            ),
            raw_payload=self._safe_raw(
                payload, self._CUSTOMER_RAW_FIELDS
            ),
        )
