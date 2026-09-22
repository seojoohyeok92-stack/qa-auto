from __future__ import annotations

import inspect
import threading
from types import SimpleNamespace

import app
from dps import cdp_session
from dps import chrome_tab_manager as tab_module
from dps.chrome_tab_manager import ChromeTabManager
from dps.connection_store import ConnectionStore
from dps.keepalive_runtime import DpsKeepaliveRuntime
from services import dps_agent_client
from ui import production_dashboard


class _Scheduler:
    started = True


class _Agent:
    def __init__(self) -> None:
        self.actual_lookup_waiting = threading.Event()
        self.session_settings = SimpleNamespace(keepalive_interval_minutes=40)
        self.monitor_calls = 0
        self.defer_reasons: list[str] = []
        self.activity: list[tuple[str, int]] = []

    def monitor_session(self, **kwargs):
        self.monitor_calls += 1
        return {"success": True, "kwargs": kwargs}

    def _defer_keepalive(self, reason: str):
        self.defer_reasons.append(reason)
        return {"success": True, "deferred": True, "skip_reason": reason}

    def _record_dps_activity(self, activity_type: str, *, interval_seconds: int):
        self.activity.append((activity_type, interval_seconds))

    def status(self):
        return {
            "agent_running": True,
            "last_keepalive_at": "2026-09-22T10:00:00+09:00",
            "next_keepalive_due_at": "2026-09-22T10:40:00+09:00",
            "last_keepalive_result": "SUCCESS",
        }


def test_cdp_lookup_has_priority_and_success_records_40_minute_activity():
    agent = _Agent()
    runtime = DpsKeepaliveRuntime(agent, _Scheduler())

    def lookup():
        assert agent.actual_lookup_waiting.is_set()
        assert runtime.monitor(keepalive_enabled=True)["deferred"] is True
        return {"success": True, "backend": "CDP"}

    result = runtime.run_lookup(lookup)

    assert result["backend"] == "CDP"
    assert agent.monitor_calls == 0
    assert agent.defer_reasons == ["CDP_LOOKUP_WAITING"]
    assert agent.activity == [("LOOKUP_SUCCESS", 40 * 60)]
    assert not agent.actual_lookup_waiting.is_set()


def test_failed_cdp_lookup_does_not_reset_activity():
    agent = _Agent()
    runtime = DpsKeepaliveRuntime(agent, _Scheduler())

    assert runtime.run_lookup(lambda: {"success": False}) == {"success": False}
    assert agent.activity == []


def test_keepalive_runtime_is_not_reported_as_legacy_agent():
    status = DpsKeepaliveRuntime(_Agent(), _Scheduler()).status()

    assert status["keepalive_runtime_running"] is True
    assert status["agent_running"] is False
    assert status["legacy_lookup_enabled"] is False


def test_last_keepalive_result_is_persisted_for_dashboard(tmp_path):
    store = ConnectionStore(
        path=tmp_path / "connection.json",
        state_path=tmp_path / "state.json",
    )

    store.save_agent_state({"last_keepalive_result": "LOGIN_REQUIRED"})

    assert store.load_agent_state()["last_keepalive_result"] == "LOGIN_REQUIRED"


def test_keepalive_window_discovery_is_limited_to_the_9222_browser(monkeypatch):
    class _Window:
        def __init__(self, handle: int) -> None:
            self.handle = handle
            self.element_info = SimpleNamespace(class_name="Chrome_WidgetWin_1")

    windows = [_Window(11), _Window(22)]
    manager = ChromeTabManager(
        desktop_factory=lambda **kwargs: SimpleNamespace(
            windows=lambda: windows
        ),
        allowed_process_ids_provider=lambda: {9222},
    )
    monkeypatch.setattr(tab_module, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        manager,
        "process_id_for_window",
        lambda hwnd: {11: 9222, 22: 8765}[hwnd],
    )
    monkeypatch.setattr(manager, "process_name_for_window", lambda hwnd: "chrome.exe")
    monkeypatch.setattr(manager, "window_title", lambda window: "Chrome")

    assert [window.handle for window in manager.chrome_windows()] == [11]


def test_cdp_process_filter_uses_only_port_9222(monkeypatch):
    monkeypatch.setattr(
        cdp_session,
        "chrome_browser_processes",
        lambda: [
            {"pid": 11, "argv": ["chrome.exe", "--remote-debugging-port=9222"]},
            {"pid": 22, "argv": ["chrome.exe", "--remote-debugging-port=9333"]},
        ],
    )

    assert cdp_session.cdp_chrome_process_ids(port=9222) == {11}


def test_production_lookup_stays_cdp_and_has_no_legacy_fallback(monkeypatch):
    calls: list[str] = []

    monkeypatch.setattr(cdp_session, "CdpDpsReader", lambda browser: object())
    monkeypatch.setattr(cdp_session, "_browser", lambda: object())
    monkeypatch.setattr(
        cdp_session,
        "lookup_dps_order_cdp",
        lambda *args, **kwargs: calls.append("CDP") or {"success": False},
    )
    monkeypatch.setattr(
        "dps.keepalive_runtime.run_with_dps_lookup_priority",
        lambda operation: calls.append("COORDINATED") or operation(),
    )
    monkeypatch.setattr(
        dps_agent_client,
        "lookup_dps_order",
        lambda *args, **kwargs: calls.append("LEGACY"),
    )

    assert cdp_session.lookup_dps_order_production("order-1")["success"] is False
    assert calls == ["COORDINATED", "CDP"]
    assert dps_agent_client.start_dps_agent()["code"] == "LEGACY_DPS_AGENT_DORMANT"


def test_app_and_dashboard_use_the_real_keepalive_runtime_status():
    app_source = inspect.getsource(app)
    dashboard_source = inspect.getsource(production_dashboard)

    assert "ensure_cdp_chrome_on_start()" in app_source
    assert "ensure_dps_keepalive_runtime()" in app_source
    assert "ensure_dps_session_monitor" not in app_source
    assert "keepalive_runtime_running" in dashboard_source
    assert "last_keepalive_result" in dashboard_source
    assert "next_keepalive_due_at" in dashboard_source
