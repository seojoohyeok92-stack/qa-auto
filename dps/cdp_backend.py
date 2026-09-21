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
import re
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
from dps.sales_detail import (
    ITEM_FIELDS,
    canonical_detail_label,
    merge_list_and_detail,
    normalize_label,
    parse_flat_detail,
)

DEFAULT_CDP_PORT = 9333
# The shape the production parser already treats as a DPS 판매번호 / 전자주문번호
# (``parse_lookup_result`` long_numbers: ``\d{8,20}``).
_SALES_NUMBER = re.compile(r"\d{8,20}")
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
// The purchase list opens a sale with parent.go_sendSearchMain('<판매번호>');
// the other 10-digit link in the same row calls go_transterPop instead.
const SEND_SALES=/go_sendSearchMain\(\s*(['"])\s*(\d+)\s*\1\s*\)/;
function salesLinks(tr){const out=[];for(const el of tr.querySelectorAll('[onclick]')){
  const m=SEND_SALES.exec(el.getAttribute('onclick')||'');if(m)out.push({el,sales:m[2],text:txt(el)});}return out;}
"""

PAGE_STATE_JS = r"""
((markers)=>{const all=docs();let text='';let password=false;
 for(const {d} of all){text+=' '+norm(d.body?d.body.innerText:'');
  if(d.querySelector('input[type=password]'))password=true;}
 return {url:location.href,title:document.title,password,
  marker_hits:markers.filter(m=>text.indexOf(m)>=0),frames:all.length};})
"""

FILL_AND_QUERY_JS = r"""
((order,start,end,token)=>{
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
 for(const {d} of docs()) for(const el of d.querySelectorAll('tr,[role=row],div,span,p,td,li,strong'))
  el.setAttribute('data-cdp-before',token);
 button.click();
 return {ok:true,readback,button:button.tagName,order_candidates:orderCands.length};
})
"""

RESULT_SNAPSHOT_JS = r"""
((markers,order,token,debug)=>{const texts=[];const seen=new Set();let headers=[];const rows=[];let loading=false;
 let staleOrderRows=0;let formTablesSkipped=0;const salesEvidence=[];
 // debug (4th argument, diagnostics only): where each header and matched-row
 // cell came from. Production passes three arguments and gets none of it.
 const dbg={headers:[],rows:[]};
 const meta=(el,t,i)=>{const r=el.getBoundingClientRect();return {index:i,text:t,tag:el.tagName,
  class:String(el.className||'').slice(0,60),id:String(el.id||'').slice(0,40),
  in_tblSort:!!(el.closest&&el.closest('table#tblSort')),x:Math.round(r.left),y:Math.round(r.top),w:Math.round(r.width)};};
 const add=t=>{if(t&&!seen.has(t)&&texts.length<240){seen.add(t);texts.push(t);}};
 const fresh=el=>!token||el.getAttribute('data-cdp-before')!==token;
 const isForm=t=>!!t.querySelector('select,textarea,input:not([type=checkbox]):not([type=radio]):not([type=hidden])');
 // DpsUiAutomation.collect_result_snapshot's header sources, in document
 // order over the document that holds the result row (its result root):
 //   a td whose class contains "thead"               (DataItem + thead)
 //   the cells of table#tblSort's rows               (parent automation_id tblSort)
 //   th / [role=columnheader]                        (Header / HeaderItem)
 //   a text leaf whose own or parent class/id says header/thead/columnheader
 // then, only if none: the result texts that are known header words
 // (_infer_headers_from_texts). Nothing inside the search form counts, and
 // nothing inside a data row does.
 const HINT=/header|thead|columnheader/i;const KNOWN=['판매번호','주문번호','상품명','모델명','수량','설치예정일','배송예정일','설치상태','진행상태'];
 const inForm=el=>{const t=el.closest('table');return !!(t&&isForm(t));};
 const listHeaders=(d,dataRows)=>{const hs=[];const push=(t,el)=>{if(t&&!hs.includes(t)){hs.push(t);
   if(debug)dbg.headers.push({...meta(el,t,hs.length-1),source:'rule'});}};
  const inData=el=>dataRows.some(tr=>tr.contains(el));
  for(const el of d.querySelectorAll('*')){if(!vis(el)||inForm(el)||inData(el))continue;const tag=el.tagName;
   const cls=String(el.className||'')+' '+(el.id||'');
   if(tag==='TR'&&el.closest('table#tblSort')){for(const c of el.querySelectorAll('td,th'))push(txt(c),c);continue;}
   if(el.closest('table#tblSort'))continue;
   if(tag==='TD'&&/thead/i.test(String(el.className||''))){push(txt(el),el);continue;}
   if(tag==='TH'||el.getAttribute('role')==='columnheader'){push(txt(el),el);continue;}
   if(['SPAN','DIV','P','FONT','B','STRONG','LABEL'].includes(tag)&&!el.children.length){
    const pc=el.parentElement?String(el.parentElement.className||'')+' '+(el.parentElement.id||''):'';
    if(HINT.test(cls)||HINT.test(pc))push(txt(el),el);}}
  if(!hs.length&&dataRows.length){for(const el of d.querySelectorAll('td,th,span,div')){
   if(!vis(el)||inForm(el)||inData(el)||el.children.length)continue;const t=txt(el);
   if(KNOWN.includes(t)){hs.push(t);if(debug)dbg.headers.push({...meta(el,t,hs.length-1),source:'inferred'});}}}
  return hs;};
 for(const {d} of docs()){
  for(const el of d.querySelectorAll('[class*=loading],[id*=loading],[class*=progress],[class*=spinner]'))if(vis(el))loading=true;
  for(const el of d.querySelectorAll('div,span,p,td,li,strong')){if(!vis(el))continue;const t=txt(el);
   if(!t||t.length>=80)continue;
   if(/로딩|처리중|조회중|loading/i.test(t)){loading=true;continue;}
   if(fresh(el)&&markers.some(m=>t.indexOf(m)>=0))add(t);}
  const docRows=[];
  for(const table of d.querySelectorAll('table,[role=grid]')){if(!vis(table))continue;
   if(isForm(table)){formTablesSkipped++;continue;}
   for(const tr of table.querySelectorAll('tr,[role=row]')){const cells=Array.from(tr.querySelectorAll('td,[role=gridcell]'));
    if(cells.length<2)continue;const vals=cells.map(txt);
    if(!vals.includes(order))continue;
    if(!fresh(tr)){staleOrderRows++;continue;}
    docRows.push(tr);
    if(debug)dbg.rows.push(cells.map((c,i)=>meta(c,vals[i],i)));
    const key=JSON.stringify(vals);
    if(!rows.some(r=>JSON.stringify(r)===key)&&rows.length<100){rows.push(vals);vals.forEach(add);
     for(const l of salesLinks(tr))salesEvidence.push({sales:l.sales,text:l.text});}}}
  if(docRows.length&&!headers.length)headers=listHeaders(d,docRows);}
 return {raw_result_texts:texts,table_headers:headers.slice(0,40),table_rows:rows,loading,
  ...(debug?{debug:dbg}:{}),
  stale_order_rows:staleOrderRows,form_tables_skipped:formTablesSkipped,sales_links:salesEvidence};})
"""

SCROLL_GRID_JS = r"""
((step)=>{let moved=false;for(const {d} of docs()) for(const el of d.querySelectorAll('div,tbody'))
 {if(el.scrollHeight>el.clientHeight+4&&el.querySelector('tr,[role=row]')){
   const before=el.scrollTop;el.scrollTop=before+step;if(el.scrollTop!==before)moved=true;}}return moved;})
"""

CLICK_SALES_LINK_JS = r"""
((order,sales,token)=>{const hits=[];for(const {d} of docs()) for(const tr of d.querySelectorAll('tr,[role=row]'))
 {if(token&&tr.getAttribute('data-cdp-before')===token)continue;
  const cells=Array.from(tr.querySelectorAll('td,[role=gridcell]')).map(txt);if(!cells.includes(order))continue;
  for(const l of salesLinks(tr)){if(l.sales===sales&&(!l.text||l.text===sales)&&vis(l.el)){hits.push(l.el);break;}}}
 if(!hits.length)return {ok:false,code:'DPS_SALES_LINK_NOT_FOUND',rows:0};
 hits[0].click();return {ok:true,rows:hits.length};})
"""

DETAIL_SNAPSHOT_JS = r"""
(()=>{const records=[];let marker=false;
 const SEL='th,td,a,label,span,div,input,select,textarea,strong,li,font,b,p';
 for(const {d,ox,oy} of docs()){
  if(norm(d.body?d.body.innerText:'').indexOf('품목상세내역')>=0)marker=true;
  for(const el of d.querySelectorAll(SEL)){
   if(!vis(el)||records.length>=3000)continue;const tag=el.tagName;
   const field=['INPUT','SELECT','TEXTAREA'].includes(tag);
   // a leaf: nothing inside it carries text of its own, so one cell -- a
   // TD.theadFree, a TD.tcontentFree, or the <a> inside a TD -- is one record
   const leaf=field||!Array.from(el.querySelectorAll(SEL)).some(c=>txt(c));
   if(!leaf)continue;const t=txt(el);if(!t)continue;const r=el.getBoundingClientRect();
   records.push({name:t,control_type:field?'Edit':(tag==='TD'||tag==='TH')?'DataItem':tag==='A'?'Hyperlink':'Text',
    class_name:String(el.className||'').slice(0,60),
    top:Math.round(r.top+oy),left:Math.round(r.left+ox),bottom:Math.round(r.bottom+oy),right:Math.round(r.right+ox)});}}
 return {records,marker};})
"""


def detail_item_table(records: list[dict[str, Any]]) -> tuple[list[str], list[list[str]]]:
    """``DpsUiAutomation._detail_table`` over DOM rectangles, rule for rule.

    The production reader does not look for a <table>: it finds the one row of
    cells whose labels are item fields (모델, 수량, 판매금액, 요구납기일, ...),
    at least three distinct, and reads every cell below it into the column
    whose span contains the cell's centre -- exactly one cell, else empty --
    keeping rows that carry a digit. The same arithmetic on the page's own
    boxes works whether the header is TH or TD.theadFree and whether header
    and body are one table or two nested ones. The one addition: a cell drawn
    twice at the same place with the same text (a cloned header or row) is one
    cell, where UIA would have exposed one element.
    """

    def label(record: dict[str, Any]) -> str:
        return canonical_detail_label(record.get("name")) or normalize_label(record.get("name"))

    def band(record: dict[str, Any]) -> int:
        return int(round(int(record.get("top") or 0) / 5.0) * 5)

    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for record in records:
        key = (normalize_label(record.get("name")), band(record),
               int(round(int(record.get("left") or 0) / 5.0) * 5))
        unique.setdefault(key, record)
    cells = list(unique.values())
    grouped: dict[int, list[dict[str, Any]]] = {}
    for record in cells:
        if label(record) in ITEM_FIELDS and record.get("control_type") != "Edit":
            grouped.setdefault(band(record), []).append(record)
    header_cells = max(grouped.values(), key=lambda values: len({label(v) for v in values}),
                       default=[])
    if len({label(value) for value in header_cells}) < 3:
        return [], []
    header_cells = sorted(header_cells, key=lambda value: int(value.get("left") or 0))
    headers = [label(value) for value in header_cells]
    header_top = min(int(value.get("top") or 0) for value in header_cells)
    rows_by_y: dict[int, list[dict[str, Any]]] = {}
    for record in cells:
        if record.get("control_type") not in {"Text", "Cell", "DataItem", "Hyperlink"}:
            continue
        if int(record.get("top") or 0) <= header_top + 5:
            continue
        rows_by_y.setdefault(band(record), []).append(record)
    rows: list[list[str]] = []
    for key in sorted(rows_by_y):
        row: list[str] = []
        for header in header_cells:
            left, right = int(header.get("left") or 0), int(header.get("right") or 0)
            values = {
                normalize_label(value.get("name")) for value in rows_by_y[key]
                if left <= (int(value.get("left") or 0) + int(value.get("right") or 0)) / 2 <= right
            }
            row.append(next(iter(values)) if len(values) == 1 else "")
        if any(row) and any(re.search(r"\d", value) for value in row):
            rows.append(row)
    return headers, rows[:100]


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
            state = dict(page.evaluate(_call(PAGE_STATE_JS, list(PURCHASE_PAGE_TEXT_MARKERS))) or {})
            if state.get("password") or any(m in url for m in LOGIN_URL_MARKERS):
                login_seen = True
                page.close()
                continue
            if state.get("marker_hits"):
                return target, page, state
            page.close()
        return None, None, {"code": "DPS_LOGIN_REQUIRED" if login_seen else "DPS_TAB_NOT_FOUND"}

    def _wait_for_result(self, page: Any, expected: str, token: str) -> dict[str, Any]:
        """Wait for *this* query's answer, never for "something is on screen".

        Only two things end the wait: a fresh row whose cell equals the
        queried order (stable over two polls, loading finished), or a fresh
        no-result message. Rows and messages that were on screen before 조회
        carry the ``token`` mark and are never read -- that is what keeps a
        previous lookup's row, left by either backend, from answering this
        one. A search form or an unrelated table is not a result at all.
        Anything else runs out the clock and fails closed.
        """

        deadline = self.clock() + max(1.0, self.result_timeout)
        latest: dict[str, Any] = {}
        stable, scrolled = 0, 0
        script = _call(RESULT_SNAPSHOT_JS, list(NO_RESULT_MARKERS), expected, token)
        while self.clock() < deadline:
            self.sleep(self.poll_interval)
            latest = dict(page.evaluate(script) or {})
            if latest.get("loading"):
                stable = 0
                continue
            folded = "\n".join(latest.get("raw_result_texts") or []).casefold()
            rows = latest.get("table_rows") or []
            if not rows and any(marker.casefold() in folded for marker in NO_RESULT_MARKERS):
                return {"status": "no_result", "snapshot": latest}
            if rows:
                stable += 1
                if stable >= 2:
                    return {"status": "complete", "snapshot": latest}
                continue
            stable = 0
            # A virtual grid renders only what is on screen; bring the rest in
            # before deciding the order is not there.
            if scrolled < 30 and page.evaluate(_call(SCROLL_GRID_JS, 400)):
                scrolled += 1
        return {"status": "timeout", "snapshot": latest}

    def _open_detail(self, page: Any, known_ids: set[str], order: str, sales: str,
                     token: str) -> tuple[Any, str | None, dict[str, Any]]:
        clicked = dict(page.evaluate(_call(CLICK_SALES_LINK_JS, order, sales, token)) or {})
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
            if same.get("marker") and detail_item_table(same.get("records") or [])[0]:
                return page, None, {"status": "DETAIL_OPENED",
                                    "window_form": "SAME_WINDOW_OR_MODAL",
                                    "invocation_count": 1}
            self.sleep(self.poll_interval)
        return None, None, {"status": "DETAIL_OPEN_FAILED", "invocation_count": 1}

    @staticmethod
    def _sales_cell_proven_blank(snapshot: dict[str, Any], matched_row: list[str],
                                 parsed: dict[str, Any]) -> bool:
        """Whether the row *shows* no 판매번호, as opposed to one we failed to read.

        Proven only when every piece agrees: no go_sendSearchMain element in
        the exact row at all, the production headers name exactly one sales
        number column, the row lines up with those headers cell for cell (the
        parser's own direct mapping), that cell is empty, and the parser read
        no sales number either. Missing headers prove nothing -- then it is a
        reading failure and fails closed.
        """

        if snapshot.get("sales_links"):
            return False
        headers = [normalize_label(value).replace(" ", "")
                   for value in snapshot.get("table_headers") or []]
        columns = [index for index, value in enumerate(headers)
                   if value in {"DPS판매번호", "판매번호"}]
        if len(columns) != 1 or len(matched_row) != len(headers):
            return False
        return (matched_row[columns[0]] == ""
                and not str(parsed["data"].get("dps_sales_number") or "").strip())

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
            token = os.urandom(6).hex()
            filled = dict(page.evaluate(_call(FILL_AND_QUERY_JS, order, dps_period_start,
                                              dps_period_end, token)) or {})
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
            polling = self._wait_for_result(page, order, token)
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
            # The success invariant: the production parser matched exactly one
            # fresh row carrying this order number, and the 판매번호 it read is
            # a real sales number from that same row. A label that happens to
            # sit next to "판매번호" ("기간") is neither.
            matched_row = [str(value).strip() for value in
                           parsed.get("diagnostics", {}).get("matched_row") or []]
            if not parsed["found"] or order not in matched_row:
                return self._failure("LOOKUP_RESULT_NOT_FOUND",
                                     "조회는 완료했지만 이 주문번호의 결과 행을 확인하지 못했습니다.",
                                     diagnostics)
            # The live grid has no header cells, so the parser cannot name the
            # 판매번호 column. The row names it itself: the element whose onclick
            # calls go_sendSearchMain('<판매번호>'). Exactly one such value, in
            # this row, agreeing with its own link text -- never a guess from
            # digits or position (the go_transterPop number beside it is not it).
            candidates = {
                str(link.get("sales") or "").strip()
                for link in after.get("sales_links") or []
                if isinstance(link, dict)
                and _SALES_NUMBER.fullmatch(str(link.get("sales") or "").strip())
                and str(link.get("sales")).strip() != order
                and str(link.get("text") or "").strip() in ("", str(link.get("sales")).strip())
            }
            diagnostics["sales_link_candidates"] = len(candidates)
            sales_value = next(iter(candidates)) if len(candidates) == 1 else ""
            if sales_value and sales_value in matched_row:
                parsed["data"]["dps_sales_number"] = sales_value
            elif self._sales_cell_proven_blank(after, matched_row, parsed):
                # DPS has not issued a 판매번호 for this order yet. That is data,
                # not a reading failure, and production treats it so:
                # lookup_sales_detail stops at DPS_SALES_NUMBER_MISSING without
                # opening anything and the lookup ends RESULT_FOUND_DETAIL_PARTIAL.
                # The branch below does exactly that when the sales number is
                # empty; no other number stands in for it.
                parsed["data"]["dps_sales_number"] = None
                diagnostics["sales_number_cell"] = "BLANK"
            else:
                return self._failure("DPS_SALES_NUMBER_MISSING",
                                     "결과 행에서 DPS 판매번호를 확인하지 못했습니다.", diagnostics)
            detail_lookup = {"attempted": False, "opened": False, "parsed": False,
                             "closed": False, "status": "NOT_ATTEMPTED", "invocation_count": 0}
            detail = None
            sales = str(parsed["data"].get("dps_sales_number") or "").strip()
            if sales:
                detail_lookup["attempted"] = True
                detail_page, detail_id, opened = self._open_detail(page, known_ids, order, sales, token)
                detail_lookup.update(opened)
                if detail_page is not None:
                    detail_lookup["opened"] = True
                    snapshot = dict(detail_page.evaluate(_call(DETAIL_SNAPSHOT_JS)) or {})
                    item_headers, item_rows = detail_item_table(snapshot.get("records") or [])
                    snapshot.update(headers=item_headers, rows=item_rows)
                    detail = parse_flat_detail(snapshot.get("records") or [],
                                               table_headers=item_headers,
                                               table_rows=item_rows)
                    # A detail without a single item row has not been read:
                    # the installation date lives only in those rows.
                    detail_lookup["parsed"] = bool(detail.get("detail_items"))
                    detail_lookup["status"] = "DETAIL_PARSED" if detail_lookup["parsed"] else "DETAIL_PARSE_FAILED"
                    # The page that opened must be this sale's: a detail that
                    # names another 판매번호 is not evidence for this order.
                    shown = str(dict(detail.get("customer_info") or {}).get("dps_sales_number") or "").strip()
                    if shown and shown != sales:
                        detail_lookup["parsed"] = False
                        detail_lookup["status"] = "DETAIL_PARSE_FAILED"
                        detail = None
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
