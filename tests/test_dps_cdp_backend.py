"""CDP/DOM shadow backend for the Naver DPS lookup -- offline.

Two layers, neither touches DPS:

  real DOM    a throwaway headless Chrome (temp profile, random loopback
              port, every non-loopback hostname resolved to NOTFOUND) loads
              representative fixture pages from a loopback HTTP server, and
              the real CDP client + DOM scripts run against them
  fake CDP    scripted page results for the transport and failure paths

The fixture DOM is representative, not DPS's real DOM: selectors that only a
live DPS page can confirm are listed as UNVERIFIED in the report.
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from dps.cdp_backend import (
    CdpBrowser,
    CdpDpsReader,
    CdpError,
    chrome_launch_command,
    lookup_dps_order_cdp,
)
from dps.dates import calculate_dps_lookup_period
from services.dps_result_normalizer import normalize_dps_result

ORDER = "2026091912345678"
OTHER = "2026091987654321"
SALES = "9100123456"
EORDER = "5100999888777"
CHROME = next((p for p in (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    shutil.which("chrome") or "", shutil.which("google-chrome") or "",
) if p and os.path.exists(p)), None)

# ----------------------------------------------------------------- fixtures
PURCHASE = """<html><head><title>Samsung DPS</title></head><body>
<div>판매 &gt; 온라인판매 &gt; 구매요청리스트</div>
<iframe id="main" src="frame.html" style="width:1200px;height:800px"></iframe></body></html>"""

FRAME = r"""<html><body>
<table><tr><th>온라인판매 주문번호</th><td><input id="ord" type="text"></td>
<th>상품주문번호</th><td><input id="pord" type="text"></td></tr>
<tr><th>조회기간</th><td><input id="s" type="text" value="2026-08-01"> ~
<input id="e" type="text" value="2026-08-31"></td></tr></table>
<button id="q">조회</button> <button id="save">저장</button>
<div id="loading" class="loading" style="display:none">조회중</div>
<div id="out"></div>
<script>
window.top.__detailOpens = 0; window.top.__saves = 0;
document.getElementById('save').onclick = () => { window.top.__saves++; };
const H = ['온라인판매 주문번호','모델명','수량','판매금액','구매자','DPS판매번호','전자주문번호','상태'];
function row(o, s){ return '<tr><td>'+o+'</td><td>LH43BEFHLGFXKR</td><td>1</td><td>700,000</td><td>홍*동</td>'
  + '<td><a href="#" onclick="window.top.__detailOpens++;window.open(\'sd010_0048_DP_SSearchSalesMain.do.html?sales='+s+'\',\'detail\');return false;">'+s+'</a></td>'
  + '<td>__EORDER__</td><td>구매요청</td></tr>'; }
function grid(body){ return '<table><thead><tr>'+H.map(h=>'<th>'+h+'</th>').join('')+'</tr></thead><tbody>'+body+'</tbody></table>'; }
document.getElementById('q').onclick = () => {
  const o = document.getElementById('ord').value;
  if (o === '__CONFIRM__') { if (!confirm('저장하시겠습니까?')) return; }
  document.getElementById('loading').style.display = 'block';
  document.getElementById('out').innerHTML = '';
  setTimeout(() => {
    document.getElementById('loading').style.display = 'none';
    const out = document.getElementById('out');
    if (o === '__NONE__') { out.innerHTML = '<div>조회 결과가 없습니다</div>'; return; }
    if (o === '__MALFORMED__') { out.innerHTML = '<table><tr><td>'+o+'</td></tr></table>'; return; }
    if (o === '__MULTI__') { out.innerHTML = grid(row('__OTHER__','9100000001') + row(o,'__SALES__') + row(o,'__SALES__')); return; }
    if (o === '__VIRTUAL__') {
      out.innerHTML = '<div id="vs" style="height:120px;overflow:auto"><div id="sp" style="height:1200px;position:relative"><div id="win" style="position:absolute;top:0;width:100%"></div></div></div>';
      const vs = document.getElementById('vs');
      const draw = () => { const start = Math.floor(vs.scrollTop / 30); let body = '';
        for (let i = start; i < Math.min(start + 5, 40); i++) body += (i === 30 ? row(o,'__SALES__') : row('20260919000000'+String(i).padStart(2,'0'),'91000000'+String(i).padStart(2,'0')));
        document.getElementById('win').style.top = vs.scrollTop + 'px';
        document.getElementById('win').innerHTML = grid(body); };
      vs.onscroll = draw; draw(); return; }
    out.innerHTML = grid(row(o,'__SALES__'));
  }, 400);
};
</script></body></html>"""

DETAIL = """<html><head><title>판매조회</title></head><body>
<div style="display:flex"><div style="width:380px"><strong>판매처정보</strong>
<table><tr><th>판매경로</th><td><input value="온라인"></td></tr></table></div>
<div style="width:380px"><strong>고객정보</strong><table>
<tr><th>판매번호</th><td><input value="__SALES__"></td></tr>
<tr><th>구매자</th><td><input value="홍*동"></td></tr>
<tr><th>인수자</th><td><input value="테스트인수자"></td></tr>
<tr><th>요구납기일</th><td><input value="2026-09-30"></td></tr></table></div>
<div style="width:380px"><strong>입금정보</strong><table>
<tr><th>주문금액</th><td><input value="700,000"></td></tr></table></div></div>
<div style="margin-top:40px"><strong>품목상세내역</strong><table>
<thead><tr><th>행번</th><th>모델</th><th>수량</th><th>판매단가</th><th>판매금액</th><th>요구납기일</th></tr></thead>
<tbody><tr><td>1</td><td>LH43BEFHLGFXKR</td><td>1</td><td>700,000</td><td>700,000</td><td>2026-09-24</td></tr></tbody>
</table></div></body></html>"""

LOGIN = """<html><head><title>Samsung DPS 로그인</title></head><body>
<input type="text" placeholder="아이디"><input type="password"></body></html>"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("dps_site")
    frame = FRAME.replace("__EORDER__", EORDER).replace("__OTHER__", OTHER).replace("__SALES__", SALES)
    for name, body in {
        "purchase.html": PURCHASE, "frame.html": frame,
        "sd010_0048_DP_SSearchSalesMain.do.html": DETAIL.replace("__SALES__", SALES),
        "login.html": LOGIN,
        "missing.html": "<html><body>구매요청리스트<div>입력 없음</div></body></html>",
    }.items():
        # Real DPS pages declare their charset; so must the fixtures.
        (root / name).write_text('<meta charset="utf-8">' + body, encoding="utf-8")

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(root), **k)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(scope="module")
def chrome(site):
    if CHROME is None:
        pytest.skip("Chrome not installed")
    port = _free_port()
    profile = tempfile.mkdtemp(prefix="dps-cdp-test-")
    command = chrome_launch_command(CHROME, port=port, profile_dir=profile) + [
        "--headless=new", "--disable-background-networking", "--disable-sync",
        "--disable-component-update", "--disable-default-apps",
        "--disable-popup-blocking", "--metrics-recording-only",
        # Every hostname but loopback fails to resolve: nothing leaves the PC.
        "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
        "about:blank",
    ]
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    browser = CdpBrowser(port=port)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            browser.version()
            break
        except CdpError:
            time.sleep(0.2)
    else:
        process.kill()
        pytest.skip("headless Chrome did not start")
    yield browser, port
    process.kill()
    process.wait(timeout=10)
    shutil.rmtree(profile, ignore_errors=True)


def _open(port: int, url: str) -> None:
    import urllib.request
    request = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?{url}", method="PUT")
    urllib.request.urlopen(request, timeout=5).read()


def _reset(browser: CdpBrowser, port: int, url: str) -> None:
    for page in browser.pages():
        browser.close_page(str(page["id"]))
    _open(port, url)
    time.sleep(1.0)


def _reader(browser: CdpBrowser, **kw) -> CdpDpsReader:
    return CdpDpsReader(browser, allowed_hosts=("127.0.0.1",), **kw)


def _top_eval(browser: CdpBrowser, expression: str) -> Any:
    target = next(p for p in browser.pages() if "purchase" in p["url"])
    page = browser.open_page(target)
    try:
        return page.evaluate(expression)
    finally:
        page.close()


def _lookup(browser, order, **kw):
    return _reader(browser, **kw).perform_lookup(
        order_id=order, dps_period_start="2026-09-01", dps_period_end="2026-09-21",
    )


# ------------------------------------------------------------ real DOM runs


def test_single_row_reads_detail_and_item_required_date(chrome, site):
    browser, port = chrome
    _reset(browser, port, f"{site}/purchase.html")
    result = _lookup(browser, ORDER)

    assert result["success"] and result["found"], result
    assert result["status"] == "RESULT_FOUND_WITH_DETAIL"
    assert result["automation_method"] == "CHROME_CDP_DOM_V1"
    assert result["data"]["dps_sales_number"] == SALES
    # 품목상세내역 요구납기일, never the 고객정보 요구납기일 (2026-09-30)
    assert result["installation_date"] == "2026-09-24"
    assert result["installation_date_source"] == "DPS_ITEM_DETAIL_REQUIRED_DELIVERY_DATE"
    assert result["data"]["recipient_name"] == "테스트인수자"
    assert result["detail_lookup"]["closed"] is True
    assert not any("sd010_0048" in p["url"] for p in browser.pages())
    # the period and the order number really reached the iframe inputs
    fill = _top_eval(browser, "(()=>{const d=document.getElementById('main').contentDocument;"
                              "return [d.getElementById('ord').value,d.getElementById('s').value,"
                              "d.getElementById('e').value, window.__saves];})()")
    assert fill == [ORDER, "2026-09-01", "2026-09-21", 0]   # 저장 never pressed

    normalized = normalize_dps_result(result, order_id=ORDER, elapsed_seconds=1.0)
    assert normalized["lookup_status"] == "SUCCESS"
    assert normalized["installation_date"] == "2026-09-24"
    assert normalized["sales_number"] == SALES


def test_multiple_rows_select_the_order_and_open_detail_once(chrome, site):
    browser, port = chrome
    _reset(browser, port, f"{site}/purchase.html")
    result = _lookup(browser, "__MULTI__")
    assert result["found"] and result["data"]["dps_sales_number"] == SALES
    assert result["data"].get("dps_sales_number") != "9100000001"   # not OTHER's row
    assert _top_eval(browser, "window.__detailOpens") == 1


def test_no_result_keeps_the_no_result_meaning(chrome, site):
    browser, port = chrome
    _reset(browser, port, f"{site}/purchase.html")
    result = _lookup(browser, "__NONE__")
    assert result["success"] is True and result["found"] is False
    assert result["code"] == "NO_DPS_RESULT"
    assert normalize_dps_result(result, order_id=ORDER, elapsed_seconds=1)["lookup_status"] == "NOT_FOUND"


def test_virtual_grid_is_scrolled_until_the_order_row_renders(chrome, site):
    browser, port = chrome
    _reset(browser, port, f"{site}/purchase.html")
    result = _lookup(browser, "__VIRTUAL__")
    assert result["found"], result
    assert result["data"]["dps_sales_number"] == SALES


def test_malformed_grid_is_an_error_not_a_result(chrome, site):
    browser, port = chrome
    _reset(browser, port, f"{site}/purchase.html")
    result = _lookup(browser, "__MALFORMED__", result_timeout=3.0)
    assert result["success"] is False
    assert "TIMEOUT" in result["code"]


def test_login_page_is_reported_never_filled(chrome, site):
    browser, port = chrome
    _reset(browser, port, f"{site}/login.html")
    result = _lookup(browser, ORDER)
    assert result["code"] == "DPS_LOGIN_REQUIRED"
    assert normalize_dps_result(result, order_id=ORDER, elapsed_seconds=0)["lookup_status"] == "AUTOMATION_ERROR"


def test_confirm_dialog_is_refused(chrome, site):
    browser, port = chrome
    _reset(browser, port, f"{site}/purchase.html")
    result = _lookup(browser, "__CONFIRM__", result_timeout=3.0)
    assert result["success"] is False
    assert result["code"] in {"DIALOG_CONFIRM_REFUSED", "SEARCH_RESULT_TIMEOUT"}
    assert _top_eval(browser, "window.__saves") == 0


def test_missing_order_input(chrome, site):
    browser, port = chrome
    _reset(browser, port, f"{site}/missing.html")
    result = _lookup(browser, ORDER)
    assert result["code"] == "ORDER_INPUT_NOT_FOUND"


def test_real_dps_host_is_required_by_default(chrome, site):
    browser, port = chrome
    _reset(browser, port, f"{site}/purchase.html")
    result = CdpDpsReader(browser).perform_lookup(   # default: dps2u.co.kr only
        order_id=ORDER, dps_period_start="2026-09-01", dps_period_end="2026-09-21")
    assert result["code"] == "DPS_TAB_NOT_FOUND"


# ------------------------------------------------------------- fake CDP


class FakePage:
    def __init__(self, script):
        self.script = script
        self.dialogs = []
        self.closed = False

    def evaluate(self, expression, timeout=10.0):
        return self.script(expression)

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, pages, page):
        self._pages, self.page = pages, page

    def pages(self):
        return list(self._pages)

    def open_page(self, target):
        return self.page

    def close_page(self, target_id):
        return True


DPS_TARGET = {"id": "t1", "type": "page", "url": "https://www.dps2u.co.kr/x", "title": "DPS"}


def _script(on_snapshot):
    def run(expression):
        if "password" in expression and "frames" in expression:
            return {"password": False, "text": "구매요청리스트 온라인판매 주문번호"}
        if "orderCands" in expression:
            return {"ok": True, "readback": {"order": ORDER}}
        if "raw_result_texts" in expression:
            return on_snapshot()
        return False
    return run


def test_tab_closed_mid_lookup_is_an_error():
    def snapshot():
        raise CdpError("DPS_TAB_CLOSED", "gone")
    page = FakePage(_script(snapshot))
    result = CdpDpsReader(FakeBrowser([DPS_TARGET], page), poll_interval=0).perform_lookup(
        order_id=ORDER, dps_period_start="2026-09-01", dps_period_end="2026-09-21")
    assert result["code"] == "DPS_TAB_CLOSED"
    assert page.closed


def test_result_timeout_uses_polling_not_a_fixed_sleep():
    ticks = iter(range(0, 10_000))
    page = FakePage(_script(lambda: {"raw_result_texts": [], "table_rows": [], "loading": True}))
    result = CdpDpsReader(FakeBrowser([DPS_TARGET], page), result_timeout=5,
                          clock=lambda: float(next(ticks)), sleep=lambda s: None).perform_lookup(
        order_id=ORDER, dps_period_start="2026-09-01", dps_period_end="2026-09-21")
    assert result["code"] == "SEARCH_RESULT_TIMEOUT"
    assert normalize_dps_result(result, order_id=ORDER, elapsed_seconds=5)["lookup_status"] == "TIMEOUT"


def test_no_dps_tab():
    result = CdpDpsReader(FakeBrowser([], None)).perform_lookup(
        order_id=ORDER, dps_period_start="2026-09-01", dps_period_end="2026-09-21")
    assert result["code"] == "DPS_TAB_NOT_FOUND"


def test_chrome_not_running_is_chrome_not_found():
    result = CdpDpsReader(CdpBrowser(port=_free_port(), timeout=0.5)).perform_lookup(
        order_id=ORDER, dps_period_start="2026-09-01", dps_period_end="2026-09-21")
    assert result["code"] == "CHROME_NOT_FOUND"
    assert normalize_dps_result(result, order_id=ORDER, elapsed_seconds=0)["lookup_status"] == "AUTOMATION_ERROR"


def test_only_loopback_devtools_is_accepted():
    with pytest.raises(CdpError) as raised:
        CdpBrowser(host="10.0.0.5")
    assert raised.value.code == "CDP_NON_LOOPBACK_REFUSED"


def test_entry_point_keeps_the_agent_identifier_guards_and_period():
    assert lookup_dps_order_cdp(order_id="")["code"] == "DPS_ORDER_ID_MISSING"
    assert lookup_dps_order_cdp(order_id=ORDER, dps_query_value_type="product_order_id")[
        "code"] == "INVALID_DPS_QUERY_TYPE"
    assert lookup_dps_order_cdp(order_id=ORDER, dps_query_value=OTHER)["code"] == "CLIENT_ORDER_ID_MISMATCH"
    assert lookup_dps_order_cdp(order_id=ORDER)["code"] == "DATE_SOURCE_MISSING"

    seen = {}

    class Recorder:
        def perform_lookup(self, **kw):
            seen.update(kw)
            return {"success": True, "found": False, "code": "NO_DPS_RESULT",
                    "diagnostics": {"dps_input_verified_value": ORDER}}

    result = lookup_dps_order_cdp(order_id=ORDER, order_date="2026-08-23", reader=Recorder())
    expected = calculate_dps_lookup_period("2026-08-23")
    assert (seen["dps_period_start"], seen["dps_period_end"]) == (
        expected.start.isoformat(), expected.end.isoformat())
    assert result["all_identifiers_match"] is True


def test_production_default_is_unchanged():
    from services.dps_agent_client import lookup_dps_order
    from services.dps_enrichment_service import DpsEnrichmentService
    from services.market_policy import DPS_MARKETS
    import inspect

    assert DPS_MARKETS == frozenset({"NAVER"})
    assert "client or lookup_dps_order" in inspect.getsource(DpsEnrichmentService.__init__)
    assert lookup_dps_order.__module__ == "services.dps_agent_client"
