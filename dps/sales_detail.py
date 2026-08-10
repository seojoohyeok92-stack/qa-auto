from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from dps.dates import parse_date_value


DETAIL_MARKERS = (
    "판매조회",
    "판매처정보",
    "고객정보",
    "입금정보",
    "품목상세내역",
)

SECTION_FIELDS: dict[str, dict[str, str]] = {
    "sales_office_info": {
        "주문사유": "order_reason",
        "판매경로": "sales_channel",
        "사업장": "business_site",
        "판매장": "sales_location",
        "판매사원": "sales_employee",
        "한도코드": "limit_code",
    },
    "customer_info": {
        "고객번호": "customer_number",
        "판매번호": "dps_sales_number",
        "구매자": "buyer_name",
        "인수자": "recipient_name",
        "주소": "delivery_address",
        "요구납기일": "requested_delivery_date",
        "배달시간": "delivery_time",
        "배송정보": "delivery_note",
        "배송 정보": "delivery_note",
        "판매경로(소분류)": "sales_route_detail",
        "판매경로 (소분류)": "sales_route_detail",
    },
    "payment_info": {
        "주문금액": "order_amount",
        "입력금액": "entered_amount",
        "외상": "outstanding_amount",
        "차이금액": "payment_difference",
    },
}

ITEM_FIELDS = {
    "행번": "line_number",
    "주문유형": "order_type",
    "모델": "model_name",
    "모델명": "model_name",
    "수량": "quantity",
    "사업장": "business_site",
    "창고": "warehouse",
    "배송처": "delivery_location_code",
    "진열구분": "display_type",
    "판매단가": "unit_price",
    "판매금액": "sale_amount",
    "요구납기일": "requested_delivery_date",
    "배달시간": "delivery_time",
    "한도코드": "limit_code",
    "비고": "note",
    "SVC주문번호": "service_order_number",
}

SENSITIVE_CACHE_FIELDS = {
    "buyer_phone",
    "recipient_phone",
    "delivery_address",
}


def normalize_label(value: Any) -> str:
    text = " ".join(str(value or "").replace("\n", " ").split())
    return re.sub(r"[\[\]＊*※]", "", text.rstrip(":：").strip())


def canonical_detail_label(value: Any) -> str | None:
    text = normalize_label(value)
    known = {
        *DETAIL_MARKERS,
        *ITEM_FIELDS.keys(),
        "전화번호",
        *(
            label
            for fields in SECTION_FIELDS.values()
            for label in fields
        ),
    }
    for label in sorted(known, key=len, reverse=True):
        if (
            text == label
            or text == f"{label} {label}"
            or text == f"{label}{label}"
            or text.startswith(f"{label} ")
            or text.startswith(f"{label}{label}")
        ):
            return label
    return None


def normalize_date(value: Any) -> str | None:
    parsed = parse_date_value(value)
    return parsed.isoformat() if parsed is not None else None


def parse_required_delivery_date(value: Any) -> dict[str, Any]:
    """Normalize a DPS item-detail required date without inventing a value."""

    raw = None if value is None else str(value)
    text = str(value or "").strip()
    if not text:
        return {
            "raw_required_delivery_date": raw,
            "required_delivery_date": None,
            "date_parse_status": "MISSING",
        }
    normalized = normalize_date(text)
    return {
        "raw_required_delivery_date": raw,
        "required_delivery_date": normalized,
        "date_parse_status": "PARSED" if normalized else "PARSE_FAILED",
    }


def _quantity(value: Any) -> int | None:
    match = re.search(r"\d+", str(value or "").replace(",", ""))
    return int(match.group()) if match else None


def map_detail_items(
    headers: Sequence[Any],
    rows: Sequence[Sequence[Any]],
) -> list[dict[str, Any]]:
    normalized_headers = [
        canonical_detail_label(value) or normalize_label(value)
        for value in headers
    ]
    items: list[dict[str, Any]] = []
    for raw_row in rows:
        row = [normalize_label(value) for value in raw_row]
        # Empty cells are significant and must not shift later columns.
        if len(row) < len(normalized_headers):
            row.extend([""] * (len(normalized_headers) - len(row)))
        mapped: dict[str, Any] = {}
        for header, value in zip(normalized_headers, row):
            field = ITEM_FIELDS.get(header)
            if not field:
                continue
            mapped[field] = value or None
        if not any(mapped.values()):
            continue
        mapped["quantity"] = _quantity(mapped.get("quantity"))
        required_date = parse_required_delivery_date(
            mapped.get("requested_delivery_date")
        )
        mapped.update(required_date)
        # Keep the old key as a read-only compatibility alias. The canonical
        # DPS source field is required_delivery_date.
        mapped["requested_delivery_date"] = required_date[
            "required_delivery_date"
        ]
        items.append(mapped)
    return items


def parse_flat_detail(
    records: Sequence[Mapping[str, Any]],
    *,
    table_headers: Sequence[Any] = (),
    table_rows: Sequence[Sequence[Any]] = (),
) -> dict[str, Any]:
    """Parse UIA label/value records without inventing unlabeled values."""

    ordered = sorted(
        (
            {
                **dict(record),
                "name": normalize_label(record.get("name")),
            }
            for record in records
            if normalize_label(record.get("name"))
        ),
        key=lambda value: (
            int(value.get("top") or 0),
            int(value.get("left") or 0),
        ),
    )
    names = [
        canonical_detail_label(record["name"]) or record["name"]
        for record in ordered
    ]
    all_labels = {
        label
        for fields in SECTION_FIELDS.values()
        for label in fields
    }
    section_names = set(DETAIL_MARKERS)
    raw_labels: dict[str, str | None] = {}
    result: dict[str, Any] = {
        "sales_office_info": {},
        "customer_info": {},
        "payment_info": {},
        "detail_items": map_detail_items(table_headers, table_rows),
        "detail_raw_labels": raw_labels,
        "parse_warnings": [],
    }
    section_by_marker = {
        "판매처정보": "sales_office_info",
        "고객정보": "customer_info",
        "입금정보": "payment_info",
    }
    section_anchors = sorted(
        (
            (
                int(record.get("left") or 0),
                section_by_marker[record["name"]],
            )
            for record in ordered
            if record["name"] in section_by_marker
        ),
        key=lambda value: value[0],
    )
    item_section_top = min(
        (
            int(record.get("top") or 0)
            for record in ordered
            if record["name"] == "품목상세내역"
        ),
        default=10**9,
    )

    def section_for(record: Mapping[str, Any]) -> str | None:
        if not section_anchors:
            return None
        left = int(record.get("left") or 0)
        available = [
            section
            for anchor_left, section in section_anchors
            if anchor_left <= left
        ]
        return available[-1] if available else section_anchors[0][1]

    phone_occurrence = 0
    for index, (record, label) in enumerate(zip(ordered, names)):
        if int(record.get("top") or 0) >= item_section_top:
            continue
        if label in section_by_marker:
            continue
        if label in section_names:
            continue
        active_section = section_for(record)
        if active_section == "customer_info" and label == "전화번호":
            phone_occurrence += 1
            field = "buyer_phone" if phone_occurrence == 1 else "recipient_phone"
        else:
            field = SECTION_FIELDS.get(active_section or "", {}).get(label)
        if not field:
            continue
        value: str | None = None
        record_top = int(record.get("top") or 0)
        record_bottom = int(record.get("bottom") or record_top + 24)
        record_left = int(record.get("left") or 0)
        section_index = next(
            (
                anchor_index
                for anchor_index, (_, section) in enumerate(section_anchors)
                if section == active_section
            ),
            0,
        )
        section_right = (
            section_anchors[section_index + 1][0]
            if section_index + 1 < len(section_anchors)
            else 10**9
        )
        same_row_label_lefts = [
            int(candidate_record.get("left") or 0)
            for candidate_index, candidate_record in enumerate(ordered)
            if candidate_index != index
            and canonical_detail_label(candidate_record.get("name"))
            and int(candidate_record.get("left") or 0) > record_left
            and (
                min(
                    record_bottom,
                    int(
                        candidate_record.get("bottom")
                        or int(candidate_record.get("top") or 0) + 24
                    ),
                )
                - max(
                    record_top,
                    int(candidate_record.get("top") or 0),
                )
                > 0
            )
        ]
        value_right = min(
            [section_right, *same_row_label_lefts]
        )
        spatial_candidates: list[tuple[int, int, int, str]] = []
        for candidate_index, candidate_record in enumerate(ordered):
            if candidate_index == index:
                continue
            candidate_name = normalize_label(candidate_record.get("name"))
            if not candidate_name or canonical_detail_label(candidate_name):
                continue
            candidate_top = int(candidate_record.get("top") or 0)
            candidate_bottom = int(
                candidate_record.get("bottom") or candidate_top + 24
            )
            candidate_left = int(candidate_record.get("left") or 0)
            vertical_overlap = min(
                record_bottom, candidate_bottom
            ) - max(record_top, candidate_top)
            if (
                vertical_overlap <= 0
                or candidate_left < record_left
                or candidate_left >= value_right
            ):
                continue
            control_type = str(
                candidate_record.get("control_type") or ""
            )
            prefer_edit = field not in {
                "buyer_phone",
                "recipient_phone",
                "delivery_address",
            }
            preference = (
                0
                if (
                    (prefer_edit and control_type == "Edit")
                    or (
                        not prefer_edit
                        and control_type == "DataItem"
                    )
                )
                else 1
            )
            spatial_candidates.append(
                (
                    preference,
                    abs(candidate_top - record_top),
                    max(0, candidate_left - record_left),
                    candidate_name,
                )
            )
        if spatial_candidates:
            spatial_candidates.sort()
            value = spatial_candidates[0][3]
        for candidate in names[index + 1 : index + 5]:
            if value:
                break
            if candidate in section_names or candidate in all_labels:
                break
            if candidate != label:
                value = candidate
                break
        raw_labels[f"{active_section}.{label}"] = value
        if value:
            result[active_section][field] = value

    customer = result["customer_info"]
    customer["requested_delivery_date"] = normalize_date(
        customer.get("requested_delivery_date")
    )
    return result


def resolve_delivery_date(
    *,
    customer_requested_date: Any = None,
    item_requested_dates: Sequence[Any] = (),
    list_requested_date: Any = None,
) -> dict[str, Any]:
    """Resolve only item-detail required dates.

    Customer summary and purchase-list dates remain accepted in the signature
    for backwards API compatibility, but are intentionally not installation
    date fallbacks.
    """

    del customer_requested_date, list_requested_date
    normalized_items = [
        value for value in (normalize_date(item) for item in item_requested_dates)
        if value
    ]
    unique_items = list(dict.fromkeys(normalized_items))
    base = {
        "required_delivery_date": None,
        "raw_required_delivery_date": None,
        "installation_date": None,
        "installation_date_source": (
            "DPS_ITEM_DETAIL_REQUIRED_DELIVERY_DATE"
        ),
        "date_parse_status": "MISSING",
        "requires_human_review": False,
        "requested_delivery_date": None,
        "delivery_scheduled_date": None,
        "delivery_date_source": None,
        "delivery_date_status": "NOT_AVAILABLE",
        "customer_requested_date": None,
        "item_requested_dates": unique_items,
    }
    if len(unique_items) > 1:
        return {
            **base,
            "date_parse_status": "CONFLICT",
            "requires_human_review": True,
            "delivery_date_status": "MULTIPLE_DATES",
        }
    item_date = unique_items[0] if unique_items else None
    if item_date:
        return {
            **base,
            "required_delivery_date": item_date,
            "installation_date": item_date,
            "installation_date_source": (
                "DPS_ITEM_DETAIL_REQUIRED_DELIVERY_DATE"
            ),
            "date_parse_status": "PARSED",
            "requested_delivery_date": item_date,
            "delivery_scheduled_date": item_date,
            "delivery_date_source": (
                "DPS_ITEM_DETAIL_REQUIRED_DELIVERY_DATE"
            ),
            "delivery_date_status": "CONFIRMED",
        }
    return base


def resolve_item_required_delivery_date(
    items: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Apply the representative-item policy and retain conflict evidence."""

    values = [dict(item) for item in items]
    representative = [
        item
        for item in values
        if item.get("matches_online_order") or item.get("is_representative")
    ]
    candidates = representative or values
    dated_candidates = [
        item for item in candidates if item.get("required_delivery_date")
    ]
    unique = list(
        dict.fromkeys(
            str(item["required_delivery_date"]) for item in dated_candidates
        )
    )
    if len(unique) > 1 and not representative:
        tv_items = [
            item
            for item in dated_candidates
            if item.get("is_installation_target")
            or item.get("product_type") == "TV"
            or re.match(
                r"^(?:LH|KQ|QA|QN|UN|HG)\d{2}",
                str(item.get("model_name") or "").upper(),
            )
        ]
        tv_unique = list(
            dict.fromkeys(
                str(item["required_delivery_date"]) for item in tv_items
            )
        )
        if len(tv_unique) == 1:
            unique = tv_unique

    result = resolve_delivery_date(item_requested_dates=unique)
    raw_values = [
        item.get("raw_required_delivery_date")
        for item in candidates
        if item.get("raw_required_delivery_date") not in (None, "")
    ]
    result["raw_required_delivery_date"] = (
        raw_values[0] if len(raw_values) == 1 else raw_values
    ) or None
    if not unique and any(
        item.get("date_parse_status") == "PARSE_FAILED" for item in candidates
    ):
        result["date_parse_status"] = "PARSE_FAILED"
    result["required_delivery_date_row_count"] = len(values)
    return result


def merge_list_and_detail(
    list_result: Mapping[str, Any],
    detail_result: Mapping[str, Any] | None,
    *,
    detail_lookup: Mapping[str, Any],
) -> dict[str, Any]:
    merged = dict(list_result)
    detail = dict(detail_result or {})
    customer = dict(detail.get("customer_info") or {})
    office = dict(detail.get("sales_office_info") or {})
    payment = dict(detail.get("payment_info") or {})
    items = [
        dict(value)
        for value in detail.get("detail_items", [])
        if isinstance(value, Mapping)
    ]
    date_resolution = resolve_item_required_delivery_date(items)
    for field in (
        "customer_number",
        "buyer_name",
        "buyer_phone",
        "recipient_name",
        "recipient_phone",
        "delivery_address",
        "delivery_time",
        "delivery_note",
        "sales_route_detail",
    ):
        if customer.get(field) not in (None, ""):
            merged[field] = customer[field]
    if payment.get("order_amount"):
        merged["order_amount"] = payment["order_amount"]
    merged.update(date_resolution)
    merged["detail_items"] = items
    merged["sales_office_info"] = office
    merged["detail_lookup"] = dict(detail_lookup)
    return merged


def mask_name(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) == 1:
        return "*"
    return text[0] + "*" * (len(text) - 1)


def mask_phone(value: Any) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits:
        return None
    if len(digits) < 7:
        return "*" * len(digits)
    return f"{digits[:3]}-****-{digits[-4:]}"


def mask_address(value: Any) -> str | None:
    text = " ".join(str(value or "").split())
    if not text:
        return None
    parts = text.split()
    return " ".join((*parts[:2], "***")) if len(parts) > 2 else "***"


def sanitize_detail_for_cache(result: Mapping[str, Any]) -> dict[str, Any]:
    """Return a cache-safe copy; phone and full address are never persisted."""

    value = dict(result)
    value.pop("buyer_phone", None)
    value.pop("recipient_phone", None)
    value.pop("delivery_address", None)
    if value.get("buyer_name"):
        value["buyer_name"] = mask_name(value["buyer_name"])
    if value.get("recipient_name"):
        value["recipient_name"] = mask_name(value["recipient_name"])
    data = dict(value.get("data") or {})
    data.pop("buyer_phone", None)
    data.pop("recipient_phone", None)
    data.pop("delivery_address", None)
    if data.get("buyer_name"):
        data["buyer_name"] = mask_name(data["buyer_name"])
    if data.get("recipient_name"):
        data["recipient_name"] = mask_name(data["recipient_name"])
    value["data"] = data
    diagnostics = dict(value.get("diagnostics") or {})
    diagnostics.pop("detail_raw_labels", None)
    diagnostics.pop("detail_raw_rows", None)
    value["diagnostics"] = diagnostics
    return value
