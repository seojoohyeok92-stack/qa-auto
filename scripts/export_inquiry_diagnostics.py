"""Everything needed to explain one inquiry, in a file small enough to send.

Diagnosing inquiry 325318746 meant copying a 520MB operational database onto a
development machine. What the investigation actually used was one row from
``inquiries``, one draft, its stored metadata, and the activity log for that
inquiry -- a few dozen kilobytes. This exports exactly that.

Read-only, and structurally so: the database is opened ``mode=ro`` with
``PRAGMA query_only = ON``, so a stray write fails loudly rather than silently
altering the store an investigation is supposed to observe. Nothing here calls
the answer pipeline. It reads what was recorded at the time, which is the point
-- re-running generation today would answer a different question, on today's
code, and destroy the evidence being collected.

Privacy is not re-implemented. Every free-text field goes through
``LearningPrivacyService.mask``, the path the rest of the project already uses;
a second masking scheme would drift from the first and the drift would be a
leak. Identifiers that must stay correlatable -- an order number -- are reduced
to their last four digits from the column itself, never carried in prose.

Before anything is written the whole document is walked once more for keys that
look like credentials. That check has nothing to do with what this file
collects; it is there because an export is a file that leaves the building.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from services.learning_privacy_service import LearningPrivacyService


SCHEMA_VERSION = "inquiry-diagnostics-1"
DEFAULT_DATABASE = Path("data") / "oje_automation.db"
DEFAULT_OUTPUT_DIR = Path("diagnostics")

NOT_AVAILABLE = "not_available"

# How much of the answer body is worth carrying. Enough to see which template
# spoke; not the whole customer-facing letter.
ANSWER_EXCERPT = 600
MESSAGE_EXCERPT = 300

# Keys whose values are never diagnostic and always dangerous.
SECRET_KEY = re.compile(
    r"token|secret|password|passwd|authorization|cookie|api[_-]?key"
    r"|credential|private[_-]?key|session[_-]?id|bearer",
    re.IGNORECASE,
)
REDACTED = "<redacted-secret>"

# Identifiers this export is keyed on, which the masking pass must not touch.
#
# The privacy path treats any 8-15 digit run as a product order number, and a
# Naver inquiry id is nine digits -- so the first run of this tool masked the
# very id the operator uses to correlate the file, and named the file after the
# redaction token. These keys are written by this module from identifier
# columns, never from customer prose, so exempting them exposes nothing the
# operator did not already type on the command line.
IDENTIFIER_KEYS = frozenset({
    "naver_inquiry_id", "external_inquiry_id", "internal_id", "draft_id",
    "posted_draft_id", "product_id", "store_code", "source_type",
    "schema_version", "exported_at", "source_database",
    "learning_example_id", "historical_case_id", "answer_draft_id",
    "retry_of_attempt_id", "correlation_id",
})

# Stored blobs that are large, mostly irrelevant to routing, and full of raw
# third-party payloads. Their presence is recorded; their contents are not.
BULK_KEYS = frozenset({
    "raw_json", "raw_result_json", "source_metadata_json", "raw_response",
    "payload", "response_body", "selected_facts_json",
})

_privacy = LearningPrivacyService()


class DiagnosticsError(RuntimeError):
    """The export cannot be produced, and no partial file is written."""


# --- reading -----------------------------------------------------------------


def open_read_only(path: Path) -> sqlite3.Connection:
    """A connection that cannot write, and says so twice."""

    if not path.is_file():
        raise DiagnosticsError(f"database not found: {path}")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _rows(
    connection: sqlite3.Connection, table: str, sql: str, params: tuple
) -> list[dict[str, Any]]:
    """Query a table that an older deployment may simply not have."""

    if not _table_exists(connection, table):
        return []
    try:
        return [dict(row) for row in connection.execute(sql, params)]
    except sqlite3.Error:
        # A column added after this export was written is not a reason to fail
        # the whole diagnosis.
        return []


def _json(value: object) -> Any:
    """Stored JSON, or a marker saying it could not be read."""

    if value in (None, ""):
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return {"_unparsed": True}


# --- privacy -----------------------------------------------------------------


def mask_text(value: object, limit: int | None = None) -> str | None:
    """Free text, through the project's own masking path."""

    if value in (None, ""):
        return None
    masked = _privacy.mask(value)
    if limit is not None and len(masked) > limit:
        return masked[:limit] + "…"
    return masked


def last4(value: object) -> str | None:
    """An identifier reduced to what correlation needs.

    Taken from the column rather than from prose: a number in a sentence is
    masked wholesale by the privacy path, and rightly so.
    """

    digits = re.sub(r"\D", "", str(value or ""))
    if not digits:
        return None
    return f"last4:{digits[-4:]}" if len(digits) >= 4 else "last4:short"


def scrub(value: Any, *, path: str = "") -> Any:
    """Walk the finished document and remove anything credential-shaped.

    Defence in depth. Nothing above is expected to produce a token; this exists
    because the file leaves the machine, and because a table gaining a column
    later should not turn an export into a leak.
    """

    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if SECRET_KEY.search(name):
                cleaned[name] = REDACTED
                continue
            if name in BULK_KEYS:
                cleaned[name] = (
                    "<omitted-bulk>" if item not in (None, "") else None
                )
                continue
            if name in IDENTIFIER_KEYS and not isinstance(item, (Mapping, list)):
                cleaned[name] = item
                continue
            cleaned[name] = scrub(item, path=f"{path}.{name}")
        return cleaned
    if isinstance(value, list):
        return [scrub(item, path=path) for item in value]
    if isinstance(value, str):
        return _privacy.mask(value) if value else value
    return value


# --- sections ----------------------------------------------------------------


def resolve_inquiry(
    connection: sqlite3.Connection,
    *,
    naver_id: str | None,
    internal_id: int | None,
) -> dict[str, Any]:
    """Find the inquiry by whichever identifier the caller has.

    ``source_question_id`` is the Naver identifier and is stored as text;
    ``id`` is this database's own. They are looked up separately rather than
    guessed at, because the two number spaces overlap.
    """

    if internal_id is not None:
        row = connection.execute(
            "SELECT * FROM inquiries WHERE id=?", (int(internal_id),)
        ).fetchone()
        if row is None:
            raise DiagnosticsError(f"no inquiry with internal id {internal_id}")
        return dict(row)

    row = connection.execute(
        "SELECT * FROM inquiries WHERE source_question_id=? "
        "   OR external_inquiry_id=? ORDER BY id DESC LIMIT 1",
        (str(naver_id), str(naver_id)),
    ).fetchone()
    if row is None:
        raise DiagnosticsError(f"no inquiry with Naver id {naver_id}")
    return dict(row)


def inquiry_section(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "internal_id": row.get("id"),
        "naver_inquiry_id": row.get("source_question_id"),
        "external_inquiry_id": row.get("external_inquiry_id"),
        "store_code": row.get("store_code"),
        "source_type": row.get("source_type"),
        "inquiry_type": row.get("inquiry_type"),
        "title": mask_text(row.get("title")),
        "content": mask_text(row.get("content")),
        "product_name": mask_text(row.get("product_name")),
        "product_id": row.get("product_id"),
        "option_name": mask_text(row.get("option_name")),
        "order_id": last4(row.get("order_id")),
        "product_order_id": last4(row.get("product_order_id")),
        "order_status": row.get("order_status"),
        "is_private": row.get("is_private"),
        "workflow_status": row.get("workflow_status"),
        "answer_status": row.get("answer_status"),
        "approval_status": row.get("approval_status"),
        "phase9_status": row.get("phase9_status"),
        "post_status": row.get("post_status"),
        "source_answered": row.get("source_answered"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "registered_at": row.get("registered_at"),
    }


def _analysis_of(metadata: Mapping[str, Any]) -> dict[str, Any]:
    plan = metadata.get("processing_plan")
    plan = plan if isinstance(plan, Mapping) else {}
    analysis = plan.get("analysis")
    return dict(analysis) if isinstance(analysis, Mapping) else {}


def analysis_section(metadata: Mapping[str, Any], draft: Mapping[str, Any]):
    stored = _json(draft.get("inquiry_analysis_json"))
    analysis = _analysis_of(metadata)
    if not analysis and isinstance(stored, Mapping):
        analysis = dict(stored)
    if not analysis:
        return NOT_AVAILABLE
    keep = (
        "inquiry_type", "question_category", "inquiry_subtype",
        "detected_intent", "confidence", "manual_review_required",
        "requires_order_lookup", "requires_dps_lookup", "answer_strategy",
        "can_generate_answer", "can_execute_dps_lookup", "order_id_status",
        "delivery_related", "delivery_question", "reasons",
    )
    known = {name: analysis.get(name) for name in keep if name in analysis}
    extra = sorted(set(analysis) - set(keep))
    known["_other_keys_present"] = extra
    return known


def atomic_section(metadata: Mapping[str, Any]):
    completeness = metadata.get("atomic_completeness")
    breakdown = metadata.get("question_breakdown")
    if not isinstance(completeness, Mapping) and breakdown in (None, ""):
        return NOT_AVAILABLE
    payload: dict[str, Any] = {}
    if isinstance(completeness, Mapping):
        payload["completeness"] = {
            key: completeness.get(key)
            for key in ("answered", "unresolved", "undetermined",
                        "total_questions", "completed", "uncovered_topics")
        }
        questions = completeness.get("questions")
        if isinstance(questions, list):
            payload["questions"] = [
                {
                    "question": mask_text(item.get("question")),
                    "status": item.get("status"),
                    "covered_topics": item.get("covered_topics"),
                    "uncovered_topics": item.get("uncovered_topics"),
                }
                for item in questions if isinstance(item, Mapping)
            ]
    if breakdown not in (None, ""):
        payload["question_breakdown"] = _json(breakdown)
    payload["question_count"] = metadata.get("question_count")
    return payload


def order_dps_section(
    connection: sqlite3.Connection,
    inquiry: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    lookups = _rows(
        connection, "dps_lookup_results",
        "SELECT * FROM dps_lookup_results WHERE inquiry_id=? "
        " ORDER BY id DESC LIMIT 5", (inquiry.get("id"),),
    )
    return {
        "order_id_present": bool(inquiry.get("order_id")),
        "order_id": last4(inquiry.get("order_id")),
        "order_id_status": metadata.get("order_id_status"),
        "order_lookup_status": metadata.get("order_lookup_status"),
        "requires_order_lookup": metadata.get("requires_order_lookup"),
        "requires_dps_lookup": metadata.get("requires_dps_lookup"),
        "dps_lookup_attempted": metadata.get("dps_lookup_attempted"),
        "dps_lookup_status": metadata.get("dps_lookup_status"),
        "delivery_date_found": metadata.get("delivery_date_found"),
        "dps_lookup_rows": [
            {
                "id": row.get("id"),
                "order_id": last4(row.get("order_id")),
                "lookup_status": row.get("lookup_status"),
                "error_code": row.get("error_code"),
                "installation_date_present": bool(row.get("installation_date")),
                "installation_date_source": row.get("installation_date_source"),
                "required_delivery_date_present": bool(
                    row.get("required_delivery_date")
                ),
                "date_parse_status": row.get("date_parse_status"),
                "cached": row.get("cached"),
                "queried_at": row.get("queried_at"),
                "expires_at": row.get("expires_at"),
            }
            for row in lookups
        ] or NOT_AVAILABLE,
    }


def routing_section(metadata: Mapping[str, Any]) -> dict[str, Any]:
    guard = metadata.get("product_fact_guard")
    guard = guard if isinstance(guard, Mapping) else {}
    return {
        "selected_answer_route": metadata.get("selected_answer_route"),
        "answer_source": metadata.get("answer_source"),
        "answer_type": metadata.get("answer_type"),
        "generation_mode": metadata.get("generation_mode"),
        "template_id": metadata.get("template_id"),
        "template_name": metadata.get("template_name"),
        "template_match_kind": metadata.get("template_match_kind"),
        "template_version": metadata.get("template_version"),
        "reason_code": metadata.get("reason_code"),
        "question_category": metadata.get("question_category"),
        "gpt_called": metadata.get("gpt_called"),
        "requires_manual_review": metadata.get("requires_manual_review"),
        "product_fact_guard": {
            "classification": guard.get("classification"),
            "sensitive": guard.get("sensitive"),
            "current_fact_verified": guard.get("current_fact_verified"),
            "current_fact_source": guard.get("current_fact_source"),
            "auto_post_allowed": guard.get("auto_post_allowed"),
        } if guard else NOT_AVAILABLE,
    }


def semantic_section(metadata: Mapping[str, Any]):
    analysis = metadata.get("semantic_analysis")
    support = metadata.get("semantic_action_support")
    if not isinstance(analysis, Mapping) and not isinstance(support, Mapping):
        return NOT_AVAILABLE
    payload: dict[str, Any] = {}
    if isinstance(analysis, Mapping):
        router = analysis.get("router")
        router = router if isinstance(router, Mapping) else {}
        semantic = analysis.get("semantic")
        semantic = semantic if isinstance(semantic, Mapping) else {}
        trace = analysis.get("trace")
        trace = trace if isinstance(trace, Mapping) else {}
        payload["router"] = {
            "use_semantic": router.get("use_semantic"),
            "reasons": router.get("reasons"),
        }
        payload["called"] = analysis.get("called")
        payload["decision_value"] = analysis.get("decision_value")
        payload["result"] = {
            "primary_action": semantic.get("primary_action"),
            "secondary_actions": semantic.get("secondary_actions"),
            "request_type": semantic.get("request_type"),
            "objects": semantic.get("objects"),
            "deadline": semantic.get("deadline"),
            "confidence": semantic.get("confidence"),
            "source": semantic.get("source"),
            "reason": semantic.get("reason"),
        } if semantic else NOT_AVAILABLE
        payload["trace"] = {
            "outcome": trace.get("outcome"),
            "cache_hit": trace.get("cache_hit"),
            "latency_ms": trace.get("latency_ms"),
        } if trace else NOT_AVAILABLE
    payload["action_support"] = dict(support) if isinstance(support, Mapping) \
        else NOT_AVAILABLE
    return payload


def verdict_section(
    draft: Mapping[str, Any], metadata: Mapping[str, Any]
) -> dict[str, Any]:
    validator = _json(draft.get("validator_result_json"))
    validator = validator if isinstance(validator, Mapping) else {}
    coverage = metadata.get("semantic_coverage")
    coverage = coverage if isinstance(coverage, Mapping) else {}
    return {
        "validation_status": draft.get("validation_status"),
        "validator": {
            "passed": validator.get("passed"),
            "status": validator.get("status"),
            "errors": validator.get("errors"),
            "warnings": validator.get("warnings"),
            "reasons": validator.get("reasons"),
        } if validator else NOT_AVAILABLE,
        "semantic_coverage": {
            "status": coverage.get("status"),
            "score": coverage.get("score"),
            "phase": coverage.get("phase"),
            "reason": coverage.get("reason"),
            "covered_subquestions": coverage.get("covered_subquestions"),
            "uncovered_subquestions": coverage.get("uncovered_subquestions"),
            "answer_topics": coverage.get("answer_topics"),
            "subquestions": [
                {
                    "question": mask_text(item.get("question")),
                    "status": item.get("status"),
                    "reason": item.get("reason"),
                    "topics": item.get("topics"),
                }
                for item in (coverage.get("subquestions") or [])
                if isinstance(item, Mapping)
            ],
        } if coverage else NOT_AVAILABLE,
    }


def draft_section(draft: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "draft_id": draft.get("id"),
        "exists": True,
        "source": draft.get("source"),
        "provider": draft.get("provider"),
        "category": draft.get("category"),
        "reason": mask_text(draft.get("reason")),
        "answer_strategy": draft.get("answer_strategy"),
        "review_status": draft.get("review_status"),
        "program_status": draft.get("program_status"),
        "is_active": draft.get("is_active"),
        "stale": draft.get("stale"),
        "stale_reason": draft.get("stale_reason"),
        "posted": draft.get("posted"),
        "posted_at": draft.get("posted_at"),
        "created_at": draft.get("created_at"),
        "answer_excerpt": mask_text(
            draft.get("final_answer") or draft.get("edited_answer")
            or draft.get("original_answer"),
            ANSWER_EXCERPT,
        ),
        "answer_length": len(str(draft.get("original_answer") or "")),
    }


def auto_post_section(
    connection: sqlite3.Connection, inquiry: Mapping[str, Any]
) -> dict[str, Any]:
    attempts = _rows(
        connection, "naver_post_attempts",
        "SELECT * FROM naver_post_attempts WHERE inquiry_id=? "
        " ORDER BY id DESC LIMIT 10", (inquiry.get("id"),),
    )
    posted = _rows(
        connection, "naver_posted_answers",
        "SELECT id, inquiry_id, fetch_status, author_type, provenance, "
        "       source_api, is_current, posted_at FROM naver_posted_answers "
        " WHERE inquiry_id=? ORDER BY id DESC LIMIT 5", (inquiry.get("id"),),
    )
    return {
        "post_status": inquiry.get("post_status"),
        "posted_at": inquiry.get("posted_at"),
        "post_error_code": inquiry.get("post_error_code"),
        "post_http_status": inquiry.get("post_http_status"),
        "post_actor": inquiry.get("post_actor"),
        "posted_draft_id": inquiry.get("posted_draft_id"),
        "attempts": [
            {
                "id": row.get("id"),
                "status": row.get("status"),
                "method": row.get("method"),
                "endpoint_kind": row.get("endpoint_kind"),
                "http_status": row.get("http_status"),
                "error_code": row.get("error_code"),
                "actor": row.get("actor"),
                # The key itself is never exported; whether one existed is what
                # tells you duplicate prevention was in play.
                "idempotency_key_present": bool(row.get("idempotency_key")),
                "retry_of_attempt_id": row.get("retry_of_attempt_id"),
                "started_at": row.get("started_at"),
                "completed_at": row.get("completed_at"),
            }
            for row in attempts
        ] or NOT_AVAILABLE,
        "attempt_count": len(attempts),
        "observed_posted_answers": posted or NOT_AVAILABLE,
    }


def evidence_section(
    connection: sqlite3.Connection, inquiry: Mapping[str, Any]
) -> dict[str, Any]:
    """Which Learning was considered, by identifier -- never by body.

    The question this answers is "why did that answer appear", and the row id
    is enough to look one up. Copying the text would turn a diagnosis into a
    partial export of the Learning store.
    """

    rows = _rows(
        connection, "answer_learning_provenance",
        "SELECT * FROM answer_learning_provenance WHERE inquiry_id=? "
        " ORDER BY id", (inquiry.get("id"),),
    )
    return {
        "learning_references": [
            {
                "learning_example_id": row.get("learning_example_id"),
                "historical_case_id": row.get("historical_case_id"),
                "reference_kind": row.get("reference_kind"),
                "source_label": row.get("source_label"),
                "relevance": row.get("relevance"),
                "included_in_prompt": row.get("included_in_prompt"),
                "usage_status": row.get("usage_status"),
                "usage_reason": row.get("usage_reason"),
                "matched_subquestion": mask_text(row.get("matched_subquestion")),
                "answer_support_score": row.get("answer_support_score"),
                "evidence_coverage": row.get("evidence_coverage"),
                "system_verified_usage": row.get("system_verified_usage"),
            }
            for row in rows
        ] or NOT_AVAILABLE,
        "learning_reference_count": len(rows),
        "note": "identifiers only; Learning bodies are not exported",
    }


def product_fact_section(metadata: Mapping[str, Any]) -> dict[str, Any]:
    guard = metadata.get("product_fact_guard")
    guard = guard if isinstance(guard, Mapping) else {}
    facts = guard.get("product_fact_claims_supported")
    return {
        "classification": guard.get("classification") or NOT_AVAILABLE,
        "sensitive": guard.get("sensitive"),
        "current_fact_verified": guard.get("current_fact_verified"),
        "current_fact_source": guard.get("current_fact_source"),
        "claims_supported": facts if isinstance(facts, (list, dict, bool))
        else NOT_AVAILABLE,
        "note": "identifiers and verdicts only; product_facts.db is not read",
    }


def activity_section(
    connection: sqlite3.Connection, inquiry: Mapping[str, Any], limit: int
) -> list[dict[str, Any]] | str:
    rows = _rows(
        connection, "activity_logs",
        "SELECT id, level, event_code, message, details_json, created_at "
        "  FROM activity_logs WHERE inquiry_id=? ORDER BY id LIMIT ?",
        (inquiry.get("id"), int(limit)),
    )
    if not rows:
        return NOT_AVAILABLE
    return [
        {
            "at": row.get("created_at"),
            "level": row.get("level"),
            "event": row.get("event_code"),
            "message": mask_text(row.get("message"), MESSAGE_EXCERPT),
            "details": _json(row.get("details_json")),
        }
        for row in rows
    ]


def workflow_section(
    connection: sqlite3.Connection, inquiry: Mapping[str, Any]
):
    rows = _rows(
        connection, "workflow_steps",
        "SELECT step_code, step_status, attempt_count, last_error_code, "
        "       started_at, completed_at FROM workflow_steps "
        " WHERE inquiry_id=? ORDER BY id", (inquiry.get("id"),),
    )
    return rows or NOT_AVAILABLE


# --- assembly ----------------------------------------------------------------


def build(
    connection: sqlite3.Connection,
    *,
    naver_id: str | None = None,
    internal_id: int | None = None,
    activity_limit: int = 400,
    database_path: Path | None = None,
) -> dict[str, Any]:
    inquiry = resolve_inquiry(
        connection, naver_id=naver_id, internal_id=internal_id
    )
    drafts = _rows(
        connection, "answer_drafts",
        "SELECT * FROM answer_drafts WHERE inquiry_id=? ORDER BY id DESC",
        (inquiry.get("id"),),
    )
    draft = drafts[0] if drafts else {}
    metadata = _json(draft.get("metadata_json"))
    metadata = metadata if isinstance(metadata, Mapping) else {}

    document = {
        "export": {
            "schema_version": SCHEMA_VERSION,
            "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
            # The file name, never the path: where the database sits on an
            # operator's machine is not diagnostic information.
            "source_database": (
                database_path.name if database_path else NOT_AVAILABLE
            ),
            "internal_id": inquiry.get("id"),
            "naver_inquiry_id": inquiry.get("source_question_id"),
            "draft_count": len(drafts),
        },
        "inquiry": inquiry_section(inquiry),
        "analysis": analysis_section(metadata, draft),
        "atomic_questions": atomic_section(metadata),
        "order_and_dps": order_dps_section(connection, inquiry, metadata),
        "answer_routing": routing_section(metadata) if metadata
        else NOT_AVAILABLE,
        "semantic": semantic_section(metadata),
        "verdicts": verdict_section(draft, metadata) if draft
        else NOT_AVAILABLE,
        "draft": draft_section(draft) if draft
        else {"exists": False, "draft_id": None},
        "auto_post": auto_post_section(connection, inquiry),
        "evidence": evidence_section(connection, inquiry),
        "product_facts": product_fact_section(metadata),
        "workflow_steps": workflow_section(connection, inquiry),
        "activity": activity_section(connection, inquiry, activity_limit),
    }
    return scrub(document)


def write_export(
    document: Mapping[str, Any], *, output_dir: Path
) -> Path:
    """Write the file, and never over an existing one."""

    output_dir.mkdir(parents=True, exist_ok=True)
    export = document.get("export") or {}
    label = (
        export.get("naver_inquiry_id")
        or f"internal{export.get('internal_id')}"
    )
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"inquiry_{label}_{stamp}.json"
    suffix = 1
    while path.exists():
        path = output_dir / f"inquiry_{label}_{stamp}_{suffix}.json"
        suffix += 1
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    identifier = parser.add_mutually_exclusive_group(required=True)
    identifier.add_argument(
        "--inquiry", help="Naver inquiry id, e.g. 325318746")
    identifier.add_argument(
        "--internal-id", type=int, help="this database's own inquiries.id")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--activity-limit", type=int, default=400,
        help="most recent activity rows to include")
    parser.add_argument(
        "--stdout", action="store_true",
        help="print the document instead of writing a file")
    args = parser.parse_args(argv)

    try:
        connection = open_read_only(args.database)
    except DiagnosticsError as error:
        parser.error(str(error))
    try:
        document = build(
            connection,
            naver_id=args.inquiry,
            internal_id=args.internal_id,
            activity_limit=args.activity_limit,
            database_path=args.database,
        )
    except DiagnosticsError as error:
        parser.error(str(error))
    finally:
        connection.close()

    if args.stdout:
        print(json.dumps(document, ensure_ascii=False, indent=1))
        return 0
    path = write_export(document, output_dir=args.out)
    size = path.stat().st_size
    print(f"exported -> {path}  ({size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
