from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from answer.facts import build_answer_facts
from answer.models import AnswerResult, AnswerStatus
from answer.source_adapter import answer_request_from_inquiry
from repositories.database import Database
from repositories.dps_repository import DpsRepository
from repositories.inquiry_repository import InquiryRepository
from services.dps_agent_client import (
    auto_connect_dps_tab,
    get_dps_agent_status,
    open_dps_browser,
)
from services.dps_lookup_orchestrator import DpsLookupOrchestrator
from ui.dps_presenter import build_dps_display


SOURCE = "DPS_ITEM_DETAIL_REQUIRED_DELIVERY_DATE"


def _eligible_inquiry(database: Database) -> dict | None:
    with database.connection() as connection:
        row = connection.execute(
            """
            SELECT *
            FROM inquiries
            WHERE order_id IS NOT NULL
              AND length(trim(order_id)) > 0
              AND order_date IS NOT NULL
              AND length(trim(order_date)) > 0
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
    return dict(row) if row is not None else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Force a real DPS lookup and persist the result.",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open the managed DPS Chrome and attempt tab auto-connect.",
    )
    parser.add_argument(
        "--inspect-latest",
        action="store_true",
        help="Print only non-sensitive parser structure from the latest row.",
    )
    arguments = parser.parse_args()
    database = Database()
    database.initialize()
    if arguments.open:
        open_dps_browser()
        auto_connect_dps_tab(select_tab=True, force=True)
    agent = get_dps_agent_status()
    inquiry = _eligible_inquiry(database)
    report = {
        "agent_running": bool(agent.get("agent_running")),
        "agent_mode": agent.get("mode"),
        "login_status": agent.get("login_status"),
        "eligible_order_available": inquiry is not None,
        "executed": False,
    }
    if arguments.inspect_latest and inquiry is not None:
        latest = DpsRepository(database).get_latest_by_inquiry_id(
            int(inquiry["id"])
        )
        raw = (
            latest.get("raw_result_json")
            if latest and isinstance(latest.get("raw_result_json"), dict)
            else {}
        )
        raw_data = (
            raw.get("data")
            if isinstance(raw.get("data"), dict)
            else {}
        )
        detail_items = [
            item
            for item in raw_data.get("detail_items", [])
            if isinstance(item, dict)
        ]
        headers = [
            str(value)
            for value in raw.get("table_headers", [])
            if value not in (None, "")
        ]
        diagnostics = (
            raw.get("diagnostics")
            if isinstance(raw.get("diagnostics"), dict)
            else {}
        )
        detail_link = (
            diagnostics.get("detail_link")
            if isinstance(diagnostics.get("detail_link"), dict)
            else {}
        )
        report["latest_parser_structure"] = {
            "raw_status": raw.get("status"),
            "detail_lookup": {
                key: (raw.get("detail_lookup") or {}).get(key)
                for key in (
                    "attempted",
                    "opened",
                    "parsed",
                    "closed",
                    "status",
                    "invocation_count",
                )
            },
            "list_headers": headers,
            "data_keys": sorted(raw_data),
            "detail_item_count": len(detail_items),
            "detail_link": {
                "reason": detail_link.get("reason"),
                "selected_reason": detail_link.get("selected_reason"),
                "header_rect_count": detail_link.get(
                    "header_rect_count"
                ),
                "online_header_rect_count": detail_link.get(
                    "online_header_rect_count"
                ),
                "dps_column_control_counts": detail_link.get(
                    "dps_column_control_counts"
                ),
                "sales_row_count": detail_link.get("sales_row_count"),
                "candidate_count": len(
                    detail_link.get("candidates") or []
                ),
                "candidate_control_types": [
                    candidate.get("control_type")
                    for candidate in detail_link.get("candidates", [])
                    if isinstance(candidate, dict)
                ],
            },
            "detail_item_date_fields": [
                {
                    key: item.get(key)
                    for key in (
                        "raw_required_delivery_date",
                        "required_delivery_date",
                        "date_parse_status",
                    )
                }
                for item in detail_items
            ],
        }
    if not arguments.execute or inquiry is None:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    outcome = DpsLookupOrchestrator(database).lookup(
        int(inquiry["id"]),
        force_refresh=True,
        correlation_id="phase8-4-windows-validation",
    )
    latest = DpsRepository(database).get_latest_by_inquiry_id(
        int(inquiry["id"])
    )
    normalized = dict(outcome.metadata)
    display = build_dps_display(
        lookup_required=True,
        order_id=None,
        latest_row=latest,
    )
    request = answer_request_from_inquiry(
        InquiryRepository(database).get(int(inquiry["id"]))
    )
    request.metadata["dps"] = normalized
    facts = build_answer_facts(
        request,
        AnswerResult(
            status=AnswerStatus.GENERATED,
            category="DPS validation",
            reason="Phase 8.4 validation",
            answer="validation",
            provider="rules",
            auto_answerable=True,
            needs_review=False,
        ),
    )
    report.update(
        {
            "executed": True,
            "lookup_status": normalized.get("lookup_status"),
            "date_parse_status": normalized.get("date_parse_status"),
            "source": normalized.get("installation_date_source"),
            "dps_required_delivery_date": normalized.get(
                "required_delivery_date"
            ),
            "db_installation_date": (
                latest.get("installation_date") if latest else None
            ),
            "dashboard_installation_date": display.get(
                "installation_date"
            ),
            "answer_facts_installation_date": facts.installation.get(
                "date"
            ),
            "all_dates_match": (
                bool(normalized.get("required_delivery_date"))
                and normalized.get("required_delivery_date")
                == (latest or {}).get("installation_date")
                == display.get("installation_date")
                == facts.installation.get("date")
            ),
            "source_is_required_delivery_date": (
                normalized.get("installation_date_source") == SOURCE
            ),
        }
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
