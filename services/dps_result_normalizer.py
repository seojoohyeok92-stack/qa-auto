from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Mapping

from services.dps_lookup_policy import DpsLookupStatus


EMPTY_MARKERS = {"", "-", "없음", "n/a", "na", "none", "null"}
SENSITIVE_KEYS = (
    "phone",
    "mobile",
    "address",
    "buyer",
    "recipient",
    "customer",
    "token",
    "secret",
    "password",
    "cookie",
    "authorization",
)


def meaningful(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return None if text.casefold() in EMPTY_MARKERS else text
    return value


def normalize_date(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = meaningful(value)
    if not isinstance(text, str):
        return None
    match = re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", text)
    if not match:
        return None
    try:
        return date(*(int(part) for part in match.groups())).isoformat()
    except ValueError:
        return None


def sanitize_raw_result(value: Any, key: str = "") -> Any:
    if any(marker in key.casefold() for marker in SENSITIVE_KEYS):
        return None
    if isinstance(value, Mapping):
        return {
            str(child_key): sanitize_raw_result(child, str(child_key))
            for child_key, child in value.items()
        }
    if isinstance(value, list):
        return [sanitize_raw_result(child) for child in value[:100]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _status(raw: Mapping[str, Any]) -> DpsLookupStatus:
    code = str(raw.get("code") or raw.get("error_code") or "").upper()
    source_status = str(raw.get("status") or "").upper()
    detail = raw.get("detail_lookup")
    if not isinstance(detail, Mapping):
        detail = {}
    detail_status = str(detail.get("status") or "").upper()
    combined = " ".join((code, source_status, detail_status))
    if any(token in combined for token in ("PARSE", "RESPONSE_INVALID")):
        return DpsLookupStatus.PARSE_ERROR
    if code in {
        "AGENT_CONNECTION_FAILED",
        "AGENT_CONNECT_TIMEOUT",
        "AGENT_REQUEST_FAILED",
        "AGENT_RESTART_REQUIRED",
        "AGENT_START_FAILED",
        "AGENT_START_TIMEOUT",
    }:
        return DpsLookupStatus.AGENT_OFFLINE
    if any(
        token in combined
        for token in (
            "DPS_LOGIN_REQUIRED",
            "LOGIN_REQUIRED",
            "OTP_REQUIRED",
            "CHROME_NOT_FOUND",
            "DPS_TAB_NOT_FOUND",
        )
    ):
        return DpsLookupStatus.AUTOMATION_ERROR
    if "TIMEOUT" in combined:
        return DpsLookupStatus.TIMEOUT
    if any(
        token in combined
        for token in (
            "DETAIL_OPEN_FAILED",
            "DETAIL_CLOSE_FAILED",
            "ORDER_INPUT_NOT_FOUND",
            "ORDER_INPUT_FAILED",
            "WRONG_FIELD_INPUT",
            "INPUT_VERIFY_FAILED",
            "NAVIGATION_FAILED",
        )
    ):
        return DpsLookupStatus.AUTOMATION_ERROR
    if raw.get("success") and raw.get("found") is not False:
        return DpsLookupStatus.SUCCESS
    if raw.get("found") is False or code == "NO_DPS_RESULT":
        return DpsLookupStatus.NOT_FOUND
    if "CANCEL" in combined:
        return DpsLookupStatus.CANCELLED
    return DpsLookupStatus.AUTOMATION_ERROR


def normalize_dps_result(
    raw: Mapping[str, Any],
    *,
    order_id: str,
    elapsed_seconds: float,
    cache_used: bool = False,
) -> dict[str, Any]:
    data = raw.get("data") if isinstance(raw.get("data"), Mapping) else {}
    status = _status(raw)
    queried_at = meaningful(
        raw.get("queried_at") or data.get("queried_at")
    ) or datetime.now().astimezone().isoformat(timespec="seconds")
    detail_items = [
        item
        for item in data.get("detail_items", [])
        if isinstance(item, Mapping)
    ]
    date_candidates = (
        ("required_delivery_date", data.get("required_delivery_date")),
        ("installation_date", data.get("installation_date")),
        ("installation_date_raw", data.get("installation_date_raw")),
        ("requiredDeliveryDate", data.get("requiredDeliveryDate")),
        ("품목상세내역 요구납기일", data.get("품목상세내역 요구납기일")),
        ("required_delivery_date", raw.get("required_delivery_date")),
        ("installation_date", raw.get("installation_date")),
        ("installation_date_raw", raw.get("installation_date_raw")),
        ("requiredDeliveryDate", raw.get("requiredDeliveryDate")),
        ("품목상세내역 요구납기일", raw.get("품목상세내역 요구납기일")),
    )
    selected_date_field, selected_date_value = next(
        (
            (field, value)
            for field, value in date_candidates
            if value not in (None, "")
        ),
        (None, None),
    )
    required_delivery_date = normalize_date(selected_date_value)
    raw_required_delivery_date = (
        data.get("raw_required_delivery_date")
        if data.get("raw_required_delivery_date") not in (None, "")
        else selected_date_value
    )
    date_parse_status = str(
        data.get("date_parse_status") or ""
    ).upper()
    if not date_parse_status:
        if required_delivery_date:
            date_parse_status = "PARSED"
        elif any(
            str(item.get("date_parse_status") or "").upper()
            == "PARSE_FAILED"
            for item in detail_items
        ):
            date_parse_status = "PARSE_FAILED"
        else:
            date_parse_status = "MISSING"
    source = meaningful(
        data.get("installation_date_source")
        or raw.get("installation_date_source")
    )
    if required_delivery_date and not source and selected_date_field:
        source = "DPS_ITEM_DETAIL_REQUIRED_DELIVERY_DATE"
    installation_date = (
        required_delivery_date
        if (
            required_delivery_date
            and source == "DPS_ITEM_DETAIL_REQUIRED_DELIVERY_DATE"
            and date_parse_status == "PARSED"
        )
        else None
    )
    requires_human_review = bool(
        data.get("requires_human_review")
        or date_parse_status in {"CONFLICT", "PARTIAL"}
    )
    warnings: list[str] = []
    source_status = meaningful(raw.get("status"))
    if source_status in {
        "DETAIL_DATE_CONFLICT",
        "RESULT_FOUND_DETAIL_PARTIAL",
        "DETAIL_CLOSE_FAILED",
    }:
        warnings.append(str(source_status))
    if date_parse_status == "PARSE_FAILED":
        warnings.append("DPS_REQUIRED_DATE_PARSE_FAILED")
    elif date_parse_status == "CONFLICT":
        warnings.append("DPS_REQUIRED_DATE_CONFLICT")
    elif date_parse_status == "PARTIAL":
        warnings.append("DPS_REQUIRED_DATE_PARTIAL")
    elif date_parse_status == "MISSING":
        warnings.append("DPS_REQUIRED_DATE_MISSING")
    return {
        "lookup_required": True,
        "lookup_status": status.value,
        "source": "DPS_AGENT",
        "order_id": order_id,
        "sales_number": meaningful(
            data.get("dps_sales_number")
            or raw.get("dps_sale_number")
            or raw.get("sales_number")
        ),
        "delivery_status": meaningful(
            data.get("delivery_status")
            or data.get("progress_status")
            or raw.get("delivery_status")
        ),
        "installation_status": meaningful(
            data.get("installation_status")
            or raw.get("installation_status")
        ),
        "required_delivery_date": required_delivery_date,
        "installation_date": installation_date,
        "installation_date_text": required_delivery_date,
        "installation_date_source": source,
        "raw_required_delivery_date": raw_required_delivery_date,
        "date_parse_status": date_parse_status,
        "requires_human_review": requires_human_review,
        "required_delivery_date_row_count": int(
            data.get("required_delivery_date_row_count")
            or len(detail_items)
        ),
        "installation_time_text": meaningful(
            data.get("delivery_time")
            or data.get("visit_time_window")
            or raw.get("visit_time_window")
        ),
        "installation_type": meaningful(
            data.get("installation_type")
        ),
        "product_name": meaningful(
            data.get("product_name")
            or data.get("model_name")
            or raw.get("model_name")
        ),
        "customer_region": None,
        "queried_at": str(queried_at),
        "cache_used": bool(cache_used),
        "cache_age_seconds": 0,
        "elapsed_seconds": round(max(0.0, elapsed_seconds), 3),
        "error_code": meaningful(
            raw.get("code") or raw.get("error_code")
        ),
        "error_message": meaningful(
            raw.get("message") or raw.get("error_message")
        ),
        "warnings": warnings,
    }


USER_STATUS_MESSAGES = {
    DpsLookupStatus.WAITING_FOR_ORDER_ID: "네이버 일반 주문번호가 없어 DPS 조회를 실행하지 않았습니다.",
    DpsLookupStatus.NOT_FOUND: "DPS에서 주문 정보를 찾지 못했습니다.",
    DpsLookupStatus.TIMEOUT: "DPS 조회 응답 시간이 초과되었습니다. 다시 조회해 주세요.",
    DpsLookupStatus.AGENT_OFFLINE: "DPS Agent에 연결할 수 없습니다. Agent 실행 상태를 확인해 주세요.",
    DpsLookupStatus.PARSE_ERROR: "DPS 상세정보를 읽지 못했습니다. 직원 확인이 필요합니다.",
    DpsLookupStatus.AUTOMATION_ERROR: "DPS 화면 자동화 중 오류가 발생했습니다. 직원 확인이 필요합니다.",
    DpsLookupStatus.CANCELLED: "DPS 조회가 취소되었습니다.",
}


def user_message_for_status(status: DpsLookupStatus) -> str:
    return USER_STATUS_MESSAGES.get(status, "")
