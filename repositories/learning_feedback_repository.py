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
