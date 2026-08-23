"""Print the provider telemetry recorded for one inquiry's latest draft.

Read-only. Answers "where did the minute go" for a single generation:
how many provider round trips it made, which stage each was, how long each
took and how large its prompt was.

    python scripts/show_generation_telemetry.py --database <path> \
        --external-id 686097134

It opens the database through a SQLite read-only URI, issues SELECT only, and
prints sizes and timings -- never prompt text, context values or the API key.

The event timeline prints the gap between consecutive rows, the recorded
``rerun_elapsed_seconds`` for the Streamlit re-execution that followed the
generation, and -- when the dashboard ran with ``OJE_RERUN_PROFILE=1`` -- the
per-stage split of that rerun.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime
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


def _elapsed_between(earlier: str, later: str) -> float | None:
    """Seconds between two activity-log timestamps, or None if unparsable."""

    def parse(value: str) -> datetime | None:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    start, end = parse(earlier), parse(later)
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


def render_components(
    components: dict[str, int], total: int | None, *, top: int | None = None
) -> list[str]:
    """Render the whole prompt breakdown as a size-ordered tree.

    Every component is shown, nested under its parent and sorted largest
    first, so the dominant branch is the first line and its internals sit
    directly beneath it. `.count` and `.max_record` describe the component
    they belong to and are attached to its row rather than listed separately.
    The previous rendering stopped after fifteen rows, which hid the inside
    of the one branch that mattered.
    """

    if not components:
        return []
    values = {
        name: chars
        for name, chars in components.items()
        if not name.endswith((".count", ".max_record"))
    }
    children: dict[str, list[str]] = {}
    for name in values:
        parent = name.rsplit(".", 1)[0] if "." in name else ""
        children.setdefault(parent, []).append(name)

    lines = ["TOP PROMPT COMPONENTS"]

    def emit(parent: str, depth: int) -> None:
        ordered = sorted(children.get(parent, []), key=lambda item: -values[item])
        if top is not None and depth == 0:
            ordered = ordered[:top]
        for name in ordered:
            chars = values[name]
            share = f"{chars / total:>7.1%}" if total else "       -"
            extras = []
            count = components.get(f"{name}.count")
            if count is not None:
                extras.append(f"records={count}")
            biggest = components.get(f"{name}.max_record")
            if biggest is not None:
                extras.append(f"max_record={biggest:,}")
            suffix = ("  " + "  ".join(extras)) if extras else ""
            label = "  " * depth + name.rsplit(".", 1)[-1]
            lines.append(f"  {label:<46}{chars:>12,}{share}{suffix}")
            emit(name, depth + 1)

    emit("", 0)
    return lines


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
                "SELECT event_code, created_at, details_json FROM activity_logs"
                " WHERE inquiry_id=? ORDER BY id",
                (inquiry_id,),
            ).fetchall()
        ]
        try:
            runs = [
                dict(row)
                for row in connection.execute(
                    "SELECT id, model, started_at, completed_at, duration_ms,"
                    " success, error_type, input_tokens, output_tokens,"
                    " total_tokens, input_size, output_size"
                    " FROM gpt_provider_runs WHERE inquiry_id=?"
                    " ORDER BY id DESC LIMIT 5",
                    (inquiry_id,),
                ).fetchall()
            ]
        except sqlite3.OperationalError:
            runs = []
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
        stages = telemetry.get("stage_seconds") or {}
        if stages:
            print("  stage_seconds         :")
            for name, seconds in stages.items():
                print(f"      {name:<28}{seconds:>10}s")
        else:
            print("  stage_seconds         : NOT_STORED"
                  " (draft predates the timing change)")
        budget = telemetry.get("prompt_budget") or {}
        if budget:
            print(f"  prompt_budget         : {budget.get('final_chars')} /"
                  f" {budget.get('budget_chars')}"
                  f"  within={budget.get('within_budget')}"
                  f"  dropped={budget.get('dropped')}")
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
            for line in render_components(components, total):
                print(f"        {line}")


    print("\n=== provider runs (gpt_provider_runs) ===")
    if not runs:
        print("  (none)")
    for run in runs:
        print(
            f"  run {run['id']}  model={run['model']}"
            f"  duration_ms={run['duration_ms']}"
            f"  success={run['success']}  error={run['error_type']}"
        )
        print(
            f"      started={run['started_at']}"
            f"  completed={run['completed_at']}"
        )
        print(
            f"      tokens in/out/total="
            f"{run['input_tokens']}/{run['output_tokens']}/{run['total_tokens']}"
            f"   input_size={run['input_size']}"
            f"  output_size={run['output_size']}"
        )

    print("\n=== event timeline ===")
    interesting = (
        "rerun_elapsed_seconds", "draft_id", "stage", "status",
        "error_type", "reason", "reason_code", "safe_error_code",
        "existing_draft_preserved",
    )
    previous = None
    for item in events:
        stamp = str(item["created_at"])
        gap = ""
        if previous is not None:
            delta = _elapsed_between(previous, stamp)
            if delta is not None:
                gap = f"  (+{delta:.3f}s)"
        previous = stamp
        print(f"  {stamp}  {item['event_code']}{gap}")
        detail = _json(item.get("details_json"))
        shown = {key: detail[key] for key in interesting if key in detail}
        if shown:
            print(f"        {shown}")
        profile = detail.get("rerun_profile")
        if isinstance(profile, dict):
            print(f"        rerun profile: total"
                  f" {profile.get('total_seconds')}s")
            for row in profile.get("stages") or []:
                print(f"          {str(row.get('stage')):<26}"
                      f"{row.get('elapsed_seconds'):>9}s"
                      f"  cumulative {row.get('cumulative_seconds')}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
