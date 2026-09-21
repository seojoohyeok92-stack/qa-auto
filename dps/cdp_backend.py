"""Read-only DPS lookup over Chrome DevTools Protocol (shadow backend).

The pywinauto agent (``dps/agent_server.py`` -> ``DpsUiAutomation``) stays the
production path. This module reads the *same* screens through the page DOM
instead of Windows UI Automation, and hands what it reads to the *same* pure
parsers, so the two backends can differ only in how the DOM was reached:

  grid     DpsUiAutomation.parse_lookup_result   (snapshot -> list data)
  detail   dps.sales_detail.parse_flat_detail    (label/value records)
  date     dps.sales_detail.merge_list_and_detail (품목상세내역 요구납기일)

Screens are the ones the current contract uses, identified by content, not by
an assumed URL: 판매 > 온라인판매 > 구매요청리스트 ("온라인판매 주문번호" input)
and the 판매조회 detail (SSearchSalesMain / sd010_0048).

Read only. The page is driven the way a person drives it -- type the order
number and period, press 조회, open the 판매번호 link, read, close -- and the
DPS page's own scripts make every DPS request. Nothing here builds a DPS HTTP
request, reads cookies, or touches a control whose label is a write action.
Login is always the operator's: a login page is reported, never filled.

Chrome must be started by the operator with remote debugging on a dedicated
profile (see ``chrome_launch_command``); an ordinary Chrome window cannot be
attached to, and this module never starts, stops or reuses one.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

from dps.dates import (
    calculate_dps_lookup_period,
    select_dps_date_source,
    validate_dps_lookup_period,
)
from dps.dps_ui_automation import NO_RESULT_MARKERS, DpsUiAutomation
from dps.sales_detail import merge_list_and_detail, parse_flat_detail

DEFAULT_CDP_PORT = 9333
AUTOMATION_METHOD = "CHROME_CDP_DOM_V1"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# The purchase-request list the current pywinauto path drives.
PURCHASE_PAGE_TEXT_MARKERS = ("온라인판매 주문번호", "구매요청리스트")
DETAIL_URL_MARKERS = ("sd010_0048", "ssearchsalesmain")
DETAIL_TITLE_MARKERS = ("판매조회",)
DETAIL_TEXT_MARKER = "품목상세내역"
LOGIN_URL_MARKERS = ("login.do", "/login", "otp")


def chrome_launch_command(
    chrome_path: str, *, port: int = DEFAULT_CDP_PORT, profile_dir: str,
) -> list[str]:
    """The command an operator runs once to open the DPS Chrome for CDP.

    A dedicated ``--user-data-dir`` keeps it apart from the operator's own
    Chrome (which cannot be attached to while it runs without debugging), and
    the port binds to loopback only. The operator then logs in to DPS in that
    window by hand, OTP included.
    """

    return [
        chrome_path,
        f"--remote-debugging-port={int(port)}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]


class CdpError(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


# ---------------------------------------------------------------------------
# Transport: /json endpoints and one page WebSocket, loopback only, stdlib.
# ---------------------------------------------------------------------------


def _require_loopback(host: str) -> None:
    if host not in LOOPBACK_HOSTS:
        raise CdpError("CDP_NON_LOOPBACK_REFUSED", f"refused host {host!r}")


class _WebSocket:
    """Minimal RFC 6455 client: text frames, ping/pong, close. No TLS."""

    def __init__(self, url: str, *, timeout: float) -> None:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        _require_loopback(host)
        self.sock = socket.create_connection((host, parsed.port or 80), timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        self.sock.sendall((
            f"GET {path} HTTP/1.1\r\nHost: {host}:{parsed.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CdpError("CDP_HANDSHAKE_FAILED")
            response += chunk
        head, self._buffer = response.split(b"\r\n\r\n", 1)
        accept = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
        ).digest()).decode()
        if b" 101 " not in head.split(b"\r\n", 1)[0] or accept.encode() not in head:
            raise CdpError("CDP_HANDSHAKE_FAILED")

    def _read(self, size: int) -> bytes:
        while len(self._buffer) < size:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise CdpError("DPS_TAB_CLOSED", "CDP socket closed")
            self._buffer += chunk
        data, self._buffer = self._buffer[:size], self._buffer[size:]
        return data

    def send_text(self, text: str, opcode: int = 0x1) -> None:
        payload = text.encode() if isinstance(text, str) else text
        header = bytes([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", length)
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def recv_text(self, timeout: float) -> str:
        self.sock.settimeout(max(0.05, timeout))
        message = b""
        while True:
            first, second = self._read(2)
            opcode, length = first & 0x0F, second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            if second & 0x80:
                mask = self._read(4)
                payload = bytes(
                    byte ^ mask[index % 4]
                    for index, byte in enumerate(self._read(length))
                )
            else:
                payload = self._read(length)
            if opcode == 0x8:
                raise CdpError("DPS_TAB_CLOSED", "CDP close frame")
            if opcode == 0x9:
                self.send_text(payload.decode(errors="ignore"), opcode=0xA)
                continue
            if opcode in (0x0, 0x1, 0x2):
                message += payload
                if first & 0x80:
                    return message.decode()

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class CdpPage:
    """One page target: commands, events, and JavaScript dialogs."""

    def __init__(self, websocket_url: str, *, timeout: float = 10.0) -> None:
        self.ws = _WebSocket(websocket_url, timeout=timeout)
        self._next_id = 0
        self.dialogs: list[dict[str, Any]] = []
        self.send("Page.enable")
        self.send("Runtime.enable")

    def _handle_event(self, message: dict[str, Any]) -> None:
        if message.get("method") != "Page.javascriptDialogOpening":
            return
        params = dict(message.get("params") or {})
        kind = str(params.get("type") or "")
        # An alert only informs. Anything that asks for consent is refused:
        # a read lookup never has a reason to confirm anything.
        accept = kind == "alert"
        self.dialogs.append({"type": kind, "message": str(params.get("message") or "")[:200],
                             "accepted": accept})
        self._send_raw("Page.handleJavaScriptDialog", {"accept": accept})

    def _send_raw(self, method: str, params: dict[str, Any] | None = None) -> int:
        self._next_id += 1
        self.ws.send_text(json.dumps(
            {"id": self._next_id, "method": method, "params": params or {}}
        ))
        return self._next_id

    def send(self, method: str, params: dict[str, Any] | None = None,
             *, timeout: float = 10.0) -> dict[str, Any]:
        command_id = self._send_raw(method, params)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CdpError("CDP_COMMAND_TIMEOUT", method)
            try:
                message = json.loads(self.ws.recv_text(remaining))
            except socket.timeout as error:
                raise CdpError("CDP_COMMAND_TIMEOUT", method) from error
            if message.get("id") == command_id:
                if "error" in message:
                    raise CdpError("CDP_COMMAND_FAILED", str(message["error"])[:200])
                return dict(message.get("result") or {})
            self._handle_event(message)

    def evaluate(self, expression: str, *, timeout: float = 10.0) -> Any:
        result = self.send("Runtime.evaluate", {
            "expression": expression, "returnByValue": True,
            "awaitPromise": True, "userGesture": True,
        }, timeout=timeout)
        if result.get("exceptionDetails"):
            details = dict(result["exceptionDetails"])
            described = dict(details.get("exception") or {}).get("description")
            raise CdpError("DOM_SCRIPT_FAILED",
                           str(described or details.get("text") or "")[:300])
        return dict(result.get("result") or {}).get("value")

    def close(self) -> None:
        self.ws.close()


class CdpBrowser:
    def __init__(self, *, host: str = "127.0.0.1", port: int = DEFAULT_CDP_PORT,
                 timeout: float = 3.0) -> None:
        _require_loopback(host)
        self.base = f"http://{host}:{int(port)}"
        self.timeout = timeout

    def _get(self, path: str) -> Any:
        try:
            with urllib.request.urlopen(self.base + path, timeout=self.timeout) as response:
                return json.loads(response.read().decode() or "null")
        except (OSError, ValueError) as error:
            raise CdpError("CHROME_NOT_FOUND",
                           "DPS용 Chrome(remote debugging)에 연결할 수 없습니다.") from error

    def version(self) -> dict[str, Any]:
        return dict(self._get("/json/version") or {})

    def pages(self) -> list[dict[str, Any]]:
        return [dict(item) for item in (self._get("/json/list") or [])
                if isinstance(item, dict) and item.get("type") == "page"]

    def close_page(self, target_id: str) -> bool:
        try:
            with urllib.request.urlopen(f"{self.base}/json/close/{target_id}",
                                        timeout=self.timeout):
                return True
        except OSError:
            return False

    def open_page(self, target: dict[str, Any]) -> CdpPage:
        url = str(target.get("webSocketDebuggerUrl") or "")
        if not url:
            raise CdpError("DPS_TAB_NOT_FOUND", "target has no debugger url")
        return CdpPage(url)


# ---------------------------------------------------------------------------
# DOM scripts. Every script walks the document and its same-origin frames,
# returns plain JSON, and clicks at most one element it has named exactly.
# ---------------------------------------------------------------------------

_PRELUDE = r"""
const WRITE_WORDS = ['저장','등록','생성','수정','삭제','확정','승인','취소','발행','전송','출력'];
function docs(){const out=[];const walk=(d,ox,oy)=>{out.push({d,ox,oy});
  for(const f of d.querySelectorAll('iframe,frame')){try{const r=f.getBoundingClientRect();
   if(f.contentDocument) walk(f.contentDocument,ox+r.left,oy+r.top);}catch(e){}}};
  walk(document,0,0);return out;}
function norm(s){return String(s||'').replace(/\s+/g,' ').trim();}
function txt(el){if(!el)return '';const t=el.tagName;
  if(t==='INPUT'||t==='TEXTAREA')return norm(el.value);
  if(t==='SELECT'){const o=el.options[el.selectedIndex];return norm(o?o.text:'');}
  return norm(el.innerText!==undefined?el.innerText:el.textContent);}
function vis(el){const r=el.getBoundingClientRect();if(r.width<=0||r.height<=0)return false;
  const s=el.ownerDocument.defaultView.getComputedStyle(el);return s.visibility!=='hidden'&&s.display!=='none';}
function labelOf(el){const d=el.ownerDocument;let parts=[];
  if(el.id){const l=d.querySelector('label[for="'+el.id+'"]');if(l)parts.push(txt(l));}
  parts.push(el.getAttribute('aria-label')||'',el.getAttribute('title')||'',el.getAttribute('placeholder')||'');
  const cell=el.closest('td,th,dd,div');if(cell){let p=cell.previousElementSibling;
   for(let i=0;p&&i<2;i++,p=p.previousElementSibling){const t=txt(p);if(t&&t.length<30){parts.push(t);break;}}}
  return norm(parts.join(' '));}
function isWrite(t){return WRITE_WORDS.some(w=>t.indexOf(w)>=0);}
"""

PAGE_STATE_JS = r"""
(()=>{const all=docs();let text='';let password=false;
 for(const {d} of all){text+=' '+norm(d.body?d.body.innerText:'');
  if(d.querySelector('input[type=password]'))password=true;}
 return {url:location.href,title:document.title,password,
  text:text.slice(0,4000),frames:all.length};})
"""

FILL_AND_QUERY_JS = r"""
((order,start,end)=>{
 const inputs=[];for(const {d} of docs()) for(const el of d.querySelectorAll('input'))
  {const ty=(el.type||'text').toLowerCase();if(['text','search','number','tel',''].includes(ty)&&vis(el)&&!el.disabled)inputs.push(el);}
 const orderCands=inputs.map(el=>({el,label:labelOf(el)})).filter(c=>
  c.label.indexOf('주문번호')>=0&&c.label.indexOf('상품주문번호')<0&&c.label.indexOf('판매번호')<0&&c.label.indexOf('SVC')<0);
 const best=orderCands.filter(c=>c.label.indexOf('온라인판매 주문번호')>=0);
 const pick=best.length?best:orderCands;
 if(pick.length!==1)return {ok:false,code:pick.length?'ORDER_INPUT_AMBIGUOUS':'ORDER_INPUT_NOT_FOUND',count:pick.length};
 const target=pick[0].el;
 const set=(el,v)=>{const proto=Object.getPrototypeOf(el);const desc=Object.getOwnPropertyDescriptor(proto,'value');
  el.focus();desc&&desc.set?desc.set.call(el,v):(el.value=v);
  for(const e of ['input','change','keyup'])el.dispatchEvent(new Event(e,{bubbles:true}));el.dispatchEvent(new Event('blur'));};
 const dateLike=/^\d{4}[-./]?\d{2}[-./]?\d{2}$/;
 const scope=target.closest('form,table,fieldset')||target.ownerDocument;
 const periods=Array.from(scope.querySelectorAll('input')).filter(el=>vis(el)&&el!==target&&dateLike.test(norm(el.value)));
 if(periods.length<2)return {ok:false,code:'PERIOD_INPUT_NOT_FOUND',count:periods.length};
 const fmt=(iso,sample)=>{const s=norm(sample);const sep=s.length===8?'':s[4];return iso.split('-').join(sep);};
 set(target,order);set(periods[0],fmt(start,periods[0].value));set(periods[1],fmt(end,periods[1].value));
 const buttons=[];for(const {d} of docs()) for(const el of d.querySelectorAll('button,a,input[type=button],input[type=submit],[role=button],span,img'))
  {const t=el.tagName==='IMG'?norm(el.alt):txt(el);if(t==='조회'&&vis(el)&&!isWrite(t))buttons.push(el);}
 const near=buttons.filter(b=>b.ownerDocument===target.ownerDocument);
 const button=(near.length?near:buttons)[0];
 if(!button)return {ok:false,code:'QUERY_BUTTON_NOT_FOUND'};
 const readback={order:norm(target.value),start:norm(periods[0].value),end:norm(periods[1].value)};
 button.click();
 return {ok:true,readback,button:button.tagName,order_candidates:orderCands.length};
})
"""

RESULT_SNAPSHOT_JS = r"""
((markers)=>{const texts=[];const seen=new Set();let headers=[];const rows=[];let loading=false;
 const add=t=>{if(t&&!seen.has(t)&&texts.length<240){seen.add(t);texts.push(t);}};
 for(const {d} of docs()){
  for(const el of d.querySelectorAll('[class*=loading],[id*=loading],[class*=progress],[class*=spinner]'))if(vis(el))loading=true;
  for(const el of d.querySelectorAll('div,span,p,td,li,strong')){if(!vis(el))continue;const t=txt(el);
   if(t&&t.length<80&&(markers.some(m=>t.indexOf(m)>=0)||/로딩|처리중|조회중|loading/i.test(t))){add(t);if(/로딩|처리중|조회중|loading/i.test(t))loading=true;}}
  for(const table of d.querySelectorAll('table,[role=grid]')){if(!vis(table))continue;
   const hs=Array.from(table.querySelectorAll('th,[role=columnheader]')).map(txt).filter(Boolean);
   if(hs.length&&!headers.length)headers=hs;
   for(const tr of table.querySelectorAll('tr,[role=row]')){const cells=Array.from(tr.querySelectorAll('td,[role=gridcell]'));
    if(cells.length<2)continue;const vals=cells.map(txt);const key=JSON.stringify(vals);
    if(!rows.some(r=>JSON.stringify(r)===key)&&rows.length<100){rows.push(vals);vals.forEach(add);}}}}
 return {raw_result_texts:texts,table_headers:headers.slice(0,40),table_rows:rows,loading};})
"""

SCROLL_GRID_JS = r"""
((step)=>{let moved=false;for(const {d} of docs()) for(const el of d.querySelectorAll('div,tbody'))
 {if(el.scrollHeight>el.clientHeight+4&&el.querySelector('tr,[role=row]')){
   const before=el.scrollTop;el.scrollTop=before+step;if(el.scrollTop!==before)moved=true;}}return moved;})
"""

CLICK_SALES_LINK_JS = r"""
((order,sales)=>{const hits=[];for(const {d} of docs()) for(const tr of d.querySelectorAll('tr,[role=row]'))
 {const cells=Array.from(tr.querySelectorAll('td,[role=gridcell]')).map(txt);if(!cells.includes(order))continue;
  for(const a of tr.querySelectorAll('a,[onclick],span,u')){if(txt(a)===sales&&vis(a)){hits.push(a);break;}}}
 if(!hits.length)return {ok:false,code:'DPS_SALES_LINK_NOT_FOUND',rows:0};
 hits[0].click();return {ok:true,rows:hits.length};})
"""

DETAIL_SNAPSHOT_JS = r"""
(()=>{const records=[];let headers=[];let rows=[];let marker=false;
 for(const {d,ox,oy} of docs()){
  if(norm(d.body?d.body.innerText:'').indexOf('품목상세내역')>=0)marker=true;
  for(const el of d.querySelectorAll('th,td,label,span,div,input,select,textarea,strong,li')){
   if(!vis(el)||records.length>=1500)continue;const tag=el.tagName;
   const leaf=['INPUT','SELECT','TEXTAREA'].includes(tag)||!el.querySelector('th,td,div,input,select,table,span,label');
   if(!leaf)continue;const t=txt(el);if(!t)continue;const r=el.getBoundingClientRect();
   records.push({name:t,control_type:['INPUT','SELECT','TEXTAREA'].includes(tag)?'Edit':(tag==='TD'||tag==='TH')?'DataItem':'Text',
    top:Math.round(r.top+oy),left:Math.round(r.left+ox),bottom:Math.round(r.bottom+oy),right:Math.round(r.right+ox)});}
  for(const table of d.querySelectorAll('table')){
   const hs=Array.from(table.querySelectorAll('th')).map(txt);
   if(!(hs.includes('요구납기일')&&(hs.includes('모델')||hs.includes('모델명'))))continue;
   headers=hs;rows=Array.from(table.querySelectorAll('tbody tr')).map(tr=>Array.from(tr.querySelectorAll('td')).map(txt)).filter(r=>r.length>=2);}}
 return {records,headers,rows,marker};})
"""


def _call(script: str, *args: Any) -> str:
    """One self-contained expression: the helpers and one call, in a closure.

    Nothing is left on the page's global scope, so evaluating twice never
    redeclares a name and the page's own scripts never see ours.
    """

    arguments = ", ".join(json.dumps(arg, ensure_ascii=False) for arg in args)
    return "(() => {" + _PRELUDE + "\nreturn (" + script.strip() + ")(" + arguments + ");})()"


# ---------------------------------------------------------------------------
# The lookup. Same inputs and the same result shape as
# DpsUiAutomation.perform_lookup, so the agent wrapper, the normalizer and
# DpsRepository cannot tell which backend produced it.
# ---------------------------------------------------------------------------


class _Pages(Protocol):  # what the reader needs from a browser
    def pages(self) -> list[dict[str, Any]]: ...
    def open_page(self, target: dict[str, Any]) -> Any: ...
    def close_page(self, target_id: str) -> bool: ...


@dataclass
class CdpDpsReader:
    browser: _Pages
    result_timeout: float = 20.0
    detail_timeout: float = 10.0
    poll_interval: float = 0.35
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    parser: DpsUiAutomation = field(default_factory=DpsUiAutomation)
    # The agent's own host rule (DPS_ALLOWED_HOSTS, default dps2u.co.kr).
    allowed_hosts: tuple[str, ...] = field(default_factory=lambda: tuple(
        value.strip().casefold().lstrip(".")
        for value in os.getenv("DPS_ALLOWED_HOSTS", "dps2u.co.kr").split(",")
        if value.strip()
    ))

    def _allowed(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").casefold()
        return any(host == allowed or host.endswith("." + allowed)
                   for allowed in self.allowed_hosts)

    @staticmethod
    def _failure(code: str, message: str, diagnostics: dict[str, Any] | None = None
                 ) -> dict[str, Any]:
        return {"ok": False, "success": False, "code": code, "status": code,
                "message": message, "automation_method": AUTOMATION_METHOD,
                "diagnostics": dict(diagnostics or {})}

    def _purchase_page(self) -> tuple[dict[str, Any] | None, Any, dict[str, Any]]:
        pages = self.browser.pages()
        if not pages:
            return None, None, {"code": "DPS_TAB_NOT_FOUND"}
        login_seen = False
        for target in pages:
            url = str(target.get("url") or "").casefold()
            if not self._allowed(url):
                continue
            page = self.browser.open_page(target)
            state = dict(page.evaluate(_call(PAGE_STATE_JS)) or {})
            if state.get("password") or any(m in url for m in LOGIN_URL_MARKERS):
                login_seen = True
                page.close()
                continue
            if any(marker in str(state.get("text") or "") for marker in PURCHASE_PAGE_TEXT_MARKERS):
                return target, page, state
            page.close()
        return None, None, {"code": "DPS_LOGIN_REQUIRED" if login_seen else "DPS_TAB_NOT_FOUND"}

    def _wait_for_result(self, page: Any, before: dict[str, Any], expected: str
                         ) -> dict[str, Any]:
        before_signature = DpsUiAutomation._result_signature(before)
        deadline = self.clock() + max(1.0, self.result_timeout)
        latest, stable, scrolled = before, 0, 0
        while self.clock() < deadline:
            self.sleep(self.poll_interval)
            latest = dict(page.evaluate(_call(RESULT_SNAPSHOT_JS, list(NO_RESULT_MARKERS))) or {})
            folded = "\n".join(latest.get("raw_result_texts") or []).casefold()
            if any(marker.casefold() in folded for marker in NO_RESULT_MARKERS):
                return {"status": "no_result", "snapshot": latest}
            rows = latest.get("table_rows") or []
            exact = any(expected in [str(v).strip() for v in row] for row in rows)
            if latest.get("loading"):
                stable = 0
                continue
            if exact:
                stable += 1
                if stable >= 2:
                    return {"status": "complete", "snapshot": latest}
                continue
            stable = 0
            changed = DpsUiAutomation._result_signature(latest) != before_signature
            # A virtual grid renders only what is on screen; bring the rest in
            # before deciding the order is not there.
            if changed and rows and scrolled < 30 and page.evaluate(_call(SCROLL_GRID_JS, 400)):
                scrolled += 1
                continue
            if changed and rows:
                return {"status": "complete", "snapshot": latest}
        return {"status": "timeout", "snapshot": latest}

    def _open_detail(self, page: Any, known_ids: set[str], order: str, sales: str
                     ) -> tuple[Any, str | None, dict[str, Any]]:
        clicked = dict(page.evaluate(_call(CLICK_SALES_LINK_JS, order, sales)) or {})
        if not clicked.get("ok"):
            return None, None, {"status": clicked.get("code") or "DPS_SALES_LINK_NOT_FOUND",
                                "invocation_count": 0}
        deadline = self.clock() + self.detail_timeout
        while self.clock() < deadline:
            for target in self.browser.pages():
                target_id = str(target.get("id") or "")
                url = str(target.get("url") or "").casefold()
                title = str(target.get("title") or "")
                if target_id not in known_ids and (
                    any(m in url for m in DETAIL_URL_MARKERS)
                    or any(m in title for m in DETAIL_TITLE_MARKERS)
                ):
                    return self.browser.open_page(target), target_id, {
                        "status": "DETAIL_OPENED", "window_form": "NEW_WINDOW",
                        "invocation_count": 1}
            same = dict(page.evaluate(_call(DETAIL_SNAPSHOT_JS)) or {})
            if same.get("marker") and same.get("headers"):
                return page, None, {"status": "DETAIL_OPENED",
                                    "window_form": "SAME_WINDOW_OR_MODAL",
                                    "invocation_count": 1}
            self.sleep(self.poll_interval)
        return None, None, {"status": "DETAIL_OPEN_FAILED", "invocation_count": 1}

    def perform_lookup(
        self, *, order_id: str, dps_period_start: str, dps_period_end: str,
        product_order_id: str | None = None, dps_date_source: str | None = None,
        dps_reference_date: str | None = None,
    ) -> dict[str, Any]:
        order = str(order_id or "").strip()
        if not order:
            return self._failure("DPS_ORDER_ID_MISSING", "네이버 주문번호가 없어 DPS 조회를 실행할 수 없습니다.")
        page = None
        detail_page = None
        dialogs: list[dict[str, Any]] = []
        try:
            target, page, state = self._purchase_page()
            if page is None:
                code = state.get("code") or "DPS_TAB_NOT_FOUND"
                return self._failure(code, "DPS 구매요청리스트 탭을 찾지 못했거나 로그인이 필요합니다.")
            known_ids = {str(item.get("id") or "") for item in self.browser.pages()}
            before = dict(page.evaluate(_call(RESULT_SNAPSHOT_JS, list(NO_RESULT_MARKERS))) or {})
            filled = dict(page.evaluate(_call(FILL_AND_QUERY_JS, order, dps_period_start, dps_period_end)) or {})
            dialogs.extend(getattr(page, "dialogs", []) or [])
            if any(d.get("type") != "alert" for d in dialogs):
                return self._failure("DIALOG_CONFIRM_REFUSED", "조회 중 확인 대화상자가 떠 중단했습니다.",
                                     {"dialogs": dialogs})
            if not filled.get("ok"):
                return self._failure(str(filled.get("code") or "ORDER_INPUT_FAILED"),
                                     "DPS 조회 입력을 준비하지 못했습니다.", {"fill": filled})
            readback = dict(filled.get("readback") or {})
            if readback.get("order") != order:
                return self._failure("INPUT_VERIFY_FAILED", "입력한 주문번호를 확인하지 못했습니다.")
            polling = self._wait_for_result(page, before, order)
            after = dict(polling["snapshot"])
            if polling["status"] == "timeout":
                return self._failure("SEARCH_RESULT_TIMEOUT", "DPS 조회 결과 로딩 시간이 초과되었습니다.")
            common = dict(
                naver_order_id=order, order_id=order, product_order_id=product_order_id,
                dps_query_value=order, dps_query_value_type="order_id",
                dps_date_source=dps_date_source, dps_reference_date=dps_reference_date,
                dps_period_start=dps_period_start, dps_period_end=dps_period_end,
            )
            parsed = self.parser.parse_lookup_result(after, **common)
            diagnostics = {"query_invocation_count": 1, "result_parser_version": "v2",
                           "dialogs": dialogs, "dps_input_verified_value": order,
                           **parsed.get("diagnostics", {})}
            base = {"automation_method": AUTOMATION_METHOD,
                    "raw_result_texts": after.get("raw_result_texts") or [],
                    "table_headers": after.get("table_headers") or [],
                    "table_rows": after.get("table_rows") or []}
            if polling["status"] == "no_result":
                return {"ok": True, "success": True, "found": False, "code": "NO_DPS_RESULT",
                        "status": "NO_DPS_RESULT", "message": "해당 주문번호의 DPS 조회 결과가 없습니다.",
                        "data": {**parsed["data"], "naver_order_id": order},
                        "diagnostics": diagnostics, **base}
            if not parsed["found"]:
                return self._failure("LOOKUP_RESULT_NOT_FOUND",
                                     "조회는 완료했지만 결과 항목을 해석하지 못했습니다.", diagnostics)
            detail_lookup = {"attempted": False, "opened": False, "parsed": False,
                             "closed": False, "status": "NOT_ATTEMPTED", "invocation_count": 0}
            detail = None
            sales = str(parsed["data"].get("dps_sales_number") or "").strip()
            if sales:
                detail_lookup["attempted"] = True
                detail_page, detail_id, opened = self._open_detail(page, known_ids, order, sales)
                detail_lookup.update(opened)
                if detail_page is not None:
                    detail_lookup["opened"] = True
                    snapshot = dict(detail_page.evaluate(_call(DETAIL_SNAPSHOT_JS)) or {})
                    detail = parse_flat_detail(snapshot.get("records") or [],
                                               table_headers=snapshot.get("headers") or [],
                                               table_rows=snapshot.get("rows") or [])
                    detail_lookup["parsed"] = bool(detail.get("customer_info") or detail.get("detail_items"))
                    detail_lookup["status"] = "DETAIL_PARSED" if detail_lookup["parsed"] else "DETAIL_PARSE_FAILED"
                    diagnostics.update({"detail_raw_headers": snapshot.get("headers"),
                                        "detail_raw_rows": snapshot.get("rows")})
                    if detail_id is not None:
                        if detail_page is not page:
                            detail_page.close()
                            detail_page = None
                        detail_lookup["closed"] = self.browser.close_page(detail_id)
                        detail_lookup["close_method"] = "CDP_CLOSE_TARGET"
                    else:
                        detail_lookup["closed"] = True
                        detail_lookup["close_method"] = "SAME_WINDOW"
                    if detail_lookup["closed"] and detail_lookup["parsed"]:
                        detail_lookup["status"] = "DETAIL_CLOSED"
            else:
                detail_lookup["status"] = "DPS_SALES_NUMBER_MISSING"
            merged = merge_list_and_detail(parsed["data"], detail, detail_lookup=detail_lookup)
            status = "RESULT_PARSE_PARTIAL" if parsed.get("diagnostics", {}).get("parse_warnings") else "RESULT_FOUND"
            if detail_lookup["parsed"]:
                if merged.get("delivery_date_status") in {"DATE_CONFLICT", "MULTIPLE_DATES", "PARTIALLY_CONFIRMED"}:
                    status = "DETAIL_DATE_CONFLICT"
                elif detail_lookup["closed"]:
                    status = "RESULT_FOUND_WITH_DETAIL"
                else:
                    status = "DETAIL_CLOSE_FAILED"
            elif detail_lookup["attempted"] or not sales:
                status = "RESULT_FOUND_DETAIL_PARTIAL"
            result = {"ok": True, "success": True, "found": True, "code": "LOOKUP_COMPLETE",
                      "status": status, "message": "DPS 조회와 결과 수집을 완료했습니다.",
                      "data": merged, "detail_lookup": detail_lookup, "diagnostics": diagnostics,
                      "detail_items": merged.get("detail_items", []), **base}
            for key in ("requested_delivery_date", "required_delivery_date", "installation_date",
                        "installation_date_source", "raw_required_delivery_date", "date_parse_status",
                        "requires_human_review", "required_delivery_date_row_count",
                        "delivery_scheduled_date", "delivery_date_source", "delivery_date_status",
                        "delivery_time", "order_amount"):
                result[key] = merged.get(key)
            result.update(DpsUiAutomation._legacy_result_aliases(merged))
            return result
        except CdpError as error:
            return self._failure(error.code, str(error))
        finally:
            for value in (detail_page, page):
                if value is not None:
                    try:
                        value.close()
                    except Exception:  # noqa: BLE001
                        pass


def lookup_dps_order_cdp(
    naver_order_id: str | None = None, *, reader: CdpDpsReader | None = None,
    request_id: str | None = None, order_id: str | None = None,
    product_order_id: str | None = None, dps_query_value: str | None = None,
    dps_query_value_type: str | None = None, order_date: str | None = None,
    order_created_at: str | None = None, payment_date: str | None = None,
    payment_completed_at: str | None = None, place_order_date: str | None = None,
    shipping_due_date: str | None = None, **_: Any,
) -> dict[str, Any]:
    """Drop-in for ``services.dps_agent_client.lookup_dps_order``.

    Same identifier guards and the same period the agent computes
    (``select_dps_date_source`` -> ``calculate_dps_lookup_period``), then the
    CDP reader, then the agent's identifier echo. Not wired as a default
    anywhere; ``DpsEnrichmentService(client=...)`` and the comparison script
    are its only callers.
    """

    normalized = str(order_id or naver_order_id or "").strip()
    query = str(dps_query_value or normalized).strip()
    query_type = str(dps_query_value_type or ("order_id" if normalized else "")).strip()
    fail = CdpDpsReader._failure
    if not normalized:
        return fail("DPS_ORDER_ID_MISSING", "네이버 주문번호가 없어 DPS 조회를 실행할 수 없습니다.")
    if query_type != "order_id":
        return fail("INVALID_DPS_QUERY_TYPE", "상품주문번호가 DPS 조회값으로 전달되어 중단했습니다.")
    if query != normalized:
        return fail("CLIENT_ORDER_ID_MISMATCH", "요청 주문번호가 서로 다릅니다.")
    selected = select_dps_date_source({
        "order_date": order_date, "order_created_at": order_created_at,
        "payment_date": payment_date, "payment_completed_at": payment_completed_at,
        "place_order_date": place_order_date, "shipping_due_date": shipping_due_date,
    })
    if selected.reference_date is None:
        return fail("DATE_SOURCE_MISSING", "주문일을 확인하지 못해 DPS 조회 기간을 계산할 수 없습니다.")
    period = calculate_dps_lookup_period(selected.reference_date.isoformat())
    start, end = period.start.isoformat(), period.end.isoformat()
    valid, code, _, _ = validate_dps_lookup_period(start, end)
    if not valid:
        return fail(code, "DPS 조회 기간이 안전 조건을 충족하지 않습니다.")
    reader = reader or CdpDpsReader(CdpBrowser())
    result = reader.perform_lookup(
        order_id=normalized, dps_period_start=start, dps_period_end=end,
        product_order_id=product_order_id, dps_date_source=selected.source,
        dps_reference_date=selected.reference_date.isoformat(),
    )
    verified = dict(result.get("diagnostics") or {}).get("dps_input_verified_value")
    result.update({
        "request_id": request_id, "order_id": normalized, "received_order_id": normalized,
        "received_dps_query_value": query,
        "executed_dps_query_value": query if result.get("success") else None,
        "dps_query_value": query, "dps_query_value_type": query_type,
        "dps_date_source": selected.source, "dps_reference_date": selected.reference_date.isoformat(),
        "dps_period_start": start, "dps_period_end": end,
        "queried_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "cached": False, "cache_hit": False,
    })
    if result.get("success"):
        all_match = normalized == query == str(verified or "").strip()
        result["all_identifiers_match"] = all_match
        if not all_match:
            return fail("REQUEST_CONTEXT_MISMATCH", "DPS 실행 식별자가 요청과 일치하지 않습니다.")
    return result
