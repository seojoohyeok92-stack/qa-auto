from __future__ import annotations

from typing import Any

from repositories.database import Database
from repositories.inquiry_repository import deserialize_json, serialize_json


class LearningSignalRepository:
    """Structured Learning Signal storage (GOOD/BAD pattern, correction, fact).

    Decoupled from ``learning_feedback`` (the raw evaluation event) and
    ``learning_examples`` (the reused answer content) so that retrieval can
    rank and scope-gate signals independently, without touching either
    table's existing schema or hot-path queries.
    """

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _row(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        for key in ("topics_json", "product_identity_json", "metadata_json"):
            result[key] = deserialize_json(result.get(key))
        result["active"] = bool(result["active"])
        return result

    def upsert(self, signal: dict[str, Any]) -> dict[str, Any]:
        columns = (
            "source_key", "signal_kind", "origin_kind",
            "learning_feedback_id", "learning_example_id",
            "historical_case_id", "inquiry_id", "store_code",
            "question_masked", "content_text", "product_scope",
            "topics_json", "product_identity_json", "metadata_json",
            "active", "actor",
        )
        values = []
        for column in columns:
            value = signal.get(column)
            if column in {"topics_json", "product_identity_json", "metadata_json"}:
                value = serialize_json(value or ({} if column != "topics_json" else []))
            elif column == "active":
                value = int(bool(value if value is not None else True))
            values.append(value)
        assignments = ", ".join(
            f"{column}=excluded.{column}" for column in columns if column != "source_key"
        )
        with self.database.transaction() as connection:
            connection.execute(
                f"""
                INSERT INTO learning_signals ({', '.join(columns)})
                VALUES ({', '.join('?' for _ in columns)})
                ON CONFLICT(source_key) DO UPDATE SET
                    {assignments},
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                values,
            )
            row = connection.execute(
                "SELECT * FROM learning_signals WHERE source_key=?",
                (signal["source_key"],),
            ).fetchone()
        result = self._row(row)
        assert result is not None
        return result

    def get(self, signal_id: int) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM learning_signals WHERE id=?", (int(signal_id),)
            ).fetchone()
        return self._row(row)

    def candidates(
        self,
        *,
        store_code: str | None,
        signal_kinds: tuple[str, ...] | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Active signals joined to their originating inquiry's product identity.

        Bounded by ``limit`` and, when supplied, ``signal_kinds`` -- the
        caller (``LearningSignalService``) always restricts this to the
        handful of kinds it can use as evidence or guidance, so this never
        performs a full unfiltered table scan in the runtime path.
        """

        safe_limit = max(1, min(int(limit), 2000))
        kind_clause = ""
        params: list[Any] = [store_code, store_code]
        if signal_kinds:
            kind_clause = " AND ls.signal_kind IN (" + ",".join(
                "?" for _ in signal_kinds
            ) + ")"
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT ls.*,
                       i.product_id AS source_product_id,
                       i.product_name AS source_product_name,
                       i.option_name AS source_option_name,
                       i.inquiry_type AS source_inquiry_type,
                       i.external_inquiry_id AS source_external_inquiry_id,
                       i.source_question_id AS source_question_number
                FROM learning_signals ls
                LEFT JOIN inquiries i ON i.id=ls.inquiry_id
                WHERE ls.active=1
                  AND (ls.store_code=? OR ls.store_code IS NULL OR ? IS NULL)
                  {kind_clause}
                ORDER BY ls.created_at DESC
                LIMIT ?
                """,
                (*params, *(signal_kinds or ()), safe_limit),
            ).fetchall()
        return [item for row in rows if (item := self._row(row)) is not None]

    def for_learning_example(self, learning_example_id: int) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM learning_signals
                WHERE learning_example_id=? ORDER BY created_at DESC
                """,
                (int(learning_example_id),),
            ).fetchall()
        return [item for row in rows if (item := self._row(row)) is not None]

    def for_historical_case(self, historical_case_id: int) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM learning_signals
                WHERE historical_case_id=? ORDER BY created_at DESC
                """,
                (int(historical_case_id),),
            ).fetchall()
        return [item for row in rows if (item := self._row(row)) is not None]

    def for_inquiry(self, inquiry_id: int) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM learning_signals
                WHERE inquiry_id=? ORDER BY created_at DESC
                """,
                (int(inquiry_id),),
            ).fetchall()
        return [item for row in rows if (item := self._row(row)) is not None]

    def manager_rows(self, *, limit: int = 500) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT ls.*,
                       i.source_question_id, i.external_inquiry_id,
                       i.source_created_at, i.registered_at,
                       i.product_name AS inquiry_product_name,
                       i.title AS inquiry_title, i.content AS inquiry_content
                FROM learning_signals AS ls
                LEFT JOIN inquiries AS i ON i.id=ls.inquiry_id
                ORDER BY COALESCE(
                             i.source_created_at, i.registered_at, ls.created_at
                         ) DESC, ls.id DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 2_000)),),
            ).fetchall()
        return [item for row in rows if (item := self._row(row)) is not None]

    def deactivate(self, signal_id: int, *, reason: str, actor: str) -> dict[str, Any]:
        clean_reason = str(reason or "").strip()
        if not clean_reason:
            raise ValueError("Signal 취소 사유를 입력해 주세요.")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM learning_signals WHERE id=?", (int(signal_id),)
            ).fetchone()
            if row is None:
                raise LookupError(f"Learning signal not found: {signal_id}")
            metadata = deserialize_json(row["metadata_json"])
            metadata.update(
                {
                    "revoke_reason": clean_reason[:1_000],
                    "revoked_by": str(actor or "직원").strip() or "직원",
                }
            )
            connection.execute(
                """
                UPDATE learning_signals
                SET active=0, metadata_json=json_set(
                        ?, '$.revoked_at', strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    ),
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id=?
                """,
                (serialize_json(metadata), int(signal_id)),
            )
            updated = connection.execute(
                "SELECT * FROM learning_signals WHERE id=?", (int(signal_id),)
            ).fetchone()
        result = self._row(updated)
        assert result is not None
        return result
