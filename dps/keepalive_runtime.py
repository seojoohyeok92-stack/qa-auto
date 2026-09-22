"""Low-priority DPS keepalive runtime shared with production CDP lookups.

The legacy pywinauto lookup client stays dormant.  This module reuses only the
existing, read-only session monitor and gives the CDP lookup path priority over
its single login-time-extension action.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable

from config import DpsSessionSettings


LOGGER = logging.getLogger(__name__)
LookupOperation = Callable[[], dict[str, Any]]


class DpsKeepaliveRuntime:
    def __init__(self, agent: Any, scheduler: Any) -> None:
        self.agent = agent
        self.scheduler = scheduler
        self._operation_lock = threading.Lock()

    def monitor(self, **kwargs: Any) -> dict[str, Any]:
        if self.agent.actual_lookup_waiting.is_set():
            return self.agent._defer_keepalive("CDP_LOOKUP_WAITING")
        if not self._operation_lock.acquire(blocking=False):
            return self.agent._defer_keepalive("CDP_LOOKUP_IN_PROGRESS")
        try:
            return self.agent.monitor_session(**kwargs)
        finally:
            self._operation_lock.release()

    def run_lookup(self, operation: LookupOperation) -> dict[str, Any]:
        # Announce priority before waiting.  An in-flight keepalive checks this
        # event again immediately before its one read-only extension action.
        self.agent.actual_lookup_waiting.set()
        self._operation_lock.acquire()
        try:
            result = operation()
            if result.get("success") or result.get("ok"):
                self.agent._record_dps_activity(
                    "LOOKUP_SUCCESS",
                    interval_seconds=(
                        self.agent.session_settings.keepalive_interval_minutes
                        * 60
                    ),
                )
            return result
        finally:
            self._operation_lock.release()
            self.agent.actual_lookup_waiting.clear()

    def status(self) -> dict[str, Any]:
        status = dict(self.agent.status())
        return {
            **status,
            # This worker exposes no Agent HTTP/lookup capability.  Keep the
            # legacy production-facing Agent status dormant.
            "agent_running": False,
            "keepalive_runtime_enabled": True,
            "keepalive_runtime_running": bool(self.scheduler.started),
            "legacy_lookup_enabled": False,
        }


_RUNTIME_GUARD = threading.Lock()
_RUNTIME: DpsKeepaliveRuntime | None = None
_START_ERROR: str | None = None


def _build_runtime(settings: DpsSessionSettings) -> DpsKeepaliveRuntime:
    from dps.agent_server import DpsWindowsAgent
    from dps.cdp_session import cdp_chrome_process_ids
    from dps.chrome_tab_manager import ChromeTabManager
    from dps.connection_store import ConnectionStore
    from dps.session_scheduler import DpsSessionMonitorScheduler

    store = ConnectionStore()
    connection = store.load()
    manager = ChromeTabManager(
        keywords=connection["tab_title_keywords"],
        allowed_hosts=tuple(
            value.strip()
            for value in os.getenv("DPS_ALLOWED_HOSTS", "dps2u.co.kr").split(",")
            if value.strip()
        ),
        allowed_process_ids_provider=cdp_chrome_process_ids,
    )
    agent = DpsWindowsAgent(
        store=store,
        tab_manager=manager,
        session_settings=settings,
    )
    runtime = DpsKeepaliveRuntime(agent, scheduler=None)
    runtime.scheduler = DpsSessionMonitorScheduler(
        runtime.monitor,
        settings=settings,
    )
    return runtime


def ensure_dps_keepalive_runtime() -> dict[str, Any]:
    """Start only the keepalive scheduler; never the legacy HTTP Agent."""

    global _RUNTIME, _START_ERROR
    settings = DpsSessionSettings.from_environment()
    enabled = bool(
        os.name == "nt"
        and settings.monitor_enabled
        and settings.keepalive_enabled
    )
    if not enabled:
        return {
            "keepalive_runtime_enabled": False,
            "keepalive_runtime_running": False,
            "legacy_lookup_enabled": False,
        }
    with _RUNTIME_GUARD:
        try:
            if _RUNTIME is None:
                _RUNTIME = _build_runtime(settings)
            _RUNTIME.scheduler.start()
            _START_ERROR = None
            return _RUNTIME.status()
        except Exception as error:  # keepalive must never block app startup
            _START_ERROR = error.__class__.__name__
            LOGGER.warning(
                "DPS keepalive runtime start failed: error_type=%s",
                _START_ERROR,
            )
            return {
                "keepalive_runtime_enabled": True,
                "keepalive_runtime_running": False,
                "legacy_lookup_enabled": False,
                "last_keepalive_result": f"START_FAILED:{_START_ERROR}",
            }


def run_with_dps_lookup_priority(operation: LookupOperation) -> dict[str, Any]:
    runtime = _RUNTIME
    if runtime is None or not runtime.scheduler.started:
        return operation()
    return runtime.run_lookup(operation)


def dps_keepalive_runtime_status() -> dict[str, Any]:
    settings = DpsSessionSettings.from_environment()
    runtime = _RUNTIME
    if runtime is None:
        return {
            "keepalive_runtime_enabled": bool(
                os.name == "nt"
                and settings.monitor_enabled
                and settings.keepalive_enabled
            ),
            "keepalive_runtime_running": False,
            "legacy_lookup_enabled": False,
            "last_keepalive_at": None,
            "last_dps_activity_at": None,
            "next_keepalive_due_at": None,
            "last_keepalive_result": (
                f"START_FAILED:{_START_ERROR}" if _START_ERROR else "NOT_STARTED"
            ),
            "keepalive_due": False,
        }
    return runtime.status()
