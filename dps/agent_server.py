from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from config import DpsSessionSettings
from dps.chrome_tab_manager import (
    ChromeTabManager,
    RuntimeConnection,
    TabCandidate,
)
from dps.context import identifier_fingerprint
from dps.connection_store import ConnectionStore
from dps.dates import (
    calculate_dps_lookup_period,
    select_dps_date_source,
    validate_dps_lookup_period,
)
from dps.dps_ui_automation import DpsUiAutomation
from dps.identifiers import select_dps_query_identifier
from dps.gui_resource_guard import GUIResourceGuard, GUIResourceState
from dps.sales_detail import (
    mask_address,
    mask_name,
    mask_phone,
    sanitize_detail_for_cache,
)
from dps.session_scheduler import DpsSessionMonitorScheduler


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOST = os.getenv("DPS_AGENT_HOST", "127.0.0.1")
PORT = int(os.getenv("DPS_AGENT_PORT", "8765"))
SESSION_IDLE_SECONDS = int(os.getenv("DPS_SESSION_IDLE_SECONDS", "3600"))
CACHE_TTL_SECONDS = int(os.getenv("DPS_CACHE_TTL_SECONDS", "600"))
CACHE_SCHEMA_VERSION = 5
CACHE_FILE = PROJECT_ROOT / "data" / "dps_windows_cache.json"
LOG_FILE = PROJECT_ROOT / "logs" / "dps_agent.log"
UI_TREE_LOG_FILE = PROJECT_ROOT / "logs" / "dps_ui_tree.log"
RESULT_TREE_LOG_FILE = PROJECT_ROOT / "logs" / "dps_result_tree.log"
CALENDAR_TREE_LOG_FILE = PROJECT_ROOT / "logs" / "dps_calendar_tree.log"
SALES_DETAIL_TREE_LOG_FILE = (
    PROJECT_ROOT / "logs" / "dps_sales_detail_tree.log"
)
LAST_LOOKUP_RESULT_FILE = (
    PROJECT_ROOT / "logs" / "last_dps_lookup_result.json"
)
AGENT_MODE = "WINDOWS_UI_AUTOMATION_TAB_V6_LOGIN_NAV"
LOOKUP_JOB_TTL_SECONDS = int(
    os.getenv("DPS_LOOKUP_JOB_TTL_SECONDS", "900")
)
LOOKUP_JOB_MAX_ITEMS = int(os.getenv("DPS_LOOKUP_JOB_MAX_ITEMS", "100"))
SESSION_MONITOR_STATUSES = {
    "READY",
    "LOGIN_REQUIRED",
    "CHROME_NOT_FOUND",
    "DPS_PAGE_NOT_FOUND",
    "CONNECTION_FAILED",
    "UNKNOWN",
    "STALE",
}


def _configure_logger() -> logging.Logger:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("dps_agent")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s")
        )
        logger.addHandler(handler)
    return logger


LOGGER = _configure_logger()


def _configure_ui_tree_logger() -> logging.Logger:
    UI_TREE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("dps_ui_tree")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(
            UI_TREE_LOG_FILE,
            maxBytes=1_000_000,
            backupCount=2,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s")
        )
        logger.addHandler(handler)
    return logger


UI_TREE_LOGGER = _configure_ui_tree_logger()


def _configure_result_tree_logger() -> logging.Logger:
    RESULT_TREE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("dps_result_tree")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(
            RESULT_TREE_LOG_FILE,
            maxBytes=2_000_000,
            backupCount=2,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s")
        )
        logger.addHandler(handler)
    return logger


RESULT_TREE_LOGGER = _configure_result_tree_logger()


def _configure_calendar_tree_logger() -> logging.Logger:
    CALENDAR_TREE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("dps_calendar_tree")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(
            CALENDAR_TREE_LOG_FILE,
            maxBytes=1_500_000,
            backupCount=2,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s - %(message)s"
            )
        )
        logger.addHandler(handler)
    return logger


CALENDAR_TREE_LOGGER = _configure_calendar_tree_logger()


def _configure_sales_detail_tree_logger() -> logging.Logger:
    SALES_DETAIL_TREE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("dps_sales_detail_tree")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(
            SALES_DETAIL_TREE_LOG_FILE,
            maxBytes=1_500_000,
            backupCount=2,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s - %(message)s"
            )
        )
        logger.addHandler(handler)
    return logger


SALES_DETAIL_TREE_LOGGER = _configure_sales_detail_tree_logger()


def dps_cache_key(
    query_value: str,
    query_value_type: str,
    period_start: str | None = None,
    period_end: str | None = None,
) -> str:
    if query_value_type != "order_id":
        raise ValueError("DPS cache keys require order_id")
    key = f"order:{query_value}"
    if period_start and period_end:
        key = f"{key}:{period_start}:{period_end}"
    return key


def _safe_lookup_log_payload(value: Any, *, key: str = "") -> Any:
    folded_key = key.casefold()
    if any(
        marker in folded_key
        for marker in (
            "password",
            "otp",
            "cookie",
            "token",
            "authorization",
            "secret",
        )
    ):
        return "[REDACTED]"
    if isinstance(value, str):
        if "phone" in folded_key or "전화" in folded_key:
            return mask_phone(value)
        if "address" in folded_key or "주소" in folded_key:
            return mask_address(value)
        if any(
            marker in folded_key
            for marker in (
                "buyer_name",
                "recipient_name",
                "구매자",
                "인수자",
            )
        ):
            return mask_name(value)
    if isinstance(value, dict):
        return {
            str(child_key): _safe_lookup_log_payload(
                child_value,
                key=str(child_key),
            )
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [
            _safe_lookup_log_payload(item, key=key)
            for item in value
        ]
    if isinstance(value, str):
        return re.sub(
            r"(?<!\d)(\d{8})\d{4}(\d{4})(?!\d)",
            r"\1****\2",
            value,
        )
    return value


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temp, path)


def failure(
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    result = {
        "ok": False,
        "success": False,
        "code": code,
        "error_code": code,
        "message": message,
        "details": details or {},
    }
    result.update(extra)
    return result


def success(
    code: str,
    message: str,
    **extra: Any,
) -> dict[str, Any]:
    result = {
        "ok": True,
        "success": True,
        "code": code,
        "message": message,
    }
    result.update(extra)
    return result


class DpsWindowsAgent:
    """일반 Chrome의 DPS TabItem을 연결 단위로 관리하는 Windows Agent v6입니다."""

    def __init__(
        self,
        *,
        store: ConnectionStore | None = None,
        tab_manager: ChromeTabManager | None = None,
        ui_automation: DpsUiAutomation | None = None,
        gui_guard: GUIResourceGuard | None = None,
        session_settings: DpsSessionSettings | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.lock = threading.RLock()
        self.lookup_jobs_lock = threading.RLock()
        self.lookup_jobs: dict[str, dict[str, Any]] = {}
        self.lookup_gate = threading.Lock()
        self.lookup_gate_owner: str | None = None
        self.actual_lookup_waiting = threading.Event()
        self.active_request_id: str | None = None
        self.completed_request_ids: list[str] = []
        self.started_at = time.time()
        self.lookup_in_progress = False
        self.last_lookup_stage: str | None = None
        self.last_window_restore_warning: str | None = None
        self.store = store or ConnectionStore(logger=LOGGER)
        config = self.store.load()
        self.tab_manager = tab_manager or ChromeTabManager(
            keywords=config["tab_title_keywords"],
            allowed_hosts=tuple(
                value.strip()
                for value in os.getenv("DPS_ALLOWED_HOSTS", "dps2u.co.kr").split(",")
                if value.strip()
            ),
            logger=LOGGER,
        )
        self.ui = ui_automation or DpsUiAutomation(
            logger=LOGGER,
            navigation_logger=UI_TREE_LOGGER,
            result_logger=RESULT_TREE_LOGGER,
            calendar_logger=CALENDAR_TREE_LOGGER,
            detail_logger=SALES_DETAIL_TREE_LOGGER,
        )
        self.gui_guard = gui_guard or GUIResourceGuard(
            project_root=PROJECT_ROOT,
            sleep=sleep,
            logger=LOGGER,
        )
        self.session_settings = (
            session_settings or DpsSessionSettings.from_environment()
        )
        state = self.store.load_agent_state()
        self.login_confirmed_at = state.get("login_confirmed_at")
        self.last_activity_at = state.get("last_activity_at")
        self.last_lookup_at = state.get("last_lookup_at")
        self.last_order_number = state.get("last_order_number_masked")
        self.last_error = state.get("last_error")
        self.error_type = state.get("error_type")
        self.session_status = str(state.get("session_status") or "UNKNOWN")
        if self.session_status not in SESSION_MONITOR_STATUSES:
            self.session_status = "UNKNOWN"
        self.last_checked_at = state.get("last_checked_at")
        self.last_ready_at = state.get("last_ready_at")
        self.last_keepalive_at = state.get("last_keepalive_at")
        self.last_keepalive_attempt_at = state.get("last_keepalive_attempt_at")
        self.last_successful_lookup_at = (
            state.get("last_successful_lookup_at") or self.last_lookup_at
        )
        self.consecutive_keepalive_failures = int(
            state.get("consecutive_keepalive_failures") or 0
        )
        self.keepalive_lock_skips = int(state.get("keepalive_lock_skips") or 0)
        self.last_monitor_event = state.get("last_monitor_event")
        self.last_passive_monitor_at = state.get("last_passive_monitor_at")
        self.last_gui_operation_at = state.get("last_gui_operation_at")
        self.last_gui_operation_type = state.get("last_gui_operation_type")
        self.sleep = sleep
        # HWND/UIA 요소는 프로세스 재시작 후 재사용하지 않습니다.
        self.connection_status = "DISCONNECTED"
        self.connection: RuntimeConnection | None = None

    @staticmethod
    def _gui_resource_failure(state: GUIResourceState) -> dict[str, Any]:
        return failure(
            "GUI_RESOURCE_WAIT_TIMEOUT",
            "DPS GUI resource did not become available before the wait timeout.",
            {"gui_resource": state.to_dict()},
            gui_resource_state=state.state,
            gui_resource_reason=state.reason,
            retryable=True,
        )

    def _wait_for_gui_resource(self, *, owner: str) -> GUIResourceState:
        state = self.gui_guard.wait_for_available()
        LOGGER.info(
            "DPS GUI guard result: owner=%s state=%s reason=%s source=%s",
            owner,
            state.state,
            state.reason,
            state.detected_source,
        )
        return state

    def _run_guarded_gui_operation(
        self,
        owner: str,
        operation: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        wait_seconds = max(0.0, self.gui_guard.settings.max_wait_seconds)
        acquired = self.lookup_gate.acquire(timeout=wait_seconds)
        if not acquired:
            return self._gui_resource_failure(
                GUIResourceState(
                    False,
                    "TIMEOUT",
                    "DPS_GUI_OPERATION_LOCK_TIMEOUT",
                    "dps_gui_operation_lock",
                )
            )
        self.lookup_gate_owner = owner
        previous = None
        try:
            guard_state = self._wait_for_gui_resource(owner=owner)
            if not guard_state.available:
                return self._gui_resource_failure(guard_state)
            try:
                previous = self.tab_manager.capture_previous_context()
            except Exception:
                LOGGER.warning("DPS could not capture the previous foreground context")
            self._record_gui_operation(owner)
            return operation()
        finally:
            if previous is not None and getattr(previous, "foreground_hwnd", None):
                try:
                    target_hwnd = self.tab_manager.foreground_hwnd() or 0
                    restored = self.tab_manager.restore_previous_context(
                        previous, target_hwnd
                    )
                    if not restored:
                        self.last_window_restore_warning = (
                            "PREVIOUS_WINDOW_RESTORE_FAILED"
                        )
                        LOGGER.warning(
                            "DPS foreground restore failed: owner=%s previous_hwnd=%s",
                            owner,
                            previous.foreground_hwnd,
                        )
                    else:
                        self.last_window_restore_warning = None
                except Exception:
                    self.last_window_restore_warning = (
                        "PREVIOUS_WINDOW_RESTORE_EXCEPTION"
                    )
                    LOGGER.warning(
                        "DPS foreground restore raised an exception: owner=%s",
                        owner,
                        exc_info=True,
                    )
            self.lookup_gate_owner = None
            self.lookup_gate.release()

    def _save_state(self) -> None:
        self.store.save_agent_state(
            {
                "login_confirmed_at": self.login_confirmed_at,
                "last_activity_at": self.last_activity_at,
                "last_lookup_at": self.last_lookup_at,
                "last_successful_lookup_at": self.last_successful_lookup_at,
                "last_order_number_masked": self.last_order_number,
                "last_error": self.last_error,
                "error_type": self.error_type,
                "connection_status": self.connection_status,
                "session_status": self.session_status,
                "last_checked_at": self.last_checked_at,
                "last_ready_at": self.last_ready_at,
                "last_keepalive_at": self.last_keepalive_at,
                "last_keepalive_attempt_at": self.last_keepalive_attempt_at,
                "consecutive_keepalive_failures": (
                    self.consecutive_keepalive_failures
                ),
                "keepalive_lock_skips": self.keepalive_lock_skips,
                "last_monitor_event": self.last_monitor_event,
                "last_passive_monitor_at": self.last_passive_monitor_at,
                "last_gui_operation_at": self.last_gui_operation_at,
                "last_gui_operation_type": self.last_gui_operation_type,
            }
        )

    def _record_gui_operation(self, operation_type: str) -> None:
        self.last_gui_operation_at = now_iso()
        self.last_gui_operation_type = str(operation_type or "UNKNOWN")[:100]
        self._save_state()

    @staticmethod
    def _monitor_status_for(
        *, login_state: str = "", error_code: str = "",
    ) -> str:
        login = str(login_state or "").upper()
        code = str(error_code or "").upper()
        if login == "LOGGED_IN":
            return "READY"
        if login == "LOGIN_REQUIRED" or "LOGIN_REQUIRED" in code or "OTP" in code:
            return "LOGIN_REQUIRED"
        if code == "CHROME_NOT_FOUND":
            return "CHROME_NOT_FOUND"
        if code in {
            "DPS_TAB_NOT_FOUND", "DPS_PAGE_INVALID", "TAB_CLOSED",
            "AUTO_CONNECT_DISABLED",
        } or login in {"DPS_TAB_NOT_FOUND", "DPS_PAGE_INVALID"}:
            return "DPS_PAGE_NOT_FOUND"
        if code in {
            "AGENT_CONNECTION_FAILED", "AGENT_CONNECT_TIMEOUT",
            "AGENT_REQUEST_FAILED", "UIA_READ_FAILED",
            "TAB_VERIFICATION_FAILED",
        }:
            return "CONNECTION_FAILED"
        return "UNKNOWN"

    def _set_monitor_state(
        self,
        status: str,
        *,
        event: str,
        error: str | None = None,
        error_type: str | None = None,
    ) -> None:
        normalized = status if status in SESSION_MONITOR_STATUSES else "UNKNOWN"
        previous = self.session_status
        self.session_status = normalized
        self.last_checked_at = now_iso()
        self.last_monitor_event = event
        if normalized == "READY":
            self.last_ready_at = self.last_checked_at
            if error:
                self.last_error = str(error)[:500]
                self.error_type = str(error_type or event)[:100]
            else:
                self.last_error = None
                self.error_type = None
        else:
            self.last_error = str(error or "")[:500] or None
            self.error_type = str(error_type or normalized)[:100]
        self._save_state()
        if previous != normalized:
            LOGGER.info(
                "DPS session state changed: previous=%s current=%s event=%s",
                previous,
                normalized,
                event,
            )
        elif normalized != "READY":
            LOGGER.warning(
                "DPS session check: status=%s event=%s error_type=%s",
                normalized,
                event,
                self.error_type,
            )

    @staticmethod
    def _elapsed_since_iso(value: object) -> float | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return max(0.0, time.time() - parsed.timestamp())

    def _passive_monitor_session(self, *, trigger: str) -> dict[str, Any]:
        """Validate only saved process metadata without traversing Windows UIA."""

        config = self.store.load()
        runtime = self.connection
        metadata_present = bool(
            config.get("last_window_title")
            or config.get("last_tab_title")
            or config.get("last_connected_at")
        )
        hwnd_valid: bool | None = None
        if runtime is not None:
            try:
                hwnd_valid = bool(self.tab_manager.is_window(runtime.hwnd))
            except Exception:
                hwnd_valid = None
            if hwnd_valid is False:
                self.connection = None
                self.connection_status = "TAB_CLOSED"
            elif hwnd_valid is True:
                self.connection_status = "CONNECTED"

        if runtime is not None and hwnd_valid is True:
            passive_state = "STALE"
            event = "PASSIVE_HWND_VALID"
        elif runtime is not None and hwnd_valid is False:
            passive_state = "STALE"
            event = "PASSIVE_HWND_CLOSED"
        elif metadata_present:
            passive_state = "STALE"
            event = "PASSIVE_METADATA_ONLY"
        else:
            passive_state = "UNKNOWN"
            event = "PASSIVE_NO_METADATA"

        checked_at = now_iso()
        self.session_status = passive_state
        self.last_checked_at = checked_at
        self.last_passive_monitor_at = checked_at
        self.last_monitor_event = event
        self._save_state()
        return success(
            "PASSIVE_SESSION_MONITORED",
            "DPS session metadata was checked without activating Windows GUI.",
            session_status=passive_state,
            monitor_status=passive_state,
            passive=True,
            trigger=trigger,
            connection_metadata_present=metadata_present,
            stored_hwnd_valid=hwnd_valid,
            keepalive_performed=False,
            last_passive_monitor_at=checked_at,
        )

    def monitor_session(
        self,
        *,
        keepalive_enabled: bool = False,
        keepalive_interval_seconds: int = 1200,
        force_keepalive: bool = False,
        trigger: str = "MANUAL",
    ) -> dict[str, Any]:
        """Run passive monitoring; perform guarded GUI keepalive only when enabled and due."""

        if self.session_settings.passive_monitor_enabled:
            passive_result = self._passive_monitor_session(trigger=trigger)
            elapsed = self._elapsed_since_iso(self.last_keepalive_at)
            keepalive_due = bool(
                keepalive_enabled
                and (
                    force_keepalive
                    or elapsed is None
                    or elapsed >= max(600, int(keepalive_interval_seconds))
                )
            )
            if not keepalive_due:
                return passive_result
        return self._active_monitor_session(
            keepalive_enabled=keepalive_enabled,
            keepalive_interval_seconds=keepalive_interval_seconds,
            force_keepalive=force_keepalive,
            trigger=trigger,
        )

    def _active_monitor_session(
        self,
        *,
        keepalive_enabled: bool = False,
        keepalive_interval_seconds: int = 1200,
        force_keepalive: bool = False,
        trigger: str = "MANUAL",
    ) -> dict[str, Any]:
        """Inspect DPS session and optionally perform one read-only reload."""

        if self.actual_lookup_waiting.is_set() or not self.lookup_gate.acquire(
            blocking=False
        ):
            self.keepalive_lock_skips += 1
            self.last_checked_at = now_iso()
            self.last_monitor_event = "LOOKUP_LOCK_SKIP"
            self._save_state()
            LOGGER.info("DPS session monitor skipped: lookup lock busy")
            return success(
                "SESSION_MONITOR_SKIPPED",
                "DPS lookup is active; session monitoring was skipped.",
                session_status=self.session_status,
                monitor_status=self.session_status,
                skipped=True,
                skip_reason="LOOKUP_IN_PROGRESS",
                keepalive_performed=False,
            )

        self.lookup_gate_owner = "KEEPALIVE" if keepalive_enabled else "MONITOR"
        previous = None
        target_hwnd = 0
        try:
            guard_state = self._wait_for_gui_resource(
                owner=self.lookup_gate_owner or "MONITOR"
            )
            if not guard_state.available:
                deferred_keepalive = keepalive_enabled or force_keepalive
                code = (
                    "KEEPALIVE_DEFERRED"
                    if deferred_keepalive
                    else "SESSION_MONITOR_DEFERRED"
                )
                LOGGER.info(
                    "DPS session work deferred by GUI guard: code=%s state=%s "
                    "reason=%s source=%s",
                    code,
                    guard_state.state,
                    guard_state.reason,
                    guard_state.detected_source,
                )
                return success(
                    code,
                    "DPS session work was deferred for higher-priority GUI activity.",
                    session_status=self.session_status,
                    monitor_status=self.session_status,
                    skipped=True,
                    deferred=True,
                    skip_reason="GUI_RESOURCE_BUSY",
                    gui_resource=guard_state.to_dict(),
                    keepalive_performed=False,
                )
            previous = self.tab_manager.capture_previous_context()
            self._record_gui_operation(self.lookup_gate_owner or "MONITOR")
            with self.lock:
                candidate, connection_error = self._select_current_dps()
                if connection_error:
                    code = str(
                        connection_error.get("code")
                        or connection_error.get("error_code")
                        or "CONNECTION_FAILED"
                    )
                    status = self._monitor_status_for(error_code=code)
                    self._set_monitor_state(
                        status,
                        event="CONNECTION_CHECK_FAILED",
                        error=connection_error.get("message"),
                        error_type=code,
                    )
                    return {
                        **connection_error,
                        "session_status": status,
                        "monitor_status": status,
                        "keepalive_performed": False,
                    }
                assert candidate is not None
                target_hwnd = candidate.hwnd
                state = self._detect_candidate_state(candidate)
                status = self._monitor_status_for(
                    login_state=str(state.get("login_state") or ""),
                )
                if status != "READY":
                    self._set_monitor_state(
                        status,
                        event="LOGIN_STATE_CHECKED",
                        error=state.get("login_reason"),
                        error_type=state.get("login_state"),
                    )
                    return success(
                        "SESSION_MONITORED",
                        "DPS session state was checked.",
                        session_status=status,
                        monitor_status=status,
                        login_state=state.get("login_state"),
                        current_page=state.get("current_page"),
                        keepalive_performed=False,
                    )

                self._set_monitor_state("READY", event="READY_CHECKED")
                elapsed = self._elapsed_since_iso(self.last_keepalive_at)
                due = force_keepalive or (
                    keepalive_enabled
                    and (
                        elapsed is None
                        or elapsed >= max(600, int(keepalive_interval_seconds))
                    )
                )
                if not due:
                    return success(
                        "SESSION_MONITORED",
                        "DPS session is ready.",
                        session_status="READY",
                        monitor_status="READY",
                        keepalive_performed=False,
                        last_checked_at=self.last_checked_at,
                    )
                if self.actual_lookup_waiting.is_set():
                    self.keepalive_lock_skips += 1
                    self.last_monitor_event = "LOOKUP_PRIORITY_SKIP"
                    self._save_state()
                    return success(
                        "KEEPALIVE_SKIPPED",
                        "An actual DPS lookup has priority over keepalive.",
                        session_status="READY",
                        monitor_status="READY",
                        skipped=True,
                        skip_reason="ACTUAL_LOOKUP_WAITING",
                        keepalive_performed=False,
                    )

                self.last_keepalive_attempt_at = now_iso()
                valid, checks = self.tab_manager.validate_selected_candidate(candidate)
                if not valid:
                    return self._record_keepalive_failure(
                        "DPS_TAB_VERIFICATION_FAILED",
                        "DPS tab verification failed before keepalive.",
                        details=checks,
                    )
                if not self.tab_manager.click_reload_button(candidate.window):
                    return self._record_keepalive_failure(
                        "REFRESH_BUTTON_NOT_FOUND",
                        "Chrome reload control was not found on the verified DPS tab.",
                    )
                self.sleep(0.75)
                refreshed = self._detect_candidate_state(candidate)
                refreshed_status = self._monitor_status_for(
                    login_state=str(refreshed.get("login_state") or ""),
                )
                if refreshed_status == "LOGIN_REQUIRED":
                    self._set_monitor_state(
                        "LOGIN_REQUIRED",
                        event="KEEPALIVE_LOGIN_REQUIRED",
                        error=refreshed.get("login_reason"),
                        error_type="LOGIN_REQUIRED",
                    )
                    return failure(
                        "DPS_LOGIN_REQUIRED",
                        "DPS login is required.",
                        session_status="LOGIN_REQUIRED",
                        monitor_status="LOGIN_REQUIRED",
                        login_required=True,
                        keepalive_performed=False,
                    )
                if refreshed_status != "READY":
                    return self._record_keepalive_failure(
                        "KEEPALIVE_STATE_UNCERTAIN",
                        "DPS state could not be confirmed after the read-only reload.",
                    )
                self.consecutive_keepalive_failures = 0
                self.last_keepalive_at = now_iso()
                self.last_activity_at = time.time()
                self._set_monitor_state("READY", event="KEEPALIVE_SUCCEEDED")
                LOGGER.info(
                    "DPS keepalive succeeded: trigger=%s last_keepalive_at=%s",
                    trigger,
                    self.last_keepalive_at,
                )
                return success(
                    "SESSION_REFRESHED",
                    "The verified DPS tab was refreshed read-only.",
                    session_status="READY",
                    monitor_status="READY",
                    keepalive_performed=True,
                    last_keepalive_at=self.last_keepalive_at,
                )
        except Exception as error:
            LOGGER.exception(
                "DPS session monitor failed: error_type=%s",
                error.__class__.__name__,
            )
            self._set_monitor_state(
                "CONNECTION_FAILED",
                event="MONITOR_EXCEPTION",
                error="DPS session monitor failed.",
                error_type=error.__class__.__name__,
            )
            return failure(
                "DPS_SESSION_MONITOR_FAILED",
                "DPS session monitor failed.",
                session_status="CONNECTION_FAILED",
                monitor_status="CONNECTION_FAILED",
                keepalive_performed=False,
            )
        finally:
            if (
                previous is not None
                and previous.foreground_hwnd
                and target_hwnd
            ):
                try:
                    restored = self.tab_manager.restore_previous_context(
                        previous, target_hwnd
                    )
                    if not restored:
                        self.last_window_restore_warning = (
                            "PREVIOUS_WINDOW_RESTORE_FAILED"
                        )
                        LOGGER.warning(
                            "DPS monitor could not restore previous UI context"
                        )
                except Exception:
                    self.last_window_restore_warning = (
                        "PREVIOUS_WINDOW_RESTORE_EXCEPTION"
                    )
                    LOGGER.warning(
                        "DPS monitor could not restore previous UI context",
                        exc_info=True,
                    )
            self.lookup_gate_owner = None
            self.lookup_gate.release()

    def _record_keepalive_failure(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.consecutive_keepalive_failures += 1
        status = (
            "CONNECTION_FAILED"
            if self.consecutive_keepalive_failures >= 2
            else "READY"
        )
        self._set_monitor_state(
            status,
            event="KEEPALIVE_FAILED",
            error=message,
            error_type=code,
        )
        LOGGER.warning(
            "DPS keepalive failed: error_type=%s consecutive_failures=%s",
            code,
            self.consecutive_keepalive_failures,
        )
        return failure(
            code,
            message,
            details,
            session_status=status,
            monitor_status=status,
            keepalive_performed=False,
            consecutive_failures=self.consecutive_keepalive_failures,
        )

    def _acquire_actual_lookup_gate(self, *, timeout: float = 5.0) -> bool:
        """Give an actual order lookup priority over monitor/keepalive work."""

        self.actual_lookup_waiting.set()
        try:
            acquired = self.lookup_gate.acquire(blocking=False)
            if (
                not acquired
                and self.lookup_gate_owner in {"MONITOR", "KEEPALIVE"}
            ):
                acquired = self.lookup_gate.acquire(timeout=max(0.0, timeout))
            if acquired:
                self.lookup_gate_owner = "LOOKUP"
            return acquired
        finally:
            self.actual_lookup_waiting.clear()

    def _release_actual_lookup_gate(self) -> None:
        self.lookup_gate_owner = None
        self.lookup_gate.release()

    def _session_remaining(self) -> int:
        if not isinstance(self.last_activity_at, (int, float)):
            return 0
        return max(0, SESSION_IDLE_SECONDS - int(time.time() - self.last_activity_at))

    def logged_in(self) -> bool:
        candidate = self._current_candidate()
        if candidate is None:
            return False
        return self._detect_candidate_state(candidate)["login_state"] == "LOGGED_IN"

    def _detect_candidate_state(self, candidate: TabCandidate) -> dict[str, Any]:
        address = self.tab_manager.current_address_details(candidate.window)
        current_url = address.normalized_url
        diagnostic_elements = self.ui.diagnostic_elements(candidate.window, limit=100)
        LOGGER.info(
            "Chrome URL 진단: raw_url=%r normalized_url=%r url_source=%s",
            address.raw_url,
            address.normalized_url,
            address.url_source,
        )
        LOGGER.info(
            "DPS UIA 읽기 가능 요소(최대 100개): %s",
            json.dumps(diagnostic_elements, ensure_ascii=False, default=str),
        )
        selected_tab_title = self.tab_manager.selected_tab_title(candidate.window)
        tab_selected = self.tab_manager.is_tab_selected(candidate.tab)
        selected_identity_matches = bool(
            selected_tab_title
            and selected_tab_title.casefold() == candidate.tab_title.casefold()
        )
        selected_title_matches = bool(
            selected_tab_title
            and self.tab_manager.matches_dps_title(selected_tab_title)
        )
        login_check = self.ui.detect_login_state(
            candidate.window,
            url=current_url,
            allowed_hosts=self.tab_manager.allowed_hosts,
        )
        if (
            login_check["state"] == "DPS_PAGE_INVALID"
            and not current_url
            and selected_identity_matches
            and selected_title_matches
        ):
            login_check = {
                **login_check,
                "state": "LOGIN_UNCERTAIN",
                "result": "LOGIN_UNCERTAIN",
                "reason": "DPS 탭 제목은 일치하지만 URL과 DPS UI 요소를 확인하지 못함",
            }
        if login_check["state"] == "LOGGED_IN" and not tab_selected:
            login_check = {
                **login_check,
                "state": "LOGIN_UNCERTAIN",
                "result": "LOGIN_UNCERTAIN",
                "reason": "연결 후보가 현재 선택된 DPS 탭인지 확인할 수 없음",
            }
        page_check = self.ui.detect_current_dps_page(
            candidate.window,
            url=current_url,
            login_check=login_check,
        )
        result = {
            "login_state": login_check["state"],
            "login_reason": login_check["reason"],
            "login_signals": login_check,
            "current_page": page_check["page"],
            "current_page_label": page_check["page_label"],
            "page_reason": page_check["reason"],
            "current_url": current_url,
            "raw_url": address.raw_url,
            "normalized_url": address.normalized_url,
            "url_source": address.url_source,
            "current_selected_tab_title": selected_tab_title,
            "tab_element_selected": tab_selected,
            "selected_tab_title_matches": selected_title_matches,
        }
        LOGGER.info(
            "DPS 상태 판별: window=%r tab=%r url=%r result=%s reason=%s page=%s",
            candidate.window_title,
            selected_tab_title or candidate.tab_title,
            current_url,
            result["login_state"],
            result["login_reason"],
            result["current_page"],
        )
        return result

    def _log_login_diagnostic(
        self,
        *,
        candidate: TabCandidate | None,
        state_result: dict[str, Any] | None = None,
        failure_reason: str | None = None,
    ) -> None:
        state_result = state_result or (
            self._detect_candidate_state(candidate) if candidate is not None else {}
        )
        signals = state_result.get("login_signals") or {}
        snapshot = {
            "current_url": state_result.get("current_url", ""),
            "current_window_title": candidate.window_title if candidate else None,
            "candidate_tab_title": candidate.tab_title if candidate else None,
            "current_selected_tab_title": state_result.get(
                "current_selected_tab_title"
            ),
            "domain_ok": signals.get("domain_ok", False),
            "path_ok": signals.get("path_ok", False),
            "login_page": signals.get("login_page", False),
            "login_url_hits": signals.get("login_url_hits", []),
            "login_ui_hits": signals.get("login_ui_hits", []),
            "logout_found": signals.get("logout_found", False),
            "menu_hits": signals.get("menu_hits", []),
            "widget_hits": signals.get("widget_hits", []),
            "result": state_result.get("login_state", "DPS_TAB_NOT_FOUND"),
            "reason": failure_reason or state_result.get("login_reason"),
            "current_page": state_result.get("current_page", "UNKNOWN"),
        }
        log_method = (
            LOGGER.info
            if snapshot["result"] == "LOGGED_IN"
            else LOGGER.warning
        )
        log_method(
            "login_check: %s",
            json.dumps(snapshot, ensure_ascii=False, default=str),
        )

    @staticmethod
    def _connection_from_candidate(candidate: TabCandidate) -> RuntimeConnection:
        return RuntimeConnection(
            hwnd=candidate.hwnd,
            window_title=candidate.window_title,
            tab_title=candidate.tab_title,
            connected_at=now_iso(),
            tab=candidate.tab,
            current_url=candidate.current_url,
        )

    def _remember_connection(self, candidate: TabCandidate) -> None:
        LOGGER.info(
            "remember_connection called: hwnd=%s tab=%r url=%r",
            candidate.hwnd,
            candidate.tab_title,
            candidate.current_url,
        )
        self.connection = self._connection_from_candidate(candidate)
        LOGGER.info(
            "runtime connection created: hwnd=%s tab=%r connected_at=%s",
            self.connection.hwnd,
            self.connection.tab_title,
            self.connection.connected_at,
        )
        self.connection_status = "CONNECTED"
        self.last_error = None
        self.store.update(
            last_window_title=candidate.window_title,
            last_tab_title=candidate.tab_title,
            last_connected_at=self.connection.connected_at,
            auto_connect=True,
        )
        self._save_state()
        LOGGER.info(
            "DPS 탭 연결 저장: window=%r tab=%r",
            candidate.window_title,
            candidate.tab_title,
        )

    def _current_candidate(self) -> TabCandidate | None:
        if self.connection is None:
            LOGGER.info("connection validation result: valid=False reason=NO_CONNECTION")
            return None
        candidate = self.tab_manager.candidate_for_connection(self.connection)
        if candidate is not None:
            # UIA may return a fresh wrapper for the same TabItem on each scan.
            # Keep the runtime connection pointed at the live wrapper.
            self.connection.tab = candidate.tab
            self.connection.window_title = candidate.window_title
            LOGGER.info(
                "connection validation result: valid=True reason=STORED_TAB_PRESENT "
                "hwnd=%s tab=%r",
                candidate.hwnd,
                candidate.tab_title,
            )
            return candidate
        reason = str(
            getattr(self.tab_manager, "last_connection_failure_reason", "")
            or "VALIDATION_INCONCLUSIVE"
        )
        if reason not in {"WINDOW_CLOSED", "TAB_CLOSED"}:
            LOGGER.warning(
                "connection validation result: valid=unknown reason=%s; "
                "existing RuntimeConnection preserved",
                reason,
            )
            return None
        LOGGER.warning("연결된 DPS 탭 또는 Chrome 창이 실제로 닫혔습니다.")
        self.connection = None
        LOGGER.warning("connection cleared reason: %s", reason)
        self.connection_status = "TAB_CLOSED"
        self.last_error = "연결된 DPS 탭 또는 Chrome 창이 닫혔습니다."
        self._save_state()
        return None

    def ensure_connection(
        self,
        *,
        select_tab: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        return self._run_guarded_gui_operation(
            "AUTO_CONNECT",
            lambda: self._ensure_connection_unlocked(
                select_tab=select_tab, force=force
            ),
        )

    def _ensure_connection_unlocked(
        self,
        *,
        select_tab: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        with self.lock:
            current = self._current_candidate()
            if current is not None:
                if select_tab:
                    selected, selected_value = self.tab_manager.select_candidate(current)
                    if not selected:
                        return failure(
                            str(selected_value),
                            "연결된 DPS 탭을 선택하거나 검증하지 못했습니다.",
                        )
                return self._connection_response(
                    current,
                    code="CONNECTION_REUSED",
                    message="기존 DPS 탭 연결을 계속 사용합니다.",
                )

            config = self.store.load()
            if not force and not config.get("auto_connect", True):
                return failure(
                    "AUTO_CONNECT_DISABLED",
                    "DPS 자동 연결이 꺼져 있습니다. 자동 재연결을 눌러 주세요.",
                )

            self.connection_status = "SEARCHING"
            self._save_state()
            windows = self.tab_manager.chrome_windows()
            if not windows:
                self.connection_status = "CHROME_NOT_FOUND"
                self.last_error = "Chrome을 찾지 못했습니다."
                self._save_state()
                return failure(
                    "CHROME_NOT_FOUND",
                    "실행 중인 일반 Google Chrome을 찾지 못했습니다.",
                )

            candidates = self.tab_manager.find_candidates(windows)
            LOGGER.info("verified candidate count: %d", len(candidates))
            if not candidates:
                self.connection_status = "DPS_TAB_NOT_FOUND"
                self.last_error = "Samsung DPS 탭을 찾지 못했습니다."
                self._save_state()
                return failure(
                    "DPS_TAB_NOT_FOUND",
                    "열려 있는 Chrome에서 Samsung DPS 탭을 찾지 못했습니다.",
                    {"chrome_window_count": len(windows)},
                )

            preferred_title = str(config.get("last_tab_title") or "").casefold()
            preferred = [
                candidate
                for candidate in candidates
                if preferred_title
                and candidate.tab_title.casefold() == preferred_title
            ]
            ordered_candidates = preferred + [
                item for item in candidates if item not in preferred
            ]
            # find_candidates() only returns tabs whose selected address was
            # verified as dps2u.co.kr. Preserve that exact TabItem immediately;
            # discovery has already restored the user's original foreground tab.
            candidate = ordered_candidates[0]
            LOGGER.info(
                "selected candidate identity: hwnd=%s tab=%r url=%r",
                candidate.hwnd,
                candidate.tab_title,
                candidate.current_url,
            )
            self._remember_connection(candidate)
            LOGGER.info(
                "connection preserved after restore: preserved=%s hwnd=%s tab=%r",
                self.connection is not None,
                candidate.hwnd,
                candidate.tab_title,
            )
            return self._connection_response(
                candidate,
                code="AUTO_CONNECTED",
                message="Samsung DPS 탭을 자동으로 찾아 연결했습니다.",
            )

    def _connection_response(
        self,
        candidate: TabCandidate,
        *,
        code: str,
        message: str,
    ) -> dict[str, Any]:
        return success(
            code,
            message,
            connected=True,
            connected_hwnd=candidate.hwnd,  # 기존 API 호환용. UI에는 표시하지 않습니다.
            connected_window_title=candidate.window_title,
            connected_tab_title=candidate.tab_title,
            last_connected_at=(
                self.connection.connected_at if self.connection else None
            ),
            auto_connect=bool(self.store.load().get("auto_connect", True)),
        )

    def list_chrome_windows(self) -> dict[str, Any]:
        return self._run_guarded_gui_operation(
            "CHROME_TAB_DISCOVERY", self._list_chrome_windows_locked
        )

    def _list_chrome_windows_locked(self) -> dict[str, Any]:
        with self.lock:
            return self._list_chrome_windows_unlocked()

    def _list_chrome_windows_unlocked(self) -> dict[str, Any]:
        windows: list[dict[str, Any]] = []
        for window in self.tab_manager.chrome_windows():
            try:
                hwnd = int(window.handle)
            except Exception:
                continue
            candidates = self.tab_manager.find_candidates([window])
            if not candidates:
                continue
            tabs = [
                {
                    "title": candidate.tab_title,
                    "url": candidate.current_url,
                    "dps_score": candidate.score,
                    "dps_url_verified": True,
                }
                for candidate in candidates
            ]
            windows.append(
                {
                    "hwnd": hwnd,
                    "title": candidates[0].window_title or "DPS URL 확인된 Chrome 창",
                    "tabs": tabs,
                    "dps_score": 1000,
                    "recommended": True,
                }
            )
        windows.sort(
            key=lambda item: (item["dps_score"], item["title"]),
            reverse=True,
        )
        return success(
            "CHROME_WINDOWS_LISTED",
            "Chrome 창 목록을 확인했습니다.",
            windows=windows,
            count=len(windows),
        )

    def connect_window_by_handle(self, hwnd: Any) -> dict[str, Any]:
        return self._run_guarded_gui_operation(
            "MANUAL_CONNECT",
            lambda: self._connect_window_by_handle_locked(hwnd),
        )

    def _connect_window_by_handle_locked(self, hwnd: Any) -> dict[str, Any]:
        with self.lock:
            return self._connect_window_by_handle_unlocked(hwnd)

    def _connect_window_by_handle_unlocked(self, hwnd: Any) -> dict[str, Any]:
        try:
            target = int(hwnd)
        except (TypeError, ValueError):
            return failure("INVALID_WINDOW", "선택한 Chrome 창 정보가 올바르지 않습니다.")
        window = self.tab_manager.window_from_handle(target)
        if window is None:
            return failure(
                "CHROME_WINDOW_CLOSED",
                "선택한 Chrome 창을 찾지 못했습니다. 창 목록을 새로고침해 주세요.",
            )
        candidates = self.tab_manager.find_candidates([window])
        if not candidates:
            return failure(
                "DPS_TAB_NOT_FOUND",
                "선택한 Chrome 창 안에서 Samsung DPS 탭을 찾지 못했습니다.",
            )
        candidate = None
        selected_value = "DPS_TAB_VERIFICATION_FAILED"
        for item in candidates:
            selected, selected_value = self.tab_manager.select_candidate(item)
            if selected:
                candidate = item
                break
            LOGGER.warning(
                "수동 연결 DPS 후보 검증 실패: tab=%r code=%s",
                item.tab_title,
                selected_value,
            )
        if candidate is None:
            return failure(
                str(selected_value),
                "선택한 창에서 dps2u.co.kr 탭을 선택하고 검증하지 못했습니다.",
            )
        self._remember_connection(candidate)
        state_result = self._detect_candidate_state(candidate)
        response = self._connection_response(
            candidate,
            code="MANUAL_CONNECTED",
            message="선택한 Chrome 창의 Samsung DPS 탭을 연결했습니다.",
        )
        response.update(
            {
                "login_state": state_result["login_state"],
                "current_page": state_result["current_page"],
                "current_page_label": state_result["current_page_label"],
            }
        )
        return response

    def connect_current_window(self, delay_seconds: int = 4) -> dict[str, Any]:
        delay_seconds = max(2, min(int(delay_seconds or 4), 10))
        time.sleep(delay_seconds)
        hwnd = self.tab_manager.foreground_hwnd()
        if not hwnd:
            return failure("FOREGROUND_WINDOW_NOT_FOUND", "현재 선택된 창을 확인하지 못했습니다.")
        return self.connect_window_by_handle(hwnd)

    def disconnect_current_window(self) -> dict[str, Any]:
        self.connection = None
        self.connection_status = "DISCONNECTED"
        self.store.update(auto_connect=False)
        self._save_state()
        return success(
            "DISCONNECTED",
            "DPS 탭 연결을 해제하고 자동 연결을 껐습니다.",
            connected=False,
            auto_connect=False,
        )

    def _select_current_dps(self) -> tuple[TabCandidate | None, dict[str, Any] | None]:
        if (
            self.connection is None
            and not self.session_settings.on_demand_connect_enabled
        ):
            return None, failure(
                "ON_DEMAND_CONNECT_DISABLED",
                "DPS on-demand connection is disabled by configuration.",
            )
        connected = self._ensure_connection_unlocked(select_tab=False, force=True)
        if not connected.get("success"):
            return None, connected
        candidate = self._current_candidate()
        attempted: set[tuple[int, str]] = set()
        candidates = [candidate] if candidate is not None else []
        candidates.extend(self.tab_manager.find_candidates())
        last_code = "DPS_TAB_VERIFICATION_FAILED"
        for candidate in candidates:
            identity = (candidate.hwnd, candidate.tab_title.casefold())
            if identity in attempted:
                continue
            attempted.add(identity)
            selected, selected_value = self.tab_manager.select_candidate(candidate)
            if selected:
                self._remember_connection(candidate)
                return candidate, None
            last_code = str(selected_value)
            LOGGER.warning(
                "DPS 후보 검증 실패, 다음 후보를 확인합니다: tab=%r code=%s",
                candidate.tab_title,
                last_code,
            )
        self.connection_status = "TAB_VERIFICATION_FAILED"
        self.last_error = "선택된 Chrome TabItem과 DPS 주소를 함께 검증하지 못했습니다."
        self._save_state()
        return None, failure(
            last_code,
            "선택된 Chrome TabItem과 DPS 사이트 주소를 함께 검증하지 못해 입력을 중단했습니다.",
        )

    def open_login(self) -> dict[str, Any]:
        connected = self.ensure_connection(select_tab=True)
        if connected.get("success"):
            connected["reused_existing_window"] = True
            return connected
        connected["reused_existing_window"] = False
        connected["new_tab_opened"] = False
        return connected

    def open_browser(self) -> dict[str, Any]:
        connected = self.ensure_connection(select_tab=True)
        if connected.get("success"):
            connected["reused_existing_window"] = True
            return connected
        connected["reused_existing_window"] = False
        connected["new_tab_opened"] = False
        return connected

    def confirm_login(self) -> dict[str, Any]:
        return self._run_guarded_gui_operation(
            "LOGIN_RECHECK", self._confirm_login_unlocked
        )

    def _confirm_login_unlocked(self) -> dict[str, Any]:
        LOGGER.info("login recheck started")
        had_connection = self.connection is not None
        candidate = self._current_candidate()
        if candidate is not None:
            LOGGER.info(
                "existing connection reused: hwnd=%s tab=%r",
                candidate.hwnd,
                candidate.tab_title,
            )
            selected, selected_value = self.tab_manager.select_candidate(candidate)
            if not selected:
                LOGGER.warning(
                    "recheck validation result: success=False state=LOGIN_UNCERTAIN "
                    "reason=%s connection_preserved=%s",
                    selected_value,
                    self.connection is not None,
                )
                return failure(
                    "LOGIN_RECHECK_TEMPORARY_FAILURE",
                    "저장된 DPS 탭 연결은 유지했지만 현재 탭 선택 또는 UIA 검증에 실패했습니다.",
                    {
                        "reason": str(selected_value),
                        "connection_preserved": self.connection is not None,
                    },
                    login_state="LOGIN_UNCERTAIN",
                    logged_in=False,
                )
            self.connection.tab = candidate.tab
        else:
            if self.connection is not None:
                LOGGER.warning(
                    "recheck validation result: success=False state=LOGIN_UNCERTAIN "
                    "reason=TRANSIENT_CONNECTION_VALIDATION_FAILURE "
                    "connection_preserved=True"
                )
                return failure(
                    "LOGIN_RECHECK_TEMPORARY_FAILURE",
                    "일시적인 UIA 읽기 실패로 로그인 상태를 확인하지 못했지만 기존 연결은 유지했습니다.",
                    {"connection_preserved": True},
                    login_state="LOGIN_UNCERTAIN",
                    logged_in=False,
                )
            connected = self._ensure_connection_unlocked(
                select_tab=True, force=True
            )
            if not connected.get("success"):
                LOGGER.warning(
                    "recheck validation result: success=False state=%s "
                    "connection_preserved=%s",
                    connected.get("code"),
                    self.connection is not None,
                )
                return connected
            candidate = self._current_candidate()
        if candidate is None:
            self._log_login_diagnostic(
                candidate=None,
                failure_reason="로그인 재확인 중 DPS 탭 UIA 읽기 실패",
            )
            return failure(
                "LOGIN_RECHECK_TEMPORARY_FAILURE",
                "DPS 연결은 유지했지만 로그인 상태를 읽지 못했습니다.",
                {"connection_preserved": self.connection is not None},
                login_state="LOGIN_UNCERTAIN",
                logged_in=False,
            )
        state_result = self._detect_candidate_state(candidate)
        self._log_login_diagnostic(candidate=candidate, state_result=state_result)
        login_state = state_result["login_state"]
        LOGGER.info(
            "recheck validation result: success=%s state=%s reason=%s "
            "existing_connection=%s",
            login_state == "LOGGED_IN",
            login_state,
            state_result["login_reason"],
            had_connection,
        )
        if login_state != "LOGGED_IN":
            code = (
                "DPS_LOGIN_REQUIRED"
                if login_state == "LOGIN_REQUIRED"
                else login_state
            )
            return failure(
                code,
                (
                    "DPS 로그인이 필요합니다."
                    if login_state == "LOGIN_REQUIRED"
                    else "DPS 로그인 상태를 확실히 확인하지 못했습니다."
                ),
                {
                    "login_state": login_state,
                    "reason": state_result["login_reason"],
                    "current_url": state_result["current_url"],
                },
                login_state=login_state,
                logged_in=False,
                login_required=login_state == "LOGIN_REQUIRED",
            )
        current = time.time()
        self.login_confirmed_at = current
        self.last_activity_at = current
        self.last_error = None
        self._save_state()
        return success(
            "LOGIN_CONFIRMED",
            "DPS 로그인 완료 상태로 등록했습니다.",
            logged_in=True,
            login_state="LOGGED_IN",
            login_status="LOGGED_IN",
            current_page=state_result["current_page"],
            current_page_label=state_result["current_page_label"],
            remaining_seconds=SESSION_IDLE_SECONDS,
        )

    def mark_logged_out(self) -> dict[str, Any]:
        self.login_confirmed_at = None
        self.last_activity_at = None
        self._save_state()
        return success(
            "LOGGED_OUT",
            "DPS 상태를 미로그인으로 변경했습니다.",
            logged_in=False,
        )

    def refresh_session(self) -> dict[str, Any]:
        return self.monitor_session(
            keepalive_enabled=True,
            keepalive_interval_seconds=0,
            force_keepalive=True,
            trigger="MANUAL_REFRESH",
        )

    def status(self) -> dict[str, Any]:
        with self.lock:
            return self._status_unlocked()

    def _cleanup_lookup_jobs(self) -> None:
        cutoff = time.time() - LOOKUP_JOB_TTL_SECONDS
        with self.lookup_jobs_lock:
            expired = [
                request_id
                for request_id, job in self.lookup_jobs.items()
                if float(job.get("updated_at_epoch") or 0) < cutoff
                and job.get("job_status") != "RUNNING"
            ]
            for request_id in expired:
                self.lookup_jobs.pop(request_id, None)
            if len(self.lookup_jobs) > LOOKUP_JOB_MAX_ITEMS:
                ordered = sorted(
                    self.lookup_jobs.items(),
                    key=lambda item: float(
                        item[1].get("updated_at_epoch") or 0
                    ),
                )
                for request_id, job in ordered:
                    if len(self.lookup_jobs) <= LOOKUP_JOB_MAX_ITEMS:
                        break
                    if job.get("job_status") != "RUNNING":
                        self.lookup_jobs.pop(request_id, None)

    @staticmethod
    def _lookup_fingerprint(
        order_id: Any,
        period_start: Any,
        period_end: Any,
        force_refresh: Any,
    ) -> tuple[str, str, str, bool]:
        return (
            str(order_id or "").strip(),
            str(period_start or "").strip(),
            str(period_end or "").strip(),
            bool(force_refresh),
        )

    def _lookup_job_view(
        self,
        job: dict[str, Any],
    ) -> dict[str, Any]:
        result = job.get("result")
        if job.get("job_status") in {"COMPLETED", "FAILED"} and isinstance(
            result, dict
        ):
            recovered = dict(result)
            recovered.update(
                {
                    "request_id": job["request_id"],
                    "job_status": job["job_status"],
                    "stage": job["stage"],
                    "started_at": job["started_at"],
                    "updated_at": job["updated_at"],
                    "completed_at": job.get("completed_at"),
                    "result_source": "request_state",
                }
            )
            return recovered
        return success(
            "LOOKUP_RUNNING",
            "DPS 상세조회가 진행 중입니다.",
            request_id=job["request_id"],
            job_status=job["job_status"],
            stage=job["stage"],
            started_at=job["started_at"],
            updated_at=job["updated_at"],
            completed_at=job.get("completed_at"),
            completed=False,
        )

    def _update_lookup_stage(
        self,
        request_id: str,
        stage: str,
    ) -> None:
        self.last_lookup_stage = str(stage)
        with self.lookup_jobs_lock:
            job = self.lookup_jobs.get(request_id)
            if job is None or job.get("job_status") != "RUNNING":
                return
            job.update(
                {
                    "stage": stage,
                    "updated_at": now_iso(),
                    "updated_at_epoch": time.time(),
                }
            )

    def lookup_status(
        self,
        request_id: str,
        *,
        order_id: str | None = None,
        period_start: str | None = None,
        period_end: str | None = None,
    ) -> dict[str, Any]:
        self._cleanup_lookup_jobs()
        normalized_request_id = str(request_id or "").strip()
        with self.lookup_jobs_lock:
            job = self.lookup_jobs.get(normalized_request_id)
            if job is not None:
                return self._lookup_job_view(dict(job))

        # A completed request remains recoverable after an Agent restart only
        # when the persisted request_id is an exact match.
        try:
            last_result = json.loads(
                LAST_LOOKUP_RESULT_FILE.read_text(encoding="utf-8")
            )
        except Exception:
            last_result = None
        if (
            isinstance(last_result, dict)
            and last_result.get("request_id") == normalized_request_id
        ):
            recovered = dict(last_result)
            recovered.update(
                {
                    "job_status": (
                        "COMPLETED"
                        if recovered.get("success")
                        else "FAILED"
                    ),
                    "stage": (
                        "COMPLETED"
                        if recovered.get("success")
                        else "FAILED"
                    ),
                    "completed": True,
                    "result_source": "last_dps_lookup_result",
                }
            )
            return recovered

        # Cache recovery is permitted only for the exact order and period.
        if order_id and period_start and period_end:
            try:
                cache = self._cache()
                cache_key = dps_cache_key(
                    order_id,
                    "order_id",
                    period_start,
                    period_end,
                )
                entry = cache.get(cache_key)
            except Exception:
                entry = None
            if (
                isinstance(entry, dict)
                and entry.get("cache_schema_version")
                == CACHE_SCHEMA_VERSION
                and entry.get("order_id") == order_id
                and entry.get("dps_period_start") == period_start
                and entry.get("dps_period_end") == period_end
                and isinstance(entry.get("result"), dict)
            ):
                recovered = dict(entry["result"])
                recovered.update(
                    {
                        "request_id": normalized_request_id,
                        "job_status": "COMPLETED",
                        "stage": "COMPLETED",
                        "completed": True,
                        "result_source": "schema5_cache",
                    }
                )
                return recovered
        return failure(
            "LOOKUP_REQUEST_NOT_FOUND",
            "해당 request_id의 DPS 조회 상태를 찾지 못했습니다.",
            {"request_id": normalized_request_id},
            request_id=normalized_request_id,
            job_status="NOT_FOUND",
            stage="UNKNOWN",
        )

    def _status_unlocked(self) -> dict[str, Any]:
        remaining = self._session_remaining()
        config = self.store.load()
        runtime = self.connection
        hwnd_valid: bool | None = None
        if runtime is not None:
            try:
                hwnd_valid = bool(self.tab_manager.is_window(runtime.hwnd))
            except Exception:
                hwnd_valid = None
        logged_in = bool(self.login_confirmed_at or self.session_status == "READY")
        state_result = {
            "login_state": "LOGGED_IN" if logged_in else "LOGIN_UNCERTAIN",
            "login_reason": "passive status uses the last saved session state",
            "current_page": "UNKNOWN",
            "current_page_label": "passive status",
            "current_url": runtime.current_url if runtime else "",
            "login_signals": {},
        }
        return success(
            "STATUS_OK",
            "DPS Agent 상태를 확인했습니다.",
            agent_running=True,
            mode=AGENT_MODE,
            logged_in=logged_in,
            login_status=state_result["login_state"],
            login_state=state_result["login_state"],
            login_reason=state_result["login_reason"],
            login_signals=state_result.get("login_signals", {}),
            current_page=state_result["current_page"],
            current_page_label=state_result["current_page_label"],
            current_url=state_result["current_url"],
            remaining_seconds=remaining,
            last_lookup_at=self.last_lookup_at,
            last_successful_lookup_at=self.last_successful_lookup_at,
            last_order_number=self.last_order_number,
            last_error=self.last_error,
            agent_pid=os.getpid(),
            agent_started_at=datetime.fromtimestamp(
                self.started_at
            ).astimezone().isoformat(timespec="seconds"),
            last_lookup_stage=self.last_lookup_stage,
            last_window_restore_warning=self.last_window_restore_warning,
            lookup_in_progress=self.lookup_in_progress,
            active_request_id=self.active_request_id,
            connected=self.connection is not None,
            window_manually_connected=self.connection is not None,  # 기존 UI/클라이언트 호환
            connected_hwnd=self.connection.hwnd if self.connection else None,
            connected_window_title=(
                runtime.window_title
                if runtime
                else None
            ),
            connected_tab_title=(
                runtime.tab_title
                if runtime
                else None
            ),
            dps_window_title=(
                runtime.window_title
                if runtime
                else None
            ),
            dps_window_found=hwnd_valid is True,
            connection_mode="TAB_UIA_V6" if runtime else "ON_DEMAND",
            connection_status=self.connection_status,
            session_status=self.session_status,
            monitor_status=self.session_status,
            last_checked_at=self.last_checked_at,
            last_ready_at=self.last_ready_at,
            last_keepalive_at=self.last_keepalive_at,
            last_keepalive_attempt_at=self.last_keepalive_attempt_at,
            consecutive_keepalive_failures=self.consecutive_keepalive_failures,
            keepalive_lock_skips=self.keepalive_lock_skips,
            last_monitor_event=self.last_monitor_event,
            passive_idle_enabled=self.session_settings.passive_idle_enabled,
            passive_session_monitor_enabled=(
                self.session_settings.passive_monitor_enabled
            ),
            on_demand_connect_enabled=(
                self.session_settings.on_demand_connect_enabled
            ),
            last_passive_monitor_at=self.last_passive_monitor_at,
            last_gui_operation_at=self.last_gui_operation_at,
            last_gui_operation_type=self.last_gui_operation_type,
            lookup_gate_owner=self.lookup_gate_owner,
            last_connected_at=config.get("last_connected_at"),
            auto_connect=bool(config.get("auto_connect", True)),
            started_at=datetime.fromtimestamp(self.started_at)
            .astimezone()
            .isoformat(timespec="seconds"),
        )

    def diagnostics(self) -> dict[str, Any]:
        status = self._status_unlocked()
        runtime = self.connection
        checks = [
            {
                "name": "Passive idle",
                "ok": self.session_settings.passive_idle_enabled,
                "detail": "No UI traversal is performed by diagnostics.",
            },
            {
                "name": "Stored DPS connection",
                "ok": runtime is not None,
                "detail": runtime.tab_title if runtime else "No runtime connection metadata.",
            },
            {
                "name": "Last known session",
                "ok": self.session_status == "READY",
                "detail": self.session_status,
            },
        ]
        return success(
            "DIAGNOSTICS_COMPLETE",
            "Passive DPS diagnostics completed without Windows GUI access.",
            checks=checks,
            diagnostic_texts=[],
            log_file=str(LOG_FILE),
            ui_tree_log_file=str(UI_TREE_LOG_FILE),
            result_tree_log_file=str(RESULT_TREE_LOG_FILE),
            login_state=status["login_state"],
            current_page=status["current_page"],
            current_page_label=status["current_page_label"],
            mode=AGENT_MODE,
            passive=True,
            passive_idle_enabled=self.session_settings.passive_idle_enabled,
            passive_session_monitor_enabled=(
                self.session_settings.passive_monitor_enabled
            ),
            last_passive_monitor_at=self.last_passive_monitor_at,
            last_gui_operation_at=self.last_gui_operation_at,
            last_gui_operation_type=self.last_gui_operation_type,
        )

    def _cache(self) -> dict[str, Any]:
        value = _read_json(CACHE_FILE, {})
        return value if isinstance(value, dict) else {}

    def _navigation_safety_checks(
        self,
        candidate: TabCandidate,
    ) -> tuple[bool, dict[str, Any]]:
        tab_ok, tab_checks = self.tab_manager.validate_selected_candidate(candidate)
        state_result = self._detect_candidate_state(candidate)
        signals = state_result.get("login_signals") or {}
        checks = {
            **tab_checks,
            "login_state": state_result["login_state"],
            "login_state_logged_in": state_result["login_state"] == "LOGGED_IN",
            "current_url": state_result["current_url"],
            "current_page": state_result["current_page"],
            "dps_domain_valid": bool(signals.get("domain_ok")),
            "dps_path_valid": bool(signals.get("path_ok")),
        }
        # 메뉴 전환 중에는 본문 UI가 잠시 비어 LOGIN_UNCERTAIN이 될 수 있습니다.
        # DPS는 메뉴 이동 때 같은 TabItem의 제목을 페이지 breadcrumb로
        # 바꿉니다. 탐색 단계는 창/TabItem/URL을 고정하고, 입력 직전
        # 안전 게이트에서
        # LOGGED_IN과 구매요청 화면을 다시 필수 검증합니다.
        return (
            tab_ok
            and checks["dps_domain_valid"]
            and checks["dps_path_valid"]
        ), checks

    def _lookup_safety_checks(
        self,
        candidate: TabCandidate,
    ) -> tuple[bool, dict[str, Any]]:
        navigation_ok, checks = self._navigation_safety_checks(candidate)
        purchase = self.ui.verify_purchase_request_page(candidate.window)
        checks.update(
            {
                "purchase_page_verified": purchase.navigation_ok,
                "input_ready": purchase.input_ready,
                "online_order_input_found": purchase.edit is not None,
                "query_action_found": purchase.query_action is not None,
                "purchase_page_reason": purchase.reason,
            }
        )
        return (
            navigation_ok
            and checks["login_state_logged_in"]
            and purchase.input_ready
        ), checks

    def lookup(
        self,
        order_number: str | None = None,
        force_refresh: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        resolved_request_id = str(
            kwargs.get("request_id") or f"legacy-{uuid.uuid4()}"
        ).strip()
        kwargs["request_id"] = resolved_request_id
        fingerprint = self._lookup_fingerprint(
            kwargs.get("order_id") or order_number,
            kwargs.get("dps_period_start"),
            kwargs.get("dps_period_end"),
            force_refresh,
        )
        self._cleanup_lookup_jobs()
        with self.lookup_jobs_lock:
            existing = self.lookup_jobs.get(resolved_request_id)
            if existing is not None:
                return self._lookup_job_view(dict(existing))
            for job in self.lookup_jobs.values():
                if (
                    job.get("job_status") == "RUNNING"
                    and tuple(job.get("fingerprint") or ()) == fingerprint
                ):
                    return self._lookup_job_view(dict(job))
            timestamp = now_iso()
            self.lookup_jobs[resolved_request_id] = {
                "request_id": resolved_request_id,
                "job_status": "RUNNING",
                "stage": "REQUEST_ACCEPTED",
                "started_at": timestamp,
                "updated_at": timestamp,
                "updated_at_epoch": time.time(),
                "completed_at": None,
                "result": None,
                "error": None,
                "fingerprint": fingerprint,
            }
        with self.lookup_jobs_lock:
            self.lookup_jobs[resolved_request_id].update(
                {
                    "stage": "NAVIGATING",
                    "updated_at": now_iso(),
                    "updated_at_epoch": time.time(),
                }
            )
        result = self._execute_lookup(
            order_number,
            force_refresh,
            **kwargs,
        )
        completed_at = now_iso()
        with self.lookup_jobs_lock:
            job = self.lookup_jobs[resolved_request_id]
            job.update(
                {
                    "job_status": (
                        "COMPLETED" if result.get("success") else "FAILED"
                    ),
                    "stage": (
                        "COMPLETED" if result.get("success") else "FAILED"
                    ),
                    "updated_at": completed_at,
                    "updated_at_epoch": time.time(),
                    "completed_at": completed_at,
                    "result": dict(result),
                    "error": (
                        None
                        if result.get("success")
                        else result.get("code")
                    ),
                }
            )
        return result

    def _execute_lookup(
        self,
        order_number: str | None = None,
        force_refresh: bool = False,
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
    ) -> dict[str, Any]:
        legacy_value = str(order_number or "").strip() or None
        modern_request = any(
            value not in (None, "")
            for value in (
                request_id,
                selected_inquiry_id,
                order_id,
                product_order_id,
                dps_query_value,
                dps_query_value_type,
            )
        )
        normalized_order_id = str(order_id or legacy_value or "").strip() or None
        selected = select_dps_query_identifier(
            normalized_order_id,
            product_order_id,
        )
        resolved_request_id = str(
            request_id or f"legacy-{uuid.uuid4()}"
        ).strip()
        explicit_value = str(
            dps_query_value or selected.value or ""
        ).strip()
        explicit_type = str(
            dps_query_value_type or ("order_id" if selected.value else "")
        ).strip()
        context_details = {
            "request_id": resolved_request_id,
            "received_order_id": normalized_order_id,
            "received_product_order_id": (
                str(product_order_id).strip() if product_order_id else None
            ),
            "received_dps_query_value": explicit_value or None,
            "dps_query_value_type": explicit_type or None,
        }
        if modern_request and not request_id:
            return failure(
                "REQUEST_CONTEXT_MISMATCH",
                "request_id가 없는 DPS 조회 요청을 거부했습니다.",
                context_details,
            )
        if selected.error or not normalized_order_id:
            return failure(
                "DPS_ORDER_ID_MISSING",
                "네이버 주문번호가 없어 DPS 조회를 실행할 수 없습니다.",
                context_details,
            )
        if explicit_type != "order_id":
            return failure(
                "INVALID_DPS_QUERY_TYPE",
                "상품주문번호가 DPS 조회값으로 전달되어 안전을 위해 중단했습니다.",
                context_details,
            )
        if explicit_value != normalized_order_id:
            return failure(
                "REQUEST_CONTEXT_MISMATCH",
                "Agent 수신 네이버 주문번호와 DPS 조회값이 일치하지 않습니다.",
                context_details,
            )
        product_value = str(product_order_id or "").strip()
        if (
            product_value
            and product_value != normalized_order_id
            and explicit_value == product_value
        ):
            return failure(
                "PRODUCT_ORDER_ID_WAS_USED_BY_MISTAKE",
                "상품주문번호를 DPS 조회값으로 사용할 수 없습니다.",
                context_details,
            )
        order_fingerprint = identifier_fingerprint(normalized_order_id)
        product_fingerprint = identifier_fingerprint(product_value)
        LOGGER.info(
            "DPS request context: request_id=%s "
            "AGENT_RECEIVED_ORDER_ID tail=%s hash=%s "
            "PRODUCT_ORDER_ID tail=%s hash=%s type=order_id",
            resolved_request_id,
            order_fingerprint["tail"],
            order_fingerprint["hash"],
            product_fingerprint["tail"],
            product_fingerprint["hash"],
        )
        query_value = selected.value
        query_value_type = selected.type
        if not query_value or not query_value_type:
            return failure(
                "DPS_QUERY_ID_MISSING",
                "DPS 조회에 사용할 주문 식별자가 없습니다.",
            )
        raw_dates = {
            "order_date": order_date,
            "order_created_at": order_created_at,
            "payment_date": payment_date,
            "payment_completed_at": payment_completed_at,
            "place_order_date": place_order_date,
            "shipping_due_date": shipping_due_date,
        }
        date_request_present = any(
            value not in (None, "")
            for value in (
                *raw_dates.values(),
                dps_date_source,
                dps_reference_date,
                dps_period_start,
                dps_period_end,
            )
        )
        date_warnings: list[str] = []
        if date_request_present:
            selected_date = select_dps_date_source(raw_dates)
            if dps_reference_date and dps_date_source:
                selected_source = str(dps_date_source).strip()
                selected_reference = str(dps_reference_date).strip()
                if (
                    selected_date.source is not None
                    and selected_date.reference_date is not None
                    and (
                        selected_source != selected_date.source
                        or selected_reference
                        != selected_date.reference_date.isoformat()
                    )
                ):
                    return failure(
                        "DATE_CONTEXT_MISMATCH",
                        "Agent 수신 날짜 컨텍스트가 주문 날짜 우선순위와 일치하지 않습니다.",
                        {
                            **context_details,
                            "expected_date_source": selected_date.source,
                            "expected_reference_date": (
                                selected_date.reference_date.isoformat()
                            ),
                        },
                    )
            elif (
                selected_date.source is not None
                and selected_date.reference_date is not None
            ):
                selected_source = selected_date.source
                selected_reference = (
                    selected_date.reference_date.isoformat()
                )
                date_warnings.extend(selected_date.warnings)
            else:
                return failure(
                    "DATE_SOURCE_MISSING",
                    "주문일을 확인하지 못해 DPS 조회 기간을 계산할 수 없습니다.",
                    {"date_warnings": list(selected_date.warnings)},
                )
            try:
                calculated_period = calculate_dps_lookup_period(
                    selected_reference
                )
            except ValueError:
                return failure(
                    "DATE_SOURCE_MISSING",
                    "주문일을 확인하지 못해 DPS 조회 기간을 계산할 수 없습니다.",
                )
            canonical_start = calculated_period.start.isoformat()
            canonical_end = calculated_period.end.isoformat()
            date_warnings.extend(calculated_period.warnings)
            if (
                dps_period_start
                and str(dps_period_start).strip() != canonical_start
            ) or (
                dps_period_end
                and str(dps_period_end).strip() != canonical_end
            ):
                return failure(
                    "DATE_RANGE_INVALID",
                    "요청된 DPS 조회 기간이 주문일 기준 계산 결과와 일치하지 않습니다.",
                    {
                        "expected_start": canonical_start,
                        "expected_end": canonical_end,
                        "requested_start": dps_period_start,
                        "requested_end": dps_period_end,
                    },
                )
            period_valid, period_code, _, _ = (
                validate_dps_lookup_period(
                    canonical_start,
                    canonical_end,
                )
            )
            if not period_valid:
                return failure(
                    period_code,
                    "DPS 조회 기간이 안전 조건을 충족하지 않습니다.",
                )
            dps_date_source = selected_source
            dps_reference_date = selected_reference
            dps_period_start = canonical_start
            dps_period_end = canonical_end

        cache_key = dps_cache_key(
            query_value,
            query_value_type,
            dps_period_start,
            dps_period_end,
        )

        if resolved_request_id in self.completed_request_ids:
            return failure(
                "STALE_REQUEST_CONTEXT",
                "이미 처리한 request_id의 중복 실행을 거부했습니다.",
                context_details,
            )
        gate_acquired = self._acquire_actual_lookup_gate(timeout=5.0)
        if not gate_acquired:
            return failure(
                "CONCURRENT_REQUEST_REJECTED",
                "다른 DPS 조회가 실행 중이어서 요청을 거부했습니다.",
                {
                    **context_details,
                    "active_request_id": self.active_request_id,
                },
            )
        guard_state = self._wait_for_gui_resource(owner="LOOKUP")
        if not guard_state.available:
            self._release_actual_lookup_gate()
            return self._gui_resource_failure(guard_state)
        self._record_gui_operation("LOOKUP")
        with self.lock:
            self.lookup_in_progress = True
            self.active_request_id = resolved_request_id
            previous = self.tab_manager.capture_previous_context()
            candidate: TabCandidate | None = None
            try:
                candidate, connection_error = self._select_current_dps()
                if connection_error:
                    return connection_error
                LOGGER.info(
                    "조회 전 UI 정보: foreground_hwnd=%s window=%r selected_tab=%r",
                    previous.foreground_hwnd,
                    previous.window_title,
                    previous.selected_tab_title,
                )
                state_result = self._detect_candidate_state(candidate)
                self._log_login_diagnostic(
                    candidate=candidate,
                    state_result=state_result,
                )
                if state_result["login_state"] != "LOGGED_IN":
                    self.connection_status = state_result["login_state"]
                    self.last_error = state_result["login_reason"]
                    self._save_state()
                    login_required = (
                        state_result["login_state"] == "LOGIN_REQUIRED"
                    )
                    return failure(
                        (
                            "DPS_LOGIN_REQUIRED"
                            if login_required
                            else state_result["login_state"]
                        ),
                        (
                            "DPS 로그인이 필요합니다."
                            if login_required
                            else "DPS 로그인 상태를 확실히 확인하지 못했습니다."
                        ),
                        {
                            "current_url": state_result["current_url"],
                            "login_state": state_result["login_state"],
                            "reason": state_result["login_reason"],
                        },
                        login_required=login_required,
                        login_state=state_result["login_state"],
                    )

                navigation = self.ui.navigate_to_online_sales_purchase_request_list(
                    window=candidate.window,
                    validate_target=lambda: self._navigation_safety_checks(
                        candidate
                    ),
                )
                if not navigation.get("success"):
                    self.connection_status = str(
                        navigation.get("code") or "DPS_NAVIGATION_FAILED"
                    )
                    self.last_error = navigation.get("message")
                    details = dict(navigation.get("details") or {})
                    details.update(
                        {
                            "current_url": self.tab_manager.current_address(
                                candidate.window
                            ),
                            "login_state": state_result["login_state"],
                        }
                    )
                    navigation["details"] = details
                    self._save_state()
                    return navigation

                cache = self._cache()
                cached = cache.get(cache_key)
                if (
                    cached is None
                    and query_value_type == "order_id"
                    and legacy_value
                ):
                    cached = cache.get(query_value)
                if not force_refresh and isinstance(cached, dict):
                    saved_at = cached.get("saved_at_epoch")
                    if (
                        (
                            cached.get("cache_schema_version")
                            == CACHE_SCHEMA_VERSION
                        )
                        and cached.get("order_id") == normalized_order_id
                        and cached.get("dps_query_value")
                        == normalized_order_id
                        and cached.get("dps_query_value_type")
                        == "order_id"
                        and cached.get("dps_period_start")
                        == dps_period_start
                        and cached.get("dps_period_end")
                        == dps_period_end
                        and
                        isinstance(saved_at, (int, float))
                        and time.time() - saved_at < CACHE_TTL_SECONDS
                    ):
                        result = dict(cached.get("result") or {})
                        result.update(
                            {
                                "ok": True,
                                "success": True,
                                "cached": True,
                                "login_state": "LOGGED_IN",
                                "current_page": "PURCHASE_REQUEST_LIST",
                                "navigation": navigation,
                                "diagnostics": {
                                    **dict(result.get("diagnostics") or {}),
                                    "cache_hit": True,
                                },
                                "cache_hit": True,
                                "request_id": resolved_request_id,
                                "selected_inquiry_id": selected_inquiry_id,
                                "received_order_id": normalized_order_id,
                                "received_product_order_id": (
                                    product_value or None
                                ),
                                "received_dps_query_value": query_value,
                                "executed_dps_query_value": query_value,
                                "dps_input_verified_value": query_value,
                                "all_identifiers_match": True,
                            }
                        )
                        _write_json(
                            LAST_LOOKUP_RESULT_FILE,
                            _safe_lookup_log_payload(result),
                        )
                        return result

                automation = self.ui.perform_lookup(
                    window=candidate.window,
                    request_id=resolved_request_id,
                    expected_order_id=normalized_order_id,
                    order_number=query_value,
                    order_id=normalized_order_id,
                    product_order_id=product_order_id,
                    dps_query_value=query_value,
                    dps_query_value_type=query_value_type,
                    query_fallback_used=selected.fallback_used,
                    dps_date_source=dps_date_source,
                    dps_reference_date=dps_reference_date,
                    dps_period_start=dps_period_start,
                    dps_period_end=dps_period_end,
                    validate_target=lambda: self._lookup_safety_checks(candidate),
                    detail_window_provider=self.tab_manager.chrome_windows,
                    detail_url_reader=self.tab_manager.current_address,
                    progress_callback=lambda stage: self._update_lookup_stage(
                        resolved_request_id,
                        stage,
                    ),
                )
                self.last_activity_at = time.time()
                self.last_lookup_at = now_iso()
                self.last_order_number = self.ui.mask_order_number(query_value)
                if not automation.get("success"):
                    automation.update(
                        {
                            "request_id": resolved_request_id,
                            "received_order_id": normalized_order_id,
                            "received_product_order_id": product_value or None,
                            "received_dps_query_value": query_value,
                            "executed_dps_query_value": None,
                            "dps_input_verified_value": dict(
                                automation.get("diagnostics") or {}
                            ).get("verified_value"),
                            "all_identifiers_match": False,
                        }
                    )
                    self.last_error = automation.get("message")
                    self.connection_status = (
                        "PAGE_VERIFICATION_FAILED"
                        if automation.get("code") == "DPS_PAGE_VERIFICATION_FAILED"
                        else "LOOKUP_FAILED"
                    )
                    self._save_state()
                    return automation

                self.connection_status = "LOOKUP_COMPLETE"
                self.last_successful_lookup_at = self.last_lookup_at
                self.last_error = None
                result = dict(automation)
                result.update(
                    {
                        "request_id": resolved_request_id,
                        "selected_inquiry_id": selected_inquiry_id,
                        "received_order_id": normalized_order_id,
                        "received_product_order_id": product_value or None,
                        "received_dps_query_value": query_value,
                        "executed_dps_query_value": query_value,
                        "cached": False,
                        "cache_hit": False,
                        "source_order_number": query_value,
                        "order_id": normalized_order_id,
                        "product_order_id": (
                            str(product_order_id).strip()
                            if product_order_id
                            else None
                        ),
                        "dps_query_value": query_value,
                        "dps_query_value_type": query_value_type,
                        "query_fallback_used": selected.fallback_used,
                        "dps_date_source": dps_date_source,
                        "dps_reference_date": dps_reference_date,
                        "dps_period_start": dps_period_start,
                        "dps_period_end": dps_period_end,
                        "queried_at": self.last_lookup_at,
                        "connected_window_title": candidate.window_title,
                        "connected_tab_title": candidate.tab_title,
                        "window_mode": "Samsung DPS 탭 UI Automation",
                        "login_state": "LOGGED_IN",
                        "current_page": "PURCHASE_REQUEST_LIST",
                        "navigation": navigation,
                    }
                )
                verified_value = (
                    dict(result.get("diagnostics") or {}).get(
                        "verified_value"
                    )
                    or dict(result.get("data") or {}).get(
                        "dps_input_verified_value"
                    )
                    or query_value
                )
                all_match = (
                    normalized_order_id
                    == query_value
                    == result.get("executed_dps_query_value")
                    == str(verified_value or "").strip()
                )
                result.update(
                    {
                        "dps_input_verified_value": verified_value,
                        "all_identifiers_match": all_match,
                    }
                )
                result["diagnostics"] = {
                    **dict(result.get("diagnostics") or {}),
                    "request_id": resolved_request_id,
                    "order_id_fingerprint": identifier_fingerprint(
                        normalized_order_id
                    ),
                    "product_order_id_fingerprint": identifier_fingerprint(
                        product_value
                    ),
                    "all_order_ids_match": all_match,
                }
                if not all_match:
                    return failure(
                        "REQUEST_CONTEXT_MISMATCH",
                        "DPS 실행 식별자 echo가 요청 컨텍스트와 일치하지 않습니다.",
                        result["diagnostics"],
                    )
                _write_json(
                    LAST_LOOKUP_RESULT_FILE,
                    _safe_lookup_log_payload(result),
                )
                cache_status = result.get("status")
                if cache_status in {
                    "RESULT_FOUND",
                    "RESULT_FOUND_WITH_DETAIL",
                    "RESULT_FOUND_DETAIL_PARTIAL",
                    "DETAIL_DATE_CONFLICT",
                    "DETAIL_CLOSE_FAILED",
                    "NO_DPS_RESULT",
                } or (
                    cache_status is None
                    and result.get("success")
                ):
                    cache_result = sanitize_detail_for_cache(result)
                    cache[cache_key] = {
                        "cache_schema_version": CACHE_SCHEMA_VERSION,
                        "order_id": result.get("order_id"),
                        "product_order_id": result.get(
                            "product_order_id"
                        ),
                        "dps_query_value": query_value,
                        "dps_query_value_type": query_value_type,
                        "query_fallback_used": selected.fallback_used,
                        "dps_date_source": dps_date_source,
                        "dps_reference_date": dps_reference_date,
                        "dps_period_start": dps_period_start,
                        "dps_period_end": dps_period_end,
                        "date_warnings": date_warnings,
                        "status": (
                            result.get("status")
                            or result.get("code")
                        ),
                        "found": result.get("found"),
                        "queried_at": result.get("queried_at"),
                        "saved_at_epoch": time.time(),
                        "expires_at": datetime.fromtimestamp(
                            time.time() + CACHE_TTL_SECONDS
                        ).astimezone().isoformat(timespec="seconds"),
                        "detail_lookup": cache_result.get("detail_lookup"),
                        "requested_delivery_date": dict(
                            cache_result.get("data") or {}
                        ).get("requested_delivery_date"),
                        "delivery_scheduled_date": dict(
                            cache_result.get("data") or {}
                        ).get("delivery_scheduled_date"),
                        "delivery_date_source": dict(
                            cache_result.get("data") or {}
                        ).get("delivery_date_source"),
                        "delivery_date_status": dict(
                            cache_result.get("data") or {}
                        ).get("delivery_date_status"),
                        "detail_items": dict(
                            cache_result.get("data") or {}
                        ).get("detail_items", []),
                        "result": cache_result,
                    }
                    _write_json(CACHE_FILE, cache)
                self._save_state()
                return result
            except Exception as error:
                LOGGER.exception("DPS 조회 처리 중 예외")
                self.last_error = f"{error.__class__.__name__}: {error}"
                self.connection_status = "LOOKUP_FAILED"
                self._save_state()
                return failure(
                    "DPS_AUTOMATION_ERROR",
                    "DPS 화면 자동화 중 오류가 발생했습니다.",
                    {"error": error.__class__.__name__},
                )
            finally:
                try:
                    target_hwnd = (
                        candidate.hwnd
                        if candidate is not None
                        else (self.tab_manager.foreground_hwnd() or 0)
                    )
                    restored = self.tab_manager.restore_previous_context(
                        previous,
                        target_hwnd,
                    )
                    if restored:
                        self.last_window_restore_warning = None
                        LOGGER.info("원래 탭/창 복귀 결과: True")
                    else:
                        self.last_window_restore_warning = (
                            "PREVIOUS_WINDOW_RESTORE_FAILED"
                        )
                        LOGGER.warning(
                            "원래 탭/창 복귀 실패: request_id=%s "
                            "previous_hwnd=%s target_hwnd=%s",
                            resolved_request_id,
                            previous.foreground_hwnd,
                            target_hwnd,
                        )
                except Exception:
                    self.last_window_restore_warning = (
                        "PREVIOUS_WINDOW_RESTORE_EXCEPTION"
                    )
                    LOGGER.exception("원래 탭/창 복귀 중 오류")
                finally:
                    self.lookup_in_progress = False
                    self.active_request_id = None
                    self.completed_request_ids.append(resolved_request_id)
                    del self.completed_request_ids[:-100]
                    self._release_actual_lookup_gate()


AGENT = DpsWindowsAgent()


class Handler(BaseHTTPRequestHandler):
    def _send(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            LOGGER.info(
                "클라이언트 연결 종료 후 응답 생성 완료: path=%s",
                self.path,
            )

    def _payload(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/status":
                result = AGENT.status()
            elif parsed.path == "/diagnostics":
                result = AGENT.diagnostics()
            elif parsed.path == "/chrome-windows":
                result = AGENT.list_chrome_windows()
            elif parsed.path.startswith("/lookup/"):
                request_id = unquote(
                    parsed.path.removeprefix("/lookup/")
                )
                query = parse_qs(parsed.query)
                result = AGENT.lookup_status(
                    request_id,
                    order_id=(query.get("order_id") or [None])[0],
                    period_start=(
                        query.get("period_start") or [None]
                    )[0],
                    period_end=(query.get("period_end") or [None])[0],
                )
            else:
                self._send(failure("NOT_FOUND", "알 수 없는 경로입니다."), 404)
                return
            self._send(result)
        except Exception as error:
            LOGGER.exception("DPS Agent GET endpoint 처리 실패")
            self._send(
                failure(
                    "AGENT_REQUEST_FAILED",
                    "DPS Agent 요청 처리 중 오류가 발생했습니다.",
                    {"error": error.__class__.__name__},
                ),
                500,
            )

    def do_POST(self) -> None:  # noqa: N802
        payload = self._payload()
        try:
            if self.path == "/open-login":
                result = AGENT.open_login()
            elif self.path == "/open":
                result = AGENT.open_browser()
            elif self.path == "/auto-connect":
                result = AGENT.ensure_connection(
                    select_tab=bool(payload.get("select_tab")),
                    force=bool(payload.get("force")),
                )
            elif self.path == "/confirm-login":
                result = AGENT.confirm_login()
            elif self.path == "/connect-current-window":
                result = AGENT.connect_current_window(
                    int(payload.get("delay_seconds") or 4)
                )
            elif self.path == "/connect-window":
                result = AGENT.connect_window_by_handle(payload.get("hwnd"))
            elif self.path == "/disconnect-current-window":
                result = AGENT.disconnect_current_window()
            elif self.path == "/mark-logged-out":
                result = AGENT.mark_logged_out()
            elif self.path == "/refresh-session":
                result = AGENT.refresh_session()
            elif self.path == "/session-monitor":
                result = AGENT.monitor_session(
                    keepalive_enabled=bool(payload.get("keepalive")),
                    keepalive_interval_seconds=int(
                        payload.get("keepalive_interval_seconds") or 1200
                    ),
                    force_keepalive=bool(payload.get("force_keepalive")),
                    trigger=str(payload.get("trigger") or "API"),
                )
            elif self.path == "/lookup":
                result = AGENT.lookup(
                    str(
                        payload.get("naver_order_id")
                        or payload.get("order_number")
                        or ""
                    ) or None,
                    bool(payload.get("force_refresh")),
                    request_id=payload.get("request_id"),
                    selected_inquiry_id=payload.get(
                        "selected_inquiry_id"
                    ),
                    order_id=payload.get("order_id"),
                    product_order_id=payload.get("product_order_id"),
                    dps_query_value=payload.get("dps_query_value"),
                    dps_query_value_type=payload.get("dps_query_value_type"),
                    order_date=payload.get("order_date"),
                    order_created_at=payload.get("order_created_at"),
                    payment_date=payload.get("payment_date"),
                    payment_completed_at=payload.get(
                        "payment_completed_at"
                    ),
                    place_order_date=payload.get("place_order_date"),
                    shipping_due_date=payload.get("shipping_due_date"),
                    dps_date_source=payload.get("dps_date_source"),
                    dps_reference_date=payload.get(
                        "dps_reference_date"
                    ),
                    dps_period_start=payload.get("dps_period_start"),
                    dps_period_end=payload.get("dps_period_end"),
                )
            else:
                self._send(failure("NOT_FOUND", "알 수 없는 경로입니다."), 404)
                return
            self._send(result)
        except Exception as error:
            LOGGER.exception("DPS Agent endpoint 처리 실패")
            self._send(
                failure(
                    "AGENT_REQUEST_FAILED",
                    "DPS Agent 요청 처리 중 오류가 발생했습니다.",
                    {"error": error.__class__.__name__},
                ),
                500,
            )

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


def main() -> None:
    LOGGER.info("DPS Windows Agent v6 시작: http://%s:%s", HOST, PORT)
    session_scheduler = DpsSessionMonitorScheduler(
        AGENT.monitor_session,
        logger=LOGGER,
    )
    session_scheduler.start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"DPS Windows Agent v6 listening on http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    finally:
        session_scheduler.stop()


if __name__ == "__main__":
    main()
