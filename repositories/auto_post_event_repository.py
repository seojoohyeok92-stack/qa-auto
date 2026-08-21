from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

from repositories.database import Database


QUEUE_STATUSES = {
    "PENDING",
    "PROCESSING",
    "COMPLETED",
    "FAILED",
    "RETRY_BY_SCHEDULER",
    "BLOCKED_AUTO_POST_OFF",
}


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(value: datetime | None = None) -> str:
    return (value or _now()).isoformat(timespec="milliseconds")


class AutoPostEventRepository:
    """Durable outbox for inquiries discovered by Auto Sync."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        *,
        inquiry_id: int,
        store_code: str,
        external_id: str,
        source_sync_id: str | None,
        runtime_enabled: bool,
    ) -> dict[str, Any] | None:
        status = "PENDING" if runtime_enabled else "BLOCKED_AUTO_POST_OFF"
        now = _stamp()
        try:
            with self.database.transaction() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO auto_sync_events(
                        inquiry_id, store_code, external_id, source_sync_id,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(inquiry_id), str(store_code), str(external_id),
                        str(source_sync_id or "")[:100] or None, status, now, now,
                    ),
                )
                event_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError:
            return None
        return self.get(event_id)

    def get(self, event_id: int) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM auto_sync_events WHERE id=?", (int(event_id),)
            ).fetchone()
        return dict(row) if row else None

    def get_for_inquiry(self, inquiry_id: int) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM auto_sync_events WHERE inquiry_id=?",
                (int(inquiry_id),),
            ).fetchone()
        return dict(row) if row else None

    def recover_stale_processing(self, *, stale_seconds: int = 600) -> int:
        cutoff = _stamp(_now() - timedelta(seconds=max(60, int(stale_seconds))))
        now = _stamp()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE auto_sync_events
                SET status='RETRY_BY_SCHEDULER', claimed_by=NULL,
                    lease_expires_at=NULL, last_error_code='STALE_PROCESSING',
                    updated_at=?
                WHERE status='PROCESSING'
                  AND COALESCE(lease_expires_at, processing_started_at)<=?
                """,
                (now, cutoff),
            )
        return int(cursor.rowcount)

    def claim_next(
        self,
        *,
        owner_id: str,
        event_only_inquiry_id: int | None = None,
        exclude_inquiry_ids: Iterable[int] = (),
        lease_seconds: int = 600,
    ) -> dict[str, Any] | None:
        now_value = _now()
        now = _stamp(now_value)
        where_id = " AND inquiry_id=?" if event_only_inquiry_id is not None else ""
        parameters: tuple[Any, ...] = (
            (int(event_only_inquiry_id),) if event_only_inquiry_id is not None else ()
        )
        excluded = tuple(dict.fromkeys(int(value) for value in exclude_inquiry_ids))
        if excluded:
            where_id += " AND inquiry_id NOT IN ({})".format(
                ",".join("?" for _ in excluded)
            )
            parameters += excluded
        with self.database.transaction() as connection:
            row = connection.execute(
                f"""
                SELECT * FROM auto_sync_events
                WHERE status IN ('PENDING','RETRY_BY_SCHEDULER')
                  {where_id}
                ORDER BY CASE status WHEN 'PENDING' THEN 0 ELSE 1 END,
                         created_at, id
                LIMIT 1
                """,
                parameters,
            ).fetchone()
            if row is None:
                return None
            cursor = connection.execute(
                """
                UPDATE auto_sync_events
                SET status='PROCESSING', claimed_by=?, attempt_count=attempt_count+1,
                    processing_started_at=?, lease_expires_at=?, updated_at=?
                WHERE id=? AND status IN ('PENDING','RETRY_BY_SCHEDULER')
                """,
                (
                    owner_id, now,
                    _stamp(now_value + timedelta(seconds=max(60, int(lease_seconds)))),
                    now, int(row["id"]),
                ),
            )
            if cursor.rowcount != 1:
                return None
            claimed = connection.execute(
                "SELECT * FROM auto_sync_events WHERE id=?", (int(row["id"]),)
            ).fetchone()
        return dict(claimed) if claimed else None

    def pending_inquiry_ids(
        self,
        *,
        exclude_inquiry_ids: Iterable[int] = (),
        limit: int = 25,
    ) -> list[int]:
        """Return queued inquiry ids without claiming their POST events."""
        excluded = tuple(
            dict.fromkeys(int(value) for value in exclude_inquiry_ids)
        )
        where = ""
        parameters: list[Any] = []
        if excluded:
            where = " AND inquiry_id NOT IN ({})".format(
                ",".join("?" for _ in excluded)
            )
            parameters.extend(excluded)
        parameters.append(max(1, min(int(limit), 100)))
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT inquiry_id
                FROM auto_sync_events
                WHERE status IN ('PENDING','RETRY_BY_SCHEDULER')
                  {where}
                ORDER BY CASE status WHEN 'PENDING' THEN 0 ELSE 1 END,
                         created_at, id
                LIMIT ?
                """,
                tuple(parameters),
            ).fetchall()
        return [int(row["inquiry_id"]) for row in rows]

    def complete(self, event_id: int, *, owner_id: str) -> bool:
        now = _stamp()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE auto_sync_events
                SET status='COMPLETED', claimed_by=NULL, lease_expires_at=NULL,
                    completed_at=?, last_error_code=NULL, updated_at=?
                WHERE id=? AND status='PROCESSING' AND claimed_by=?
                """,
                (now, now, int(event_id), owner_id),
            )
        return cursor.rowcount == 1

    def fail(
        self,
        event_id: int,
        *,
        owner_id: str,
        error_code: str,
        retry_by_scheduler: bool,
    ) -> bool:
        status = "RETRY_BY_SCHEDULER" if retry_by_scheduler else "FAILED"
        now = _stamp()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE auto_sync_events
                SET status=?, claimed_by=NULL, lease_expires_at=NULL,
                    last_error_code=?, updated_at=?
                WHERE id=? AND status='PROCESSING' AND claimed_by=?
                """,
                (status, str(error_code)[:100], now, int(event_id), owner_id),
            )
        return cursor.rowcount == 1

    def block_new_claims(self) -> int:
        now = _stamp()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE auto_sync_events
                SET status='BLOCKED_AUTO_POST_OFF', updated_at=?
                WHERE status='PENDING'
                """,
                (now,),
            )
        return int(cursor.rowcount)

    def unblock_after_runtime_enable(self) -> int:
        """Return OFF-period events to the claimable queue on re-enable.

        ``create()`` stamps events synced while the operator switch was OFF
        as ``BLOCKED_AUTO_POST_OFF``, and ``block_new_claims()`` does the
        same to anything still ``PENDING`` at the moment of disable. Neither
        path is ever reversed elsewhere, so without this, inquiries synced
        or left pending during an OFF period would never be reconsidered
        after the operator turns auto-processing back ON. Restoring them to
        ``PENDING`` makes them claimable by the normal queue-only pipeline,
        which still applies every existing safety/validation check.
        """
        now = _stamp()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE auto_sync_events
                SET status='PENDING', updated_at=?
                WHERE status='BLOCKED_AUTO_POST_OFF'
                """,
                (now,),
            )
        return int(cursor.rowcount)

    def summary(self) -> dict[str, int]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM auto_sync_events GROUP BY status"
            ).fetchall()
        values = {status: 0 for status in QUEUE_STATUSES}
        values.update({str(row["status"]): int(row["count"]) for row in rows})
        return values
