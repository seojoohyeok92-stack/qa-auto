from __future__ import annotations

from typing import Any

from answer.learning_conflict import LearningConflictError
from answer.answer_format import format_final_answer
from repositories.database import Database
from repositories.inquiry_repository import deserialize_json, serialize_json
from services.learning_privacy_service import LearningPrivacyService


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
        with self.database.transaction() as connection:
            row = self._upsert_with_connection(connection, feedback)
        result = self._row(row)
        assert result is not None
        return result

    @staticmethod
    def _upsert_with_connection(connection: Any, feedback: dict[str, Any]) -> Any:
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
        return connection.execute(
            "SELECT * FROM learning_feedback WHERE source_key=?",
            (feedback["source_key"],),
        ).fetchone()

    def save_dashboard_evaluation_atomic(
        self,
        feedbacks: list[dict[str, Any]],
        *,
        requested_signal: str,
        positive_answer_sources: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        """Serialize conflict validation and dashboard feedback persistence."""

        if not feedbacks:
            return []
        first = feedbacks[0]
        inquiry_id = int(first["inquiry_id"])
        provenance = str(first["original_answer_source"])
        reference_id = int(first["original_answer_reference_id"])
        masked_answer = str(first.get("original_answer_masked") or "")
        requested = str(requested_signal).upper()
        opposite = (
            ("EXCLUDED",)
            if requested == "NEGATIVE"
            else ("NEGATIVE", "INTENT_CORRECTION")
        )
        with self.database.transaction() as connection:
            if provenance in {
                "PROGRAM_GENERATED", "STAFF_EDITED", "FINAL_ANSWER"
            }:
                approved = connection.execute(
                    """
                    SELECT d.original_answer, d.edited_answer, d.final_answer
                    FROM inquiries i
                    JOIN answer_drafts d ON d.inquiry_id=i.id
                    WHERE i.id=? AND d.id=?
                      AND i.approval_status='APPROVED'
                    LIMIT 1
                    """,
                    (inquiry_id, reference_id),
                ).fetchone()
                approved_provenance = (
                    "STAFF_EDITED"
                    if approved is not None
                    and str(approved["edited_answer"] or "").strip()
                    else "PROGRAM_GENERATED"
                )
                approved_body = (
                    LearningPrivacyService().mask(
                        format_final_answer(
                            str((approved or {})["final_answer"] or "")
                        )
                    )
                    if approved is not None
                    else ""
                )
                if (
                    approved is not None
                    and provenance in {approved_provenance, "FINAL_ANSWER"}
                    and approved_body == masked_answer
                ):
                    raise LearningConflictError(
                        "이 답변은 이미 승인 완료 상태입니다."
                    )
            sources = tuple(str(value) for value in positive_answer_sources)
            if sources:
                placeholders = ",".join("?" for _ in sources)
                positive_rows = connection.execute(
                    f"""
                    SELECT * FROM learning_examples
                    WHERE inquiry_id=? AND active=1
                      AND COALESCE(
                          json_extract(metadata_json, '$.learning_signal_type'),
                          'POSITIVE'
                      )='POSITIVE'
                      AND json_extract(metadata_json, '$.human_verified')=1
                      AND json_extract(metadata_json, '$.answer_provenance')
                          IN ({placeholders})
                      AND COALESCE(
                          json_extract(metadata_json, '$.answer_reference_id'),
                          json_extract(metadata_json, '$.naver_posted_answer_id'),
                          answer_draft_id
                      )=?
                    ORDER BY updated_at DESC, id DESC
                    """,
                    (inquiry_id, *sources, reference_id),
                ).fetchall()
                positive = next(
                    (
                        row
                        for row in positive_rows
                        if LearningPrivacyService().mask(
                            format_final_answer(str(row["final_answer"] or ""))
                        )
                        == masked_answer
                    ),
                    None,
                )
                if positive is not None:
                    details = dict(positive)
                    details["metadata_json"] = deserialize_json(
                        details.get("metadata_json")
                    )
                    raise LearningConflictError(
                        "동일한 답변은 이미 Human Verified Positive입니다.",
                        conflict=details,
                    )
            placeholders = ",".join("?" for _ in opposite)
            conflict = connection.execute(
                f"""
                SELECT * FROM learning_feedback
                WHERE inquiry_id=? AND active=1
                  AND source IN ('DASHBOARD_NEGATIVE_REVIEW','DASHBOARD_EXCLUDED')
                  AND original_answer_source=?
                  AND original_answer_reference_id=?
                  AND original_answer_masked=?
                  AND learning_signal_type IN ({placeholders})
                ORDER BY updated_at DESC, id DESC LIMIT 1
                """,
                (inquiry_id, provenance, reference_id, masked_answer, *opposite),
            ).fetchone()
            if conflict is not None:
                details = dict(conflict)
                details["metadata_json"] = deserialize_json(
                    details.get("metadata_json")
                )
                raise LearningConflictError(
                    "다른 사용자가 이 답변의 평가 상태를 이미 변경했습니다.",
                    conflict=details,
                )
            if requested == "NEGATIVE":
                connection.execute(
                    """
                    UPDATE learning_feedback
                    SET active=0,
                        updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE inquiry_id=?
                      AND source='DASHBOARD_NEGATIVE_REVIEW'
                      AND original_answer_source=?
                      AND original_answer_reference_id=? AND active=1
                    """,
                    (inquiry_id, provenance, reference_id),
                )
            rows = [
                self._upsert_with_connection(connection, feedback)
                for feedback in feedbacks
            ]
        return [self._row(row) for row in rows if row is not None]

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

    def active_dashboard_feedback(
        self,
        *,
        inquiry_id: int,
        original_answer_source: str,
        original_answer_reference_id: int,
        signal_types: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [
            int(inquiry_id),
            str(original_answer_source),
            int(original_answer_reference_id),
        ]
        signal_clause = ""
        if signal_types:
            normalized = tuple(str(value).upper() for value in signal_types)
            signal_clause = (
                " AND learning_signal_type IN ("
                + ",".join("?" for _ in normalized)
                + ")"
            )
            params.extend(normalized)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM learning_feedback
                WHERE inquiry_id=?
                  AND source IN ('DASHBOARD_NEGATIVE_REVIEW','DASHBOARD_EXCLUDED')
                  AND original_answer_source=?
                  AND original_answer_reference_id=? AND active=1
                  {signal_clause}
                ORDER BY updated_at DESC, id DESC
                """,
                params,
            ).fetchall()
        return [self._row(row) for row in rows if row is not None]

    def latest_active_dashboard_exclusion(
        self, inquiry_id: int
    ) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            target = connection.execute(
                """
                SELECT original_answer_source, original_answer_reference_id
                FROM learning_feedback
                WHERE inquiry_id=? AND source='DASHBOARD_EXCLUDED'
                  AND learning_signal_type='EXCLUDED' AND active=1
                ORDER BY updated_at DESC, id DESC LIMIT 1
                """,
                (int(inquiry_id),),
            ).fetchone()
        if target is None:
            return []
        return self.active_dashboard_feedback(
            inquiry_id=int(inquiry_id),
            original_answer_source=str(target["original_answer_source"]),
            original_answer_reference_id=int(target["original_answer_reference_id"]),
            signal_types=("EXCLUDED",),
        )

    def dashboard_feedback_history(
        self,
        *,
        inquiry_id: int,
        original_answer_source: str,
        original_answer_reference_id: int,
        signal_types: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        """Return active and revoked feedback for one exact answer identity."""

        params: list[Any] = [
            int(inquiry_id),
            str(original_answer_source),
            int(original_answer_reference_id),
        ]
        signal_clause = ""
        if signal_types:
            normalized = tuple(str(value).upper() for value in signal_types)
            signal_clause = (
                " AND learning_signal_type IN ("
                + ",".join("?" for _ in normalized)
                + ")"
            )
            params.extend(normalized)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM learning_feedback
                WHERE inquiry_id=?
                  AND source IN ('DASHBOARD_NEGATIVE_REVIEW','DASHBOARD_EXCLUDED')
                  AND original_answer_source=?
                  AND original_answer_reference_id=?
                  {signal_clause}
                ORDER BY active DESC, updated_at DESC, id DESC
                """,
                params,
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

    def revoke_dashboard_exclusion(
        self, *, feedback_id: int, reason: str, actor: str
    ) -> dict[str, Any]:
        clean_reason = str(reason or "").strip()
        if not clean_reason:
            raise ValueError("학습 제외 취소 사유를 입력해 주세요.")
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM learning_feedback
                WHERE id=? AND source='DASHBOARD_EXCLUDED'
                  AND learning_signal_type='EXCLUDED'
                """,
                (int(feedback_id),),
            ).fetchone()
            if row is None:
                raise LookupError(f"Excluded feedback not found: {feedback_id}")
            if not bool(row["active"]):
                raise ValueError("이미 취소된 학습 제외 기록입니다.")
            metadata = deserialize_json(row["metadata_json"])
            metadata.update(
                {
                    "status": "REVOKED",
                    "revoke_reason": clean_reason[:1_000],
                    "revoked_by": str(actor or "직원").strip() or "직원",
                }
            )
            connection.execute(
                """
                UPDATE learning_feedback
                SET active=0, metadata_json=json_set(
                        ?, '$.revoked_at', strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    ),
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id=?
                """,
                (serialize_json(metadata), int(feedback_id)),
            )
            updated = connection.execute(
                "SELECT * FROM learning_feedback WHERE id=?", (int(feedback_id),)
            ).fetchone()
        result = self._row(updated)
        assert result is not None
        return result

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
