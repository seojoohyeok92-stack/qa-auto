from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from answer.source_adapter import answer_request_from_inquiry
from repositories.database import Database
from services.inquiry_analysis_service import InquiryAnalysisService


SENSITIVE_KEYS = (
    "address",
    "authorization",
    "customer",
    "email",
    "order_id",
    "phone",
    "secret",
    "tel",
    "token",
)
LONG_NUMBER = re.compile(r"(?<!\d)\d{10,24}(?!\d)")


def _safe(value: Any, *, key: str = "") -> Any:
    lowered = key.lower()
    if any(part in lowered for part in SENSITIVE_KEYS):
        if key.endswith("_present") or key.endswith("_status"):
            return value
        return "[PRESENT]" if value not in (None, "", [], {}) else None
    if isinstance(value, Mapping):
        return {str(k): _safe(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_safe(item, key=key) for item in value]
    if isinstance(value, str):
        return LONG_NUMBER.sub("[NUMBER_MASKED]", value)[:1000]
    return value


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _rows(connection: Any, query: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(query, params).fetchall()]


def diagnose(database: Database, inquiry_id: int) -> dict[str, Any]:
    with database.connection() as connection:
        inquiry_row = connection.execute(
            "SELECT * FROM inquiries WHERE id=?", (inquiry_id,)
        ).fetchone()
        if inquiry_row is None:
            raise LookupError(f"Inquiry not found: {inquiry_id}")
        inquiry = dict(inquiry_row)
        request = answer_request_from_inquiry(inquiry)
        analysis = InquiryAnalysisService().analyze(request)
        dps_rows = _rows(
            connection,
            """
            SELECT id, lookup_status, normalized_result_json, error_code,
                   queried_at, expires_at, cached, required_delivery_date,
                   installation_date, installation_date_source,
                   raw_required_delivery_date, date_parse_status,
                   correlation_id
              FROM dps_lookup_results
             WHERE inquiry_id=? ORDER BY id DESC LIMIT 5
            """,
            (inquiry_id,),
        )
        drafts = _rows(
            connection,
            """
            SELECT id, program_status, category, provider, original_answer,
                   review_status, source, validation_status, dps_lookup_id,
                   is_active, stale, answer_strategy, metadata_json,
                   inquiry_analysis_json, selected_facts_json,
                   validator_result_json, created_at
              FROM answer_drafts
             WHERE inquiry_id=? ORDER BY id DESC LIMIT 5
            """,
            (inquiry_id,),
        )
        workflow = _rows(
            connection,
            """
            SELECT step_code, step_status, attempt_count, last_error_code,
                   last_error_message, metadata_json, updated_at
              FROM workflow_steps WHERE inquiry_id=? ORDER BY id
            """,
            (inquiry_id,),
        )
        activity = _rows(
            connection,
            """
            SELECT event_code, level, message, details_json, created_at
              FROM activity_logs WHERE inquiry_id=? ORDER BY id DESC LIMIT 40
            """,
            (inquiry_id,),
        )

    normalized_dps: list[dict[str, Any]] = []
    for row in dps_rows:
        normalized = _json(row.pop("normalized_result_json", {}))
        row["normalized"] = normalized
        normalized_dps.append(row)

    normalized_drafts: list[dict[str, Any]] = []
    for row in drafts:
        answer = str(row.pop("original_answer") or "")
        row["generated_answer_length"] = len(answer.strip())
        for field in (
            "metadata_json",
            "inquiry_analysis_json",
            "selected_facts_json",
            "validator_result_json",
        ):
            row[field] = _json(row.get(field))
        normalized_drafts.append(row)

    for row in workflow:
        row["metadata_json"] = _json(row.get("metadata_json"))
    for row in activity:
        row["details_json"] = _json(row.get("details_json"))

    raw = _json(inquiry.get("raw_json"))
    report = {
        "inquiry": {
            "inquiry_id": inquiry_id,
            "inquiry_type": inquiry.get("inquiry_type"),
            "title": inquiry.get("title"),
            "content": inquiry.get("content"),
            "inquiry_text": request.question,
            "order_id_present": bool(str(inquiry.get("order_id") or "").strip()),
            "product_order_id_present": bool(
                str(inquiry.get("product_order_id") or "").strip()
            ),
            "workflow_status": inquiry.get("workflow_status"),
            "answer_status": inquiry.get("answer_status"),
            "phase9_status": inquiry.get("phase9_status"),
            "raw_analysis": raw.get("analysis"),
        },
        "analysis": analysis.to_dict(),
        "dps_results": normalized_dps,
        "drafts": normalized_drafts,
        "workflow": workflow,
        "activity": activity,
    }
    return _safe(report)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--inquiry-id", type=int, action="append", default=[])
    parser.add_argument("--search", action="append", default=[])
    parser.add_argument("--failed-missing-order", action="store_true")
    args = parser.parse_args()
    database = Database(args.database)
    inquiry_ids = list(args.inquiry_id)
    if args.search:
        with database.connection() as connection:
            for term in args.search:
                rows = connection.execute(
                    """
                    SELECT id FROM inquiries
                     WHERE title LIKE ? OR content LIKE ? OR order_id LIKE ?
                        OR product_order_id LIKE ? OR raw_json LIKE ?
                     ORDER BY id DESC LIMIT 20
                    """,
                    tuple(f"%{term}%" for _ in range(5)),
                ).fetchall()
                inquiry_ids.extend(int(row["id"]) for row in rows)
    if args.failed_missing_order:
        with database.connection() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT i.id
                  FROM inquiries i
                  JOIN activity_logs a ON a.inquiry_id=i.id
                 WHERE a.event_code='ANSWER_GENERATION_FAILED'
                   AND COALESCE(i.order_id, '')=''
                 ORDER BY i.id DESC LIMIT 20
                """
            ).fetchall()
            inquiry_ids.extend(int(row["id"]) for row in rows)
    inquiry_ids = list(dict.fromkeys(inquiry_ids))
    if not inquiry_ids:
        parser.error("provide --inquiry-id or --search")
    reports = [diagnose(database, value) for value in inquiry_ids]
    print(json.dumps(reports, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
