from __future__ import annotations

from config import DpsSessionSettings, NaverAutoSyncSettings
from dps.connection_store import ConnectionStore, PROJECT_ROOT
from services.dps_agent_client import dps_config_check
from services.dps_lookup_policy import DpsSettings


def test_three_scheduler_time_axes_have_independent_defaults(monkeypatch) -> None:
    for name in (
        "NAVER_AUTO_SYNC_INTERVAL_MINUTES",
        "DPS_REFRESH_INTERVAL_MINUTES",
        "DPS_SUCCESS_CACHE_TTL_SECONDS",
        "DPS_SESSION_IDLE_MINUTES",
        "DPS_SESSION_KEEPALIVE_INTERVAL_MINUTES",
    ):
        monkeypatch.delenv(name, raising=False)
    assert NaverAutoSyncSettings.from_environment().interval_minutes == 10
    dps = DpsSettings.from_environment()
    assert dps.refresh_interval_minutes == 30
    assert dps.success_ttl_seconds == 30 * 60
    assert DpsSessionSettings.from_environment().keepalive_interval_minutes == 40


def test_session_idle_name_overrides_legacy_keepalive_name(monkeypatch) -> None:
    monkeypatch.setenv("DPS_SESSION_KEEPALIVE_INTERVAL_MINUTES", "55")
    monkeypatch.setenv("DPS_SESSION_IDLE_MINUTES", "40")
    assert DpsSessionSettings.from_environment().keepalive_interval_minutes == 40


def test_relative_dps_runtime_files_resolve_from_project_root(monkeypatch) -> None:
    monkeypatch.setenv("DPS_CONNECTION_FILE", "data/custom-connection.json")
    monkeypatch.setenv("DPS_AGENT_STATE_FILE", "data/custom-state.json")
    store = ConnectionStore()
    assert store.path == PROJECT_ROOT / "data/custom-connection.json"
    assert store.state_path == PROJECT_ROOT / "data/custom-state.json"


def test_dps_config_check_classifies_invalid_port_without_secret_output(monkeypatch) -> None:
    monkeypatch.setenv("DPS_AGENT_PORT", "not-a-port")
    result = dps_config_check()
    assert result["ok"] is False
    assert result["diagnostic_code"] == "CONFIG_ERROR"
    assert result["issues"] == ["DPS_AGENT_PORT_INVALID"]
    assert "password" not in str(result).lower()
