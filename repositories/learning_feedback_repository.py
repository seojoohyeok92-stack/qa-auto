from __future__ import annotations

from typing import Any

from answer.learning_conflict import LearningConflictError
from answer.answer_format import format_final_answer
from repositories.database import Database
from repositories.inquiry_repository import (
    InquiryRepository,
    deserialize_json,
    serialize_json,
)
from repositories.learning_manager_query import (
    LearningManagerPage,
    manager_page_bounds,
    manager_search_sql,
)
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
        row = connection.execute(
            "SELECT * FROM learning_feedback WHERE source_key=?",
            (feedback["source_key"],),
        ).fetchone()
        if (
            row is not None
            and bool(row["active"])
            and str(row["learning_signal_type"] or "").upper()
            in {"NEGATIVE", "EXCLUDED"}
        ):
            LearningFeedbackRepository._soft_revoke_matching_auto(
                connection, row
            )
        return row

    @staticmethod
    def _soft_revoke_matching_auto(connection: Any, feedback: Any) -> None:
        """Keep audit history while blocking the exact rejected AUTO answer."""

        inquiry_id = feedback["inquiry_id"]
        provenance = str(feedback["original_answer_source"] or "")
        reference_id = feedback["original_answer_reference_id"]
        masked_answer = str(feedback["original_answer_masked"] or "")
        if inquiry_id is None or not provenance or not masked_answer:
            return
        sources = {
            provenance,
            *(
                {"PROGRAM_GENERATED", "STAFF_EDITED"}
                if provenance == "FINAL_ANSWER"
                else set()
            ),
        }
        candidates = connection.execute(
            """
            SELECT * FROM learning_examples
            WHERE inquiry_id=? AND active=1
              AND COALESCE(
                  json_extract(metadata_json, '$.learning_signal_type'),
                  'POSITIVE'
              )='POSITIVE'
              AND COALESCE(
                  json_extract(metadata_json, '$.human_verified'), 0
              )=0
              AND approval_history_id IS NULL
              AND upper(COALESCE(validator_result, '')) NOT IN (
                  'HUMAN_VERIFIED_NAVER_POSTED',
                  'HISTORICAL_ADMIN_APPROVED'
              )
              AND NOT (
                  upper(COALESCE(json_extract(metadata_json,
                      '$.source_origin'), ''))='HISTORICAL_PROMOTED'
                  AND trim(COALESCE(json_extract(metadata_json,
                      '$.promoted_by'), ''))<>''
              )
            """,
            (int(inquiry_id),),
        ).fetchall()
        for candidate in candidates:
            metadata = deserialize_json(candidate["metadata_json"])
            candidate_provenance = str(
                metadata.get("answer_provenance") or ""
            )
            if (
                not candidate_provenance
                and str(candidate["learning_source"] or "") == "SELLER_ANSWER"
            ):
                candidate_provenance = "NAVER_POSTED"
            if candidate_provenance not in sources:
                continue
            candidate_reference = (
                metadata.get("answer_reference_id")
                or metadata.get("naver_posted_answer_id")
                or candidate["answer_draft_id"]
            )
            if (
                candidate_reference is not None
                and reference_id is not None
                and int(candidate_reference) != int(reference_id)
            ):
                continue
            candidate_answer = LearningPrivacyService().mask(
                format_final_answer(str(candidate["final_answer"] or ""))
            )
            if candidate_answer != masked_answer:
                continue
            connection.execute(
                """
                UPDATE learning_examples
                SET active=0,
                    metadata_json=json_set(
                        COALESCE(metadata_json, '{}'),
                        '$.learning_status', 'REVOKED',
                        '$.effective_exclusion', ?,
                        '$.excluded_by_feedback_id', ?,
                        '$.excluded_answer_source', ?,
                        '$.excluded_answer_reference_id', ?,
                        '$.superseded', 1,
                        '$.superseded_reason', 'NEGATIVE_FEEDBACK',
                        '$.superseded_at',
                            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    ),
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id=? AND active=1
                """,
                (
                    str(feedback["learning_signal_type"] or "NEGATIVE").upper(),
                    int(feedback["id"]), provenance, reference_id,
                    int(candidate["id"]),
                ),
            )

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

    _MANAGER_SELECT = """
        SELECT lf.*,
               i.source_question_id, i.external_inquiry_id,
               i.source_created_at, i.registered_at,
               i.source_type AS inquiry_source_type,
               i.inquiry_type AS source_inquiry_type,
               i.product_name AS inquiry_product_name,
               i.title AS inquiry_title,
               i.content AS inquiry_content,
               COALESCE(
                   i.source_created_at,
                   i.registered_at,
                   lf.created_at,
                   lf.updated_at
               ) AS inquiry_occurred_at
        FROM learning_feedback AS lf
        LEFT JOIN inquiries AS i ON i.id=lf.inquiry_id
    """
    _MANAGER_ORDER = """
        ORDER BY COALESCE(
                     i.source_created_at,
                     i.registered_at,
                     lf.created_at
                 ) DESC,
                 lf.id DESC
    """
    _MANAGER_SEARCH = " || ' ' || ".join(
        f"COALESCE(CAST({field} AS TEXT), '')"
        for field in (
            "lf.id", "lf.source_key", "lf.inquiry_id", "lf.answer_draft_id",
            "lf.original_answer_reference_id", "i.source_question_id",
            "i.external_inquiry_id", "lf.question_masked", "i.title",
            "i.content", "i.product_name", "lf.corrected_answer_masked",
            "lf.original_answer_masked", "lf.source",
            "lf.original_answer_source", "lf.learning_signal_type",
            "lf.correction_reason", "lf.correction_note",
            "json_extract(lf.metadata_json, '$.answer_provenance')",
            "json_extract(lf.metadata_json, '$.answer_reference_id')",
            "json_extract(lf.metadata_json, '$.verified_by')",
            "json_extract(lf.metadata_json, '$.learning_status')",
            "json_extract(lf.metadata_json, '$.revoke_reason')",
            "json_extract(lf.metadata_json, '$.revoked_by')",
        )
    )

    def _manager_results(self, rows: Any) -> list[dict[str, Any]]:
        results = [self._row(row) for row in rows if row is not None]
        values = [row for row in results if row is not None]
        states = InquiryRepository(self.database).learning_states([
            int(row["inquiry_id"])
            for row in values if row.get("inquiry_id") is not None
        ])
        for row in values:
            state = states.get(int(row["inquiry_id"])) if row.get("inquiry_id") else None
            row["effective_learning_status"] = (
                (state or {}).get("learning_status") or "NONE"
            )
            row["effective_learning_tooltip"] = (
                (state or {}).get("learning_tooltip") or "Learning 이력 없음"
            )
        return values

    def manager_rows(self, *, limit: int = 500) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                self._MANAGER_SELECT + self._MANAGER_ORDER + " LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return self._manager_results(rows)

    def manager_filter_options(self) -> dict[str, list[str]]:
        provenance_sql = (
            "COALESCE(lf.original_answer_source, "
            "json_extract(lf.metadata_json, '$.answer_provenance'), 'UNKNOWN')"
        )
        with self.database.connection() as connection:
            sources = connection.execute(
                "SELECT DISTINCT lf.source AS value FROM learning_feedback AS lf "
                "WHERE trim(COALESCE(lf.source, ''))<>'' ORDER BY value"
            ).fetchall()
            provenance = connection.execute(
                f"SELECT DISTINCT {provenance_sql} AS value "
                "FROM learning_feedback AS lf ORDER BY value"
            ).fetchall()
        return {
            "sources": [str(row["value"]) for row in sources],
            "provenance": [str(row["value"]) for row in provenance],
        }

    def manager_page(
        self,
        *,
        query: str = "",
        source: str = "ALL",
        provenance: str = "ALL",
        human_verified: str = "ALL",
        signal_type: str = "ALL",
        page: int = 1,
        page_size: int = 20,
    ) -> LearningManagerPage:
        clauses: list[str] = []
        parameters: list[Any] = []
        search_clause, search_parameters = manager_search_sql(
            self._MANAGER_SEARCH, query
        )
        if search_clause:
            clauses.append(search_clause)
            parameters.extend(search_parameters)
        if source != "ALL":
            clauses.append("lf.source=?")
            parameters.append(source)
        if provenance != "ALL":
            clauses.append(
                "COALESCE(lf.original_answer_source, "
                "json_extract(lf.metadata_json, '$.answer_provenance'), 'UNKNOWN')=?"
            )
            parameters.append(provenance)
        verified = "COALESCE(json_extract(lf.metadata_json, '$.human_verified'), 0)=1"
        if human_verified == "YES":
            clauses.append(verified)
        elif human_verified == "NO":
            clauses.append("NOT (" + verified + ")")
        if signal_type != "ALL":
            clauses.append("lf.learning_signal_type=?")
            parameters.append(signal_type)
        where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.database.connection() as connection:
            total = int(connection.execute(
                "SELECT COUNT(*) FROM learning_feedback AS lf "
                "LEFT JOIN inquiries AS i ON i.id=lf.inquiry_id" + where_sql,
                tuple(parameters),
            ).fetchone()[0])
            safe_page, safe_size, offset = manager_page_bounds(
                total=total, page=page, page_size=page_size
            )
            rows = connection.execute(
                self._MANAGER_SELECT + where_sql + self._MANAGER_ORDER
                + " LIMIT ? OFFSET ?",
                (*parameters, safe_size, offset),
            ).fetchall()
        return LearningManagerPage(
            rows=self._manager_results(rows), total=total,
            page=safe_page, page_size=safe_size,
        )

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
        reason: str,
        actor: str,
    ) -> int:
        """Soft-revoke one exact dashboard Negative evaluation group.

        A routing correction can create both NEGATIVE and INTENT_CORRECTION
        rows for the same evaluated answer.  They are one operator action, so
        revoke them atomically and revoke only their signal confirmations.
        """

        clean_reason = str(reason or "").strip()
        if not clean_reason:
            raise ValueError("Negative 평가 취소 사유를 입력해 주세요.")
        clean_actor = str(actor or "직원").strip() or "직원"
        with self.database.transaction() as connection:
            affected = connection.execute(
                """
                SELECT id, metadata_json FROM learning_feedback
                WHERE inquiry_id=? AND source='DASHBOARD_NEGATIVE_REVIEW'
                  AND original_answer_source=?
                  AND original_answer_reference_id=? AND active=1
                """,
                (
                    int(inquiry_id),
                    str(original_answer_source),
                    int(original_answer_reference_id),
                ),
            ).fetchall()
            changed = 0
            for row in affected:
                metadata = deserialize_json(row["metadata_json"])
                metadata.update(
                    {
                        "status": "REVOKED",
                        "revoke_reason": clean_reason[:1_000],
                        "revoked_by": clean_actor,
                    }
                )
                cursor = connection.execute(
                    """
                    UPDATE learning_feedback
                    SET active=0, metadata_json=json_set(
                            ?, '$.revoked_at',
                            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        ),
                        updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE id=? AND active=1
                    """,
                    (serialize_json(metadata), int(row["id"])),
                )
                if cursor.rowcount != 1:
                    continue
                changed += 1
                # Preserve the signal and its old answer provenance while
                # removing this feedback's current confirmation authority.
                connection.execute(
                    """
                    UPDATE learning_signal_confirmations
                    SET active=0, revoked_reason=?,
                        revoked_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE learning_feedback_id=? AND active=1
                    """,
                    (f"NEGATIVE_EVALUATION_REVOKED: {clean_reason}"[:1_000], int(row["id"])),
                )
        return changed

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
