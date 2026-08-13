from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

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
    agent.consecutive_keepalive_failures = 1

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
    assert agent.consecutive_keepalive_failures == 0

    passive = agent.monitor_session(
        keepalive_enabled=True, keepalive_interval_seconds=40 * 60
    )
    assert passive["code"] == "PASSIVE_SESSION_MONITORED"
    assert manager.click_login_time_extension.call_count == 1


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
    agent, manager, guard = make_agent(tmp_path, extension_result=False)
    set_activity(agent, 40 * 60)
    before = agent.last_dps_activity_at

    result = agent.monitor_session(
        keepalive_enabled=True, keepalive_interval_seconds=40 * 60
    )

    assert result["code"] == "KEEPALIVE_UNAVAILABLE"
    assert agent.last_dps_activity_at == before
    assert agent.keepalive_due is True

    retry = agent.monitor_session(
        keepalive_enabled=True, keepalive_interval_seconds=40 * 60
    )
    assert retry["keepalive_retry_deferred"] is True
    assert guard.calls == 1
    assert manager.capture_previous_context.call_count == 1
    assert manager.click_login_time_extension.call_count == 1


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
    assert agent.last_keepalive_attempt_at == agent_server._iso_from_epoch(NOW)
    assert (
        agent.keepalive_deferred_reason
        == "LOGIN_REQUIRED_RETRY_COOLDOWN"
    )
    manager.click_login_time_extension.assert_not_called()


def test_login_required_retry_cooldown_is_passive_until_expiry(
    tmp_path: Path, monkeypatch
) -> None:
    current = [NOW]
    monkeypatch.setattr(agent_server.time, "time", lambda: current[0])
    agent, manager, guard = make_agent(tmp_path)
    set_activity(agent, 40 * 60)
    agent._detect_candidate_state = Mock(  # type: ignore[method-assign]
        return_value={
            "login_state": "LOGIN_REQUIRED",
            "login_reason": "login form found",
            "current_page": "LOGIN",
        }
    )

    first = agent.monitor_session(
        keepalive_enabled=True, keepalive_interval_seconds=40 * 60
    )
    current[0] += 60
    second = agent.monitor_session(
        keepalive_enabled=True, keepalive_interval_seconds=40 * 60
    )
    current[0] += 4 * 60 - 1
    third = agent.monitor_session(
        keepalive_enabled=True, keepalive_interval_seconds=40 * 60
    )

    assert first["session_status"] == "LOGIN_REQUIRED"
    assert second["code"] == "PASSIVE_SESSION_MONITORED"
    assert second["keepalive_retry_deferred"] is True
    assert second["keepalive_retry_due_at"] == agent_server._iso_from_epoch(
        NOW + 5 * 60
    )
    assert third["keepalive_retry_deferred"] is True
    assert guard.calls == 1
    assert manager.capture_previous_context.call_count == 1
    manager.click_login_time_extension.assert_not_called()

    current[0] += 1
    after_expiry = agent.monitor_session(
        keepalive_enabled=True, keepalive_interval_seconds=40 * 60
    )
    assert after_expiry["session_status"] == "LOGIN_REQUIRED"
    assert guard.calls == 2
    assert manager.capture_previous_context.call_count == 2


def test_force_keepalive_bypasses_retry_cooldown(
    tmp_path: Path, monkeypatch
) -> None:
    current = [NOW]
    monkeypatch.setattr(agent_server.time, "time", lambda: current[0])
    agent, manager, guard = make_agent(tmp_path)
    set_activity(agent, 40 * 60)
    agent._detect_candidate_state = Mock(  # type: ignore[method-assign]
        return_value={
            "login_state": "LOGIN_REQUIRED",
            "login_reason": "login form found",
            "current_page": "LOGIN",
        }
    )
    agent.monitor_session(
        keepalive_enabled=True, keepalive_interval_seconds=40 * 60
    )
    current[0] += 60

    forced = agent.monitor_session(
        keepalive_enabled=True,
        keepalive_interval_seconds=40 * 60,
        force_keepalive=True,
    )

    assert forced["session_status"] == "LOGIN_REQUIRED"
    assert guard.calls == 2
    assert manager.capture_previous_context.call_count == 2


def test_retry_cooldown_preserves_urgent_signal(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(agent_server.time, "time", lambda: NOW)
    agent, manager, guard = make_agent(tmp_path)
    set_activity(agent, 56 * 60)
    agent.last_keepalive_attempt_at = agent_server._iso_from_epoch(NOW - 60)
    agent.keepalive_deferred_reason = "LOGIN_REQUIRED_RETRY_COOLDOWN"

    result = agent.monitor_session(
        keepalive_enabled=True, keepalive_interval_seconds=40 * 60
    )

    assert result["keepalive_retry_deferred"] is True
    assert result["keepalive_urgency"] == "SESSION_KEEPALIVE_URGENT"
    assert agent.last_monitor_event == "SESSION_KEEPALIVE_URGENT"
    assert guard.calls == 0
    manager.capture_previous_context.assert_not_called()


@pytest.mark.parametrize(
    "failure_code",
    ["DPS_TAB_NOT_FOUND", "AGENT_CONNECTION_FAILED"],
)
def test_connection_failure_uses_retry_cooldown(
    tmp_path: Path, monkeypatch, failure_code: str
) -> None:
    current = [NOW]
    monkeypatch.setattr(agent_server.time, "time", lambda: current[0])
    agent, manager, guard = make_agent(tmp_path)
    set_activity(agent, 40 * 60)
    agent._select_current_dps = Mock(  # type: ignore[method-assign]
        return_value=(
            None,
            {
                "success": False,
                "code": failure_code,
                "message": "DPS connection failed",
            },
        )
    )

    first = agent.monitor_session(
        keepalive_enabled=True, keepalive_interval_seconds=40 * 60
    )
    current[0] += 60
    second = agent.monitor_session(
        keepalive_enabled=True, keepalive_interval_seconds=40 * 60
    )

    assert first["code"] == failure_code
    assert second["keepalive_retry_deferred"] is True
    assert (
        second["keepalive_deferred_reason"]
        == f"{failure_code}_RETRY_COOLDOWN"
    )
    assert guard.calls == 1
    assert manager.capture_previous_context.call_count == 1


def test_persisted_failed_attempt_defers_retry_after_restart(
    tmp_path: Path, monkeypatch
) -> None:
    current = [NOW + 60]
    monkeypatch.setattr(agent_server.time, "time", lambda: current[0])
    store = ConnectionStore(
        tmp_path / "connection.json", tmp_path / "state.json"
    )
    store.save_agent_state(
        {
            "last_dps_activity_at": agent_server._iso_from_epoch(
                NOW - 40 * 60
            ),
            "last_keepalive_attempt_at": agent_server._iso_from_epoch(NOW),
            "keepalive_deferred_reason": "LOGIN_REQUIRED_RETRY_COOLDOWN",
        }
    )
    manager = Manager()
    guard = Guard("FREE")
    agent = DpsWindowsAgent(
        store=store,
        tab_manager=manager,
        ui_automation=SimpleNamespace(),
        gui_guard=guard,
        session_settings=DpsSessionSettings(
            keepalive_enabled=True,
            keepalive_interval_minutes=40,
            passive_monitor_enabled=True,
        ),
    )

    result = agent.monitor_session(
        keepalive_enabled=True, keepalive_interval_seconds=40 * 60
    )

    assert result["keepalive_retry_deferred"] is True
    assert result["passive"] is True
    assert guard.calls == 0
    manager.capture_previous_context.assert_not_called()


def test_retry_cooldown_does_not_block_actual_lookup_gate(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(agent_server.time, "time", lambda: NOW + 60)
    agent, _, _ = make_agent(tmp_path)
    agent.last_dps_activity_at = agent_server._iso_from_epoch(NOW - 40 * 60)
    agent.last_keepalive_attempt_at = agent_server._iso_from_epoch(NOW)
    agent.keepalive_deferred_reason = "LOGIN_REQUIRED_RETRY_COOLDOWN"

    acquired = agent._acquire_actual_lookup_gate(timeout=0)
    try:
        assert acquired is True
        assert agent.lookup_gate_owner == "LOOKUP"
    finally:
        if acquired:
            agent._release_actual_lookup_gate()


def test_status_exposes_retry_cooldown_without_gui(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(agent_server.time, "time", lambda: NOW + 60)
    agent, manager, guard = make_agent(tmp_path)
    agent.last_dps_activity_at = agent_server._iso_from_epoch(NOW - 40 * 60)
    agent.last_keepalive_attempt_at = agent_server._iso_from_epoch(NOW)

    status = agent.status()

    assert status["keepalive_retry_deferred"] is True
    assert status["keepalive_retry_due_at"] == agent_server._iso_from_epoch(
        NOW + 5 * 60
    )
    assert status["keepalive_retry_cooldown_seconds"] == 5 * 60
    assert guard.calls == 0
    manager.capture_previous_context.assert_not_called()


def test_retry_cooldown_setting_loads_from_environment(monkeypatch) -> None:
    monkeypatch.setenv(
        "DPS_SESSION_KEEPALIVE_RETRY_COOLDOWN_MINUTES", "10"
    )
    assert (
        DpsSessionSettings.from_environment().keepalive_retry_cooldown_minutes
        == 10
    )


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
