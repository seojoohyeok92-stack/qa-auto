"""OLD (pywinauto agent) vs NEW (CDP/DOM) DPS lookup comparison -- server only.

Read-only by construction:
  * orders come from a CSV or from a read-only SQLite connection (mode=ro)
  * neither backend's result is written to DpsRepository or any Q&A table
  * both backends only read DPS (조회 / 판매번호 상세), never save

Without ``--execute`` it only runs the preflight (agent status, CDP Chrome
reachable, purchase-list tab found) and calls DPS zero times.

    python scripts/dps_cdp_compare.py --db data/oje_automation.db --limit 5
    python scripts/dps_cdp_compare.py --db data/oje_automation.db --limit 5 --execute

The CDP Chrome is the operator's, started once with
``dps.cdp_backend.chrome_launch_command`` and logged in to DPS by hand, with
판매 > 온라인판매 > 구매요청리스트 open.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import statistics
import sys
import time
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dps.cdp_backend import (  # noqa: E402
    DEFAULT_CDP_PORT, CdpBrowser, CdpDpsReader, CdpError, lookup_dps_order_cdp,
)
from services.dps_result_normalizer import normalize_dps_result  # noqa: E402

# The fields downstream reads (normalize_dps_result), plus the item models and
# the recipient, compared for equality only -- the recipient is never printed.
COMPARED_FIELDS = (
    "lookup_status", "sales_number", "delivery_status", "installation_status",
    "required_delivery_date", "installation_date", "installation_date_source",
    "date_parse_status", "requires_human_review", "required_delivery_date_row_count",
    "product_name",
)
DATE_FIELDS = ("required_delivery_date", "installation_date")


def mask(order_id: str) -> str:
    value = str(order_id or "")
    return f"{value[:4]}****{value[-4:]}" if len(value) > 8 else "<masked>"


def load_orders(args: argparse.Namespace) -> list[dict[str, str]]:
    if args.csv:
        with open(args.csv, encoding="utf-8-sig") as handle:
            rows = [dict(row) for row in csv.DictReader(handle)]
    else:
        connection = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        rows = [dict(row) for row in connection.execute(
            """
            SELECT order_id, order_date FROM inquiries
            WHERE store_code NOT LIKE 'COUPANG_%'
              AND length(order_id) = 16 AND order_date IS NOT NULL
            GROUP BY order_id ORDER BY max(id) DESC LIMIT ?
            """, (int(args.limit),)).fetchall()]
        connection.close()
    return [row for row in rows if row.get("order_id") and row.get("order_date")][: int(args.limit)]


def comparable(raw: dict[str, Any], order_id: str, elapsed: float) -> dict[str, Any]:
    normalized = normalize_dps_result(raw, order_id=order_id, elapsed_seconds=elapsed)
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    value = {field: normalized.get(field) for field in COMPARED_FIELDS}
    value["found"] = normalized["lookup_status"] == "SUCCESS"
    value["item_models"] = sorted(
        str(item.get("model_name") or "") for item in data.get("detail_items") or []
        if isinstance(item, dict))
    value["recipient_match_key"] = str(data.get("recipient_name") or "")
    value["error_code"] = normalized.get("error_code")
    return value


def stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    return {"count": len(ordered), "average": round(statistics.fmean(ordered), 3),
            "p50": round(statistics.median(ordered), 3),
            "p95": round(ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))], 3),
            "max": round(ordered[-1], 3)}


def preflight(port: int) -> dict[str, Any]:
    from services.dps_agent_client import get_dps_agent_status

    report: dict[str, Any] = {}
    try:
        report["agent_status"] = {k: v for k, v in dict(get_dps_agent_status()).items()
                                  if k in {"agent_running", "connection_status", "login_state", "code"}}
    except Exception as error:  # noqa: BLE001
        report["agent_status"] = {"error": error.__class__.__name__}
    browser = CdpBrowser(port=port)
    try:
        report["cdp_version"] = browser.version().get("Browser")
        reader = CdpDpsReader(browser)
        target, page, state = reader._purchase_page()
        report["cdp_purchase_tab"] = bool(page)
        report["cdp_tab_code"] = state.get("code")
        if page is not None:
            page.close()
    except CdpError as error:
        report["cdp_error"] = error.code
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--csv", help="columns: order_id,order_date")
    source.add_argument("--db", help="SQLite path, opened read-only")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--port", type=int, default=DEFAULT_CDP_PORT)
    parser.add_argument("--execute", action="store_true",
                        help="actually run both lookups (reads DPS)")
    parser.add_argument("--old-force-refresh", action="store_true",
                        help="bypass the agent cache so OLD timing is a real lookup")
    parser.add_argument("--out", default="dps_cdp_compare_report.json")
    args = parser.parse_args()

    report: dict[str, Any] = {"preflight": preflight(args.port)}
    orders = load_orders(args)
    report["planned_orders"] = [mask(row["order_id"]) for row in orders]
    if not args.execute:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        print("preflight only: DPS calls 0 (add --execute to compare)")
        return 0

    from services.dps_agent_client import lookup_dps_order

    reader = CdpDpsReader(CdpBrowser(port=args.port))
    rows, timings = [], {"OLD": [], "CDP": []}
    counts = {name: {"success": 0, "no_result": 0, "error": 0} for name in timings}
    mismatch = {"FIELD_MISMATCH_COUNT": 0, "DATE_MISMATCH_COUNT": 0, "STATUS_MISMATCH_COUNT": 0}
    for row in orders:
        order_id, order_date = row["order_id"].strip(), row["order_date"].strip()
        results = {}
        for name, call in (
            ("OLD", lambda: lookup_dps_order(order_id=order_id, order_date=order_date,
                                             request_id=str(uuid.uuid4()),
                                             force_refresh=args.old_force_refresh)),
            ("CDP", lambda: lookup_dps_order_cdp(order_id=order_id, order_date=order_date,
                                                 request_id=str(uuid.uuid4()), reader=reader)),
        ):
            started = time.monotonic()
            try:
                raw = call()
            except Exception as error:  # noqa: BLE001
                raw = {"success": False, "code": f"EXCEPTION_{error.__class__.__name__}"}
            elapsed = time.monotonic() - started
            value = comparable(raw, order_id, elapsed)
            timings[name].append(elapsed)
            bucket = ("success" if value["lookup_status"] == "SUCCESS" else
                      "no_result" if value["lookup_status"] == "NOT_FOUND" else "error")
            counts[name][bucket] += 1
            results[name] = {**value, "elapsed_seconds": round(elapsed, 3)}
        old, new = results["OLD"], results["CDP"]
        diffs = sorted(key for key in (*COMPARED_FIELDS, "found", "item_models", "recipient_match_key")
                       if old.get(key) != new.get(key))
        if "lookup_status" in diffs:
            mismatch["STATUS_MISMATCH_COUNT"] += 1
        if any(field in diffs for field in DATE_FIELDS):
            mismatch["DATE_MISMATCH_COUNT"] += 1
        if diffs:
            mismatch["FIELD_MISMATCH_COUNT"] += 1
        for value in (old, new):
            value["recipient_match_key"] = "<compared-not-shown>"
        rows.append({"order": mask(order_id), "mismatched_fields": diffs, "old": old, "cdp": new})
    report.update({
        "results": rows, "counts": counts, **mismatch,
        "timing_seconds": {name: stats(values) for name, values in timings.items()},
        "decision": ("SWITCH_BLOCKED_MISMATCH" if mismatch["FIELD_MISMATCH_COUNT"]
                     else "NO_MISMATCH_IN_SAMPLE"),
    })
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({key: report[key] for key in (
        "counts", "FIELD_MISMATCH_COUNT", "DATE_MISMATCH_COUNT", "STATUS_MISMATCH_COUNT",
        "timing_seconds", "decision")}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
