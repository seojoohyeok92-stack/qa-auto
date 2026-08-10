from __future__ import annotations

import json
from typing import Any

from repositories.database import Database
from repositories.inquiry_repository import deserialize_json, serialize_json


class UatRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create_run(
        self,
        *,
        correlation_id: str,
        actor: str,
        status: str,
        started_at: str,
        completed_at: str,
        summary: dict[str, Any],
    ) -> int:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO uat_runs (
                    correlation_id, actor, status, started_at,
                    completed_at, summary_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    correlation_id,
                    str(actor).strip() or "system",
                    status,
                    started_at,
                    completed_at,
                    serialize_json(summary),
                ),
            )
        return int(cursor.lastrowid)

    def create_environment_check(
        self,
        *,
        correlation_id: str,
        actor: str,
        status: str,
        valid_count: int,
        warning_count: int,
        failure_count: int,
        summary: dict[str, Any],
    ) -> int:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO environment_check_runs (
                    correlation_id, actor, status, valid_count,
                    warning_count, failure_count, summary_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    correlation_id,
                    str(actor).strip() or "system",
                    status,
                    valid_count,
                    warning_count,
                    failure_count,
                    serialize_json(summary),
                ),
            )
        return int(cursor.lastrowid)

    def create_env_comparison(
        self,
        *,
        correlation_id: str,
        actor: str,
        current_file_name: str,
        compared_file_name: str,
        status: str,
        same_count: int,
        different_count: int,
        missing_count: int,
        summary: dict[str, Any],
    ) -> int:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO env_comparison_runs (
                    correlation_id, actor, current_file_name,
                    compared_file_name, status, same_count,
                    different_count, missing_count, summary_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    correlation_id,
                    str(actor).strip() or "system",
                    str(current_file_name)[:255],
                    str(compared_file_name)[:255],
                    status,
                    same_count,
                    different_count,
                    missing_count,
                    serialize_json(summary),
                ),
            )
        return int(cursor.lastrowid)

    def recent_runs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM uat_runs ORDER BY created_at DESC, id DESC LIMIT ?",
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        result = [dict(row) for row in rows]
        for item in result:
            item["summary_json"] = deserialize_json(item["summary_json"])
        return result

