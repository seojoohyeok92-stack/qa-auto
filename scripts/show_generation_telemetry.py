"""Print the provider telemetry recorded for one inquiry's latest draft.

Read-only. Answers "where did the minute go" for a single generation:
how many provider round trips it made, which stage each was, how long each
took and how large its prompt was.

    python scripts/show_generation_telemetry.py --database <path> \
        --external-id 686097134

It opens the database through a SQLite read-only URI, issues SELECT only, and
prints sizes and timings -- never prompt text, context values or the API key.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path
from typing import Any


def connect_readonly(database: str) -> sqlite3.Connection:
    path = Path(database).resolve(strict=True)
    try:
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True
        )
    except sqlite3.OperationalError:
        original = os.getcwd()
        os.chdir(path.parent)
        try:
            connection = sqlite3.connect(f"file:{path.name}?mode=ro", uri=True)
        finally:
            os.chdir(original)
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--external-id", required=True)
    args = parser.parse_args()

    connection = connect_readonly(args.database)
    try:
        inquiry = connection.execute(
            "SELECT id FROM inquiries WHERE external_inquiry_id=?"
            " OR source_question_id=? ORDER BY id DESC LIMIT 1",
            (args.external_id, args.external_id),
        ).fetchone()
        if inquiry is None:
            print(f"Inquiry not found: {args.external_id}")
            return 1
        inquiry_id = int(inquiry["id"])
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT id, created_at, provider, source, validation_status,"
                " metadata_json FROM answer_drafts WHERE inquiry_id=?"
                " ORDER BY id DESC LIMIT 3",
                (inquiry_id,),
            ).fetchall()
        ]
        events = [
            dict(row)
            for row in connection.execute(
                "SELECT event_code, created_at FROM activity_logs"
                " WHERE inquiry_id=? ORDER BY id",
                (inquiry_id,),
            ).fetchall()
        ]
    finally:
        connection.close()

    print(f"inquiry_id = {inquiry_id}")
    for row in rows:
        metadata = _json(row.pop("metadata_json", {}))
        hybrid = metadata.get("hybrid")
        hybrid = hybrid if isinstance(hybrid, dict) else {}
        telemetry = hybrid.get("provider_telemetry")
        telemetry = telemetry if isinstance(telemetry, dict) else {}
        validation = hybrid.get("validation")
        validation = validation if isinstance(validation, dict) else {}
        print(f"\n=== draft {row['id']} ({row['created_at']}) ===")
        print(f"  provider              : {row['provider']}")
        print(f"  answer_source         : {row['source']}")
        print(f"  selected_answer_route : {metadata.get('selected_answer_route')}")
        print(f"  generation_mode       : {metadata.get('generation_mode')}")
        print(f"  fallback_used         : {hybrid.get('fallback_used')}")
        print(f"  fallback_reason       : {hybrid.get('fallback_reason')}")
        print(f"  validation.passed     : {validation.get('passed')}")
        print(f"  validation.errors     : {validation.get('errors')}")
        print(f"  validation.warnings   : {validation.get('warnings')}")
        print(f"  review_signals        : {validation.get('review_signals')}")
        if not telemetry:
            print("  provider_telemetry    : NOT_STORED"
                  " (draft predates the telemetry change)")
            continue
        print(f"  provider_call_count   : {telemetry.get('provider_call_count')}")
        print(f"  tasks                 : {telemetry.get('tasks')}")
        print(f"  total_elapsed_seconds : {telemetry.get('total_elapsed_seconds')}")
        for call in telemetry.get("calls") or []:
            print(
                f"    - task={call.get('task')}"
                f" attempts={call.get('attempts')}"
                f" prompt_chars={call.get('prompt_chars')}"
                f" elapsed={call.get('elapsed_seconds')}"
                f" last_attempt={call.get('last_attempt_seconds')}"
                f" outcome={call.get('outcome')}"
            )
            total = call.get("prompt_chars")
            accounted = call.get("prompt_accounted_chars")
            unaccounted = call.get("prompt_unaccounted_chars")
            if accounted is not None:
                print(f"        total_prompt_chars      {total:>12,}")
                print(f"        accounted_component_chars {accounted:>10,}")
                print(f"        unaccounted_chars       {unaccounted:>12,}")
            components = call.get("prompt_component_chars") or {}
            if components:
                print("        TOP PROMPT COMPONENTS")
                shown = [
                    (name, chars)
                    for name, chars in components.items()
                    if not name.endswith(".count")
                ][:15]
                for name, chars in shown:
                    share = f"{chars / total:>6.1%}" if total else "     -"
                    count = components.get(f"{name}.count")
                    suffix = f"  records={count}" if count is not None else ""
                    print(f"          {name:<42}{chars:>12,}{share}{suffix}")

    print("\n=== event timeline ===")
    for item in events:
        print(f"  {item['created_at']}  {item['event_code']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
