from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from scripts import show_auto_sync_health


def test_health_script_reads_existing_state_without_writing(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    database = tmp_path / "health.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE naver_auto_sync_settings (
                id INTEGER PRIMARY KEY,
                enabled INTEGER,
                interval_minutes INTEGER
            );
            CREATE TABLE naver_auto_sync_state (
                id INTEGER PRIMARY KEY,
                status TEXT,
                owner_id TEXT,
                owner_pid INTEGER,
                lease_expires_at TEXT,
                consecutive_failures INTEGER,
                error_code TEXT,
                last_started_at TEXT,
                last_completed_at TEXT,
                next_run_at TEXT
            );
            CREATE TABLE naver_sync_runs (
                id INTEGER PRIMARY KEY,
                sync_id TEXT,
                status TEXT,
                requested_from TEXT,
                requested_to TEXT,
                started_at TEXT,
                completed_at TEXT,
                fetched_count INTEGER,
                inserted_count INTEGER,
                updated_count INTEGER,
                failed_count INTEGER,
                error_code TEXT
            );
            CREATE TABLE inquiries (registered_at TEXT);

            INSERT INTO naver_auto_sync_settings
                (id, enabled, interval_minutes) VALUES (1, 1, 10);
            INSERT INTO naver_auto_sync_state VALUES (
                1, 'RUNNING', 'server-1', 1234,
                '2026-08-24T05:05:00+00:00', 0, NULL,
                '2026-08-24T04:55:10+00:00',
                '2026-08-24T04:55:11+00:00',
                '2026-08-24T05:04:49+00:00'
            );
            INSERT INTO naver_sync_runs VALUES (
                1, 'sync-1', 'SUCCESS',
                '2026-08-17T04:54:49+00:00',
                '2026-08-24T04:54:49+00:00',
                '2026-08-24T04:55:10+00:00',
                '2026-08-24T04:55:11+00:00',
                2, 1, 1, 0, NULL
            );
            INSERT INTO inquiries VALUES ('2026-08-24T04:58:00+00:00');
            """
        )

    before = database.read_bytes()
    monkeypatch.setenv("NAVER_SYNC_ENABLED", "false")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "show_auto_sync_health.py",
            "--database",
            str(database),
            "--limit",
            "30",
        ],
    )

    assert show_auto_sync_health.main() == 0

    output = capsys.readouterr().out
    assert "owner_id" in output
    assert "last_started_at" in output
    assert "next_run_at" in output
    assert "minutes since last run" in output
    assert "last 1 runs" in output
    assert "SUCCESS" in output
    assert database.read_bytes() == before
