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
import sys
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
TRANSFER = "3141000999"      # the go_transterPop number beside the sales number
BAD_SALES = "9100999999"     # opens a page that is not 판매조회
CHROME = next((p for p in (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    shutil.which("chrome") or "", shutil.which("google-chrome") or "",
) if p and os.path.exists(p)), None)

# ----------------------------------------------------------------- fixtures
PURCHASE = """<html><head><title>Samsung DPS</title></head><body>
<div>판매 &gt; 온라인판매 &gt; 구매요청리스트</div>
<iframe id="main" src="frame.html" style="width:1200px;height:800px"></iframe>
<script>
window.__detailOpens = 0; window.__transferOpens = 0; window.__infoOpens = 0;
function go_sendSearchMain(s) { window.__detailOpens++;
  const live = ['3141000001','3141000003','3141000004'].includes(s);
  window.open((s === '__BAD_SALES__' ? 'notdetail.html'
    : live ? 'sd010_0050_DP_SSearchSalesMain.do.html' : 'sd010_0048_DP_SSearchSalesMain.do.html') + '?sales=' + s, 'detail'); }
function go_transterPop(s) { window.__transferOpens++; }
function go_infoPop(o, c, n) { window.__infoOpens++; }
</script></body></html>"""

FRAME = r"""<html><body>
<table><tr><th>온라인판매 주문번호</th><td><input id="ord" type="text"></td>
<th>상품주문번호</th><td><input id="pord" type="text"></td></tr>
<tr><th>조회기간</th><td><input id="s" type="text" value="2026-08-01"> ~
<input id="e" type="text" value="2026-08-31"></td></tr>
<tr><td>판매번호</td><td><input type="text"></td><td>기간</td><td><input type="text"></td>
<td>품명</td><td><input type="text"></td><td>인수자명</td><td><input type="text"></td>
<td>배송상태</td><td><select><option>전체</option><option>구매요청</option><option>설치완료</option><option>배송중</option></select></td></tr>
</table>
<table class="notice"><tr><td>공지</td><td>판매번호 기간 인수자명 안내</td>
<td><a href="javascript:;" onclick="parent.go_sendSearchMain('9999999999');">9999999999</a></td></tr></table>
<button id="q">조회</button> <button id="save">저장</button>
<div id="loading" class="loading" style="display:none">조회중</div>
<div id="out"></div>
<script>
window.top.__saves = 0;
document.getElementById('save').onclick = () => { window.top.__saves++; };
const link = (fn, arg, text) => '<a href="javascript:;" onclick="parent.' + fn + '(\'' + arg + '\');">' + text + '</a>';
function row(o, s, opt){ opt = opt || {};
  const salesCell = opt.noSend ? s : (opt.sendArgs || [s]).map(a => link('go_sendSearchMain', a, opt.sendText || a)).join('');
  return '<tr><td><input type="checkbox"></td><td>1</td><td>구매요청</td>'
    + '<td><a href="javascript:;" onclick="parent.go_infoPop(\'' + o + '\',\'NCP_1ORWWI_01\',\'10\');">' + o + '</a></td>'
    + '<td>LH43BEFHLGFXKR</td><td>1</td><td>700,000</td><td>홍*동</td><td>2026-09-01</td>'
    + '<td>' + salesCell + '</td>'
    + '<td>' + link('go_transterPop', s, '__TRANSFER__') + '</td><td>비고</td></tr>'; }
function grid(body){ return '<table class="grid"><tbody>' + body + '</tbody></table>'; }
const H_SALES = {'__H1__':'3100000001','__H2__':'3100000002','__H3__':'3100000003',
  '__L1__':'3141000001','__L3__':'3141000003','__L4__':'3141000004'};
document.getElementById('q').onclick = () => {
  if (window.top.__freeze) return;   // a page that never answers this query
  const o = document.getElementById('ord').value;
  if (o === '__CONFIRM__') { if (!confirm('저장하시겠습니까?')) return; }
  document.getElementById('loading').style.display = 'block';
  document.getElementById('out').innerHTML = '';
  setTimeout(() => {
    document.getElementById('loading').style.display = 'none';
    const out = document.getElementById('out');
    if (o === '__NONE__') { out.innerHTML = '<div>조회 결과가 없습니다</div>'; return; }
    if (o === '__MALFORMED__') { out.innerHTML = '<table><tr><td>'+o+'</td></tr></table>'; return; }
    if (o === '__FORMONLY__') { return; }
    if (o === '__UNRELATED__') { out.innerHTML = '<table><tr><td>판매번호</td><td>기간</td><td>품명</td><td>인수자명</td></tr></table>'; return; }
    if (o === '__OTHERONLY__') { out.innerHTML = grid(row('__OTHER__','9100000001')); return; }
    if (o === '__BADDETAIL__') { out.innerHTML = grid(row(o,'__BAD_SALES__')); return; }
    if (o === '__NOSEND__') { out.innerHTML = grid(row(o,'__SALES__',{noSend:true})); return; }
    if (o === '__SENDOTHERONLY__') { out.innerHTML = grid(row('__OTHER__','9100000001') + row(o,'__SALES__',{noSend:true})); return; }
    if (o === '__TWOSEND__') { out.innerHTML = grid(row(o,'__SALES__',{sendArgs:['__SALES__','9100000002']})); return; }
    if (o === '__TEXTMISMATCH__') { out.innerHTML = grid(row(o,'__SALES__',{sendText:'9100000003'})); return; }
    if (H_SALES[o]) { out.innerHTML = grid(row(o, H_SALES[o])); return; }
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
<tr><th>판매번호</th><td><input id="salesno" value=""></td></tr>
<tr><th>구매자</th><td><input value="홍*동"></td></tr>
<tr><th>인수자</th><td><input value="테스트인수자"></td></tr>
<tr><th>요구납기일</th><td><input value="2026-09-30"></td></tr></table></div>
<div style="width:380px"><strong>입금정보</strong><table>
<tr><th>주문금액</th><td><input value="700,000"></td></tr></table></div></div>
<div style="margin-top:40px"><strong>품목상세내역</strong><table>
<thead><tr><th>행번</th><th>모델</th><th>수량</th><th>판매단가</th><th>판매금액</th><th>요구납기일</th></tr></thead>
<tbody id="items"></tbody>
</table></div>
<script>
const sales = new URLSearchParams(location.search).get('sales');
document.getElementById('salesno').value = sales;
const ITEMS = {
  '3100000001': [['LH50BEHHLGFXKR','2026-09-04']],
  '3100000002': [['LS32DM500EKXKR','2026-09-02']],
  '3100000003': [['HA-MTS1S43WHT','2026-09-09'],['HA-MTSHELFWHT','2026-09-09'],['LH43BEHHLGFXKR','2026-09-09']],
};
const items = ITEMS[sales] || [['LH43BEFHLGFXKR','2026-09-24']];
document.getElementById('items').innerHTML = items.map((it, i) =>
  '<tr><td>'+(i+1)+'</td><td>'+it[0]+'</td><td>1</td><td>100,000</td><td>100,000</td><td>'+it[1]+'</td></tr>').join('');
</script></body></html>"""

LIVE_DETAIL = """<html><head><title>판매조회</title>
<style>td{width:150px;height:22px;padding:0;overflow:hidden;white-space:nowrap}
table{table-layout:fixed;border-collapse:collapse}</style></head><body>
<table><tr><td class="theadFree">판매처정보</td><td></td><td></td>
<td class="theadFree">고객정보</td><td></td><td></td><td class="theadFree">입금정보</td></tr>
<tr><td class="theadFree">판매경로</td><td class="tcontentFree">온라인</td><td></td>
<td class="theadFree">판매번호</td><td class="tcontentFree" id="salesno"></td><td></td>
<td class="theadFree">주문금액</td><td class="tcontentFree">1,234,000</td></tr>
<tr><td></td><td></td><td></td><td class="theadFree">인수자</td><td class="tcontentFree">테스트인수자B</td></tr>
<tr><td></td><td></td><td></td><td class="theadFree">요구납기일</td><td class="tcontentFree">2026-10-30</td></tr>
</table>
<div id="itemsSection" style="margin-top:30px"><div class="title">품목상세내역</div>
<table class="outer"><tr><td style="width:750px">
  <table class="head"><tr><td class="theadFree">행번</td><td class="theadFree">모델</td>
  <td class="theadFree">수량</td><td class="theadFree">판매금액</td><td class="theadFree">요구납기일</td></tr></table>
  <div style="position:relative;height:80px"><table class="body" id="b1" style="position:absolute;top:0;left:0"></table>
  <table class="body" id="b2" style="position:absolute;top:0;left:0"></table></div>
</td></tr></table></div>
<script>
const sales = new URLSearchParams(location.search).get('sales');
document.getElementById('salesno').textContent = sales;
const ITEMS = {
  '3141000001': [['LH50BEHHLGFXKR','1','1,234,000','2026-09-04']],
  '3141000003': [['HA-MTS1S43WHT','1','50,000','2026-09-09'],['HA-MTSHELFWHT','1','20,000','2026-09-09'],
                 ['LH43BEHHLGFXKR','1','700,000','2026-09-09']],
};
if (sales === '3141000004') { document.getElementById('itemsSection').remove(); }
else {
  const body = (ITEMS[sales] || []).map((it, i) => '<tr><td class="tcontentFree">' + (i + 1) + '</td>'
    + '<td class="tcontentFree left pl10">' + it[0] + '</td><td class="tcontentFree">' + it[1] + '</td>'
    + '<td class="tcontentFree">' + it[2] + '</td><td class="tcontentFree">' + it[3] + '</td></tr>').join('');
  document.getElementById('b1').innerHTML = body;
  if (sales === '3141000003') document.getElementById('b2').innerHTML = body;   // same DOM drawn twice
}
</script></body></html>"""

LOGIN = """<html><head><title>Samsung DPS 로그인</title></head><body>
<input type="text" placeholder="아이디"><input type="password"></body></html>"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("dps_site")
    frame = (FRAME.replace("__EORDER__", EORDER).replace("__OTHER__", OTHER)
             .replace("__BAD_SALES__", BAD_SALES).replace("__TRANSFER__", TRANSFER)
             .replace("__SALES__", SALES))
    for name, body in {
        "purchase.html": PURCHASE.replace("__BAD_SALES__", BAD_SALES), "frame.html": frame,
        "sd010_0048_DP_SSearchSalesMain.do.html": DETAIL.replace("__SALES__", SALES),
        "sd010_0050_DP_SSearchSalesMain.do.html": LIVE_DETAIL,
        "login.html": LOGIN,
        "notdetail.html": "<html><head><title>안내</title></head><body>준비중</body></html>",
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
        if "marker_hits" in expression:
            return {"password": False, "marker_hits": ["구매요청리스트"]}
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


# ======================================================================
# Server 5-case false success (2026-09-21) -- regressions
# ======================================================================
#
# The search form ("판매번호 | 기간", "품명 | 인수자명", 배송상태 select) was read
# as the result grid, parse_lookup_result's label->next-text fallback turned
# it into sales_number="기간" / product_name="인수자명", and the lookup came back
# SUCCESS / LOOKUP_COMPLETE with no row and no detail.


def _failed(result):
    return (result["success"] is False and
            normalize_dps_result(result, order_id=ORDER, elapsed_seconds=1)["lookup_status"] != "SUCCESS")


def test_B_search_form_is_never_the_result_grid(chrome, site):
    browser, port = chrome
    _reset(browser, port, f"{site}/purchase.html")
    result = _lookup(browser, "__FORMONLY__", result_timeout=3.0)
    assert _failed(result), result
    assert result.get("data", {}).get("dps_sales_number") != "기간"


def test_C_unrelated_table_is_not_success(chrome, site):
    browser, port = chrome
    _reset(browser, port, f"{site}/purchase.html")
    assert _failed(_lookup(browser, "__UNRELATED__", result_timeout=3.0))


def test_D_result_grid_without_this_order_is_not_success(chrome, site):
    browser, port = chrome
    _reset(browser, port, f"{site}/purchase.html")
    assert _failed(_lookup(browser, "__OTHERONLY__", result_timeout=3.0))


def test_F_detail_failure_keeps_the_existing_partial_contract(chrome, site):
    browser, port = chrome
    _reset(browser, port, f"{site}/purchase.html")
    result = _lookup(browser, "__BADDETAIL__", detail_timeout=2.0)
    assert result["found"] is True
    assert result["data"]["dps_sales_number"] == BAD_SALES
    assert result["status"] == "RESULT_FOUND_DETAIL_PARTIAL"
    assert result["detail_lookup"]["status"] == "DETAIL_OPEN_FAILED"
    assert result["installation_date"] is None
    normalized = normalize_dps_result(result, order_id="__BADDETAIL__", elapsed_seconds=1)
    assert normalized["installation_date"] is None
    assert "DPS_REQUIRED_DATE_MISSING" in normalized["warnings"]


def test_G_previous_other_order_row_is_not_this_result(chrome, site):
    browser, port = chrome
    _reset(browser, port, f"{site}/purchase.html")
    _lookup(browser, "__OTHERONLY__", result_timeout=2.0)      # leaves OTHER's row
    result = _lookup(browser, ORDER)
    assert result["found"] and result["data"]["dps_sales_number"] == SALES


def test_G_previous_same_order_row_is_not_reused(chrome, site):
    browser, port = chrome
    _reset(browser, port, f"{site}/purchase.html")
    assert _lookup(browser, ORDER)["found"] is True            # leaves ORDER's row
    _top_eval(browser, "window.__freeze = true")                # 조회 now does nothing
    try:
        result = _lookup(browser, ORDER, result_timeout=3.0)
    finally:
        _top_eval(browser, "window.__freeze = false")
    assert _failed(result), result


@pytest.mark.parametrize("order,sales,models,date,rows", [
    ("__H1__", "3100000001", ["LH50BEHHLGFXKR"], "2026-09-04", 1),
    ("__H2__", "3100000002", ["LS32DM500EKXKR"], "2026-09-02", 1),
    ("__H3__", "3100000003", ["HA-MTS1S43WHT", "HA-MTSHELFWHT", "LH43BEHHLGFXKR"],
     "2026-09-09", 3),
])
def test_H_server_representative_contracts(chrome, site, order, sales, models, date, rows):
    browser, port = chrome
    _reset(browser, port, f"{site}/purchase.html")
    result = _lookup(browser, order)
    normalized = normalize_dps_result(result, order_id=order, elapsed_seconds=1)
    assert normalized["lookup_status"] == "SUCCESS"
    assert normalized["sales_number"] == sales
    assert normalized["required_delivery_date"] == date
    assert normalized["installation_date"] == date
    assert normalized["required_delivery_date_row_count"] == rows
    assert sorted(i["model_name"] for i in result["data"]["detail_items"]) == sorted(models)


def test_invariant_label_pairs_without_a_row_never_succeed():
    """The server shape, fed straight to the reader: texts but no order row."""

    ticks = iter(range(0, 10_000))
    page = FakePage(_script(lambda: {
        "raw_result_texts": ["판매번호", "기간", "품명", "인수자명", "배송상태", "전체 구매요청 설치완료"],
        "table_headers": [], "table_rows": [], "loading": False}))
    result = CdpDpsReader(FakeBrowser([DPS_TARGET], page), result_timeout=5,
                          clock=lambda: float(next(ticks)), sleep=lambda s: None).perform_lookup(
        order_id=ORDER, dps_period_start="2026-09-01", dps_period_end="2026-09-21")
    assert result["success"] is False and result["code"] == "SEARCH_RESULT_TIMEOUT"


def test_invariant_order_row_without_a_sales_number_is_not_success():
    page = FakePage(_script(lambda: {
        "raw_result_texts": [ORDER, "LH43BEFHLGFXKR"],
        "table_headers": ["온라인판매 주문번호", "모델명"],
        "table_rows": [[ORDER, "LH43BEFHLGFXKR"]], "loading": False}))
    result = CdpDpsReader(FakeBrowser([DPS_TARGET], page), poll_interval=0).perform_lookup(
        order_id=ORDER, dps_period_start="2026-09-01", dps_period_end="2026-09-21")
    assert result["success"] is False
    assert result["code"] == "DPS_SALES_NUMBER_MISSING"


def _compare_module():
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "scripts" / "dps_cdp_compare.py"
    spec = importlib.util.spec_from_file_location("dps_cdp_compare", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_A_execute_stops_when_preflight_cannot_see_the_purchase_tab(tmp_path, monkeypatch):
    module = _compare_module()
    calls = []
    monkeypatch.setattr(module, "preflight", lambda port: {
        "cdp_purchase_tab": False, "cdp_tab_code": "DPS_TAB_NOT_FOUND"})
    monkeypatch.setattr(module, "lookup_dps_order_cdp", lambda **kw: calls.append("CDP"))
    import services.dps_agent_client as agent
    monkeypatch.setattr(agent, "lookup_dps_order", lambda **kw: calls.append("OLD"))
    orders = tmp_path / "orders.csv"
    orders.write_text("order_id,order_date\n2026090100000001,2026-09-01\n", encoding="utf-8")
    out = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", ["x", "--csv", str(orders), "--execute", "--out", str(out)])

    assert module.main() == 2
    assert calls == []
    report = json.loads(out.read_bytes().decode("ascii"))
    assert report["decision"] == "SWITCH_BLOCKED_PREFLIGHT"
    assert report["blocked_reason"].startswith("CDP_PURCHASE_TAB_NOT_READY")


def test_report_json_is_ascii_and_round_trips(tmp_path):
    module = _compare_module()
    report = {"old": {"delivery_status": "전체 구매요청 설치완료", "product_name": "인수자명"}}
    out = tmp_path / "r.json"
    module.write_report(str(out), report)
    raw = out.read_bytes()
    assert all(byte < 128 for byte in raw)          # identical under CP949 or UTF-8
    assert json.loads(raw.decode("cp949")) == report


# ======================================================================
# Live DOM sales-number contract (server 2026-09-21): headers are empty and
# the sale is named by the row's go_sendSearchMain('<판매번호>') link.
# ======================================================================


def _row_link_counts(browser):
    return _top_eval(browser, "[window.__detailOpens, window.__transferOpens]")


def test_live_A_B_sales_number_is_the_go_sendSearchMain_argument(chrome, site):
    browser, port = chrome
    _reset(browser, port, f"{site}/purchase.html")
    result = _lookup(browser, ORDER)
    assert result["data"]["dps_sales_number"] == SALES
    assert result["data"]["dps_sales_number"] != TRANSFER         # B
    assert result["diagnostics"]["sales_link_candidates"] == 1
    assert result["table_headers"] == []                           # as live


def test_live_C_F_digits_without_the_sales_link_are_not_guessed(chrome, site):
    browser, port = chrome
    _reset(browser, port, f"{site}/purchase.html")
    result = _lookup(browser, "__NOSEND__")      # search area has a decoy link
    assert result["success"] is False
    assert result["code"] == "DPS_SALES_NUMBER_MISSING"
    assert _row_link_counts(browser) == [0, 0]


def test_live_D_sales_link_in_another_orders_row_is_not_used(chrome, site):
    browser, port = chrome
    _reset(browser, port, f"{site}/purchase.html")
    result = _lookup(browser, "__SENDOTHERONLY__")
    assert result["code"] == "DPS_SALES_NUMBER_MISSING"


def test_live_E_click_opens_only_the_sales_link(chrome, site):
    browser, port = chrome
    _reset(browser, port, f"{site}/purchase.html")
    result = _lookup(browser, ORDER)
    assert result["status"] == "RESULT_FOUND_WITH_DETAIL"
    assert _row_link_counts(browser) == [1, 0]    # never go_transterPop


def test_live_two_different_sales_links_are_not_chosen_between(chrome, site):
    browser, port = chrome
    _reset(browser, port, f"{site}/purchase.html")
    result = _lookup(browser, "__TWOSEND__")
    assert result["code"] == "DPS_SALES_NUMBER_MISSING"
    assert result["diagnostics"]["sales_link_candidates"] == 2


def test_live_link_text_disagreeing_with_its_argument_is_rejected(chrome, site):
    browser, port = chrome
    _reset(browser, port, f"{site}/purchase.html")
    assert _lookup(browser, "__TEXTMISMATCH__")["code"] == "DPS_SALES_NUMBER_MISSING"


def test_live_onclick_quote_forms_are_both_read():
    """go_sendSearchMain('…') and go_sendSearchMain("…") -- the JS pattern."""

    import re as _re
    from dps.cdp_backend import _PRELUDE

    pattern = _re.search(r"const SEND_SALES=/(.+)/;", _PRELUDE).group(1)
    js = _re.compile(pattern.replace("\\/", "/"))
    for onclick in ("parent.go_sendSearchMain('3141538283');",
                    'parent.go_sendSearchMain("3141538283");',
                    "parent.go_sendSearchMain( '3141538283' )"):
        assert js.search(onclick).group(2) == "3141538283"
    assert js.search("parent.go_transterPop('3141538283');") is None


# ======================================================================
# Live 판매상세 (sd010_0050): TD.theadFree headers, TD.tcontentFree data,
# split nested header/body tables. Read the way DpsUiAutomation._detail_table
# reads it -- by position, not by <th>.
# ======================================================================


def test_live_detail_A_B_C_td_header_and_content_map_every_item_field(chrome, site):
    browser, port = chrome
    _reset(browser, port, f"{site}/purchase.html")
    result = _lookup(browser, "__L1__")
    normalized = normalize_dps_result(result, order_id="__L1__", elapsed_seconds=1)
    assert result["status"] == "RESULT_FOUND_WITH_DETAIL", result["detail_lookup"]
    assert result["diagnostics"]["detail_raw_headers"] == ["행번", "모델", "수량", "판매금액", "요구납기일"]
    [item] = result["data"]["detail_items"]
    assert (item["model_name"], item["quantity"], item["sale_amount"]) == (
        "LH50BEHHLGFXKR", 1, "1,234,000")
    assert normalized["required_delivery_date"] == "2026-09-04"
    assert normalized["installation_date"] == "2026-09-04"   # never the 고객정보 2026-10-30
    assert normalized["date_parse_status"] == "PARSED"
    assert normalized["required_delivery_date_row_count"] == 1


def test_live_detail_D_E_F_nested_tables_duplicate_dom_and_three_real_items(chrome, site):
    browser, port = chrome
    _reset(browser, port, f"{site}/purchase.html")
    result = _lookup(browser, "__L3__")
    normalized = normalize_dps_result(result, order_id="__L3__", elapsed_seconds=1)
    models = [item["model_name"] for item in result["data"]["detail_items"]]
    assert models == ["HA-MTS1S43WHT", "HA-MTSHELFWHT", "LH43BEHHLGFXKR"]   # 3, not 6
    assert normalized["required_delivery_date_row_count"] == 3
    assert normalized["installation_date"] == "2026-09-09"


def test_live_detail_G_customer_info_without_items_is_not_with_detail(chrome, site):
    browser, port = chrome
    _reset(browser, port, f"{site}/purchase.html")
    result = _lookup(browser, "__L4__")
    assert result["status"] == "RESULT_FOUND_DETAIL_PARTIAL"
    assert result["detail_lookup"]["status"] == "DETAIL_PARSE_FAILED"
    assert result["data"]["detail_items"] == []
    normalized = normalize_dps_result(result, order_id="__L4__", elapsed_seconds=1)
    assert normalized["installation_date"] is None
    assert normalized["date_parse_status"] == "MISSING"


def test_live_detail_H_I_recipient_and_sales_number_label_value(chrome, site):
    browser, port = chrome
    _reset(browser, port, f"{site}/purchase.html")
    result = _lookup(browser, "__L1__")
    assert result["data"]["recipient_name"] == "테스트인수자B"
    assert result["data"]["dps_sales_number"] == "3141000001"
    assert result["detail_lookup"]["status"] == "DETAIL_CLOSED"   # detail 판매번호 == clicked


def test_item_table_is_production_detail_table_arithmetic():
    """detail_item_table on records alone: header band, centre-in-span, digits."""

    from dps.cdp_backend import detail_item_table

    def cell(name, left, top, kind="DataItem", width=100):
        return {"name": name, "control_type": kind, "left": left, "right": left + width,
                "top": top, "bottom": top + 20}

    records = [
        cell("요구납기일", 400, 10), cell("2026-10-30", 500, 10),       # customer row
        cell("모델", 0, 100), cell("수량", 100, 100), cell("요구납기일", 200, 100),
        cell("모델", 0, 100),                                              # cloned header
        cell("LH50BEHHLGFXKR", 0, 130), cell("1", 100, 130), cell("2026-09-04", 200, 130),
        cell("LH50BEHHLGFXKR", 0, 130),                                    # cloned row
        cell("합계", 0, 160, width=60),                                   # no digit: dropped
        cell("메모입력", 0, 190, kind="Edit"),                             # Edit: not a cell
    ]
    headers, rows = detail_item_table(records)
    assert headers == ["모델", "수량", "요구납기일"]
    assert rows == [["LH50BEHHLGFXKR", "1", "2026-09-04"]]
    assert detail_item_table([cell("모델", 0, 0), cell("수량", 100, 0)]) == ([], [])  # < 3 labels
