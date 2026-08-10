from __future__ import annotations

import uuid
from typing import Any

from repositories.database import Database


class LearningProvenanceRepository:
    """Tracks references that were actually included in generation context."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def record_context(
        self, *, inquiry_id: int,
        learning: list[dict[str, Any]], historical: list[dict[str, Any]],
    ) -> str | None:
        rows: list[tuple[Any, ...]] = []
        run_id = str(uuid.uuid4())
        for item in learning:
            reference_id = item.get("learning_example_id")
            if reference_id is None:
                continue
            rows.append((
                run_id, int(inquiry_id), "LEARNING", int(reference_id), None,
                str(item.get("learning_source") or "LEARNING"),
                float(item.get("relevance") or 0),
            ))
        for item in historical:
            reference_id = item.get("historical_case_id")
            if reference_id is None:
                continue
            rows.append((
                run_id, int(inquiry_id), "HISTORICAL", None, int(reference_id),
                str(item.get("source") or "HISTORICAL_VERIFIED_LEARNING"),
                float(item.get("relevance") or 0),
            ))
        if not rows:
            return None
        with self.database.transaction() as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO answer_learning_provenance(
                    context_run_id, inquiry_id, reference_kind,
                    learning_example_id, historical_case_id,
                    source_label, relevance, included_in_prompt
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                """,
                rows,
            )
        return run_id

    def attach_latest_context(self, *, inquiry_id: int, draft_id: int) -> int:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE answer_learning_provenance
                SET answer_draft_id=?
                WHERE inquiry_id=? AND answer_draft_id IS NULL
                  AND context_run_id=(
                    SELECT context_run_id FROM answer_learning_provenance
                    WHERE inquiry_id=? AND answer_draft_id IS NULL
                    ORDER BY created_at DESC, id DESC LIMIT 1
                  )
                """,
                (int(draft_id), int(inquiry_id), int(inquiry_id)),
            )
        return int(cursor.rowcount)

    def for_draft(self, draft_id: int) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT p.*,
                       le.learning_source, le.metadata_json AS learning_metadata,
                       hc.question AS historical_question
                FROM answer_learning_provenance p
                LEFT JOIN learning_examples le ON le.id=p.learning_example_id
                LEFT JOIN historical_cases hc ON hc.id=p.historical_case_id
                WHERE p.answer_draft_id=? AND p.included_in_prompt=1
                ORDER BY p.relevance DESC, p.id
                """,
                (int(draft_id),),
            ).fetchall()
        return [dict(row) for row in rows]
