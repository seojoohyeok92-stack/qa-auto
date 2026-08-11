from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from config import DpsSessionSettings
from dps import agent_server
from dps.agent_server import DpsWindowsAgent
from dps.chrome_tab_manager import ChromeTabManager
from dps.connection_store import ConnectionStore
from dps.gui_resource_guard import GUIResourceState


NOW = 2_000_000_000.0


class Guard:
    settings = SimpleNamespace(max_wait_seconds=0.01)

    def __init__(self, *states: str) -> None:
        self.states = list(states or ("FREE",))
        self.calls = 0

    def check(self) -> GUIResourceState:
        self.calls += 1
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return GUIResourceState(
            state == "FREE", state, f"TEST_{state}", "test"
        )

    def wait_for_available(self) -> GUIResourceState:
        return self.check()


class Manager:
    def __init__(self, *, extension_result: bool = True) -> None:
        self.capture_previous_context = Mock(
            return_value=SimpleNamespace(foreground_hwnd=None)
        )
        self.restore_previous_context = Mock(return_value=True)
        self.is_window = Mock(return_value=True)
        self.validate_selected_candidate = Mock(
            return_value=(True, {"dps_domain_valid": True})
        )
        self.click_login_time_extension = Mock(
            return_value=(
                extension_result,
                "KEEPALIVE_EXTENSION_CLICKED"
                if extension_result
                else "KEEPALIVE_EXTENSION_CONTROL_NOT_FOUND",
            )
        )


def make_agent(
    tmp_path: Path,
    *,
    guard: Guard | None = None,
    extension_result: bool = True,
) -> tuple[DpsWindowsAgent, Manager, Guard]:
    manager = Manager(extension_result=extension_result)
    selected_guard = guard or Guard("FREE")
    agent = DpsWindowsAgent(
        store=ConnectionStore(
            tmp_path / "connection.json", tmp_path / "state.json"
        ),
        tab_manager=manager,
        ui_automation=SimpleNamespace(),
        gui_guard=selected_guard,
        session_settings=DpsSessionSettings(
            monitor_enabled=True,
            keepalive_enabled=True,
            keepalive_interval_minutes=40,
            keepalive_urgent_minutes=55,
            passive_monitor_enabled=True,
        ),
        sleep=lambda _: None,
    )
    candidate = SimpleNamespace(
        hwnd=101,
        window=SimpleNamespace(),
        tab=SimpleNamespace(),
        tab_title="Samsung DPS 2.0",
        window_title="Samsung DPS 2.0 - Google Chrome",
    )
    agent._select_current_dps = Mock(return_value=(candidate, None))  # type: ignore[method-assign]
    agent._detect_candidate_state = Mock(  # type: ignore[method-assign]
        return_value={
            "login_state": "LOGGED_IN",
            "login_reason": "logout_found",
            "current_page": "HOME",
        }
    )
    return agent, manager, selected_guard


def set_activity(agent: DpsWindowsAgent, seconds_ago: float) -> None:
    agent.last_dps_activity_at = agent_server._iso_from_epoch(NOW - seconds_ago)


def test_39_minutes_is_not_due_and_has_no_gui_calls(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(agent_server.time, "time", lambda: NOW)
    agent, manager, guard = make_agent(tmp_path)
    set_activity(agent, 39 * 60)

    result = agent.monitor_session(
        keepalive_enabled=True, keepalive_interval_seconds=40 * 60
    )

    assert result["code"] == "PASSIVE_SESSION_MONITORED"
    assert agent.keepalive_due is False
    assert guard.calls == 0
    manager.capture_previous_context.assert_not_called()
    manager.click_login_time_extension.assert_not_called()


def test_40_minutes_is_due(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(agent_server.time, "time", lambda: NOW)
    agent, _, _ = make_agent(tmp_path)
    set_activity(agent, 40 * 60)
    assert agent._refresh_keepalive_schedule(interval_seconds=40 * 60) is True


def test_due_and_free_extends_session_and_resets_due(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(agent_server.time, "time", lambda: NOW)
    agent, manager, _ = make_agent(tmp_path)
    set_activity(agent, 40 * 60)

    result = agent.monitor_session(
        keepalive_enabled=True, keepalive_interval_seconds=40 * 60
    )

    assert result["code"] == "SESSION_EXTENDED"
    assert manager.click_login_time_extension.call_count == 1
    assert agent.last_keepalive_at
    assert agent.last_dps_activity_at == agent_server._iso_from_epoch(NOW)
    assert agent.next_keepalive_due_at == agent_server._iso_from_epoch(
        NOW + 40 * 60
    )
    assert agent.keepalive_due is False


def test_busy_deferred_without_gui_or_failure_increment(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(agent_server.time, "time", lambda: NOW)
    agent, manager, _ = make_agent(tmp_path, guard=Guard("BUSY"))
    set_activity(agent, 40 * 60)

    result = agent.monitor_session(
        keepalive_enabled=True, keepalive_interval_seconds=40 * 60
    )

    assert result["code"] == "KEEPALIVE_DEFERRED"
    assert result["gui_resource"]["state"] == "BUSY"
    assert agent.consecutive_keepalive_failures == 0
    manager.capture_previous_context.assert_not_called()
    manager.click_login_time_extension.assert_not_called()


def test_deferred_keepalive_runs_on_later_free_tick(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(agent_server.time, "time", lambda: NOW)
    agent, manager, _ = make_agent(tmp_path, guard=Guard("BUSY", "FREE"))
    set_activity(agent, 40 * 60)

    first = agent.monitor_session(
        keepalive_enabled=True, keepalive_interval_seconds=40 * 60
    )
    second = agent.monitor_session(
        keepalive_enabled=True, keepalive_interval_seconds=40 * 60
    )

    assert first["code"] == "KEEPALIVE_DEFERRED"
    assert second["code"] == "SESSION_EXTENDED"
    assert manager.click_login_time_extension.call_count == 1


def test_passive_monitor_and_status_do_not_reset_activity_or_touch_gui(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(agent_server.time, "time", lambda: NOW)
    agent, manager, guard = make_agent(tmp_path)
    set_activity(agent, 10 * 60)
    before = agent.last_dps_activity_at

    agent.monitor_session(keepalive_enabled=False)
    status = agent.status()

    assert agent.last_dps_activity_at == before
    assert status["last_dps_activity_at"] == before
    assert status["keepalive_due"] is False
    assert guard.calls == 0
    manager.capture_previous_context.assert_not_called()
    manager.click_login_time_extension.assert_not_called()


def test_failed_keepalive_does_not_reset_activity(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(agent_server.time, "time", lambda: NOW)
    agent, _, _ = make_agent(tmp_path, extension_result=False)
    set_activity(agent, 40 * 60)
    before = agent.last_dps_activity_at

    result = agent.monitor_session(
        keepalive_enabled=True, keepalive_interval_seconds=40 * 60
    )

    assert result["code"] == "KEEPALIVE_UNAVAILABLE"
    assert agent.last_dps_activity_at == before
    assert agent.keepalive_due is True


def test_logout_does_not_click_or_attempt_login(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(agent_server.time, "time", lambda: NOW)
    agent, manager, _ = make_agent(tmp_path)
    set_activity(agent, 40 * 60)
    agent._detect_candidate_state = Mock(  # type: ignore[method-assign]
        return_value={
            "login_state": "LOGIN_REQUIRED",
            "login_reason": "login form found",
            "current_page": "LOGIN",
        }
    )

    result = agent.monitor_session(
        keepalive_enabled=True, keepalive_interval_seconds=40 * 60
    )

    assert result["session_status"] == "LOGIN_REQUIRED"
    manager.click_login_time_extension.assert_not_called()


def test_lookup_lock_defers_keepalive_without_gui(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(agent_server.time, "time", lambda: NOW)
    agent, manager, guard = make_agent(tmp_path)
    set_activity(agent, 40 * 60)
    agent.lookup_gate.acquire()
    agent.lookup_gate_owner = "LOOKUP"
    try:
        result = agent.monitor_session(
            keepalive_enabled=True, keepalive_interval_seconds=40 * 60
        )
    finally:
        agent.lookup_gate_owner = None
        agent.lookup_gate.release()

    assert result["code"] == "KEEPALIVE_DEFERRED"
    assert guard.calls == 0
    manager.capture_previous_context.assert_not_called()


def test_disabled_keepalive_never_uses_gui_when_overdue(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(agent_server.time, "time", lambda: NOW)
    agent, manager, guard = make_agent(tmp_path)
    set_activity(agent, 60 * 60)

    result = agent.monitor_session(
        keepalive_enabled=False, keepalive_interval_seconds=40 * 60
    )

    assert result["code"] == "PASSIVE_SESSION_MONITORED"
    assert guard.calls == 0
    manager.capture_previous_context.assert_not_called()
    manager.click_login_time_extension.assert_not_called()


def test_successful_lookup_activity_resets_timer(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(agent_server.time, "time", lambda: NOW)
    agent, _, _ = make_agent(tmp_path)
    set_activity(agent, 50 * 60)

    agent._record_dps_activity("LOOKUP_SUCCESS")

    assert agent.last_dps_activity_at == agent_server._iso_from_epoch(NOW)
    assert agent.next_keepalive_due_at == agent_server._iso_from_epoch(
        NOW + 40 * 60
    )
    assert agent.keepalive_due is False


def test_startup_with_persisted_activity_remains_passive(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(agent_server.time, "time", lambda: NOW)
    store = ConnectionStore(
        tmp_path / "connection.json", tmp_path / "state.json"
    )
    store.save_agent_state(
        {"last_dps_activity_at": agent_server._iso_from_epoch(NOW - 50 * 60)}
    )
    manager = Manager()
    DpsWindowsAgent(
        store=store,
        tab_manager=manager,
        ui_automation=SimpleNamespace(),
        gui_guard=Guard("FREE"),
        session_settings=DpsSessionSettings(
            keepalive_enabled=True, keepalive_interval_minutes=40
        ),
    )
    manager.capture_previous_context.assert_not_called()
    manager.click_login_time_extension.assert_not_called()


def test_login_time_extension_requires_exact_control_name() -> None:
    wrong = Mock()
    wrong.element_info.control_type = "Button"
    wrong.element_info.name = ""
    wrong.window_text.return_value = "로그인시간연장 안내"
    window = Mock()
    window.descendants.return_value = [wrong]
    manager = ChromeTabManager(desktop_factory=lambda **_: None)

    clicked, code = manager.click_login_time_extension(window)

    assert clicked is False
    assert code == "KEEPALIVE_EXTENSION_CONTROL_NOT_FOUND"
    wrong.invoke.assert_not_called()


def test_login_time_extension_invokes_exact_control() -> None:
    control = Mock()
    control.element_info.control_type = "Button"
    control.element_info.name = "로그인시간연장"
    control.is_visible.return_value = True
    control.is_enabled.return_value = True
    window = Mock()
    window.descendants.return_value = [control]
    manager = ChromeTabManager(desktop_factory=lambda **_: None)

    clicked, code = manager.click_login_time_extension(window)

    assert clicked is True
    assert code == "KEEPALIVE_EXTENSION_CLICKED"
    control.invoke.assert_called_once_with()
