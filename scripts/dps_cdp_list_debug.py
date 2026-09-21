"""Read-only diagnosis of the purchase-list row the CDP lookup parses.

Answers one question with production's own code: what exactly reaches
``parse_lookup_result`` for an order that is ALREADY on screen, and what it
makes of it. Nothing is re-implemented here --

  tab            CdpDpsReader._purchase_page         (the lookup's tab finder)
  snapshot       RESULT_SNAPSHOT_JS via _call         (the lookup's own script,
                 with its 4th ``debug`` argument, which adds where each header
                 and cell came from and changes nothing else)
  parse          DpsUiAutomation.parse_lookup_result  (same arguments as
                 CdpDpsReader.perform_lookup)

It never types, never presses 조회, never opens a sale, never writes. Run it
right after a lookup of that order left the result on screen (in the CDP
Chrome). No token is passed, so a row from that earlier lookup counts as the
current one -- which is the point: it reads what is there.

    python scripts/dps_cdp_list_debug.py --order 2026082341151601
    python scripts/dps_cdp_list_debug.py --order 2026082341151601 --out list_debug.json

Personal values are never printed: 구매자/인수자/수취인/연락처/주소 columns,
anything phone-shaped, and name-like Korean words that are not status words
are masked; the order number shows its last four digits only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dps.cdp_backend import (  # noqa: E402
    DEFAULT_CDP_PORT, NO_RESULT_MARKERS, RESULT_SNAPSHOT_JS, CdpBrowser, CdpDpsReader,
    CdpError, _call,
)

SENSITIVE_HEADERS = ("구매자", "인수자", "수취인", "수령인", "주문자", "연락처", "전화", "휴대", "주소")
# Status/label words that look like a short Korean name but are not personal.
SAFE_WORDS = {
    "구매요청", "배송완료", "배송중", "설치완료", "설치예정", "주문취소", "취소요청", "반품요청",
    "반품완료", "교환요청", "접수", "접수완료", "출고대기", "출고완료", "배정", "미배정", "완료",
    "대기", "취소", "보류", "전체", "비고", "상태",
}
PHONE = re.compile(r"(?<!\d)0\d{1,2}[-\s]?\d{3,4}[-\s]?\d{4}(?!\d)")
NAME_LIKE = re.compile(r"^[가-힣]{2,4}$|^[가-힣*]{2,5}$")


def _mask_order(value: str, order: str) -> str:
    return value.replace(order, f"****{order[-4:]}") if order and order in value else value


def _safe(value: Any, *, header: str = "", order: str = "") -> Any:
    text = str(value or "")
    if not text:
        return text
    if any(word in header for word in SENSITIVE_HEADERS):
        return "<masked-personal>"
    if PHONE.search(text):
        return "<masked-phone>"
    if NAME_LIKE.match(text) and text not in SAFE_WORDS:
        return "<masked-name-like>"
    return _mask_order(text, order)


def diagnose(browser: Any, order: str, *, allowed_hosts: tuple[str, ...] | None = None
             ) -> dict[str, Any]:
    # Same reader as the lookup; hosts default to its DPS_ALLOWED_HOSTS rule.
    reader = CdpDpsReader(browser, **({"allowed_hosts": allowed_hosts} if allowed_hosts else {}))
    _, page, state = reader._purchase_page()
    if page is None:
        return {"error": state.get("code") or "DPS_TAB_NOT_FOUND", "clicked": False}
    try:
        snapshot = dict(page.evaluate(
            _call(RESULT_SNAPSHOT_JS, list(NO_RESULT_MARKERS), order, "", True)) or {})
    finally:
        page.close()
    headers = list(snapshot.get("table_headers") or [])
    rows = list(snapshot.get("table_rows") or [])
    debug = dict(snapshot.get("debug") or {})
    parsed = reader.parser.parse_lookup_result(
        snapshot, naver_order_id=order, order_id=order, product_order_id=None,
        dps_query_value=order, dps_query_value_type="order_id",
    )
    diagnostics = dict(parsed.get("diagnostics") or {})
    matched_index = diagnostics.get("matched_row_index")
    matched = rows[matched_index] if isinstance(matched_index, int) and matched_index < len(rows) else (rows[0] if rows else [])
    aligned = len(matched) == len(headers)

    def header_at(index: int) -> str:
        return headers[index] if aligned and index < len(headers) else ""

    data = dict(parsed.get("data") or {})
    fields = ("product_name", "model_name", "dps_sales_number", "dps_order_number",
              "progress_status", "reception_status", "delivery_status", "installation_status",
              "installation_scheduled_date", "delivery_scheduled_date", "quantity")
    return {
        "clicked": False,
        "backend_features": {
            "list_header_rules": "table#tblSort" in RESULT_SNAPSHOT_JS,
            "debug_argument": "debug" in RESULT_SNAPSHOT_JS,
        },
        "RAW_LIST_HEADERS": headers,
        "RAW_LIST_HEADER_ELEMENTS": debug.get("headers") or [],
        "RAW_EXACT_ROW_VALUES": [_safe(v, header=header_at(i), order=order) for i, v in enumerate(matched)],
        "RAW_EXACT_ROW_CELLS": [
            {**cell, "text": _safe(cell.get("text"), header=header_at(int(cell.get("index") or 0)), order=order)}
            for cell in ((debug.get("rows") or [[]])[matched_index if isinstance(matched_index, int) else 0]
                         if debug.get("rows") else [])
        ],
        "EXACT_ROW_COUNT": len(rows),
        "ORDER_COLUMN_INDEX": {
            "in_row": matched.index(order) if order in matched else None,
            "in_headers": next((i for i, h in enumerate(headers) if "주문번호" in h and "상품" not in h), None),
        },
        "HEADER_COUNT": len(headers),
        "ROW_VALUE_COUNT": len(matched),
        "HEADERS_ALIGNED_WITH_ROW": aligned,
        "PARSE_LOOKUP_RESULT_OUTPUT": {
            "found": parsed.get("found"),
            "row_match_basis": diagnostics.get("row_match_basis"),
            "matched_row_index": matched_index,
            "parse_warnings": diagnostics.get("parse_warnings"),
            "data": {key: _safe(data.get(key), order=order) for key in fields},
        },
        "sales_links_in_row": len(snapshot.get("sales_links") or []),
        "form_tables_skipped": snapshot.get("form_tables_skipped"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--order", required=True, help="the order already shown in the result grid")
    parser.add_argument("--port", type=int, default=DEFAULT_CDP_PORT)
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    try:
        report = diagnose(CdpBrowser(port=args.port), args.order.strip())
    except CdpError as error:
        report = {"error": error.code, "clicked": False}
    text = json.dumps(report, ensure_ascii=True, indent=1)
    if args.out:
        Path(args.out).write_text(text, encoding="ascii")
    print(text)
    return 0 if "error" not in report else 2


if __name__ == "__main__":
    raise SystemExit(main())
