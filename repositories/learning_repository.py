from __future__ import annotations

from typing import Any

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
        with self.database.transaction() as connection:
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
            row = connection.execute(
                "SELECT * FROM learning_examples WHERE source_key=?",
                (example["source_key"],),
            ).fetchone()
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

    def candidates(self, *, store_code: str | None, limit: int = 200) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM learning_examples
                WHERE active=1
                  AND COALESCE(
                      json_extract(metadata_json, '$.learning_signal_type'),
                      'POSITIVE'
                  )='POSITIVE'
                  AND (store_code=? OR store_code IS NULL OR ? IS NULL)
                ORDER BY rating DESC, quality_score DESC, created_at DESC
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
