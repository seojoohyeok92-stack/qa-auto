from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlencode

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _port_setting(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if 1 <= value <= 65535 else default


AGENT_HOST = os.getenv("DPS_AGENT_HOST", "127.0.0.1").strip() or "127.0.0.1"
AGENT_PORT = _port_setting("DPS_AGENT_PORT", 8765)
BASE_URL = f"http://{AGENT_HOST}:{AGENT_PORT}"
EXPECTED_AGENT_MODE = "WINDOWS_UI_AUTOMATION_TAB_V6_LOGIN_NAV"


def _timeout_setting(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


AGENT_CONNECT_TIMEOUT_SECONDS = _timeout_setting(
    "DPS_CONNECT_TIMEOUT_SECONDS", 7.0
)
AGENT_LOOKUP_READ_TIMEOUT_SECONDS = _timeout_setting(
    "DPS_READ_TIMEOUT_SECONDS", 100.0
)
AGENT_LOOKUP_TOTAL_TIMEOUT_SECONDS = _timeout_setting(
    "DPS_TOTAL_TIMEOUT_SECONDS", 120.0
)
AGENT_LOOKUP_POLL_TIMEOUT_SECONDS = AGENT_LOOKUP_TOTAL_TIMEOUT_SECONDS
AGENT_LOOKUP_POLL_INTERVAL_SECONDS = 1.0


def _request_error(
    code: str,
    message: str,
    error: BaseException | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "success": False,
        "code": code,
        "error_code": code,
        "agent_running": code not in {
            "AGENT_CONNECTION_FAILED",
            "AGENT_CONNECT_TIMEOUT",
        },
        "login_status": (
            "AGENT_OFFLINE"
            if code in {"AGENT_CONNECTION_FAILED", "AGENT_CONNECT_TIMEOUT"}
            else None
        ),
        "message": message,
        "details": {
            "error": error.__class__.__name__ if error else None,
        },
    }


def _request(
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: float | tuple[float, float] = 8.0,
) -> dict[str, Any]:
    if isinstance(timeout, tuple):
        connect_timeout, read_timeout = timeout
    else:
        connect_timeout = min(AGENT_CONNECT_TIMEOUT_SECONDS, float(timeout))
        read_timeout = float(timeout)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    connection = http.client.HTTPConnection(
        AGENT_HOST,
        AGENT_PORT,
        timeout=connect_timeout,
    )
    try:
        try:
            connection.connect()
        except TimeoutError as error:
            return _request_error(
                "AGENT_CONNECT_TIMEOUT",
                "DPS Agent 연결 시간이 초과되었습니다.",
                error,
            )
        except (ConnectionError, OSError) as error:
            return _request_error(
                "AGENT_CONNECTION_FAILED",
                "DPS Agent가 실행 중인지 확인해 주세요.",
                error,
            )
        if connection.sock is not None:
            connection.sock.settimeout(read_timeout)
        connection.request(
            "POST" if payload is not None else "GET",
            path,
            body=data,
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        raw = response.read().decode("utf-8")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            return _request_error(
                "AGENT_RESPONSE_INVALID",
                "DPS Agent 응답 형식을 확인하지 못했습니다.",
                error,
            )
        if not isinstance(value, dict):
            return _request_error(
                "AGENT_RESPONSE_INVALID",
                "DPS Agent 응답 형식을 확인하지 못했습니다.",
            )
        return value
    except TimeoutError as error:
        return _request_error(
            "AGENT_READ_TIMEOUT",
            "DPS 상세조회가 진행 중입니다. 완료 결과를 확인하고 있습니다.",
            error,
        )
    except http.client.RemoteDisconnected as error:
        return _request_error(
            "AGENT_REQUEST_FAILED",
            "DPS Agent 응답 연결이 종료되었습니다.",
            error,
        )
    except (ConnectionError, OSError) as error:
        return _request_error(
            "AGENT_REQUEST_FAILED",
            "DPS Agent 요청을 완료하지 못했습니다.",
            error,
        )
    except Exception as error:
        return _request_error(
            "AGENT_REQUEST_FAILED",
            "DPS Agent 요청 처리 중 오류가 발생했습니다.",
            error,
        )
    finally:
        connection.close()


def get_dps_lookup_status(
    request_id: str,
    *,
    order_id: str | None = None,
    period_start: str | None = None,
    period_end: str | None = None,
) -> dict[str, Any]:
    query = urlencode(
        {
            key: value
            for key, value in {
                "order_id": order_id,
                "period_start": period_start,
                "period_end": period_end,
            }.items()
            if value not in (None, "")
        }
    )
    path = f"/lookup/{quote(str(request_id), safe='')}"
    if query:
        path = f"{path}?{query}"
    return _request(path, timeout=(AGENT_CONNECT_TIMEOUT_SECONDS, 8.0))


def poll_dps_lookup(
    request_id: str,
    *,
    order_id: str | None = None,
    period_start: str | None = None,
    period_end: str | None = None,
    timeout: float = AGENT_LOOKUP_POLL_TIMEOUT_SECONDS,
    interval: float = AGENT_LOOKUP_POLL_INTERVAL_SECONDS,
    recovered_after_timeout: bool = False,
    progress_callback: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.0, timeout)
    last_stage = ""
    while time.monotonic() <= deadline:
        response = get_dps_lookup_status(
            request_id,
            order_id=order_id,
            period_start=period_start,
            period_end=period_end,
        )
        stage = str(response.get("stage") or "")
        if progress_callback and stage and stage != last_stage:
            progress_callback(stage, str(response.get("message") or ""))
            last_stage = stage
        job_status = response.get("job_status")
        if job_status in {"COMPLETED", "FAILED"}:
            if recovered_after_timeout and response.get("success"):
                diagnostics = dict(response.get("diagnostics") or {})
                diagnostics.update(
                    {
                        "recovered_after_timeout": True,
                        "original_request_id": request_id,
                        "result_source": response.get("result_source"),
                    }
                )
                response["diagnostics"] = diagnostics
                response["recovered_after_timeout"] = True
                response["message"] = (
                    "DPS 조회가 완료되어 결과를 불러왔습니다."
                )
            return response
        if response.get("code") not in {
            "LOOKUP_RUNNING",
            "AGENT_READ_TIMEOUT",
            "AGENT_REQUEST_FAILED",
        }:
            return response
        time.sleep(max(0.0, interval))
    return _request_error(
        "AGENT_READ_TIMEOUT",
        "DPS 조회가 제한시간 안에 완료되지 않았습니다. Agent 로그에서 현재 단계를 확인해 주세요.",
    )


def get_dps_agent_status() -> dict[str, Any]:
    status = _request("/status", timeout=2.5)
    detected_mode = status.get("mode")
    if (
        status.get("success")
        and status.get("agent_running")
        and detected_mode
        and detected_mode != EXPECTED_AGENT_MODE
    ):
        return {
            "ok": False,
            "success": False,
            "code": "AGENT_RESTART_REQUIRED",
            "error_code": "AGENT_RESTART_REQUIRED",
            "agent_running": False,
            "legacy_agent_running": True,
            "login_status": "AGENT_RESTART_REQUIRED",
            "message": "이전 DPS Agent가 실행 중입니다. 한 번 종료한 뒤 v6 Agent를 시작해 주세요.",
            "details": {"detected_mode": detected_mode},
        }
    return status


def _normalized_session_status(status: dict[str, Any]) -> str:
    stored = str(
        status.get("session_status") or status.get("monitor_status") or ""
    ).upper()
    if stored in {
        "READY", "LOGIN_REQUIRED", "CHROME_NOT_FOUND",
        "DPS_PAGE_NOT_FOUND", "CONNECTION_FAILED", "UNKNOWN",
    }:
        return stored
    code = str(status.get("code") or status.get("error_code") or "").upper()
    login = str(status.get("login_state") or status.get("login_status") or "").upper()
    if status.get("logged_in") or login == "LOGGED_IN":
        return "READY"
    if login == "LOGIN_REQUIRED" or "LOGIN_REQUIRED" in code or "OTP" in code:
        return "LOGIN_REQUIRED"
    if code == "CHROME_NOT_FOUND":
        return "CHROME_NOT_FOUND"
    if code in {"DPS_TAB_NOT_FOUND", "DPS_PAGE_INVALID"} or login in {
        "DPS_TAB_NOT_FOUND", "DPS_PAGE_INVALID",
    }:
        return "DPS_PAGE_NOT_FOUND"
    if not status.get("agent_running") or code.startswith("AGENT_"):
        return "CONNECTION_FAILED"
    return "UNKNOWN"


def dps_config_check() -> dict[str, Any]:
    """Return non-secret runtime configuration diagnostics for operators."""

    raw_port = os.getenv("DPS_AGENT_PORT", "8765").strip()
    try:
        valid_port = 1 <= int(raw_port) <= 65535
    except ValueError:
        valid_port = False
    issues: list[str] = []
    if not os.getenv("DPS_AGENT_HOST", "127.0.0.1").strip():
        issues.append("DPS_AGENT_HOST_MISSING")
    if not valid_port:
        issues.append("DPS_AGENT_PORT_INVALID")
    return {
        "ok": not issues,
        "diagnostic_code": "OK" if not issues else "CONFIG_ERROR",
        "issues": issues,
        "agent_host": AGENT_HOST,
        "agent_port": AGENT_PORT,
        "project_root": str(PROJECT_ROOT),
        "platform_supported": os.name == "nt",
    }


def _diagnostic_code(status: dict[str, Any]) -> str:
    config = dps_config_check()
    if not config["ok"]:
        return "CONFIG_ERROR"
    session = _normalized_session_status(status)
    code = str(status.get("code") or status.get("error_code") or "").upper()
    if session == "READY":
        return "OK"
    if session == "LOGIN_REQUIRED":
        return "AUTH_ERROR"
    if "TIMEOUT" in code:
        return "TIMEOUT"
    if session == "CONNECTION_FAILED" or code.startswith("AGENT_"):
        return "NETWORK_ERROR"
    if session in {"CHROME_NOT_FOUND", "DPS_PAGE_NOT_FOUND"}:
        return "CONFIG_ERROR"
    return "UNKNOWN_ERROR"


def get_dps_session_status() -> dict[str, Any]:
    status = get_dps_agent_status()
    config = dps_config_check()
    return {
        **status,
        "session_status": _normalized_session_status(status),
        "diagnostic_code": _diagnostic_code(status),
        "config_check": config,
    }


def monitor_dps_session(
    *,
    keepalive: bool = False,
    keepalive_interval_seconds: int = 2400,
    force_keepalive: bool = False,
    trigger: str = "CLIENT",
) -> dict[str, Any]:
    started = start_dps_agent()
    if not started.get("agent_running"):
        return {
            **started,
            "session_status": _normalized_session_status(started),
        }
    result = _request(
        "/session-monitor",
        {
            "keepalive": bool(keepalive),
            "keepalive_interval_seconds": max(
                600, int(keepalive_interval_seconds)
            ),
            "force_keepalive": bool(force_keepalive),
            "trigger": str(trigger),
        },
        timeout=20.0,
    )
    return {**result, "session_status": _normalized_session_status(result)}


def ensure_dps_session_monitor() -> dict[str, Any]:
    from config import DpsSessionSettings

    settings = DpsSessionSettings.from_environment()
    if not settings.monitor_enabled:
        return {
            "ok": True,
            "success": True,
            "monitor_enabled": False,
            "session_status": "UNKNOWN",
        }
    status = start_dps_agent()
    return {
        **status,
        "session_status": _normalized_session_status(status),
        "monitor_enabled": True,
        "keepalive_enabled": settings.keepalive_enabled,
    }


def start_dps_agent() -> dict[str, Any]:
    current = get_dps_agent_status()
    if current.get("agent_running"):
        return current
    if current.get("legacy_agent_running"):
        return current
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS if os.name == "nt" else 0
    logs_dir = PROJECT_ROOT / "logs"
    try:
        logs_dir.mkdir(exist_ok=True)
        stdout_file = open(logs_dir / "dps_agent_stdout.log", "a", encoding="utf-8")
        stderr_file = open(logs_dir / "dps_agent_stderr.log", "a", encoding="utf-8")
    except Exception as error:
        return {
            "ok": False,
            "success": False,
            "code": "AGENT_LOG_OPEN_FAILED",
            "error_code": "AGENT_LOG_OPEN_FAILED",
            "agent_running": False,
            "login_status": "START_FAILED",
            "message": "DPS Agent 로그 파일을 준비하지 못했습니다.",
            "details": {"error": error.__class__.__name__},
        }
    try:
        subprocess.Popen(
            [sys.executable, "-m", "dps.agent_server"], cwd=str(PROJECT_ROOT),
            stdout=stdout_file, stderr=stderr_file, stdin=subprocess.DEVNULL,
            creationflags=creationflags, close_fds=os.name != "nt",
        )
    except Exception as error:
        return {
            "ok": False,
            "success": False,
            "code": "AGENT_START_FAILED",
            "error_code": "AGENT_START_FAILED",
            "agent_running": False,
            "login_status": "START_FAILED",
            "message": "DPS Agent를 시작하지 못했습니다.",
            "details": {"error": error.__class__.__name__},
        }
    finally:
        stdout_file.close()
        stderr_file.close()
    for _ in range(30):
        time.sleep(0.35)
        status = get_dps_agent_status()
        if status.get("agent_running"):
            return status
    return {
        "ok": False,
        "success": False,
        "code": "AGENT_START_TIMEOUT",
        "error_code": "AGENT_START_TIMEOUT",
        "agent_running": False,
        "login_status": "START_FAILED",
        "message": "DPS Agent 시작을 확인하지 못했습니다. logs/dps_agent.log를 확인해 주세요.",
        "details": {},
    }


def open_dps_login() -> dict[str, Any]:
    started = start_dps_agent()
    return started if not started.get("agent_running") else _request("/open-login", {}, timeout=20.0)


def open_dps_browser() -> dict[str, Any]:
    started = start_dps_agent()
    return started if not started.get("agent_running") else _request("/open", {}, timeout=20.0)



def list_dps_chrome_windows() -> dict[str, Any]:
    started = start_dps_agent()
    return started if not started.get("agent_running") else _request("/chrome-windows", timeout=45.0)


def auto_connect_dps_tab(
    *,
    select_tab: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """저장된 런타임 연결을 재사용하고, 필요할 때만 일반 Chrome 탭을 재탐색합니다."""

    started = start_dps_agent()
    return started if not started.get("agent_running") else _request(
        "/auto-connect",
        {"select_tab": select_tab, "force": force},
        timeout=15.0,
    )


def connect_dps_window(hwnd: int) -> dict[str, Any]:
    started = start_dps_agent()
    return started if not started.get("agent_running") else _request(
        "/connect-window", {"hwnd": int(hwnd)}, timeout=45.0
    )


def connect_current_dps_window(*, delay_seconds: int = 4) -> dict[str, Any]:
    started = start_dps_agent()
    return started if not started.get("agent_running") else _request(
        "/connect-current-window",
        {"delay_seconds": delay_seconds},
        timeout=max(12.0, float(delay_seconds) + 6.0),
    )


def disconnect_current_dps_window() -> dict[str, Any]:
    started = start_dps_agent()
    return started if not started.get("agent_running") else _request(
        "/disconnect-current-window", {}, timeout=8.0
    )

def confirm_dps_login() -> dict[str, Any]:
    started = start_dps_agent()
    return started if not started.get("agent_running") else _request("/confirm-login", {}, timeout=8.0)


def mark_dps_logged_out() -> dict[str, Any]:
    started = start_dps_agent()
    return started if not started.get("agent_running") else _request("/mark-logged-out", {}, timeout=8.0)


def refresh_dps_session() -> dict[str, Any]:
    started = start_dps_agent()
    return started if not started.get("agent_running") else _request("/refresh-session", {}, timeout=20.0)


def run_dps_diagnostics() -> dict[str, Any]:
    started = start_dps_agent()
    return started if not started.get("agent_running") else _request("/diagnostics", timeout=10.0)


def lookup_dps_order(
    naver_order_id: str | None = None,
    *,
    request_id: str | None = None,
    selected_inquiry_id: str | None = None,
    order_id: str | None = None,
    product_order_id: str | None = None,
    dps_query_value: str | None = None,
    dps_query_value_type: str | None = None,
    order_date: str | None = None,
    order_created_at: str | None = None,
    payment_date: str | None = None,
    payment_completed_at: str | None = None,
    place_order_date: str | None = None,
    shipping_due_date: str | None = None,
    dps_date_source: str | None = None,
    dps_reference_date: str | None = None,
    dps_period_start: str | None = None,
    dps_period_end: str | None = None,
    force_refresh: bool = False,
    progress_callback: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """Lookup DPS while preserving the legacy positional identifier."""

    legacy_order_id = str(naver_order_id or "").strip() or None
    normalized_order_id = str(order_id or legacy_order_id or "").strip()
    normalized_query = str(dps_query_value or normalized_order_id).strip()
    normalized_type = str(
        dps_query_value_type or ("order_id" if normalized_order_id else "")
    ).strip()
    resolved_request_id = str(request_id or uuid.uuid4()).strip()
    if not normalized_order_id:
        return {
            "success": False,
            "ok": False,
            "status": "DPS_ORDER_ID_MISSING",
            "code": "DPS_ORDER_ID_MISSING",
            "message": "네이버 주문번호가 없어 DPS 조회를 실행할 수 없습니다.",
        }
    if normalized_type != "order_id":
        return {
            "success": False,
            "ok": False,
            "status": "INVALID_DPS_QUERY_TYPE",
            "code": "INVALID_DPS_QUERY_TYPE",
            "message": "상품주문번호가 DPS 조회값으로 전달되어 안전을 위해 중단했습니다.",
        }
    if normalized_query != normalized_order_id:
        return {
            "success": False,
            "ok": False,
            "status": "CLIENT_ORDER_ID_MISMATCH",
            "code": "CLIENT_ORDER_ID_MISMATCH",
            "message": "현재 화면의 네이버 주문번호와 Agent 요청값이 다릅니다.",
        }
    started = start_dps_agent()
    if not started.get("agent_running"):
        return started
    payload = {
        "request_id": resolved_request_id,
        "selected_inquiry_id": selected_inquiry_id,
        "naver_order_id": legacy_order_id,
        "order_id": normalized_order_id,
        "product_order_id": product_order_id,
        "dps_query_value": normalized_query,
        "dps_query_value_type": normalized_type,
        "order_date": order_date,
        "order_created_at": order_created_at,
        "payment_date": payment_date,
        "payment_completed_at": payment_completed_at,
        "place_order_date": place_order_date,
        "shipping_due_date": shipping_due_date,
        "dps_date_source": dps_date_source,
        "dps_reference_date": dps_reference_date,
        "dps_period_start": dps_period_start,
        "dps_period_end": dps_period_end,
        "force_refresh": force_refresh,
    }
    lookup_started = time.monotonic()
    response = _request(
        "/lookup",
        payload,
        timeout=(
            AGENT_CONNECT_TIMEOUT_SECONDS,
            AGENT_LOOKUP_READ_TIMEOUT_SECONDS,
        ),
    )
    timed_out = response.get("code") == "AGENT_READ_TIMEOUT"
    if timed_out or response.get("code") == "LOOKUP_RUNNING":
        remaining = max(
            0.0,
            AGENT_LOOKUP_TOTAL_TIMEOUT_SECONDS
            - (time.monotonic() - lookup_started),
        )
        return poll_dps_lookup(
            resolved_request_id,
            order_id=normalized_order_id,
            period_start=dps_period_start,
            period_end=dps_period_end,
            timeout=remaining,
            recovered_after_timeout=timed_out,
            progress_callback=progress_callback,
        )
    return response
