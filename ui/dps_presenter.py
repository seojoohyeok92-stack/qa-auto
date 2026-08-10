from __future__ import annotations

from typing import Any, Mapping

from core.time_utils import format_datetime_kst
from services.dps_result_normalizer import normalize_date

STATUS_LABELS = {
    "NOT_REQUIRED": "조회 불필요",
    "WAITING_FOR_ORDER_ID": "일반 주문번호 필요",
    "PENDING": "조회 대기",
    "RUNNING": "조회 중",
    "SUCCESS": "조회 성공",
    "NOT_FOUND": "주문정보 없음",
    "TIMEOUT": "응답 시간 초과",
    "AGENT_OFFLINE": "Agent 연결 실패",
    "AUTOMATION_ERROR": "화면 자동화 오류",
    "PARSE_ERROR": "상세정보 해석 오류",
    "STALE_CACHE": "만료된 캐시",
    "CANCELLED": "조회 취소",
}


def installation_date_display(
    normalized: Mapping[str, Any] | None,
    *,
    queried: bool,
) -> str:
    value = dict(normalized or {})
    status = str(value.get("date_parse_status") or "").upper()
    if not queried:
        return "아직 DPS 조회를 실행하지 않았습니다."
    if status == "CONFLICT" or value.get("requires_human_review"):
        return "복수 품목의 요구납기일이 달라 확인이 필요합니다."
    if status == "PARSE_FAILED":
        return "요구납기일 형식을 확인할 수 없습니다."
    if value.get("installation_date"):
        return str(value["installation_date"])
    return "DPS 상세에 요구납기일이 없습니다."


def installation_date_value(
    normalized: Mapping[str, Any] | None,
) -> str | None:
    value = dict(normalized or {})
    direct = normalize_date(value.get("installation_date"))
    if direct:
        return direct
    required = normalize_date(value.get("required_delivery_date"))
    if (
        required
        and value.get("installation_date_source")
        == "DPS_ITEM_DETAIL_REQUIRED_DELIVERY_DATE"
        and str(value.get("date_parse_status") or "").upper()
        == "PARSED"
    ):
        return required
    return None


def build_dps_display(
    *,
    lookup_required: bool,
    order_id: str | None,
    latest_row: Mapping[str, Any] | None,
    pending_status: str | None = None,
) -> dict[str, Any]:
    normalized = {}
    if latest_row and isinstance(
        latest_row.get("normalized_result_json"), Mapping
    ):
        normalized = dict(latest_row["normalized_result_json"])
    status = str(
        normalized.get("lookup_status")
        or (latest_row or {}).get("lookup_status")
        or pending_status
        or ("PENDING" if lookup_required else "NOT_REQUIRED")
    )
    return {
        "lookup_required": bool(lookup_required),
        "lookup_status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "order_id": order_id,
        "cache_used": bool(normalized.get("cache_used")),
        "queried_at": format_datetime_kst(
            normalized.get("queried_at")
            or (latest_row or {}).get("queried_at")
        ),
        "elapsed_seconds": normalized.get("elapsed_seconds"),
        "delivery_status": normalized.get("delivery_status"),
        "installation_status": normalized.get("installation_status"),
        "installation_date": normalized.get("installation_date"),
        "installation_date_value": installation_date_value(normalized),
        "installation_date_status_message": (
            None
            if installation_date_value(normalized)
            else installation_date_display(
                normalized, queried=latest_row is not None
            )
        ),
        "installation_date_display": installation_date_display(
            normalized, queried=latest_row is not None
        ),
        "installation_date_help": (
            "DPS 품목상세내역의 요구납기일 기준"
        ),
        "required_delivery_date": normalized.get(
            "required_delivery_date"
        ),
        "raw_required_delivery_date": normalized.get(
            "raw_required_delivery_date"
        ),
        "installation_date_source": normalized.get(
            "installation_date_source"
        ),
        "date_parse_status": normalized.get("date_parse_status"),
        "requires_human_review": bool(
            normalized.get("requires_human_review")
        ),
        "sales_number": normalized.get("sales_number"),
        "error_message": normalized.get("error_message")
        or (latest_row or {}).get("error_message"),
        "warnings": list(normalized.get("warnings") or []),
    }
