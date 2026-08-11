from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from config import DpsSessionSettings
from dps import agent_server
from dps.agent_server import DpsWindowsAgent
from dps.connection_store import ConnectionStore
from dps.gui_resource_guard import GUIResourceState


class PassiveProbeManager:
    def __init__(self) -> None:
        self.capture_previous_context = Mock(
            return_value=SimpleNamespace(foreground_hwnd=None)
        )
        self.foreground_hwnd = Mock(return_value=None)
        self.restore_previous_context = Mock(return_value=True)
        self.chrome_windows = Mock(return_value=[])
        self.find_candidates = Mock(return_value=[])
        self.select_candidate = Mock(return_value=(False, "NOT_SELECTED"))
        self.activate_window = Mock(return_value=False)
        self.click_reload_button = Mock(return_value=False)
        self.is_window = Mock(return_value=True)


class FreeGuard:
    settings = SimpleNamespace(max_wait_seconds=0.01)

    def check(self) -> GUIResourceState:
        return GUIResourceState(True, "FREE", "TEST_FREE", "test")

    def wait_for_available(self) -> GUIResourceState:
        return self.check()


class BusyGuard:
    settings = SimpleNamespace(max_wait_seconds=0.01)

    def check(self) -> GUIResourceState:
        return GUIResourceState(False, "BUSY", "TEST_BUSY", "test")

    def wait_for_available(self) -> GUIResourceState:
        return GUIResourceState(False, "TIMEOUT", "TEST_BUSY", "test")


def make_agent(
    tmp_path: Path,
    *,
    manager: PassiveProbeManager | None = None,
    guard=None,
) -> tuple[DpsWindowsAgent, PassiveProbeManager]:
    probe = manager or PassiveProbeManager()
    agent = DpsWindowsAgent(
        store=ConnectionStore(tmp_path / "connection.json", tmp_path / "state.json"),
        tab_manager=probe,
        ui_automation=SimpleNamespace(),
        gui_guard=guard or FreeGuard(),
        session_settings=DpsSessionSettings(
            monitor_enabled=True,
            keepalive_enabled=False,
            passive_idle_enabled=True,
            passive_monitor_enabled=True,
            on_demand_connect_enabled=True,
        ),
        sleep=lambda _: None,
    )
    return agent, probe


def assert_no_active_gui_calls(manager: PassiveProbeManager) -> None:
    manager.capture_previous_context.assert_not_called()
    manager.foreground_hwnd.assert_not_called()
    manager.restore_previous_context.assert_not_called()
    manager.chrome_windows.assert_not_called()
    manager.find_candidates.assert_not_called()
    manager.select_candidate.assert_not_called()
    manager.activate_window.assert_not_called()
    manager.click_reload_button.assert_not_called()


def test_agent_startup_is_passive(tmp_path: Path) -> None:
    _, manager = make_agent(tmp_path)
    assert_no_active_gui_calls(manager)


def test_module_import_constructs_global_agent_in_fresh_process(
    tmp_path: Path,
) -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "DPS_CONNECTION_FILE": str(tmp_path / "connection.json"),
            "DPS_SESSION_MONITOR_ENABLED": "false",
            "DPS_SESSION_KEEPALIVE_ENABLED": "false",
        }
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from dps import agent_server; "
                "assert agent_server.AGENT.connection is None; "
                "print('AGENT_IMPORT_OK')"
            ),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "AGENT_IMPORT_OK"


def test_agent_startup_downgrades_persisted_ready_until_active_check(
    tmp_path: Path,
) -> None:
    store = ConnectionStore(
        tmp_path / "connection.json", tmp_path / "state.json"
    )
    store.save_agent_state(
        {
            "session_status": "READY",
            "login_confirmed_at": 123.0,
        }
    )
    manager = PassiveProbeManager()
    agent = DpsWindowsAgent(
        store=store,
        tab_manager=manager,
        ui_automation=SimpleNamespace(),
        gui_guard=FreeGuard(),
        sleep=lambda _: None,
    )

    status = agent.status()

    assert status["connection_status"] == "DISCONNECTED"
    assert status["session_status"] == "STALE"
    assert status["logged_in"] is False
    assert status["login_state"] == "LOGIN_UNCERTAIN"
    assert_no_active_gui_calls(manager)


def test_main_binds_server_before_scheduler_and_always_closes() -> None:
    events: list[str] = []

    class FakeServer:
        def __init__(self, address, handler) -> None:
            events.append("server_bound")

        def serve_forever(self) -> None:
            events.append("serve_forever")
            raise RuntimeError("stop test server")

        def server_close(self) -> None:
            events.append("server_closed")

    class FakeScheduler:
        def __init__(self, monitor, *, logger) -> None:
            events.append("scheduler_created")

        def start(self) -> bool:
            events.append("scheduler_started")
            return True

        def stop(self) -> None:
            events.append("scheduler_stopped")

    with (
        patch.object(agent_server, "ThreadingHTTPServer", FakeServer),
        patch.object(agent_server, "DpsSessionMonitorScheduler", FakeScheduler),
        pytest.raises(RuntimeError, match="stop test server"),
    ):
        agent_server.main()

    assert events == [
        "server_bound",
        "scheduler_created",
        "scheduler_started",
        "serve_forever",
        "scheduler_stopped",
        "server_closed",
    ]


def test_main_bind_failure_never_starts_scheduler() -> None:
    scheduler = Mock()
    with (
        patch.object(
            agent_server,
            "ThreadingHTTPServer",
            side_effect=OSError("port unavailable"),
        ),
        patch.object(agent_server, "DpsSessionMonitorScheduler", scheduler),
        pytest.raises(OSError, match="port unavailable"),
    ):
        agent_server.main()

    scheduler.assert_not_called()


def test_passive_monitor_has_zero_foreground_or_input_calls(tmp_path: Path) -> None:
    agent, manager = make_agent(tmp_path)
    result = agent.monitor_session(trigger="SCHEDULER")
    assert result["code"] == "PASSIVE_SESSION_MONITORED"
    assert result["passive"] is True
    assert_no_active_gui_calls(manager)


def test_passive_monitor_without_connection_metadata_does_not_discover_tabs(
    tmp_path: Path,
) -> None:
    agent, manager = make_agent(tmp_path)
    result = agent.monitor_session()
    assert result["connection_metadata_present"] is False
    assert result["session_status"] == "UNKNOWN"
    assert_no_active_gui_calls(manager)


def test_keepalive_off_ignores_force_without_gui_work(tmp_path: Path) -> None:
    agent, manager = make_agent(tmp_path)
    result = agent.monitor_session(
        keepalive_enabled=False,
        force_keepalive=True,
    )
    assert result["keepalive_performed"] is False
    assert_no_active_gui_calls(manager)


def test_status_and_diagnostics_are_passive(tmp_path: Path) -> None:
    agent, manager = make_agent(tmp_path)
    status = agent.status()
    diagnostics = agent.diagnostics()
    assert status["passive_idle_enabled"] is True
    assert diagnostics["passive"] is True
    assert diagnostics["diagnostic_texts"] == []
    assert_no_active_gui_calls(manager)


def test_lookup_is_the_on_demand_connection_trigger(tmp_path: Path) -> None:
    agent, manager = make_agent(tmp_path)
    select_current = Mock(
        return_value=(
            None,
            {"success": False, "code": "DPS_TAB_NOT_FOUND", "message": "test"},
        )
    )
    agent._select_current_dps = select_current  # type: ignore[method-assign]

    assert select_current.call_count == 0
    result = agent.lookup("2026071112345678", force_refresh=True)
    assert result["code"] == "DPS_TAB_NOT_FOUND"
    assert select_current.call_count == 1
    assert manager.capture_previous_context.call_count == 1
    assert agent.last_gui_operation_type == "LOOKUP"


def test_explicit_auto_connect_remains_guarded_and_operational(tmp_path: Path) -> None:
    agent, manager = make_agent(tmp_path)
    connect = Mock(return_value={"success": True, "code": "AUTO_CONNECTED"})
    agent._ensure_connection_unlocked = connect  # type: ignore[method-assign]
    result = agent.ensure_connection(select_tab=True, force=True)
    assert result["code"] == "AUTO_CONNECTED"
    connect.assert_called_once_with(select_tab=True, force=True)
    assert manager.capture_previous_context.call_count == 1
    assert agent.last_gui_operation_type == "AUTO_CONNECT"


def test_busy_guard_blocks_lookup_before_connection_or_ui_capture(tmp_path: Path) -> None:
    agent, manager = make_agent(tmp_path, guard=BusyGuard())
    select_current = Mock()
    agent._select_current_dps = select_current  # type: ignore[method-assign]
    result = agent.lookup("2026071112345678", force_refresh=True)
    assert result["code"] == "GUI_RESOURCE_WAIT_TIMEOUT"
    select_current.assert_not_called()
    assert_no_active_gui_calls(manager)


def test_passive_settings_default_safe_without_new_environment_keys(
    monkeypatch,
) -> None:
    for key in (
        "DPS_PASSIVE_IDLE_ENABLED",
        "DPS_PASSIVE_SESSION_MONITOR_ENABLED",
        "DPS_ON_DEMAND_CONNECT_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)
    loaded = DpsSessionSettings.from_environment()
    assert loaded.passive_idle_enabled is True
    assert loaded.passive_monitor_enabled is True
    assert loaded.on_demand_connect_enabled is True
