"""Naver DPS production switch: CDP/DOM is the lookup path, pywinauto is dormant.

Covers the production seam only (dps.cdp_session and its callers). The CDP
reader's own DOM contract is in test_dps_cdp_backend.py. No Chrome, no DPS,
no agent: every browser and process here is a fake.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import dps.cdp_session as session
from dps.cdp_backend import CdpDpsReader, CdpError
from dps.cdp_session import (
    cdp_session_status,
    ensure_cdp_chrome,
    launch_spec_from_processes,
    lookup_dps_order_production,
)

ORDER = "2026091912345678"


def _proc(*argv, executable=r"C:\Chrome\chrome.exe"):
    return {"pid": 1, "executable": executable, "argv": [executable, *argv]}


class _Browser:
    def __init__(self, running=True, pages=(), page=None):
        self.running, self._pages, self.page = running, list(pages), page

    def version(self):
        if not self.running:
            raise CdpError("CHROME_NOT_FOUND")
        return {"Browser": "Chrome/140"}

    def pages(self):
        return list(self._pages)

    def open_page(self, target):
        return self.page

    def close_page(self, target_id):
        return True


class _Page:
    def __init__(self, state):
        self.state = state
        self.closed = False

    def evaluate(self, expression, timeout=10.0):
        return self.state

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _fresh_process_flag(monkeypatch):
    monkeypatch.setattr(session, "_SPEC_RECORDED_THIS_PROCESS", False)


# ---------------------------------------------------------------- Chrome start


def test_running_9222_chrome_is_reused_and_its_profile_remembered(tmp_path):
    launches = []
    state = tmp_path / "state.json"
    result = ensure_cdp_chrome(
        port=9222, browser=_Browser(running=True), state_path=state,
        process_lister=lambda: [
            _proc("--type=renderer", "--remote-debugging-port=9222"),
            _proc("--remote-debugging-port=9222", r"--user-data-dir=D:\DPS\chrome-profile",
                  "--profile-directory=Default"),
        ],
        launcher=launches.append,
    )
    assert result["action"] == "REUSED" and launches == []
    recorded = json.loads(state.read_text(encoding="utf-8"))["chrome"]
    assert recorded == {"executable": r"C:\Chrome\chrome.exe", "port": 9222,
                        "user_data_dir": r"D:\DPS\chrome-profile", "profile_directory": "Default"}


def _recorded(tmp_path, *, profile_exists=True):
    exe = tmp_path / "chrome.exe"
    exe.write_text("", encoding="utf-8")
    profile = tmp_path / "dps-profile"
    if profile_exists:
        profile.mkdir()
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"chrome": {
        "executable": str(exe), "user_data_dir": str(profile), "port": 9222}}), encoding="utf-8")
    return state, exe, profile


def test_missing_chrome_is_started_once_with_the_recorded_profile_on_9222(tmp_path):
    state, exe, profile = _recorded(tmp_path)
    launches = []
    kwargs = dict(port=9222, browser=_Browser(running=False), state_path=state,
                  process_lister=lambda: [], launcher=lambda argv: launches.append(argv) or SimpleNamespace(pid=7),
                  clock=lambda: 1000.0)
    first = ensure_cdp_chrome(**kwargs)
    second = ensure_cdp_chrome(**kwargs)                       # a Streamlit rerun
    assert first["action"] == "LAUNCHED"
    assert second == {"action": "NOT_LAUNCHED", "reason": "LAUNCH_ALREADY_REQUESTED", "port": 9222}
    assert launches == [[str(exe), "--remote-debugging-port=9222",
                         "--remote-debugging-address=127.0.0.1", f"--user-data-dir={profile}"]]
    assert "--remote-debugging-port=9333" not in launches[0]


def test_no_recorded_profile_starts_nothing(tmp_path):
    launches = []
    result = ensure_cdp_chrome(port=9222, browser=_Browser(running=False),
                               state_path=tmp_path / "none.json", process_lister=lambda: [],
                               launcher=launches.append)
    assert result["reason"] == "PROFILE_UNKNOWN" and launches == []


def test_a_missing_profile_directory_is_never_created(tmp_path):
    state, _, profile = _recorded(tmp_path, profile_exists=False)
    launches = []
    result = ensure_cdp_chrome(port=9222, browser=_Browser(running=False), state_path=state,
                               process_lister=lambda: [], launcher=launches.append)
    assert result["reason"] == "PROFILE_PATH_MISSING" and launches == []
    assert not profile.exists()


def test_profile_already_open_without_the_port_is_left_alone(tmp_path):
    state, _, profile = _recorded(tmp_path)
    launches = []
    result = ensure_cdp_chrome(port=9222, browser=_Browser(running=False), state_path=state,
                               process_lister=lambda: [_proc(f"--user-data-dir={profile}")],
                               launcher=launches.append)
    assert result["reason"] == "PROFILE_IN_USE_WITHOUT_CDP" and launches == []


def test_default_profile_or_other_ports_are_not_learned():
    assert launch_spec_from_processes([_proc("--remote-debugging-port=9222")], 9222) is None
    assert launch_spec_from_processes(
        [_proc("--remote-debugging-port=9333", r"--user-data-dir=D:\x")], 9222) is None


def test_start_hook_skips_when_dps_lookups_are_off(monkeypatch):
    monkeypatch.setenv("DPS_AUTOMATIC_LOOKUP_ENABLED", "false")
    called = []
    monkeypatch.setattr(session, "ensure_cdp_chrome", lambda **kw: called.append(1))
    assert session.ensure_cdp_chrome_on_start() == {"action": "SKIPPED"}
    assert called == []


def test_app_start_uses_the_cdp_hook_not_the_agent_monitor():
    source = Path(__file__).resolve().parents[1].joinpath("app.py").read_text(encoding="utf-8")
    assert "ensure_cdp_chrome_on_start()" in source
    assert "ensure_dps_session_monitor" not in source


# ---------------------------------------------------------------- status


def test_status_chrome_not_running():
    status = cdp_session_status(browser=_Browser(running=False))
    assert status["session_status"] == "CHROME_NOT_FOUND" and status["backend"] == "CDP"


def test_status_manual_login_required():
    page = _Page({"password": True, "marker_hits": []})
    status = cdp_session_status(browser=_Browser(
        pages=[{"id": "1", "type": "page", "url": "https://www.dps2u.co.kr/login.do"}], page=page))
    assert status["session_status"] == "LOGIN_REQUIRED" and status["ok"] is False


def test_status_logged_in_on_the_purchase_list_is_ready():
    page = _Page({"password": False, "marker_hits": ["구매요청리스트"]})
    status = cdp_session_status(browser=_Browser(
        pages=[{"id": "1", "type": "page", "url": "https://www.dps2u.co.kr/main.do"}], page=page))
    assert status["session_status"] == "READY" and status["purchase_list_ready"] is True
    assert status["login_status"] == "LOGGED_IN"


def test_status_logged_in_elsewhere_asks_for_the_purchase_list():
    page = _Page({"password": False, "marker_hits": []})
    status = cdp_session_status(browser=_Browser(
        pages=[{"id": "1", "type": "page", "url": "https://www.dps2u.co.kr/main.do"}], page=page))
    assert status["session_status"] == "DPS_PAGE_NOT_FOUND" and status["dps_tab_found"] is True


def test_auto_post_and_dashboard_read_cdp_status_not_the_agent():
    from services.auto_post_pipeline_service import AutoPostPipelineService
    import ui.production_dashboard as dashboard

    assert "cdp_session_status" in inspect.getsource(AutoPostPipelineService.__init__)
    assert "cdp_session_status()" in inspect.getsource(dashboard._cached_dps_session_status)


# ---------------------------------------------------------------- lookup path


def _enrichment(database_path):
    from repositories.database import Database
    from services.dps_enrichment_service import DpsEnrichmentService

    database = Database(database_path)
    database.initialize()
    return DpsEnrichmentService(database)


def test_production_client_is_cdp_and_the_agent_is_never_called(tmp_path, monkeypatch):
    import services.dps_agent_client as agent

    monkeypatch.setattr(agent, "lookup_dps_order",
                        lambda *a, **k: pytest.fail("pywinauto agent must not be called"))
    seen = {}

    def fake_perform(self, **kwargs):
        seen.update(kwargs, port=self.browser.base)
        return {"success": False, "code": "DPS_TAB_NOT_FOUND", "status": "DPS_TAB_NOT_FOUND"}

    monkeypatch.setattr(CdpDpsReader, "perform_lookup", fake_perform)
    enrichment = _enrichment(tmp_path / "e.db")
    assert enrichment.client is lookup_dps_order_production
    result = enrichment.client(order_id=ORDER, dps_query_value=ORDER,
                               dps_query_value_type="order_id", order_date="2026-09-19")
    assert seen["order_id"] == ORDER and seen["port"].endswith(":1")   # conftest's closed port
    assert result["success"] is False and result["code"] == "DPS_TAB_NOT_FOUND"  # no fallback


def test_blank_sales_number_stays_a_normal_success_through_the_client(monkeypatch):
    from services.dps_result_normalizer import normalize_dps_result

    monkeypatch.setattr(CdpDpsReader, "perform_lookup", lambda self, **kw: {
        "ok": True, "success": True, "found": True, "code": "LOOKUP_COMPLETE",
        "status": "RESULT_FOUND_DETAIL_PARTIAL",
        "detail_lookup": {"attempted": False, "status": "DPS_SALES_NUMBER_MISSING"},
        "data": {"dps_sales_number": None, "detail_items": [], "product_name": "LS32DM500EKXKR",
                 "delivery_status": "배송완료"},
        "diagnostics": {"dps_input_verified_value": ORDER, "sales_number_cell": "BLANK"}})
    raw = lookup_dps_order_production(order_id=ORDER, order_date="2026-09-19")
    normalized = normalize_dps_result(raw, order_id=ORDER, elapsed_seconds=2.5)
    assert normalized["lookup_status"] == "SUCCESS"
    assert normalized["sales_number"] is None and normalized["installation_date"] is None
    assert normalized["product_name"] == "LS32DM500EKXKR"


def test_found_detail_date_contract_passes_through_unchanged(monkeypatch):
    from services.dps_result_normalizer import normalize_dps_result

    monkeypatch.setattr(CdpDpsReader, "perform_lookup", lambda self, **kw: {
        "ok": True, "success": True, "found": True, "code": "LOOKUP_COMPLETE",
        "status": "RESULT_FOUND_WITH_DETAIL",
        "data": {"dps_sales_number": "3141553776", "required_delivery_date": "2026-09-04",
                 "installation_date": "2026-09-04", "date_parse_status": "PARSED",
                 "installation_date_source": "DPS_ITEM_DETAIL_REQUIRED_DELIVERY_DATE",
                 "detail_items": [{"model_name": "LH50BEHHLGFXKR"}]},
        "diagnostics": {"dps_input_verified_value": ORDER}})
    normalized = normalize_dps_result(lookup_dps_order_production(order_id=ORDER, order_date="2026-09-01"),
                                      order_id=ORDER, elapsed_seconds=2.5)
    assert (normalized["lookup_status"], normalized["installation_date"], normalized["sales_number"]) == (
        "SUCCESS", "2026-09-04", "3141553776")


def test_legacy_agent_is_dormant_and_never_launched(monkeypatch):
    import services.dps_agent_client as agent

    monkeypatch.setattr(agent.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("agent must not be started"))
    monkeypatch.setattr(agent, "_request", lambda *a, **k: pytest.fail("agent must not be called"))
    assert agent.start_dps_agent()["code"] == "LEGACY_DPS_AGENT_DORMANT"
    assert agent.lookup_dps_order(order_id=ORDER)["code"] == "LEGACY_DPS_AGENT_DORMANT"
    assert agent.ensure_dps_session_monitor().get("agent_running") is not True


def test_coupang_dps_stays_off():
    from services.market_policy import DPS_MARKETS, is_store_dps_enabled

    assert DPS_MARKETS == frozenset({"NAVER"})
    assert not is_store_dps_enabled("COUPANG_OJE_NS")
