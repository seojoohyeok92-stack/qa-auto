from __future__ import annotations

from typing import Any

from repositories.database import Database
from repositories.inquiry_repository import deserialize_json, serialize_json


class LearningFeedbackRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _row(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["metadata_json"] = deserialize_json(result.get("metadata_json"))
        result["active"] = bool(result["active"])
        return result

    def upsert(self, feedback: dict[str, Any]) -> dict[str, Any]:
        columns = (
            "source_key", "feedback_type", "correction_reason",
            "correction_note", "corrected_intent", "learning_signal_type",
            "source", "inquiry_id", "answer_draft_id", "historical_case_id",
            "original_answer_source", "original_answer_reference_id",
            "question_masked", "original_answer_masked",
            "corrected_answer_masked", "metadata_json", "active",
        )
        values = [
            serialize_json(feedback.get(column) or {})
            if column == "metadata_json"
            else int(bool(feedback.get(column)))
            if column == "active"
            else feedback.get(column)
            for column in columns
        ]
        assignments = ", ".join(
            f"{column}=excluded.{column}"
            for column in columns
            if column != "source_key"
        )
        with self.database.transaction() as connection:
            connection.execute(
                f"""
                INSERT INTO learning_feedback ({', '.join(columns)})
                VALUES ({', '.join('?' for _ in columns)})
                ON CONFLICT(source_key) DO UPDATE SET
                    {assignments},
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                values,
            )
            row = connection.execute(
                "SELECT * FROM learning_feedback WHERE source_key=?",
                (feedback["source_key"],),
            ).fetchone()
        result = self._row(row)
        assert result is not None
        return result

    def for_inquiry(self, inquiry_id: int) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM learning_feedback
                WHERE inquiry_id=? ORDER BY id
                """,
                (int(inquiry_id),),
            ).fetchall()
        return [self._row(row) for row in rows if row is not None]

    def active_dashboard_evaluation(
        self,
        *,
        inquiry_id: int,
        original_answer_source: str,
        original_answer_reference_id: int,
    ) -> list[dict[str, Any]]:
        """Return persisted active feedback for one evaluated dashboard answer."""
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM learning_feedback
                WHERE inquiry_id=? AND source='DASHBOARD_NEGATIVE_REVIEW'
                  AND original_answer_source=?
                  AND original_answer_reference_id=? AND active=1
                ORDER BY CASE learning_signal_type
                    WHEN 'NEGATIVE' THEN 0
                    WHEN 'INTENT_CORRECTION' THEN 1
                    ELSE 2 END,
                    id
                """,
                (
                    int(inquiry_id),
                    str(original_answer_source),
                    int(original_answer_reference_id),
                ),
            ).fetchall()
        return [self._row(row) for row in rows if row is not None]

    def latest_active_dashboard_evaluation(
        self, inquiry_id: int
    ) -> list[dict[str, Any]]:
        """Return the latest persisted dashboard evaluation for an inquiry."""
        with self.database.connection() as connection:
            target = connection.execute(
                """
                SELECT original_answer_source, original_answer_reference_id
                FROM learning_feedback
                WHERE inquiry_id=? AND source='DASHBOARD_NEGATIVE_REVIEW'
                  AND learning_signal_type='NEGATIVE' AND active=1
                ORDER BY updated_at DESC, id DESC LIMIT 1
                """,
                (int(inquiry_id),),
            ).fetchone()
        if target is None:
            return []
        return self.active_dashboard_evaluation(
            inquiry_id=int(inquiry_id),
            original_answer_source=str(target["original_answer_source"]),
            original_answer_reference_id=int(
                target["original_answer_reference_id"]
            ),
        )

    def for_historical_case(self, case_id: int) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM learning_feedback
                WHERE historical_case_id=? ORDER BY id
                """,
                (int(case_id),),
            ).fetchall()
        return [self._row(row) for row in rows if row is not None]

    def candidates(self, signal_type: str) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM learning_feedback
                WHERE active=1 AND learning_signal_type=?
                ORDER BY created_at DESC, id DESC
                """,
                (str(signal_type).upper(),),
            ).fetchall()
        return [self._row(row) for row in rows if row is not None]

    def manager_rows(self, *, limit: int = 500) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM learning_feedback
                ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                (max(1, min(int(limit), 2_000)),),
            ).fetchall()
        return [self._row(row) for row in rows if row is not None]

    def manager_summary(self) -> dict[str, int]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT learning_signal_type, COUNT(*) AS count
                FROM learning_feedback WHERE active=1
                GROUP BY learning_signal_type
                """
            ).fetchall()
        return {str(row["learning_signal_type"]): int(row["count"]) for row in rows}

    def deactivate_dashboard_evaluation(
        self,
        *,
        inquiry_id: int,
        original_answer_source: str,
        original_answer_reference_id: int,
    ) -> int:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE learning_feedback
                SET active=0,
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE inquiry_id=? AND source='DASHBOARD_NEGATIVE_REVIEW'
                  AND original_answer_source=?
                  AND original_answer_reference_id=? AND active=1
                """,
                (
                    int(inquiry_id),
                    str(original_answer_source),
                    int(original_answer_reference_id),
                ),
            )
        return int(cursor.rowcount)

    def deactivate_for_draft(self, draft_id: int) -> int:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE learning_feedback SET active=0,
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE answer_draft_id=? AND feedback_type='STAFF_CORRECTION'
                  AND active=1
                """,
                (int(draft_id),),
            )
        return int(cursor.rowcount)

    def deactivate_for_historical_case(self, case_id: int) -> int:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE learning_feedback SET active=0,
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE historical_case_id=? AND feedback_type='HISTORICAL_REVIEW'
                  AND active=1
                """,
                (int(case_id),),
            )
        return int(cursor.rowcount)
