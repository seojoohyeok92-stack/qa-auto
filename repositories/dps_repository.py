from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from repositories.database import Database
from repositories.inquiry_repository import deserialize_json, serialize_json, utc_now
from services.dps_lookup_policy import DpsLookupStatus


class DpsRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _row(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        for field in ("raw_result_json", "normalized_result_json"):
            try:
                result[field] = deserialize_json(result[field])
                result[f"_{field}_corrupt"] = False
            except (TypeError, ValueError):
                result[field] = {}
                result[f"_{field}_corrupt"] = True
        return result

    def create_lookup_result(
        self,
        *,
        inquiry_id: int,
        order_id: str,
        lookup_status: str | DpsLookupStatus,
        raw_result: Any,
        normalized_result: Any,
        error_code: str | None = None,
        error_message: str | None = None,
        queried_at: str | None = None,
        expires_at: str | None = None,
        correlation_id: str | None = None,
        lookup_started_at: str | None = None,
        lookup_completed_at: str | None = None,
        duration_seconds: float | None = None,
        cached: bool = False,
    ) -> dict[str, Any]:
        status = (
            lookup_status
            if isinstance(lookup_status, DpsLookupStatus)
            else DpsLookupStatus(str(lookup_status))
        )
        now = utc_now()
        normalized = (
            dict(normalized_result)
            if isinstance(normalized_result, dict)
            else {}
        )
        raw_required = normalized.get("raw_required_delivery_date")
        if isinstance(raw_required, (dict, list, tuple)):
            raw_required = serialize_json(raw_required)
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO dps_lookup_results (
                    inquiry_id, order_id, lookup_status,
                    raw_result_json, normalized_result_json,
                    error_code, error_message, queried_at, expires_at,
                    correlation_id, lookup_started_at, lookup_completed_at,
                    duration_seconds, cached,
                    required_delivery_date, installation_date,
                    installation_date_source, raw_required_delivery_date,
                    date_parse_status, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    inquiry_id,
                    str(order_id),
                    status.value,
                    serialize_json(raw_result),
                    serialize_json(normalized_result),
                    str(error_code)[:100] if error_code else None,
                    str(error_message)[:2_000] if error_message else None,
                    queried_at or now,
                    expires_at,
                    correlation_id,
                    lookup_started_at,
                    lookup_completed_at,
                    duration_seconds,
                    1 if cached else 0,
                    normalized.get("required_delivery_date"),
                    normalized.get("installation_date"),
                    normalized.get("installation_date_source"),
                    raw_required,
                    normalized.get("date_parse_status"),
                    now,
                    now,
                ),
            )
            row_id = int(cursor.lastrowid)
        return self.get(row_id)

    def get(self, result_id: int) -> dict[str, Any]:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM dps_lookup_results WHERE id = ?",
                (result_id,),
            ).fetchone()
        result = self._row(row)
        if result is None:
            raise LookupError(f"DPS lookup result not found: {result_id}")
        return result

    def get_latest_by_order_id(self, order_id: str) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM dps_lookup_results
                WHERE order_id = ?
                ORDER BY queried_at DESC, id DESC LIMIT 1
                """,
                (str(order_id),),
            ).fetchone()
        return self._row(row)

    def get_latest_success_by_order_id(
        self,
        order_id: str,
        *,
        valid_only: bool = False,
        now: str | None = None,
    ) -> dict[str, Any] | None:
        clauses = ["order_id = ?", "lookup_status = ?"]
        parameters: list[Any] = [
            str(order_id),
            DpsLookupStatus.SUCCESS.value,
        ]
        if valid_only:
            clauses.append("expires_at IS NOT NULL")
            clauses.append("expires_at > ?")
            parameters.append(now or utc_now())
        sql = (
            "SELECT * FROM dps_lookup_results WHERE "
            + " AND ".join(clauses)
            + " ORDER BY queried_at DESC, id DESC LIMIT 1"
        )
        with self.database.connection() as connection:
            row = connection.execute(sql, tuple(parameters)).fetchone()
        return self._row(row)

    def get_latest_by_inquiry_id(
        self, inquiry_id: int
    ) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM dps_lookup_results
                WHERE inquiry_id = ?
                ORDER BY queried_at DESC, id DESC LIMIT 1
                """,
                (inquiry_id,),
            ).fetchone()
        return self._row(row)

    def get_latest_success_by_inquiry_and_order(
        self,
        inquiry_id: int,
        order_id: str,
    ) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM dps_lookup_results
                WHERE inquiry_id = ?
                  AND order_id = ?
                  AND lookup_status = ?
                ORDER BY queried_at DESC, id DESC
                LIMIT 1
                """,
                (
                    int(inquiry_id),
                    str(order_id),
                    DpsLookupStatus.SUCCESS.value,
                ),
            ).fetchone()
        return self._row(row)

    def get_latest_by_inquiry_and_order(
        self,
        inquiry_id: int,
        order_id: str,
    ) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM dps_lookup_results
                WHERE inquiry_id = ? AND order_id = ?
                ORDER BY queried_at DESC, id DESC LIMIT 1
                """,
                (int(inquiry_id), str(order_id)),
            ).fetchone()
        return self._row(row)

    def get_preferred_for_inquiry_and_order(
        self,
        inquiry_id: int,
        order_id: str,
    ) -> dict[str, Any] | None:
        return self.get_latest_success_by_inquiry_and_order(
            inquiry_id, order_id
        ) or self.get_latest_by_inquiry_and_order(inquiry_id, order_id)

    def list_history_by_inquiry_id(
        self, inquiry_id: int, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM dps_lookup_results
                WHERE inquiry_id = ?
                ORDER BY queried_at DESC, id DESC LIMIT ?
                """,
                (inquiry_id, max(1, min(int(limit), 1_000))),
            ).fetchall()
        return [result for row in rows if (result := self._row(row))]

    def mark_or_store_failure(
        self,
        *,
        inquiry_id: int,
        order_id: str,
        lookup_status: str | DpsLookupStatus,
        error_code: str,
        error_message: str,
        raw_result: Any = None,
        normalized_result: Any = None,
        queried_at: str | None = None,
        expires_at: str | None = None,
        correlation_id: str | None = None,
        lookup_started_at: str | None = None,
        lookup_completed_at: str | None = None,
        duration_seconds: float | None = None,
    ) -> dict[str, Any]:
        return self.create_lookup_result(
            inquiry_id=inquiry_id,
            order_id=order_id,
            lookup_status=lookup_status,
            raw_result=raw_result or {},
            normalized_result=normalized_result or {},
            error_code=error_code,
            error_message=error_message,
            queried_at=queried_at,
            expires_at=expires_at,
            correlation_id=correlation_id,
            lookup_started_at=lookup_started_at,
            lookup_completed_at=lookup_completed_at,
            duration_seconds=duration_seconds,
        )

    @staticmethod
    def is_cache_valid(row: dict[str, Any] | None, *, now: str | None = None) -> bool:
        if not row or row.get("lookup_status") != DpsLookupStatus.SUCCESS.value:
            return False
        expires_at = row.get("expires_at")
        if not expires_at:
            return False
        try:
            expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            current = datetime.fromisoformat(
                str(now or utc_now()).replace("Z", "+00:00")
            )
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=UTC)
            if current.tzinfo is None:
                current = current.replace(tzinfo=UTC)
            return expiry > current
        except ValueError:
            return False
