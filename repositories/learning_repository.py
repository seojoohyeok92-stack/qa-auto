from __future__ import annotations

from typing import Any

from answer.learning_conflict import LearningConflictError
from repositories.database import Database
from repositories.inquiry_repository import deserialize_json, serialize_json


class LearningRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _row(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        for key in ("style_features_json", "metadata_json"):
            result[key] = deserialize_json(result.get(key))
        for key in ("posted", "auto_posted", "style_only", "active"):
            result[key] = bool(result[key])
        return result

    def upsert(self, example: dict[str, Any]) -> dict[str, Any]:
        with self.database.transaction() as connection:
            row = self._upsert_with_connection(connection, example)
        result = self._row(row)
        assert result is not None
        return result

    @staticmethod
    def _is_human_authority(row: Any) -> bool:
        """Return whether a Learning row has ever held human authority."""

        values = dict(row)
        raw_metadata = values.get("metadata_json")
        metadata = (
            raw_metadata
            if isinstance(raw_metadata, dict)
            else deserialize_json(raw_metadata)
        )
        return bool(
            metadata.get("human_verified")
            or metadata.get("revoked_from_human_verified")
            or metadata.get("verification_revoked")
            or str(values.get("validator_result") or "").upper()
            == "HUMAN_VERIFIED_NAVER_POSTED"
        )

    @classmethod
    def _upsert_with_connection(
        cls,
        connection: Any,
        example: dict[str, Any],
        *,
        allow_human_authority_update: bool = False,
    ) -> Any:
        existing = connection.execute(
            "SELECT * FROM learning_examples WHERE source_key=?",
            (example["source_key"],),
        ).fetchone()
        if (
            existing is not None
            and cls._is_human_authority(existing)
            and not allow_human_authority_update
        ):
            # Auto Sync/rebuild/import all use the ordinary upsert path.  Once
            # a person has verified this exact source key, those weaker
            # automatic observations must not downgrade or resurrect it.
            return existing

        columns = (
            "source_key", "inquiry_id", "answer_draft_id",
            "approval_history_id", "learning_source",
            "question_original_masked", "question_normalized", "store_code",
            "inquiry_type", "intent", "product_name", "model_code",
            "generation_mode", "template_id", "processing_route",
            "validator_result", "seller_answer", "gpt_draft",
            "edited_answer", "final_answer", "posted", "posted_at",
            "auto_posted", "rating", "edit_ratio", "quality_score",
            "style_only", "version", "style_features_json", "metadata_json",
            "active", "usage_count", "last_used_at",
        )
        values = []
        for column in columns:
            value = example.get(column)
            if column in {"style_features_json", "metadata_json"}:
                value = serialize_json(value or {})
            elif column in {"posted", "auto_posted", "style_only", "active"}:
                value = int(bool(value))
            elif column == "usage_count":
                value = max(0, int(value or 0))
            values.append(value)
        assignments = ", ".join(
            f"{column}=excluded.{column}" for column in columns if column != "source_key"
        )
        connection.execute(
            f"""
            INSERT INTO learning_examples ({', '.join(columns)})
            VALUES ({', '.join('?' for _ in columns)})
            ON CONFLICT(source_key) DO UPDATE SET
                {assignments},
                updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """,
            values,
        )
        return connection.execute(
            "SELECT * FROM learning_examples WHERE source_key=?",
            (example["source_key"],),
        ).fetchone()

    def upsert_human_verified_atomic(
        self,
        example: dict[str, Any],
        *,
        feedback_answer_sources: tuple[str, ...],
    ) -> dict[str, Any]:
        """Persist a Positive only if no opposite exact-answer signal exists."""

        if not self._is_human_authority(example):
            raise ValueError(
                "Human-verified upsert requires explicit human authority metadata."
            )
        metadata = example.get("metadata_json") or {}
        inquiry_id = int(example["inquiry_id"])
        reference_id = int(
            metadata.get("answer_reference_id")
            or metadata.get("naver_posted_answer_id")
            or example.get("answer_draft_id")
        )
        masked_answer = str(example.get("final_answer") or "")
        sources = tuple(str(value) for value in feedback_answer_sources)
        with self.database.transaction() as connection:
            if sources:
                placeholders = ",".join("?" for _ in sources)
                conflict = connection.execute(
                    f"""
                    SELECT * FROM learning_feedback
                    WHERE inquiry_id=? AND active=1
                      AND source IN ('DASHBOARD_NEGATIVE_REVIEW','DASHBOARD_EXCLUDED')
                      AND learning_signal_type IN ('NEGATIVE','EXCLUDED')
                      AND original_answer_reference_id=?
                      AND original_answer_source IN ({placeholders})
                      AND original_answer_masked=?
                    ORDER BY updated_at DESC, id DESC LIMIT 1
                    """,
                    (inquiry_id, reference_id, *sources, masked_answer),
                ).fetchone()
                if conflict is not None:
                    details = dict(conflict)
                    details["metadata_json"] = deserialize_json(
                        details.get("metadata_json")
                    )
                    raise LearningConflictError(
                        "동일한 답변의 Negative/EXCLUDED 상태가 이미 저장되었습니다.",
                        conflict=details,
                    )
            row = self._upsert_with_connection(
                connection,
                example,
                allow_human_authority_update=True,
            )
        result = self._row(row)
        assert result is not None
        return result

    def get_by_source_key(self, source_key: str) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM learning_examples WHERE source_key=?",
                (str(source_key),),
            ).fetchone()
        return self._row(row)

    def get(self, learning_id: int) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM learning_examples WHERE id=?",
                (int(learning_id),),
            ).fetchone()
        return self._row(row)

    def revoke_human_verified(
        self,
        *,
        learning_id: int,
        inquiry_id: int,
        reason: str,
        actor: str,
        approval_history_id: int,
    ) -> dict[str, Any]:
        """Soft-revoke one exact Human Verified Positive Learning row."""

        clean_reason = str(reason or "").strip()
        if not clean_reason:
            raise ValueError("승인 취소 사유를 입력해 주세요.")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM learning_examples WHERE id=? AND inquiry_id=?",
                (int(learning_id), int(inquiry_id)),
            ).fetchone()
            if row is None:
                raise LookupError(f"Learning not found: {learning_id}")
            metadata = deserialize_json(row["metadata_json"])
            if not bool(row["active"]) or not bool(metadata.get("human_verified")):
                raise ValueError("활성 Human Verified Positive Learning만 취소할 수 있습니다.")
            connection.execute(
                """
                UPDATE learning_examples
                SET active=0,
                    metadata_json=json_set(
                        COALESCE(metadata_json, '{}'),
                        '$.human_verified', 0,
                        '$.learning_status', 'REVOKED',
                        '$.verification_revoked', 1,
                        '$.revoked_from_human_verified', 1,
                        '$.revoke_reason', ?,
                        '$.revoked_by', ?,
                        '$.revoked_at', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                        '$.revoke_approval_history_id', ?
                    ),
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id=? AND inquiry_id=?
                """,
                (
                    clean_reason[:1_000],
                    str(actor or "관리자").strip() or "관리자",
                    int(approval_history_id),
                    int(learning_id),
                    int(inquiry_id),
                ),
            )
            updated = connection.execute(
                "SELECT * FROM learning_examples WHERE id=?",
                (int(learning_id),),
            ).fetchone()
        result = self._row(updated)
        assert result is not None
        return result

    def for_inquiry(self, inquiry_id: int) -> list[dict[str, Any]]:
        """Return every Learning example for one inquiry, newest first."""

        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM learning_examples
                WHERE inquiry_id=?
                ORDER BY created_at DESC, id DESC
                """,
                (int(inquiry_id),),
            ).fetchall()
        return [self._row(row) for row in rows if row is not None]

    def active_human_verified_for_answer(
        self,
        *,
        inquiry_id: int,
        answer_provenance: str,
        answer_reference_id: int,
    ) -> list[dict[str, Any]]:
        """Return active human-verified positives for one persisted answer reference."""

        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM learning_examples
                WHERE inquiry_id=? AND active=1
                  AND COALESCE(
                      json_extract(metadata_json, '$.learning_signal_type'),
                      'POSITIVE'
                  )='POSITIVE'
                  AND json_extract(metadata_json, '$.human_verified')=1
                  AND json_extract(metadata_json, '$.answer_provenance')=?
                  AND COALESCE(
                      json_extract(metadata_json, '$.answer_reference_id'),
                      CASE
                        WHEN json_extract(metadata_json, '$.answer_provenance')='NAVER_POSTED'
                          THEN json_extract(metadata_json, '$.naver_posted_answer_id')
                        ELSE answer_draft_id
                      END
                  )=?
                ORDER BY created_at DESC, id DESC
                """,
                (
                    int(inquiry_id),
                    str(answer_provenance),
                    int(answer_reference_id),
                ),
            ).fetchall()
        return [self._row(row) for row in rows if row is not None]

    def active_for_answer(
        self,
        *,
        inquiry_id: int,
        answer_provenance: str,
        answer_reference_id: int,
    ) -> list[dict[str, Any]]:
        """Return active Positive rows for one exact persisted answer identity."""
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM learning_examples
                WHERE inquiry_id=? AND active=1
                  AND COALESCE(
                      json_extract(metadata_json, '$.learning_signal_type'),
                      'POSITIVE'
                  )='POSITIVE'
                  AND json_extract(metadata_json, '$.answer_provenance')=?
                  AND COALESCE(
                      json_extract(metadata_json, '$.answer_reference_id'),
                      CASE
                        WHEN json_extract(metadata_json, '$.answer_provenance')='NAVER_POSTED'
                          THEN json_extract(metadata_json, '$.naver_posted_answer_id')
                        ELSE answer_draft_id
                      END
                  )=?
                ORDER BY created_at DESC, id DESC
                """,
                (int(inquiry_id), str(answer_provenance), int(answer_reference_id)),
            ).fetchall()
        return [self._row(row) for row in rows if row is not None]

    def candidates(self, *, store_code: str | None, limit: int = 200) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT learning_examples.*, inquiries.product_id AS source_product_id
                FROM learning_examples
                LEFT JOIN inquiries ON inquiries.id=learning_examples.inquiry_id
                WHERE learning_examples.active=1
                  AND COALESCE(
                      json_extract(metadata_json, '$.learning_signal_type'),
                      'POSITIVE'
                  )='POSITIVE'
                  AND (
                      learning_examples.store_code=?
                      OR learning_examples.store_code IS NULL OR ? IS NULL
                  )
                ORDER BY learning_examples.rating DESC,
                         learning_examples.quality_score DESC,
                         learning_examples.created_at DESC
                LIMIT ?
                """,
                (store_code, store_code, max(1, min(int(limit), 500))),
            ).fetchall()
        return [self._row(row) for row in rows]

    def mark_posted(self, inquiry_id: int, *, posted_at: str | None, auto_posted: bool) -> int:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE learning_examples
                SET posted=1, posted_at=COALESCE(?, posted_at), auto_posted=?,
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE inquiry_id=? AND active=1
                """,
                (posted_at, int(bool(auto_posted)), int(inquiry_id)),
            )
            return int(cursor.rowcount)

    def count(self) -> int:
        with self.database.connection() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM learning_examples").fetchone()[0])

    def mark_used(self, ids: list[int]) -> None:
        clean = sorted({int(value) for value in ids})
        if not clean:
            return
        placeholders = ",".join("?" for _ in clean)
        with self.database.transaction() as connection:
            connection.execute(
                f"""
                UPDATE learning_examples
                SET usage_count=usage_count+1,
                    last_used_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id IN ({placeholders}) AND active=1
                """,
                clean,
            )

    def manager_summary(self) -> dict[str, Any]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT learning_source, COUNT(*) AS count
                FROM learning_examples GROUP BY learning_source
                """
            ).fetchall()
            totals = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN active=1 THEN 1 ELSE 0 END) AS active,
                       SUM(usage_count) AS searches,
                       MAX(created_at) AS recent
                FROM learning_examples
                """
            ).fetchone()
            automatic_positive = int(connection.execute(
                """
                SELECT COUNT(*) FROM learning_examples
                WHERE learning_source='AUTO_POST_REVIEWED_NO_CHANGE'
                  AND json_extract(metadata_json, '$.acceptance_mode')='AUTO_OBSERVATION'
                """
            ).fetchone()[0])
            human_verified = int(connection.execute(
                """
                SELECT COUNT(*) FROM learning_examples
                WHERE active=1
                  AND COALESCE(
                      json_extract(metadata_json, '$.learning_signal_type'),
                      'POSITIVE'
                  )='POSITIVE'
                  AND json_extract(metadata_json, '$.human_verified')=1
                """
            ).fetchone()[0])
            positive_active = int(connection.execute(
                """
                SELECT COUNT(*) FROM learning_examples
                WHERE active=1
                  AND COALESCE(
                      json_extract(metadata_json, '$.learning_signal_type'),
                      'POSITIVE'
                  )='POSITIVE'
                """
            ).fetchone()[0])
        sources = {str(row["learning_source"]): int(row["count"]) for row in rows}
        return {
            "total": int(totals["total"] or 0),
            "active": int(totals["active"] or 0),
            "inactive": int(totals["total"] or 0) - int(totals["active"] or 0),
            "searches": int(totals["searches"] or 0),
            "recent": totals["recent"],
            "sources": sources,
            "automatic_positive": automatic_positive,
            "human_verified": human_verified,
            "positive_active": positive_active,
        }

    def deactivate_automatic_positive(
        self, inquiry_id: int, *, superseded_by_learning_id: int | None = None
    ) -> int:
        """Preserve but stop using an observation-only signal after a later edit."""
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE learning_examples
                SET active=0,
                    metadata_json=json_set(
                        COALESCE(metadata_json, '{}'),
                        '$.superseded', 1,
                        '$.superseded_reason', 'LATER_NAVER_STAFF_EDIT',
                        '$.superseded_by_learning_id', ?
                    ),
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE inquiry_id=? AND active=1
                  AND learning_source='AUTO_POST_REVIEWED_NO_CHANGE'
                  AND json_extract(metadata_json, '$.acceptance_mode')='AUTO_OBSERVATION'
                """,
                (superseded_by_learning_id, int(inquiry_id)),
            )
        return int(cursor.rowcount)

    def manager_rows(self, *, limit: int = 500) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, source_key, inquiry_id, answer_draft_id,
                       approval_history_id, question_original_masked, gpt_draft,
                       edited_answer, final_answer, learning_source,
                       rating, quality_score, usage_count, last_used_at,
                       active, metadata_json, created_at, updated_at
                FROM learning_examples
                ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                (max(1, min(int(limit), 2000)),),
            ).fetchall()
        results = [dict(row) for row in rows]
        for row in results:
            row["active"] = bool(row["active"])
            row["metadata_json"] = deserialize_json(row.get("metadata_json"))
            metadata = row["metadata_json"]
            row["provenance"] = metadata.get("answer_provenance")
            row["human_verified"] = bool(metadata.get("human_verified"))
            row["signal_type"] = metadata.get(
                "learning_signal_type", "POSITIVE"
            )
        return results

    def deactivate_draft(self, draft_id: int) -> int:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE learning_examples SET active=0,
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE answer_draft_id=? AND active=1
                """,
                (int(draft_id),),
            )
            return int(cursor.rowcount)
