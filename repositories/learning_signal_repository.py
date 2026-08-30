from __future__ import annotations

from typing import Any

from repositories.database import Database
from repositories.inquiry_repository import deserialize_json, serialize_json


def confirmation_key(
    *,
    learning_example_id: int | None,
    learning_feedback_id: int | None,
    inquiry_id: int | None,
) -> str:
    """Identify exactly which source row a confirmation was derived from.

    Used as the dedup key so re-running extraction against the same
    already-confirmed source (e.g. re-approving unchanged content) never
    creates a duplicate ledger row -- see ``record_confirmation``.
    """

    if learning_example_id is not None:
        return f"LE:{int(learning_example_id)}"
    if learning_feedback_id is not None:
        return f"LF:{int(learning_feedback_id)}"
    return f"INQ:{inquiry_id}"


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
            "active", "actor", "generation_mode", "confirmation_status",
            "normalized_identity_key", "diff_category",
        )
        values = []
        for column in columns:
            value = signal.get(column)
            if column in {"topics_json", "product_identity_json", "metadata_json"}:
                value = serialize_json(value or ({} if column != "topics_json" else []))
            elif column == "active":
                value = int(bool(value if value is not None else True))
            elif column == "generation_mode":
                value = str(value or "MANUAL")
            elif column == "confirmation_status":
                value = str(value or "ACTIVE")
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

    _LIVE_CONFIRMATION_SUBQUERY = """
        (SELECT COUNT(DISTINCT COALESCE(c.learning_example_id, c.learning_feedback_id, c.inquiry_id))
         FROM learning_signal_confirmations c
         LEFT JOIN learning_examples le2 ON le2.id = c.learning_example_id
         LEFT JOIN learning_feedback lf2 ON lf2.id = c.learning_feedback_id
         WHERE c.learning_signal_id = ls.id
           AND c.active = 1
           AND (c.learning_example_id IS NULL OR le2.active = 1)
           AND (c.learning_feedback_id IS NULL OR lf2.active = 1)
        ) AS live_confirmation_count
    """

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

        ``live_confirmation_count`` is computed by joining each confirmation
        to its source row and checking that source's *current* ``active``
        flag -- so a cancelled approval is reflected immediately without any
        cascading update anywhere else (see 4th-phase report on revoke).
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
                       i.source_question_id AS source_question_number,
                       {self._LIVE_CONFIRMATION_SUBQUERY}
                FROM learning_signals ls
                LEFT JOIN inquiries i ON i.id=ls.inquiry_id
                WHERE ls.active=1
                  AND ls.confirmation_status NOT IN ('REJECTED','SUPERSEDED')
                  AND (ls.store_code=? OR ls.store_code IS NULL OR ? IS NULL)
                  AND (
                      ls.generation_mode<>'MANUAL'
                      OR (
                          (ls.learning_feedback_id IS NULL OR EXISTS (
                              SELECT 1 FROM learning_feedback lf
                              WHERE lf.id=ls.learning_feedback_id AND lf.active=1
                          ))
                          AND (ls.learning_example_id IS NULL OR EXISTS (
                              SELECT 1 FROM learning_examples le
                              WHERE le.id=ls.learning_example_id AND le.active=1
                          ))
                          AND (ls.historical_case_id IS NULL OR EXISTS (
                              SELECT 1 FROM historical_cases hc
                              WHERE hc.id=ls.historical_case_id AND hc.active=1
                          ))
                      )
                  )
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

    def find_by_normalized_identity(
        self, key: str, *, exclude_signal_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Locate existing signals stating essentially the same claim.

        Used by auto-extraction to accumulate confirmations onto one row
        instead of creating a fresh duplicate every time the same fact is
        independently re-confirmed (section 14: dedup by normalized
        identity across inquiries).
        """

        if not key:
            return []
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM learning_signals
                WHERE normalized_identity_key=? AND id<>?
                ORDER BY created_at ASC
                """,
                (key, int(exclude_signal_id or 0)),
            ).fetchall()
        return [item for row in rows if (item := self._row(row)) is not None]

    def manager_rows(
        self,
        *,
        limit: int = 500,
        generation_mode: str | None = None,
        signal_kind: str | None = None,
        confirmation_status: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if generation_mode:
            clauses.append("ls.generation_mode=?")
            params.append(str(generation_mode).upper())
        if signal_kind:
            clauses.append("ls.signal_kind=?")
            params.append(str(signal_kind).upper())
        if confirmation_status:
            clauses.append("ls.confirmation_status=?")
            params.append(str(confirmation_status).upper())
        where_clause = (" AND " + " AND ".join(clauses)) if clauses else ""
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT ls.*,
                       i.source_question_id, i.external_inquiry_id,
                       i.source_created_at, i.registered_at,
                       i.product_name AS inquiry_product_name,
                       i.title AS inquiry_title, i.content AS inquiry_content,
                       {self._LIVE_CONFIRMATION_SUBQUERY}
                FROM learning_signals AS ls
                LEFT JOIN inquiries AS i ON i.id=ls.inquiry_id
                WHERE 1=1 {where_clause}
                ORDER BY COALESCE(
                             i.source_created_at, i.registered_at, ls.created_at
                         ) DESC, ls.id DESC
                LIMIT ?
                """,
                (*params, max(1, min(int(limit), 2_000))),
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

    def promote(self, signal_id: int, *, actor: str) -> dict[str, Any]:
        """Operator explicitly confirms an auto-extracted CANDIDATE.

        Bypasses the confirmation-count threshold permanently for this one
        signal (human-in-the-loop escape valve -- section 13's manual path
        stays available even when auto-promotion is fully SHADOW/off).
        """

        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE learning_signals
                SET confirmation_status='MANUALLY_PROMOTED',
                    metadata_json=json_set(
                        COALESCE(metadata_json, '{}'),
                        '$.manually_promoted_by', ?,
                        '$.manually_promoted_at',
                            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    ),
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id=?
                """,
                (str(actor or "관리자").strip() or "관리자", int(signal_id)),
            )
            row = connection.execute(
                "SELECT * FROM learning_signals WHERE id=?", (int(signal_id),)
            ).fetchone()
        if row is None:
            raise LookupError(f"Learning signal not found: {signal_id}")
        result = self._row(row)
        assert result is not None
        return result

    def reject(self, signal_id: int, *, actor: str, reason: str) -> dict[str, Any]:
        clean_reason = str(reason or "").strip()
        if not clean_reason:
            raise ValueError("거부 사유를 입력해 주세요.")
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE learning_signals
                SET confirmation_status='REJECTED', active=0,
                    metadata_json=json_set(
                        COALESCE(metadata_json, '{}'),
                        '$.rejected_by', ?,
                        '$.rejected_reason', ?,
                        '$.rejected_at', strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    ),
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id=?
                """,
                (
                    str(actor or "관리자").strip() or "관리자",
                    clean_reason[:1_000],
                    int(signal_id),
                ),
            )
            row = connection.execute(
                "SELECT * FROM learning_signals WHERE id=?", (int(signal_id),)
            ).fetchone()
        if row is None:
            raise LookupError(f"Learning signal not found: {signal_id}")
        result = self._row(row)
        assert result is not None
        return result

    def record_confirmation(
        self,
        *,
        learning_signal_id: int,
        inquiry_id: int | None,
        source_authority: str,
        learning_example_id: int | None = None,
        learning_feedback_id: int | None = None,
        approval_history_id: int | None = None,
    ) -> dict[str, Any]:
        """Record (or reactivate) one confirmation of an auto-extracted signal.

        Idempotent per exact source row: re-running extraction against a
        source that already confirmed this signal updates nothing new; a
        previously-revoked confirmation (its source was cancelled, then
        re-approved) is reactivated in place rather than duplicated.
        """

        key = confirmation_key(
            learning_example_id=learning_example_id,
            learning_feedback_id=learning_feedback_id,
            inquiry_id=inquiry_id,
        )
        with self.database.transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM learning_signal_confirmations
                WHERE learning_signal_id=? AND confirmation_key=?
                """,
                (int(learning_signal_id), key),
            ).fetchone()
            if existing is not None:
                connection.execute(
                    """
                    UPDATE learning_signal_confirmations
                    SET active=1, revoked_at=NULL, revoked_reason=NULL,
                        confirmed_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                        approval_history_id=COALESCE(?, approval_history_id)
                    WHERE id=?
                    """,
                    (approval_history_id, int(existing["id"])),
                )
                row_id = int(existing["id"])
            else:
                cursor = connection.execute(
                    """
                    INSERT INTO learning_signal_confirmations(
                        learning_signal_id, confirmation_key, inquiry_id,
                        learning_example_id, learning_feedback_id,
                        approval_history_id, source_authority, active
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        int(learning_signal_id), key,
                        int(inquiry_id) if inquiry_id is not None else None,
                        int(learning_example_id) if learning_example_id is not None else None,
                        int(learning_feedback_id) if learning_feedback_id is not None else None,
                        int(approval_history_id) if approval_history_id is not None else None,
                        str(source_authority),
                    ),
                )
                row_id = int(cursor.lastrowid)
            row = connection.execute(
                "SELECT * FROM learning_signal_confirmations WHERE id=?",
                (row_id,),
            ).fetchone()
        result = dict(row) if row is not None else None
        assert result is not None
        return result

    def live_confirmation_count(self, learning_signal_id: int) -> int:
        with self.database.connection() as connection:
            row = connection.execute(
                f"""
                SELECT {self._LIVE_CONFIRMATION_SUBQUERY.replace('ls.id', '?')}
                """,
                (int(learning_signal_id),),
            ).fetchone()
        return int(row["live_confirmation_count"] or 0) if row is not None else 0

    def deactivate_confirmations_for_learning_example(
        self, learning_example_id: int, *, reason: str, actor: str,
    ) -> int:
        """Scoped revoke: only confirmations traced to this exact approval.

        Called from ``LearningRepository.revoke_human_verified`` so that
        cancelling one approval only affects the Structured Signals it
        actually confirmed -- other inquiries' independent confirmations of
        the same normalized fact are left untouched.
        """

        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE learning_signal_confirmations
                SET active=0, revoked_reason=?,
                    revoked_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE learning_example_id=? AND active=1
                """,
                (str(reason or "")[:1_000], int(learning_example_id)),
            )
        return int(cursor.rowcount)

    def deactivate_confirmations_for_learning_feedback(
        self, learning_feedback_id: int, *, reason: str, actor: str,
    ) -> int:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE learning_signal_confirmations
                SET active=0, revoked_reason=?,
                    revoked_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE learning_feedback_id=? AND active=1
                """,
                (str(reason or "")[:1_000], int(learning_feedback_id)),
            )
        return int(cursor.rowcount)
