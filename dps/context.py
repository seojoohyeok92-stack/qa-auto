from __future__ import annotations

import hashlib
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Mapping

from dps.dates import calculate_dps_lookup_period, select_dps_date_source
from dps.identifiers import _text_identifier, select_dps_query_identifier


class DpsLookupContextError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class DPSLookupContext:
    request_id: str
    selected_inquiry_id: str
    order_id: str
    product_order_id: str | None
    dps_query_value: str
    dps_query_value_type: str
    order_date: str | None
    order_created_at: str | None
    payment_date: str | None
    payment_completed_at: str | None
    place_order_date: str | None
    shipping_due_date: str | None
    dps_date_source: str
    dps_reference_date: str
    dps_period_start: str
    dps_period_end: str
    created_at: str
    date_warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DATE_FIELDS = (
    "order_date",
    "order_created_at",
    "payment_date",
    "payment_completed_at",
    "place_order_date",
    "shipping_due_date",
)


def create_dps_lookup_context(
    order_snapshot: Mapping[str, Any],
    *,
    selected_inquiry_id: Any,
    request_id: str | None = None,
    today: Any = None,
) -> DPSLookupContext:
    """Freeze one displayed order into the request sent through every layer."""

    order_id = _text_identifier(order_snapshot.get("order_id"))
    product_order_id = _text_identifier(order_snapshot.get("product_order_id"))
    selected = select_dps_query_identifier(order_id, product_order_id)
    if selected.error or not selected.value:
        raise DpsLookupContextError(
            "DPS_ORDER_ID_MISSING",
            "네이버 주문번호가 없어 DPS 조회를 실행할 수 없습니다.",
        )
    if selected.type != "order_id" or selected.value != order_id:
        raise DpsLookupContextError(
            "DPS_QUERY_IDENTIFIER_MISMATCH",
            "DPS 조회 식별자가 현재 네이버 주문번호와 일치하지 않습니다.",
        )

    date_values = {field: order_snapshot.get(field) for field in DATE_FIELDS}
    selected_date = select_dps_date_source(date_values)
    if selected_date.reference_date is None or selected_date.source is None:
        raise DpsLookupContextError(
            "DATE_SOURCE_MISSING",
            "주문일을 확인하지 못해 DPS 조회 기간을 계산할 수 없습니다.",
        )
    period = calculate_dps_lookup_period(
        selected_date.reference_date,
        today=today,
    )
    inquiry_id = _text_identifier(selected_inquiry_id)
    if not inquiry_id:
        raise DpsLookupContextError(
            "SELECTED_INQUIRY_ID_MISSING",
            "현재 선택된 문의를 확정하지 못했습니다.",
        )
    resolved_request_id = _text_identifier(request_id) or str(uuid.uuid4())
    return DPSLookupContext(
        request_id=resolved_request_id,
        selected_inquiry_id=inquiry_id,
        order_id=order_id,
        product_order_id=product_order_id,
        dps_query_value=order_id,
        dps_query_value_type="order_id",
        order_date=_text_identifier(date_values["order_date"]),
        order_created_at=_text_identifier(date_values["order_created_at"]),
        payment_date=_text_identifier(date_values["payment_date"]),
        payment_completed_at=_text_identifier(
            date_values["payment_completed_at"]
        ),
        place_order_date=_text_identifier(date_values["place_order_date"]),
        shipping_due_date=_text_identifier(date_values["shipping_due_date"]),
        dps_date_source=selected_date.source,
        dps_reference_date=selected_date.reference_date.isoformat(),
        dps_period_start=period.start.isoformat(),
        dps_period_end=period.end.isoformat(),
        created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        date_warnings=tuple((*selected_date.warnings, *period.warnings)),
    )


def identifier_fingerprint(value: Any) -> dict[str, str | None]:
    text = _text_identifier(value)
    if not text:
        return {"tail": None, "hash": None}
    return {
        "tail": text[-4:],
        "hash": hashlib.sha256(text.encode("utf-8")).hexdigest()[:12],
    }
