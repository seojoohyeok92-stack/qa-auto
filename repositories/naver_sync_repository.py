from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
import sqlite3
from typing import Any

from repositories.database import Database
from repositories.inquiry_repository import deserialize_json, serialize_json
from config import validate_auto_sync_interval


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class NaverSyncRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _row(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        value["details_json"] = deserialize_json(value.get("details_json"))
        return value

    def start(
        self,
        *,
        sync_id: str,
        store_id: str,
        inquiry_type: str,
        requested_from: str,
        requested_to: str,
    ) -> dict[str, Any]:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO naver_sync_runs (
                    sync_id, store_id, inquiry_type, started_at, status,
                    requested_from, requested_to
                ) VALUES (?, ?, ?, ?, 'RUNNING', ?, ?)
                """,
                (
                    sync_id,
                    store_id,
                    inquiry_type,
                    _utc_now(),
                    requested_from,
                    requested_to,
                ),
            )
        return self.get(sync_id) or {}

    def finish(
        self,
        sync_id: str,
        *,
        status: str,
        fetched_count: int,
        inserted_count: int,
        updated_count: int,
        unchanged_count: int,
        skipped_count: int,
        failed_count: int,
        duration_ms: int,
        error_code: str | None = None,
        error_message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in {"SUCCESS", "PARTIAL_SYNC", "FAILED", "SKIPPED"}:
            raise ValueError(f"Invalid sync status: {status}")
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE naver_sync_runs
                SET completed_at = ?, status = ?, fetched_count = ?,
                    inserted_count = ?, updated_count = ?,
                    unchanged_count = ?, skipped_count = ?,
                    failed_count = ?, error_code = ?, error_message = ?,
                    duration_ms = ?, details_json = ?
                WHERE sync_id = ?
                """,
                (
                    _utc_now(),
                    status,
                    int(fetched_count),
                    int(inserted_count),
                    int(updated_count),
                    int(unchanged_count),
                    int(skipped_count),
                    int(failed_count),
                    error_code,
                    str(error_message or "")[:500] or None,
                    max(0, int(duration_ms)),
                    serialize_json(details or {}),
                    sync_id,
                ),
            )
        return self.get(sync_id) or {}

    def get(self, sync_id: str) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM naver_sync_runs WHERE sync_id = ?",
                (sync_id,),
            ).fetchone()
        return self._row(row)

    def latest(self, *, successful_only: bool = False) -> dict[str, Any] | None:
        clause = "WHERE status = 'SUCCESS'" if successful_only else ""
        with self.database.connection() as connection:
            row = connection.execute(
                f"""
                SELECT * FROM naver_sync_runs
                {clause}
                ORDER BY COALESCE(completed_at, started_at) DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
        return self._row(row)

    def latest_success_for(
        self, *, store_code: str, inquiry_type: str
    ) -> dict[str, Any] | None:
        """Return the latest successful list snapshot covering one target."""

        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM naver_sync_runs
                WHERE status='SUCCESS'
                  AND instr(',' || replace(store_id, ' ', '') || ',',
                            ',' || ? || ',') > 0
                  AND instr(',' || replace(inquiry_type, ' ', '') || ',',
                            ',' || ? || ',') > 0
                ORDER BY COALESCE(completed_at, started_at) DESC, id DESC
                LIMIT 1
                """,
                (
                    str(store_code or "").strip(),
                    str(inquiry_type or "").strip().upper(),
                ),
            ).fetchone()
        return self._row(row)

    def acquire_lock(
        self,
        *,
        store_id: str,
        sync_id: str,
        ttl_seconds: int,
        sync_type: str = "MANUAL",
        owner_id: str | None = None,
    ) -> bool:
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=max(30, int(ttl_seconds)))
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM naver_sync_locks WHERE expires_at <= ?",
                (now.isoformat(timespec="milliseconds"),),
            )
            try:
                connection.execute(
                    """
                    INSERT INTO naver_sync_locks(
                        store_id, sync_id, acquired_at, expires_at,
                        sync_type, owner_id, status
                    ) VALUES (?, ?, ?, ?, ?, ?, 'RUNNING')
                    """,
                    (
                        store_id,
                        sync_id,
                        now.isoformat(timespec="milliseconds"),
                        expires.isoformat(timespec="milliseconds"),
                        str(sync_type or "MANUAL").upper(),
                        str(owner_id or "") or None,
                    ),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def release_locks(self, sync_id: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM naver_sync_locks WHERE sync_id = ?",
                (sync_id,),
            )

    def active_locks(self) -> list[dict[str, Any]]:
        now = _utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM naver_sync_locks WHERE expires_at <= ?",
                (now,),
            )
            rows = connection.execute(
                """
                SELECT store_id, sync_id, acquired_at, expires_at,
                       sync_type, owner_id, status
                FROM naver_sync_locks
                ORDER BY acquired_at
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def ensure_auto_settings(
        self, *, enabled: bool, interval_minutes: int
    ) -> dict[str, Any]:
        interval = validate_auto_sync_interval(interval_minutes)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT updated_at FROM naver_auto_sync_settings WHERE id=1"
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO naver_auto_sync_settings(
                        id, enabled, interval_minutes, updated_at
                    ) VALUES (1, ?, ?, NULL)
                    """,
                    (1 if enabled else 0, interval),
                )
            elif row["updated_at"] is None:
                connection.execute(
                    """
                    UPDATE naver_auto_sync_settings
                    SET enabled=?, interval_minutes=?
                    WHERE id=1
                    """,
                    (1 if enabled else 0, interval),
                )
        return self.auto_settings()

    def auto_settings(self) -> dict[str, Any]:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM naver_auto_sync_settings WHERE id=1"
            ).fetchone()
        if row is None:
            return {
                "enabled": False,
                "interval_minutes": 10,
                "updated_at": None,
            }
        value = dict(row)
        value["enabled"] = bool(value["enabled"])
        return value

    def save_auto_settings(
        self, *, enabled: bool, interval_minutes: int
    ) -> dict[str, Any]:
        interval = validate_auto_sync_interval(interval_minutes)
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE naver_auto_sync_settings
                SET enabled=?, interval_minutes=?, updated_at=?
                WHERE id=1
                """,
                (1 if enabled else 0, interval, _utc_now()),
            )
        return self.auto_settings()

    def auto_state(self) -> dict[str, Any]:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM naver_auto_sync_state WHERE id=1"
            ).fetchone()
        return dict(row) if row is not None else {"status": "STOPPED"}

    def acquire_scheduler_lease(
        self,
        *,
        owner_id: str,
        ttl_seconds: int = 90,
    ) -> bool:
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=max(30, int(ttl_seconds)))
        now_text = now.isoformat(timespec="milliseconds")
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT owner_id, lease_expires_at
                FROM naver_auto_sync_state WHERE id=1
                """
            ).fetchone()
            if row is None:
                return False
            lease_expired = (
                not row["lease_expires_at"]
                or str(row["lease_expires_at"]) <= now_text
            )
            if row["owner_id"] not in (None, "", owner_id) and not lease_expired:
                return False
            connection.execute(
                """
                UPDATE naver_auto_sync_state
                SET owner_id=?, owner_pid=?, lease_expires_at=?,
                    status=CASE WHEN status='RUNNING' THEN status ELSE 'IDLE' END,
                    updated_at=?
                WHERE id=1
                """,
                (
                    owner_id,
                    os.getpid(),
                    expires.isoformat(timespec="milliseconds"),
                    now_text,
                ),
            )
        return True

    def release_scheduler_lease(self, owner_id: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE naver_auto_sync_state
                SET owner_id=NULL, owner_pid=NULL, lease_expires_at=NULL,
                    status='STOPPED', updated_at=?
                WHERE id=1 AND owner_id=?
                """,
                (_utc_now(), owner_id),
            )

    def set_auto_next_run(
        self, *, owner_id: str, next_run_at: str
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE naver_auto_sync_state
                SET next_run_at=?, status='IDLE', updated_at=?
                WHERE id=1 AND owner_id=?
                """,
                (next_run_at, _utc_now(), owner_id),
            )

    def mark_auto_running(self, *, owner_id: str) -> None:
        now = _utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE naver_auto_sync_state
                SET status='RUNNING', last_started_at=?,
                    error_code=NULL, error_message=NULL, updated_at=?
                WHERE id=1 AND owner_id=?
                """,
                (now, now, owner_id),
            )

    def finish_auto_run(
        self,
        *,
        owner_id: str,
        status: str,
        next_run_at: str,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        values = dict(result or {})
        now = _utc_now()
        succeeded = status == "SUCCESS"
        failed = status not in {"SUCCESS", "SKIPPED"}
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE naver_auto_sync_state
                SET status=?, last_completed_at=?,
                    last_success_at=CASE WHEN ? THEN ? ELSE last_success_at END,
                    next_run_at=?,
                    consecutive_failures=CASE WHEN ? THEN 0
                        WHEN ? THEN consecutive_failures + 1
                        ELSE consecutive_failures END,
                    fetched_count=?, inserted_count=?, updated_count=?,
                    unchanged_count=?, failed_count=?,
                    error_code=?, error_message=?, sync_id=?, updated_at=?
                WHERE id=1 AND owner_id=?
                """,
                (
                    status,
                    now,
                    1 if succeeded else 0,
                    now,
                    next_run_at,
                    1 if succeeded else 0,
                    1 if failed else 0,
                    int(values.get("fetched_count") or 0),
                    int(
                        values.get("inserted_count")
                        or values.get("created_count")
                        or 0
                    ),
                    int(values.get("updated_count") or 0),
                    int(values.get("unchanged_count") or 0),
                    int(values.get("failed_count") or 0),
                    str(error_code or "")[:100] or None,
                    str(error_message or "")[:500] or None,
                    values.get("sync_id")
                    or values.get("correlation_id"),
                    now,
                    owner_id,
                ),
            )
