"""Extract why a Program Answer ended as the safe/RULE_FALLBACK draft.

Strictly read-only. Run this on the server against the production DB to
capture the evidence the pipeline already recorded for an inquiry:

  * which generation route was chosen and whether GPT was called
  * the validator failure reason and safe_error_code that triggered fallback
  * the sub-questions GPT decomposed the inquiry into
  * the provider's raw answer, kept in the draft metadata even when the
    validator rejected it and the safe draft replaced it

It issues SELECT statements only.  It never calls a provider, never posts,
never writes, and never migrates.

Connection safety
-----------------
This script deliberately does NOT use ``repositories.database.Database``.
That helper is built for the running application: it creates the parent
directory and issues ``PRAGMA journal_mode = WAL`` on connect, which touches
the WAL sidecar files.  A diagnostic must leave no trace, so we open the file
ourselves through a SQLite read-only URI (``file:<path>?mode=ro``).  SQLite
refuses every write on such a connection, and a missing file is an error
rather than a silently created empty database.

    python scripts/diagnose_compound_fallback.py --database <path> \
        --external-id <naver inquiry id>
    python scripts/diagnose_compound_fallback.py --database <path> \
        --inquiry-id 123

Sensitive values are masked with the project's existing masking rules.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from answer.text_utils import mask_personal_information


# Marker for a field the pipeline never persisted, so a reader can tell
# "the pipeline recorded nothing here" apart from "the value was empty".
NOT_STORED = "NOT_STORED"

# Events that record how the answer was produced, or why it was not.
GENERATION_EVENTS = (
    "GPT_BUTTON_CLICKED",
    "AUTOMATIC_DRAFT_STARTED",
    "GPT_UNDERSTANDING_FINISHED",
    "GPT_DRAFT_FINISHED",
    "GPT_VALIDATOR_STARTED",
    "GPT_VALIDATOR_FINISHED",
    "GPT_VALIDATION_FAILED",
    "GPT_CORRECTIVE_REGENERATION_COMPLETED",
    "GPT_APPROVED",
    "GPT_FALLBACK_RULE",
    "GPT_DIRECT_FAILED",
    "SAFE_DRAFT_CREATED",
    "ANSWER_GENERATION_FAILED",
    "ANSWER_VALIDATION_PASSED",
    "TEMPLATE_MATCHED",
    "PRODUCT_DB_MATCHED",
    "AUTOMATIC_DRAFT_COMPLETED",
    "AUTOMATIC_DRAFT_FAILED",
)

# Events that mark the answer being abandoned or replaced.
FALLBACK_EVENTS = frozenset(
    {
        "GPT_VALIDATION_FAILED",
        "GPT_FALLBACK_RULE",
        "GPT_DIRECT_FAILED",
        "SAFE_DRAFT_CREATED",
        "ANSWER_GENERATION_FAILED",
        "AUTOMATIC_DRAFT_FAILED",
    }
)


def connect_readonly(database: str) -> sqlite3.Connection:
    """Open ``database`` through a SQLite read-only URI.

    Every write -- INSERT/UPDATE/DELETE/DDL, and the journal-mode change the
    application helper performs -- fails on this connection.
    """

    path = Path(database).resolve(strict=True)
    try:
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True
        )
    except sqlite3.OperationalError:
        # A URI carrying non-ASCII characters can fail to open on Windows.
        # The file name itself is ASCII, so open it relative to its directory.
        original_cwd = os.getcwd()
        os.chdir(path.parent)
        try:
            connection = sqlite3.connect(
                f"file:{path.name}?mode=ro", uri=True
            )
        finally:
            os.chdir(original_cwd)
    connection.row_factory = sqlite3.Row
    return connection


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _section(container: Any, key: str) -> dict[str, Any]:
    if not isinstance(container, dict):
        return {}
    value = container.get(key)
    return value if isinstance(value, dict) else {}


def _stored(value: Any) -> Any:
    """Report a missing value as NOT_STORED instead of inventing one."""

    return NOT_STORED if value is None else value


def _mask(value: Any) -> Any:
    if isinstance(value, str):
        return mask_personal_information(value)
    if isinstance(value, dict):
        return {key: _mask(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_mask(item) for item in value]
    return value


def _mask_stored(value: Any) -> Any:
    return NOT_STORED if value is None else _mask(value)


def resolve_inquiry_id(
    connection: sqlite3.Connection,
    *,
    inquiry_id: int | None,
    external_id: str | None,
) -> int:
    if inquiry_id is not None:
        return int(inquiry_id)
    row = connection.execute(
        """
        SELECT id FROM inquiries
        WHERE external_inquiry_id = ? OR source_question_id = ?
        ORDER BY id DESC LIMIT 1
        """,
        (str(external_id), str(external_id)),
    ).fetchone()
    if row is None:
        raise LookupError(f"Inquiry not found for external id: {external_id}")
    return int(row["id"])


def diagnose(connection: sqlite3.Connection, inquiry_id: int) -> dict[str, Any]:
    inquiry = connection.execute(
        "SELECT * FROM inquiries WHERE id=?", (inquiry_id,)
    ).fetchone()
    if inquiry is None:
        raise LookupError(f"Inquiry not found: {inquiry_id}")
    inquiry = dict(inquiry)
    drafts = [
        dict(row)
        for row in connection.execute(
            """
            SELECT id, is_active, provider, source, program_status,
                   validation_status, answer_strategy, original_answer,
                   edited_answer, final_answer, posted, review_status,
                   metadata_json, validator_result_json,
                   inquiry_analysis_json, created_at
              FROM answer_drafts
             WHERE inquiry_id=? ORDER BY id DESC LIMIT 5
            """,
            (inquiry_id,),
        ).fetchall()
    ]
    events = [
        dict(row)
        for row in connection.execute(
            """
            SELECT event_code, level, message, details_json, created_at
              FROM activity_logs
             WHERE inquiry_id=? AND event_code IN ({codes})
             ORDER BY id
            """.format(codes=",".join("?" for _ in GENERATION_EVENTS)),
            (inquiry_id, *GENERATION_EVENTS),
        ).fetchall()
    ]
    dps_rows = [
        dict(row)
        for row in connection.execute(
            """
            SELECT id, lookup_status, installation_date, date_parse_status,
                   required_delivery_date, queried_at
              FROM dps_lookup_results
             WHERE inquiry_id=? ORDER BY id DESC LIMIT 3
            """,
            (inquiry_id,),
        ).fetchall()
    ]

    # Where the answer was abandoned, and why.
    fallback: list[dict[str, Any]] = []
    for event in events:
        if event["event_code"] not in FALLBACK_EVENTS:
            continue
        details = _json(event["details_json"])
        fallback.append(
            {
                "event": event["event_code"],
                "at": event["created_at"],
                "message": _mask(event["message"]),
                "reason": _stored(details.get("reason")),
                "validator_failure_reason": _stored(
                    details.get("validator_failure_reason")
                ),
                "safe_error_code": _stored(details.get("safe_error_code")),
                "validator_result": _stored(details.get("validator_result")),
                "errors": _mask_stored(details.get("errors")),
                "error_type": _stored(details.get("error_type")),
                "selected_answer_route": _stored(
                    details.get("selected_answer_route")
                ),
                "generation_mode": _stored(details.get("generation_mode")),
                "gpt_called": _stored(details.get("gpt_called")),
            }
        )

    report: list[dict[str, Any]] = []
    for draft in drafts:
        metadata = _json(draft.pop("metadata_json", {}))
        validator_result = _json(draft.pop("validator_result_json", {}))
        inquiry_analysis = _json(draft.pop("inquiry_analysis_json", {}))
        hybrid = _section(metadata, "hybrid")
        gpt_draft = _section(hybrid, "draft")
        self_review = _section(hybrid, "self_review")
        validation = _section(hybrid, "validation")
        intent = _section(hybrid, "intent")
        report.append(
            {
                "draft_id": draft["id"],
                "is_active": bool(draft["is_active"]),
                "created_at": draft["created_at"],
                "provider": draft["provider"],
                "answer_source": draft["source"],
                "program_status": draft["program_status"],
                "validation_status": draft["validation_status"],
                "answer_strategy": draft["answer_strategy"],
                "review_status": draft["review_status"],
                "posted": draft["posted"],
                "selected_answer_route": _stored(
                    metadata.get("selected_answer_route")
                ),
                "generation_mode": _stored(metadata.get("generation_mode")),
                "gpt_called": _stored(metadata.get("gpt_called")),
                "fallback_used": _stored(hybrid.get("fallback_used")),
                "fallback_reason": _stored(hybrid.get("fallback_reason")),
                # What the provider actually produced, if it got that far.
                # Preserved by hybrid._fallback even when the answer was
                # discarded, so this is the evidence for CASE A vs CASE B.
                "provider_raw_answer": _mask_stored(gpt_draft.get("answer")),
                "provider_confidence": _stored(gpt_draft.get("confidence")),
                "provider_requires_review": _stored(
                    gpt_draft.get("requires_review")
                ),
                "provider_missing_information": _mask_stored(
                    gpt_draft.get("missing_information")
                ),
                "subquestions": _mask_stored(intent.get("questions")),
                "intent_requires_review": _stored(
                    intent.get("requires_review")
                ),
                # What the validator saw and decided.
                "self_review": _mask_stored(self_review or None),
                "validation_status_detail": _stored(validation.get("status")),
                "validation_passed": _stored(validation.get("passed")),
                "validation_errors": _mask_stored(validation.get("errors")),
                "validation_warnings": _mask_stored(
                    validation.get("warnings")
                ),
                "validation_review_signals": _mask_stored(
                    validation.get("review_signals")
                ),
                # Separately persisted validator/analysis columns.
                "validator_result_json": _mask_stored(
                    validator_result or None
                ),
                "inquiry_analysis_json": _mask_stored(
                    inquiry_analysis or None
                ),
                # The answers the customer would actually see.
                "stored_original_answer": _mask_stored(
                    draft.get("original_answer")
                ),
                "stored_edited_answer": _mask_stored(
                    draft.get("edited_answer")
                ),
                "stored_final_answer": _mask_stored(draft.get("final_answer")),
            }
        )

    return {
        "inquiry": {
            "inquiry_id": inquiry_id,
            "external_inquiry_id": _stored(inquiry.get("external_inquiry_id")),
            "source_question_id": _stored(inquiry.get("source_question_id")),
            "inquiry_type": _stored(inquiry.get("inquiry_type")),
            "workflow_status": _stored(inquiry.get("workflow_status")),
            "answer_status": _stored(inquiry.get("answer_status")),
            "post_status": _stored(inquiry.get("post_status")),
            "registered_at": _stored(inquiry.get("registered_at")),
            "question": _mask_stored(inquiry.get("content")),
            "order_id_present": bool(
                str(inquiry.get("order_id") or "").strip()
            ),
        },
        "fallback_trace": fallback or NOT_STORED,
        "drafts": report or NOT_STORED,
        "dps": dps_rows or NOT_STORED,
        "events": [
            {
                "event": item["event_code"],
                "level": item["level"],
                "at": item["created_at"],
            }
            for item in events
        ]
        or NOT_STORED,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--inquiry-id", type=int)
    parser.add_argument("--external-id")
    args = parser.parse_args()
    if args.inquiry_id is None and not args.external_id:
        parser.error("provide --inquiry-id or --external-id")

    connection = connect_readonly(args.database)
    try:
        inquiry_id = resolve_inquiry_id(
            connection,
            inquiry_id=args.inquiry_id,
            external_id=args.external_id,
        )
        report = diagnose(connection, inquiry_id)
    finally:
        connection.close()

    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
