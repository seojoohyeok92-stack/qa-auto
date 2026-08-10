from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

from config import validate_auto_sync_interval
from repositories.database import Database
from repositories.naver_post_repository import NON_RETRYABLE_TARGET_ERRORS


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class AutoPostRepository:
    """Persistent scheduler, retry, and cross-process lock state."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _dict(row: Any) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def ensure_settings(
        self, *, enabled: bool, interval_minutes: int, max_retries: int
    ) -> dict[str, Any]:
        interval = validate_auto_sync_interval(interval_minutes)
        retries = max(0, min(int(max_retries), 10))
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT updated_at FROM naver_auto_post_settings WHERE id=1"
            ).fetchone()
            if row is not None and row["updated_at"] is None:
                connection.execute(
                    """
                    UPDATE naver_auto_post_settings
                    SET interval_minutes=?, max_retries=?
                    WHERE id=1
                    """,
                    (interval, retries),
                )
        return self.settings()

    def settings(self) -> dict[str, Any]:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM naver_auto_post_settings WHERE id=1"
            ).fetchone()
        value = self._dict(row) or {
            "enabled": False, "interval_minutes": 10,
            "max_retries": 1, "updated_at": None,
        }
        value["enabled"] = bool(value["enabled"])
        value["runtime_auto_post_enabled"] = value["enabled"]
        value["allow_existing_pending"] = bool(
            value.get("allow_existing_pending", False)
        )
        return value

    def save_settings(
        self, *, enabled: bool, interval_minutes: int, max_retries: int
    ) -> dict[str, Any]:
        interval = validate_auto_sync_interval(interval_minutes)
        retries = max(0, min(int(max_retries), 10))
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE naver_auto_post_settings
                SET enabled=?, interval_minutes=?, max_retries=?,
                    enabled_at=CASE
                        WHEN ?=1 AND enabled=0 THEN ?
                        WHEN ?=0 THEN NULL ELSE enabled_at END,
                    pause_reason=CASE WHEN ?=1 THEN NULL ELSE pause_reason END,
                    updated_at=?
                WHERE id=1
                """,
                (
                    int(bool(enabled)), interval, retries,
                    int(bool(enabled)), _utc_now(), int(bool(enabled)),
                    int(bool(enabled)), _utc_now(),
                ),
            )
        return self.settings()

    def set_pause_reason(self, reason: str | None) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE naver_auto_post_settings
                SET pause_reason=?, updated_at=? WHERE id=1
                """,
                (str(reason or "")[:100] or None, _utc_now()),
            )

    def set_state(
        self,
        status: str,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
        owner_id: str | None = None,
    ) -> None:
        now = _utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE naver_auto_post_state
                SET status=?, error_code=?, error_message=?, updated_at=?
                WHERE id=1 AND (? IS NULL OR owner_id=? OR owner_id IS NULL)
                """,
                (
                    str(status).upper()[:50],
                    str(error_code or "")[:100] or None,
                    str(error_message or "")[:500] or None,
                    now, owner_id, owner_id,
                ),
            )

    def posting_count(self) -> int:
        with self.database.connection() as connection:
            return int(connection.execute(
                "SELECT COUNT(*) FROM inquiries WHERE post_status='POSTING'"
            ).fetchone()[0])

    def post_unknown_count(self) -> int:
        with self.database.connection() as connection:
            return int(connection.execute(
                "SELECT COUNT(*) FROM inquiries WHERE post_status='POST_UNKNOWN'"
            ).fetchone()[0])

    def state(self) -> dict[str, Any]:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM naver_auto_post_state WHERE id=1"
            ).fetchone()
        return self._dict(row) or {"status": "STOPPED"}

    def acquire_scheduler_lease(
        self, *, owner_id: str, ttl_seconds: int = 180
    ) -> bool:
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=max(30, int(ttl_seconds)))
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT owner_id, lease_expires_at FROM naver_auto_post_state WHERE id=1"
            ).fetchone()
            if row is None:
                return False
            expired = not row["lease_expires_at"] or str(row["lease_expires_at"]) <= now.isoformat(timespec="milliseconds")
            if row["owner_id"] not in (None, "", owner_id) and not expired:
                return False
            connection.execute(
                """
                UPDATE naver_auto_post_state
                SET owner_id=?, owner_pid=?, lease_expires_at=?, updated_at=?
                WHERE id=1
                """,
                (owner_id, os.getpid(), expires.isoformat(timespec="milliseconds"), _utc_now()),
            )
        return True

    def release_scheduler_lease(self, owner_id: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE naver_auto_post_state
                SET owner_id=NULL, owner_pid=NULL, lease_expires_at=NULL,
                    status='STOPPED', updated_at=?
                WHERE id=1 AND owner_id=?
                """,
                (_utc_now(), owner_id),
            )

    def start_run(self, *, owner_id: str, run_id: str | None = None) -> str:
        value = run_id or str(uuid.uuid4())
        now = _utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO naver_auto_post_runs(
                    run_id, owner_id, status, started_at
                ) VALUES (?, ?, 'RUNNING', ?)
                """,
                (value, owner_id, now),
            )
            connection.execute(
                """
                UPDATE naver_auto_post_state
                SET status='RUNNING', run_id=?, last_started_at=?,
                    error_code=NULL, error_message=NULL, updated_at=?
                WHERE id=1 AND owner_id=?
                """,
                (value, now, now, owner_id),
            )
        return value

    def finish_run(
        self, *, owner_id: str, run_id: str, status: str,
        result: dict[str, int], next_run_at: str,
        error_code: str | None = None, error_message: str | None = None,
    ) -> None:
        target = str(status).upper()
        if target not in {"SUCCESS", "PARTIAL", "FAILED", "SKIPPED"}:
            raise ValueError(f"Invalid auto-post run status: {status}")
        now = _utc_now()
        counts = {
            key: int(result.get(key) or 0)
            for key in ("processed_count", "succeeded_count", "failed_count", "unknown_count", "skipped_count")
        }
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE naver_auto_post_runs
                SET status=?, processed_count=?, succeeded_count=?,
                    failed_count=?, unknown_count=?, skipped_count=?,
                    error_code=?, error_message=?, completed_at=?
                WHERE run_id=? AND status='RUNNING'
                """,
                (target, counts["processed_count"], counts["succeeded_count"],
                 counts["failed_count"], counts["unknown_count"], counts["skipped_count"],
                 str(error_code or "")[:100] or None,
                 str(error_message or "")[:500] or None, now, run_id),
            )
            connection.execute(
                """
                UPDATE naver_auto_post_state
                SET status=?, last_completed_at=?,
                    last_success_at=CASE WHEN ?='SUCCESS' THEN ? ELSE last_success_at END,
                    next_run_at=?, processed_count=?, succeeded_count=?,
                    failed_count=?, unknown_count=?, skipped_count=?,
                    error_code=?, error_message=?, updated_at=?
                WHERE id=1 AND owner_id=?
                """,
                (target, now, target, now, next_run_at,
                 counts["processed_count"], counts["succeeded_count"],
                 counts["failed_count"], counts["unknown_count"], counts["skipped_count"],
                 str(error_code or "")[:100] or None,
                 str(error_message or "")[:500] or None, now, owner_id),
            )

    def candidates(
        self,
        *,
        max_retries: int,
        limit: int = 100,
        inquiry_ids: Iterable[int] | None = None,
    ) -> list[dict[str, Any]]:
        terminal_errors = tuple(sorted(NON_RETRYABLE_TARGET_ERRORS))
        placeholders = ",".join("?" for _ in terminal_errors)
        selected_ids = tuple(dict.fromkeys(int(value) for value in (inquiry_ids or ())))
        id_clause = ""
        id_parameters: tuple[int, ...] = ()
        if inquiry_ids is not None:
            if not selected_ids:
                return []
            id_clause = " AND i.id IN ({})".format(
                ",".join("?" for _ in selected_ids)
            )
            id_parameters = selected_ids
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT i.*,
                       (SELECT COUNT(*) FROM naver_post_attempts a
                        WHERE a.inquiry_id=i.id AND a.status='POST_FAILED') AS failed_attempts
                FROM inquiries i
                WHERE COALESCE(i.source_answered,0)=0
                  AND i.post_status IN ('NOT_POSTED','POST_FAILED')
                  AND trim(COALESCE(i.store_code,''))<>''
                  AND trim(COALESCE(i.source_type,''))<>''
                  AND trim(COALESCE(i.external_inquiry_id,i.source_question_id,''))<>''
                  AND upper(COALESCE(i.post_error_code,'')) NOT IN ({placeholders})
                  {id_clause}
                  AND (
                      i.post_status<>'POST_FAILED' OR
                      (SELECT COUNT(*) FROM naver_post_attempts a
                       WHERE a.inquiry_id=i.id AND a.status='POST_FAILED') <= ?
                  )
                ORDER BY i.registered_at, i.id
                LIMIT ?
                """,
                (
                    *terminal_errors, *id_parameters,
                    max(0, int(max_retries)), max(1, min(int(limit), 500)),
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def acquire_inquiry_lock(
        self, *, inquiry_id: int, store_code: str, external_id: str,
        owner_id: str, run_id: str, ttl_seconds: int = 300,
    ) -> bool:
        now = datetime.now(UTC)
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM naver_auto_post_locks WHERE expires_at<=?",
                (now.isoformat(timespec="milliseconds"),),
            )
            try:
                connection.execute(
                    """
                    INSERT INTO naver_auto_post_locks(
                        inquiry_id, store_code, external_id, owner_id, run_id,
                        acquired_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (int(inquiry_id), store_code, external_id, owner_id, run_id,
                     now.isoformat(timespec="milliseconds"),
                     (now + timedelta(seconds=max(30, int(ttl_seconds)))).isoformat(timespec="milliseconds")),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def release_inquiry_lock(self, *, inquiry_id: int, owner_id: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM naver_auto_post_locks WHERE inquiry_id=? AND owner_id=?",
                (int(inquiry_id), owner_id),
            )

    def recover_stale_posting(self, *, stale_seconds: int = 600) -> int:
        cutoff = (datetime.now(UTC) - timedelta(seconds=max(60, int(stale_seconds)))).isoformat(timespec="milliseconds")
        now = _utc_now()
        with self.database.transaction() as connection:
            rows = connection.execute(
                """
                SELECT id, inquiry_id FROM naver_post_attempts
                WHERE status='POSTING' AND started_at<=?
                """,
                (cutoff,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE naver_post_attempts
                    SET status='POST_UNKNOWN', error_code='PROCESS_RESTART_UNKNOWN',
                        error_message='Posting outcome requires manual verification.',
                        completed_at=? WHERE id=? AND status='POSTING'
                    """,
                    (now, int(row["id"])),
                )
                connection.execute(
                    """
                    UPDATE inquiries SET post_status='POST_UNKNOWN',
                        post_error_code='PROCESS_RESTART_UNKNOWN',
                        post_error_message='Posting outcome requires manual verification.',
                        updated_at=? WHERE id=? AND post_status='POSTING'
                    """,
                    (now, int(row["inquiry_id"])),
                )
        return len(rows)
