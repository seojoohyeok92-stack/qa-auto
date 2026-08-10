from __future__ import annotations

from typing import Any


DPS_UAT_ERROR_MAP = {
    "AGENT_CONNECTION_FAILED": "AGENT_OFFLINE",
    "AGENT_CONNECT_TIMEOUT": "AGENT_TIMEOUT",
    "AGENT_READ_TIMEOUT": "AGENT_TIMEOUT",
    "TIMEOUT": "AGENT_TIMEOUT",
    "LOGIN_REQUIRED": "DPS_LOGIN_REQUIRED",
    "DPS_LOGIN_REQUIRED": "DPS_LOGIN_REQUIRED",
    "CHROME_NOT_FOUND": "CHROME_NOT_FOUND",
    "DPS_TAB_NOT_FOUND": "DPS_TAB_NOT_FOUND",
    "ORDER_INPUT_NOT_FOUND": "ORDER_INPUT_NOT_FOUND",
    "QUERY_CONTROL_NOT_FOUND": "QUERY_CONTROL_NOT_FOUND",
    "NOT_FOUND": "ORDER_NOT_FOUND",
    "ORDER_NOT_FOUND": "ORDER_NOT_FOUND",
    "MULTIPLE_RESULTS": "MULTIPLE_RESULTS",
    "DETAIL_OPEN_FAILED": "DETAIL_OPEN_FAILED",
    "PARSE_ERROR": "DETAIL_PARSE_FAILED",
    "DETAIL_PARSE_FAILED": "DETAIL_PARSE_FAILED",
    "WINDOW_RESTORE_FAILED": "WINDOW_RESTORE_FAILED",
}


def classify_dps_uat_error(payload: dict[str, Any] | None) -> str | None:
    if not payload:
        return None
    candidates = [
        str(payload.get(name) or "").upper()
        for name in ("error_code", "lookup_status", "login_status", "code")
    ]
    for code in candidates:
        if code in DPS_UAT_ERROR_MAP:
            return DPS_UAT_ERROR_MAP[code]
    if payload.get("success") or payload.get("ok"):
        return None
    code = next((item for item in candidates if item), "")
    return DPS_UAT_ERROR_MAP.get(code, "UNKNOWN")
