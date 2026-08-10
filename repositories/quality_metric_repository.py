from __future__ import annotations

from typing import Any

from repositories.database import Database
from repositories.inquiry_repository import deserialize_json, serialize_json


class QualityMetricRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, metric: dict[str, Any]) -> dict[str, Any]:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO quality_metrics (
                    inquiry_id, answer_draft_id, actor, category,
                    character_change_ratio, word_change_ratio,
                    sentences_added, sentences_deleted, fact_changed,
                    prohibited_expression_changed, tone_changed,
                    edit_duration_seconds, approved, regeneration_count,
                    details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    metric["inquiry_id"],
                    metric["answer_draft_id"],
                    metric["actor"],
                    metric.get("category"),
                    metric["character_change_ratio"],
                    metric["word_change_ratio"],
                    metric["sentences_added"],
                    metric["sentences_deleted"],
                    int(metric["fact_changed"]),
                    int(metric["prohibited_expression_changed"]),
                    int(metric["tone_changed"]),
                    metric.get("edit_duration_seconds"),
                    int(metric["approved"]),
                    metric["regeneration_count"],
                    serialize_json(metric.get("details", {})),
                ),
            )
            row_id = int(cursor.lastrowid)
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM quality_metrics WHERE id = ?", (row_id,)
            ).fetchone()
        result = dict(row)
        result["details_json"] = deserialize_json(result["details_json"])
        return result

    def latest_for_draft(self, draft_id: int) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM quality_metrics
                WHERE answer_draft_id = ?
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (draft_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["details_json"] = deserialize_json(result["details_json"])
        return result

