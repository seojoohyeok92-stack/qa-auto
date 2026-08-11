from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

from config import DpsSessionSettings
from dps.agent_server import DpsWindowsAgent
from dps.chrome_tab_manager import PreviousUiContext
from dps.connection_store import ConnectionStore
from dps.gui_resource_guard import GUIResourceState
from dps.session_scheduler import DpsSessionMonitorScheduler


class FakeTabManager:
    def __init__(self, *, extension_result: bool = True) -> None:
        self.extension_result = extension_result
        self.extension_calls = 0
        self.restore_calls = 0

    def capture_previous_context(self) -> PreviousUiContext:
        return PreviousUiContext(None, "", None)

    def validate_selected_candidate(self, candidate):
        return True, {"dps_domain_valid": True, "dps_path_valid": True}

    def click_login_time_extension(self, window) -> tuple[bool, str]:
        self.extension_calls += 1
        return self.extension_result, (
            "KEEPALIVE_EXTENSION_CLICKED"
            if self.extension_result
            else "KEEPALIVE_EXTENSION_CONTROL_NOT_FOUND"
        )

    def restore_previous_context(self, context, target_hwnd) -> bool:
        self.restore_calls += 1
        return True


class FreeGuard:
    settings = SimpleNamespace(max_wait_seconds=0.01)

    def check(self) -> GUIResourceState:
        return GUIResourceState(True, "FREE", "TEST_FREE", "test")

    def wait_for_available(self) -> GUIResourceState:
        return self.check()


def make_agent(tmp_path: Path, *, extension_result: bool = True):
    manager = FakeTabManager(extension_result=extension_result)
    store = ConnectionStore(
        tmp_path / "connection.json", tmp_path / "state.json"
    )
    agent = DpsWindowsAgent(
        store=store,
        tab_manager=manager,
        ui_automation=SimpleNamespace(),
        gui_guard=FreeGuard(),
        session_settings=DpsSessionSettings(passive_monitor_enabled=False),
        sleep=lambda _: None,
    )
    candidate = SimpleNamespace(
        hwnd=101,
        window=SimpleNamespace(),
        tab=SimpleNamespace(),
        tab_title="Samsung DPS 2.0",
        window_title="Samsung DPS 2.0 - Google Chrome",
        current_url="https://dps2u.co.kr/dpsweb/main.do",
    )
    agent._select_current_dps = lambda: (candidate, None)  # type: ignore[method-assign]
    return agent, manager


def logged_in_state() -> dict:
    return {
        "login_state": "LOGGED_IN",
        "login_reason": "logout_found",
        "current_page": "HOME",
    }


def test_ready_keepalive_succeeds_and_stays_ready(tmp_path: Path) -> None:
    agent, manager = make_agent(tmp_path)
    agent._detect_candidate_state = lambda candidate: logged_in_state()  # type: ignore[method-assign]

    result = agent.monitor_session(
        keepalive_enabled=True,
        keepalive_interval_seconds=1200,
        force_keepalive=True,
    )

    assert result["success"] is True
    assert result["session_status"] == "READY"
    assert result["keepalive_performed"] is True
    assert manager.extension_calls == 1
    assert agent.last_keepalive_at


def test_login_page_is_login_required_and_recovers_next_check(
    tmp_path: Path,
) -> None:
    agent, _ = make_agent(tmp_path)
    states = iter(
        [
            {
                "login_state": "LOGIN_REQUIRED",
                "login_reason": "login form found",
                "current_page": "LOGIN",
            },
            logged_in_state(),
        ]
    )
    agent._detect_candidate_state = lambda candidate: next(states)  # type: ignore[method-assign]

    first = agent.monitor_session()
    second = agent.monitor_session()

    assert first["session_status"] == "LOGIN_REQUIRED"
    assert second["session_status"] == "READY"
    assert agent.last_ready_at


def test_chrome_not_found_is_distinct(tmp_path: Path) -> None:
    agent, _ = make_agent(tmp_path)
    agent._select_current_dps = lambda: (  # type: ignore[method-assign]
        None,
        {
            "success": False,
            "code": "CHROME_NOT_FOUND",
            "message": "Chrome not found",
        },
    )
    result = agent.monitor_session()
    assert result["session_status"] == "CHROME_NOT_FOUND"


def test_dps_page_not_found_is_distinct(tmp_path: Path) -> None:
    agent, _ = make_agent(tmp_path)
    agent._select_current_dps = lambda: (  # type: ignore[method-assign]
        None,
        {
            "success": False,
            "code": "DPS_TAB_NOT_FOUND",
            "message": "DPS page not found",
        },
    )
    result = agent.monitor_session()
    assert result["session_status"] == "DPS_PAGE_NOT_FOUND"


def test_lookup_lock_makes_keepalive_skip(tmp_path: Path) -> None:
    agent, manager = make_agent(tmp_path)
    agent.lookup_gate.acquire()
    agent.lookup_gate_owner = "LOOKUP"
    try:
        result = agent.monitor_session(
            keepalive_enabled=True, force_keepalive=True
        )
    finally:
        agent.lookup_gate_owner = None
        agent.lookup_gate.release()
    assert result["code"] == "KEEPALIVE_DEFERRED"
    assert result["skip_reason"] == "LOOKUP_IN_PROGRESS"
    assert manager.extension_calls == 0


def test_actual_lookup_waits_for_keepalive_and_gets_priority(
    tmp_path: Path,
) -> None:
    agent, _ = make_agent(tmp_path)
    agent.lookup_gate.acquire()
    agent.lookup_gate_owner = "KEEPALIVE"

    def finish_keepalive() -> None:
        deadline = time.monotonic() + 1
        while not agent.actual_lookup_waiting.is_set() and time.monotonic() < deadline:
            time.sleep(0.001)
        agent.lookup_gate_owner = None
        agent.lookup_gate.release()

    worker = threading.Thread(target=finish_keepalive)
    worker.start()
    acquired = agent._acquire_actual_lookup_gate(timeout=1)
    worker.join(timeout=1)
    try:
        assert acquired is True
        assert agent.lookup_gate_owner == "LOOKUP"
    finally:
        if acquired:
            agent._release_actual_lookup_gate()


def test_one_keepalive_failure_does_not_immediately_mark_connection_failed(
    tmp_path: Path,
) -> None:
    agent, _ = make_agent(tmp_path, extension_result=False)
    agent._detect_candidate_state = lambda candidate: logged_in_state()  # type: ignore[method-assign]

    first = agent.monitor_session(force_keepalive=True)
    second = agent.monitor_session(force_keepalive=True)

    assert first["session_status"] == "READY"
    assert second["session_status"] == "CONNECTION_FAILED"


class FakeTimer:
    created: list["FakeTimer"] = []

    def __init__(self, seconds, callback) -> None:
        self.seconds = seconds
        self.callback = callback
        self.daemon = False
        self.cancelled = False
        self.started = False
        self.created.append(self)

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True


def test_scheduler_start_is_idempotent_and_does_not_duplicate_worker() -> None:
    FakeTimer.created = []
    calls: list[dict] = []
    scheduler = DpsSessionMonitorScheduler(
        lambda **kwargs: calls.append(kwargs) or {"success": True},
        settings=DpsSessionSettings(
            monitor_enabled=True,
            keepalive_enabled=True,
            monitor_interval_seconds=60,
            keepalive_interval_minutes=20,
        ),
        timer_factory=FakeTimer,
    )

    assert scheduler.start() is True
    assert scheduler.start() is True
    assert len(FakeTimer.created) == 1
    FakeTimer.created[0].callback()
    assert len(calls) == 1
    assert len(FakeTimer.created) == 2
    scheduler.stop()
