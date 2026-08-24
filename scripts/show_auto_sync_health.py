"""Show whether the Naver auto sync is actually running on its interval.

Read-only. Opens the database through a SQLite read-only URI, issues SELECT
only, and never syncs, posts or writes anything.

    python scripts/show_auto_sync_health.py --database <path>

The dashboard's sync panel describes one run; this describes the cadence. It
prints the scheduler state, the gap between consecutive runs (so a stalled
scheduler is visible as a gap much larger than the configured interval), and
whether each run's query window ended at the time that run started -- which is
what determines whether an inquiry registered between two runs was collected.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


def connect_readonly(database: str) -> sqlite3.Connection:
    path = Path(database).resolve(strict=True)
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        original = os.getcwd()
        os.chdir(path.parent)
        try:
            connection = sqlite3.connect(f"file:{path.name}?mode=ro", uri=True)
        finally:
            os.chdir(original)
    connection.row_factory = sqlite3.Row
    return connection


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed


def _kst(value: Any) -> str:
    parsed = _parse(value)
    if parsed is None:
        return "-"
    try:
        from zoneinfo import ZoneInfo

        return parsed.astimezone(ZoneInfo("Asia/Seoul")).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except Exception:  # pragma: no cover - tzdata missing
        return str(value)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    connection = connect_readonly(args.database)
    try:
        state = connection.execute(
            "SELECT * FROM naver_auto_sync_state WHERE id=1"
        ).fetchone()
        settings = connection.execute(
            "SELECT * FROM naver_auto_sync_settings WHERE id=1"
        ).fetchone()
        runs = [
            dict(row)
            for row in connection.execute(
                "SELECT id, sync_id, status, requested_from, requested_to,"
                " started_at, completed_at, fetched_count, inserted_count,"
                " updated_count, failed_count, error_code"
                " FROM naver_sync_runs ORDER BY id DESC LIMIT ?",
                (max(1, int(args.limit)),),
            ).fetchall()
        ]
        newest = connection.execute(
            "SELECT MAX(registered_at) FROM inquiries"
        ).fetchone()[0]
    finally:
        connection.close()

    settings = dict(settings) if settings is not None else {}
    state = dict(state) if state is not None else {}
    interval = int(settings.get("interval_minutes") or 0)
    print("=== auto sync settings / state ===")
    if settings:
        print(f"  enabled            : {settings.get('enabled')}")
        print(f"  interval_minutes   : {interval}")
    if state:
        for key in (
            "status",
            "owner_id",
            "owner_pid",
            "lease_expires_at",
            "consecutive_failures",
            "error_code",
        ):
            print(f"  {key:<19}: {state.get(key)}")
        print(f"  last_started_at    : {_kst(state.get('last_started_at'))} KST")
        print(f"  last_completed_at  : {_kst(state.get('last_completed_at'))} KST")
        print(f"  next_run_at        : {_kst(state.get('next_run_at'))} KST")

    now = datetime.now().astimezone()
    print(f"\n  server clock now   : {now.strftime('%Y-%m-%d %H:%M:%S')} "
          f"({now.tzname()})")
    last_started = _parse(state.get("last_started_at"))
    if last_started is not None:
        age = (now - last_started).total_seconds() / 60.0
        verdict = "OK" if interval and age <= interval * 2 + 2 else "STALLED?"
        print(f"  minutes since last run: {age:.1f}  -> {verdict}")
    print(f"  newest inquiry in DB : {_kst(newest)} KST")

    print(f"\n=== last {len(runs)} runs (newest first) ===")
    print(f"  {'id':>5} {'status':<12}{'started (KST)':<21}"
          f"{'gap':>7}  {'window ends (KST)':<21}{'lag':>6}  new/fetch")
    previous_start: datetime | None = None
    for run in runs:
        started = _parse(run["started_at"])
        window_end = _parse(run["requested_to"])
        gap = ""
        if previous_start is not None and started is not None:
            minutes = (previous_start - started).total_seconds() / 60.0
            gap = f"{minutes:.1f}m"
        # How far the window's end trailed the moment the run began. A healthy
        # run reads the clock when it starts, so this is ~0.
        lag = ""
        if started is not None and window_end is not None:
            lag = f"{(started - window_end).total_seconds():.0f}s"
        print(
            f"  {run['id']:>5} {str(run['status']):<12}"
            f"{_kst(run['started_at']):<21}{gap:>7}  "
            f"{_kst(run['requested_to']):<21}{lag:>6}  "
            f"{run['inserted_count']}/{run['fetched_count']}"
            + (f"  {run['error_code']}" if run["error_code"] else "")
        )
        previous_start = started

    print("\n  gap  = minutes between this run and the next newer one;"
          " should match interval_minutes")
    print("  lag  = window end vs. run start; should be ~0s."
          " A large lag means the window was stale.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
