from __future__ import annotations

import os
import threading
from pathlib import Path
from types import SimpleNamespace

from config import DpsGuiGuardSettings
from dps.agent_server import DpsWindowsAgent
from dps.connection_store import ConnectionStore
from dps.gui_resource_guard import (
    ForegroundActivity,
    GUIResourceGuard,
    GUIResourceState,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def settings(**overrides) -> DpsGuiGuardSettings:
    values = {
        "enabled": True,
        "recheck_seconds": 1.0,
        "cooldown_seconds": 2.0,
        "max_wait_seconds": 10.0,
        "activity_grace_seconds": 5.0,
        "process_patterns": ("kakaotalk.exe", "kakao_dispatcher.py"),
        "window_patterns": ("KakaoTalk", "카카오톡"),
        "activity_paths": (),
    }
    values.update(overrides)
    return DpsGuiGuardSettings(**values)


def test_gui_free_is_immediately_available(tmp_path: Path) -> None:
    clock = FakeClock()
    guard = GUIResourceGuard(
        settings(),
        project_root=tmp_path,
        foreground_reader=lambda: ForegroundActivity(
            hwnd=1, window_title="Editor", process_name="code.exe"
        ),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    result = guard.wait_for_available()
    assert result.state == "FREE"
    assert result.available is True
    assert clock.sleeps == []


def test_gui_busy_waits(tmp_path: Path) -> None:
    clock = FakeClock()
    guard = GUIResourceGuard(
        settings(max_wait_seconds=2.0),
        project_root=tmp_path,
        foreground_reader=lambda: ForegroundActivity(
            hwnd=2, window_title="KakaoTalk", process_name="KakaoTalk.exe"
        ),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    result = guard.wait_for_available()
    assert result.state == "TIMEOUT"
    assert clock.sleeps


def test_busy_to_free_resumes_after_cooldown(tmp_path: Path) -> None:
    clock = FakeClock()

    def foreground() -> ForegroundActivity:
        if clock.now < 2:
            return ForegroundActivity(2, "KakaoTalk", "KakaoTalk.exe")
        return ForegroundActivity(1, "Editor", "code.exe")

    guard = GUIResourceGuard(
        settings(cooldown_seconds=3.0),
        project_root=tmp_path,
        foreground_reader=foreground,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    result = guard.wait_for_available()
    assert result.state == "FREE"
    assert clock.now == 4.0


def test_continuous_busy_times_out_with_retryable_reason(tmp_path: Path) -> None:
    clock = FakeClock()
    guard = GUIResourceGuard(
        settings(max_wait_seconds=3.0),
        project_root=tmp_path,
        foreground_reader=lambda: ForegroundActivity(2, "카카오톡", "KakaoTalk.exe"),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    result = guard.wait_for_available()
    assert result.state == "TIMEOUT"
    assert "GUI_RESOURCE_WAIT_TIMEOUT" in result.reason


def test_resident_dispatcher_name_alone_is_not_busy(tmp_path: Path) -> None:
    guard = GUIResourceGuard(
        settings(),
        project_root=tmp_path,
        foreground_reader=lambda: ForegroundActivity(
            1, "Visual Studio Code", "code.exe"
        ),
    )
    assert guard.check().state == "FREE"


def test_foreground_kakao_activity_is_busy(tmp_path: Path) -> None:
    guard = GUIResourceGuard(
        settings(),
        project_root=tmp_path,
        foreground_reader=lambda: ForegroundActivity(
            2, "대화방 - KakaoTalk", "KakaoTalk.exe"
        ),
    )
    result = guard.check()
    assert result.state == "BUSY"
    assert result.detected_source.startswith("foreground_window")


def test_recent_activity_marker_is_busy(tmp_path: Path) -> None:
    marker = tmp_path / "kakao.active"
    marker.write_text("busy", encoding="utf-8")
    guard = GUIResourceGuard(
        settings(activity_paths=("kakao.active",)),
        project_root=tmp_path,
        foreground_reader=lambda: ForegroundActivity(1, "Editor", "code.exe"),
        wall_time=lambda: marker.stat().st_mtime,
    )
    assert guard.check().reason == "RECENT_GUI_ACTIVITY_MARKER"


def test_guard_disabled_preserves_existing_behavior(tmp_path: Path) -> None:
    guard = GUIResourceGuard(
        settings(enabled=False),
        project_root=tmp_path,
        foreground_reader=lambda: ForegroundActivity(2, "KakaoTalk", "KakaoTalk.exe"),
    )
    result = guard.check()
    assert result.available is True
    assert result.reason == "GUARD_DISABLED"


class BusyGuard:
    settings = settings(max_wait_seconds=1.0)

    def check(self) -> GUIResourceState:
        return GUIResourceState(False, "BUSY", "KAKAO_ACTIVE", "fake")

    def wait_for_available(self) -> GUIResourceState:
        return self.check()


class FreeGuard:
    settings = settings(max_wait_seconds=1.0)

    def check(self) -> GUIResourceState:
        return GUIResourceState(True, "FREE", "TEST_FREE", "fake")

    def wait_for_available(self) -> GUIResourceState:
        return self.check()


class FakeManager:
    def __init__(self, *, restore_result: bool = True) -> None:
        self.restore_result = restore_result
        self.restore_calls = 0

    def capture_previous_context(self):
        return SimpleNamespace(foreground_hwnd=99)

    def foreground_hwnd(self) -> int:
        return 101

    def restore_previous_context(self, previous, target) -> bool:
        self.restore_calls += 1
        return self.restore_result


def make_agent(tmp_path: Path, guard, manager=None) -> DpsWindowsAgent:
    return DpsWindowsAgent(
        store=ConnectionStore(tmp_path / "connection.json", tmp_path / "state.json"),
        tab_manager=manager or FakeManager(),
        ui_automation=SimpleNamespace(),
        gui_guard=guard,
        sleep=lambda _: None,
    )


def test_keepalive_busy_is_deferred_without_ui(tmp_path: Path) -> None:
    manager = FakeManager()
    agent = make_agent(tmp_path, BusyGuard(), manager)
    result = agent.monitor_session(keepalive_enabled=True, force_keepalive=True)
    assert result["code"] == "KEEPALIVE_DEFERRED"
    assert result["keepalive_performed"] is False
    assert manager.restore_calls == 0


def test_lookup_gate_blocks_keepalive_from_concurrent_gui(tmp_path: Path) -> None:
    agent = make_agent(tmp_path, FreeGuard())
    entered = threading.Event()
    release = threading.Event()

    def lookup_operation() -> dict:
        entered.set()
        release.wait(timeout=2)
        return {"success": True}

    worker = threading.Thread(
        target=lambda: agent._run_guarded_gui_operation("LOOKUP", lookup_operation)
    )
    worker.start()
    assert entered.wait(timeout=1)
    result = agent.monitor_session(keepalive_enabled=True, force_keepalive=True)
    release.set()
    worker.join(timeout=2)
    assert result["code"] == "SESSION_MONITOR_SKIPPED"
    assert result["skip_reason"] == "LOOKUP_IN_PROGRESS"


def test_foreground_restore_failure_keeps_core_result(tmp_path: Path) -> None:
    manager = FakeManager(restore_result=False)
    agent = make_agent(tmp_path, FreeGuard(), manager)
    result = agent._run_guarded_gui_operation(
        "TEST", lambda: {"success": True, "value": "preserved"}
    )
    assert result == {"success": True, "value": "preserved"}
    assert agent.last_window_restore_warning == "PREVIOUS_WINDOW_RESTORE_FAILED"


def test_missing_guard_environment_uses_safe_defaults(monkeypatch) -> None:
    for key in (
        "DPS_GUI_GUARD_ENABLED",
        "DPS_GUI_GUARD_RECHECK_SECONDS",
        "DPS_GUI_GUARD_COOLDOWN_SECONDS",
        "DPS_GUI_RESOURCE_MAX_WAIT_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)
    loaded = DpsGuiGuardSettings.from_environment()
    assert loaded.enabled is True
    assert loaded.recheck_seconds == 5.0
    assert loaded.cooldown_seconds == 5.0
    assert loaded.max_wait_seconds == 600.0
