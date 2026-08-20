from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from answer.learning_conflict import LearningConflictError
from answer.answer_format import format_final_answer
from repositories.database import Database
from repositories.inquiry_repository import (
    InquiryRepository,
    deserialize_json,
    serialize_json,
)
from services.learning_validity_service import (
    is_learning_usable,
    normalize_validity_update,
    validity_status,
)
from services.learning_privacy_service import LearningPrivacyService


class LearningRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _row(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        for key in ("style_features_json", "metadata_json", "condition_json"):
            result[key] = deserialize_json(result.get(key))
        for key in ("posted", "auto_posted", "style_only", "active"):
            result[key] = bool(result[key])
        # A few unit-test doubles model pre-migration rows. Production rows have
        # this column after Database.initialize(), while the fallback preserves
        # the backward-compatible PERMANENT/active interpretation.
        result["validity_active"] = bool(result.get("validity_active", True))
        result["validity_status"] = validity_status(result)
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
            learning_id = int(row["id"])
            # A later, distinct Human Verified answer may supersede an older
            # inquiry-level Negative/Correction for lifecycle display.  The
            # old feedback remains active for its exact answer identity, so
            # that answer stays blocked from runtime retrieval.
            feedback_rows = connection.execute(
                """
                SELECT id, original_answer_masked, metadata_json
                FROM learning_feedback
                WHERE inquiry_id=? AND active=1
                  AND learning_signal_type IN (
                      'NEGATIVE','EXCLUDED','INTENT_CORRECTION'
                  )
                """,
                (inquiry_id,),
            ).fetchall()
            for feedback in feedback_rows:
                if str(feedback["original_answer_masked"] or "") == masked_answer:
                    continue
                connection.execute(
                    """
                    UPDATE learning_feedback
                    SET metadata_json=json_set(
                            COALESCE(metadata_json, '{}'),
                            '$.lifecycle_superseded_by_learning_id', ?,
                            '$.lifecycle_superseded_at',
                                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        ),
                        updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE id=?
                    """,
                    (learning_id, int(feedback["id"])),
                )
            revoked_rows = connection.execute(
                """
                SELECT id, final_answer FROM learning_examples
                WHERE inquiry_id=? AND active=0
                  AND COALESCE(
                      json_extract(metadata_json, '$.verification_revoked'), 0
                  )=1
                  AND id<>?
                """,
                (inquiry_id, learning_id),
            ).fetchall()
            for revoked in revoked_rows:
                if str(revoked["final_answer"] or "") == masked_answer:
                    continue
                connection.execute(
                    """
                    UPDATE learning_examples
                    SET metadata_json=json_set(
                            COALESCE(metadata_json, '{}'),
                            '$.lifecycle_superseded_by_learning_id', ?,
                            '$.lifecycle_superseded_at',
                                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        ),
                        updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE id=?
                    """,
                    (learning_id, int(revoked["id"])),
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
        # The live store has more than 500 ACTIVE rows.  Callers that perform
        # relevance ranking must be able to inspect the complete usable pool;
        # the default remains bounded for legacy/style aggregation callers.
        safe_limit = max(1, min(int(limit), 2000))
        now = datetime.now(UTC).isoformat(timespec="milliseconds")
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT learning_examples.*, inquiries.product_id AS source_product_id
                FROM learning_examples
                LEFT JOIN inquiries ON inquiries.id=learning_examples.inquiry_id
                WHERE learning_examples.active=1
                  AND learning_examples.validity_active=1
                  AND (
                      learning_examples.validity_type='PERMANENT'
                      OR (
                          learning_examples.validity_type='TEMPORARY'
                          AND learning_examples.valid_from IS NOT NULL
                          AND learning_examples.valid_until IS NOT NULL
                          AND julianday(learning_examples.valid_from)<=julianday(?)
                          AND julianday(learning_examples.valid_until)>=julianday(?)
                      )
                  )
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
                (now, now, store_code, store_code, safe_limit),
            ).fetchall()
        # Keep the shared Python policy as a second guard if a legacy timestamp
        # cannot be interpreted consistently by SQLite.  Active feedback is
        # loaded once and matched to exact answer identity so legacy AUTO rows
        # remain auditable but cannot enter runtime evidence.
        items = [
            item
            for row in rows
            if (item := self._row(row)) is not None
            and is_learning_usable(item)
        ]
        inquiry_ids = sorted({
            int(item["inquiry_id"])
            for item in items if item.get("inquiry_id") is not None
        })
        feedback_by_inquiry: dict[int, list[dict[str, Any]]] = {}
        if inquiry_ids:
            placeholders = ",".join("?" for _ in inquiry_ids)
            with self.database.connection() as connection:
                feedback_rows = connection.execute(
                    f"""
                    SELECT * FROM learning_feedback
                    WHERE inquiry_id IN ({placeholders}) AND active=1
                      AND learning_signal_type IN ('NEGATIVE','EXCLUDED')
                    """,
                    inquiry_ids,
                ).fetchall()
            for raw in feedback_rows:
                feedback = dict(raw)
                feedback_by_inquiry.setdefault(
                    int(feedback["inquiry_id"]), []
                ).append(feedback)
        return [
            item for item in items
            if not self._excluded_by_exact_feedback(
                item,
                feedback_by_inquiry.get(int(item.get("inquiry_id") or 0), []),
            )
        ][:safe_limit]

    @staticmethod
    def _excluded_by_exact_feedback(
        item: dict[str, Any], feedbacks: list[dict[str, Any]]
    ) -> bool:
        if not feedbacks:
            return False
        metadata = item.get("metadata_json")
        metadata = metadata if isinstance(metadata, dict) else {}
        provenance = str(metadata.get("answer_provenance") or "")
        if not provenance and str(item.get("learning_source") or "") == "SELLER_ANSWER":
            provenance = "NAVER_POSTED"
        reference = (
            metadata.get("answer_reference_id")
            or metadata.get("naver_posted_answer_id")
            or item.get("answer_draft_id")
        )
        answer = LearningPrivacyService().mask(
            format_final_answer(str(item.get("final_answer") or ""))
        )
        for feedback in feedbacks:
            if str(feedback.get("original_answer_source") or "") != provenance:
                continue
            feedback_reference = feedback.get("original_answer_reference_id")
            if (
                reference is not None
                and feedback_reference is not None
                and int(reference) != int(feedback_reference)
            ):
                continue
            if str(feedback.get("original_answer_masked") or "") == answer:
                return True
        return False

    def candidate_diagnostics(self, *, store_code: str | None) -> dict[str, int]:
        """Summarize why rows cannot enter the ACTIVE Positive candidate pool."""

        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM learning_examples
                WHERE store_code=? OR store_code IS NULL OR ? IS NULL
                """,
                (store_code, store_code),
            ).fetchall()
            inquiry_ids = sorted({
                int(row["inquiry_id"])
                for row in rows if row["inquiry_id"] is not None
            })
            feedback_rows = []
            if inquiry_ids:
                placeholders = ",".join("?" for _ in inquiry_ids)
                feedback_rows = connection.execute(
                    f"""
                    SELECT * FROM learning_feedback
                    WHERE inquiry_id IN ({placeholders}) AND active=1
                      AND learning_signal_type IN ('NEGATIVE','EXCLUDED')
                    """,
                    inquiry_ids,
                ).fetchall()
        feedback_by_inquiry: dict[int, list[dict[str, Any]]] = {}
        for raw in feedback_rows:
            feedback = dict(raw)
            feedback_by_inquiry.setdefault(
                int(feedback["inquiry_id"]), []
            ).append(feedback)
        counts = {
            "repository_rows": len(rows),
            "active_candidates": 0,
            "filtered_by_validity": 0,
            "revoked": 0,
            "negative_excluded": 0,
        }
        for raw in rows:
            item = self._row(raw)
            if item is None:
                continue
            metadata = item.get("metadata_json")
            metadata = metadata if isinstance(metadata, dict) else {}
            signal = str(metadata.get("learning_signal_type") or "POSITIVE").upper()
            if signal != "POSITIVE":
                counts["negative_excluded"] += 1
                continue
            if not item.get("active"):
                if metadata.get("verification_revoked") or str(
                    metadata.get("learning_status") or ""
                ).upper() == "REVOKED":
                    counts["revoked"] += 1
                else:
                    counts["negative_excluded"] += 1
                continue
            if not is_learning_usable(item):
                counts["filtered_by_validity"] += 1
                continue
            if self._excluded_by_exact_feedback(
                item,
                feedback_by_inquiry.get(int(item.get("inquiry_id") or 0), []),
            ):
                counts["negative_excluded"] += 1
                continue
            counts["active_candidates"] += 1
        return counts

    def update_validity(
        self,
        learning_id: int,
        *,
        validity_type: str,
        event_name: str | None = None,
        valid_from: object = None,
        valid_until: object = None,
        validity_active: bool = True,
        validity_note: str | None = None,
        condition: dict[str, Any] | None = None,
        expected_updated_at: str | None = None,
    ) -> dict[str, Any]:
        """Update only the validity axis without changing Learning authority."""

        values = normalize_validity_update(
            validity_type=validity_type,
            event_name=event_name,
            valid_from=valid_from,
            valid_until=valid_until,
            validity_active=validity_active,
            validity_note=validity_note,
            condition=condition,
        )
        expired_at = None if values["validity_active"] else datetime.now(UTC).isoformat(
            timespec="milliseconds"
        )
        clauses = ["id=?"]
        parameters: list[Any] = [
            values["validity_type"],
            values["event_name"],
            values["valid_from"],
            values["valid_until"],
            int(values["validity_active"]),
            expired_at,
            values["validity_note"],
            serialize_json(values["condition_json"]),
            int(learning_id),
        ]
        if expected_updated_at is not None:
            clauses.append("updated_at=?")
            parameters.append(str(expected_updated_at))
        with self.database.transaction() as connection:
            cursor = connection.execute(
                f"""
                UPDATE learning_examples
                SET validity_type=?, event_name=?, valid_from=?, valid_until=?,
                    validity_active=?, expired_at=?, validity_note=?,
                    condition_json=?,
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE {' AND '.join(clauses)}
                """,
                tuple(parameters),
            )
            if cursor.rowcount != 1:
                exists = connection.execute(
                    "SELECT 1 FROM learning_examples WHERE id=?",
                    (int(learning_id),),
                ).fetchone()
                if exists is None:
                    raise LookupError(f"Learning not found: {learning_id}")
                raise RuntimeError(
                    "Learning 유효성 정보가 다른 사용자에 의해 변경되었습니다. "
                    "목록을 새로고침한 뒤 다시 시도해 주세요."
                )
            row = connection.execute(
                "SELECT * FROM learning_examples WHERE id=?",
                (int(learning_id),),
            ).fetchone()
        result = self._row(row)
        assert result is not None
        return result

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
        now = datetime.now(UTC).isoformat(timespec="milliseconds")
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
                  AND validity_active=1
                  AND (
                      validity_type='PERMANENT'
                      OR (
                          validity_type='TEMPORARY'
                          AND julianday(valid_from)<=julianday(?)
                          AND julianday(valid_until)>=julianday(?)
                      )
                  )
                  AND COALESCE(
                      json_extract(metadata_json, '$.learning_signal_type'),
                      'POSITIVE'
                  )='POSITIVE'
                """
            , (now, now)).fetchone()[0])
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
                SELECT le.id, le.source_key, le.inquiry_id,
                       le.answer_draft_id, le.approval_history_id,
                       le.question_original_masked, le.gpt_draft, le.seller_answer,
                       le.edited_answer, le.final_answer, le.learning_source,
                       le.inquiry_type, le.product_name, le.validator_result,
                       le.validity_type, le.event_name, le.valid_from,
                       le.valid_until, le.validity_active, le.expired_at,
                       le.validity_note, le.condition_json,
                       le.rating, le.quality_score, le.usage_count,
                       le.last_used_at, le.active, le.metadata_json,
                       le.created_at, le.updated_at,
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
                           le.created_at,
                           le.updated_at
                       ) AS inquiry_occurred_at
                FROM learning_examples AS le
                LEFT JOIN inquiries AS i ON i.id=le.inquiry_id
                ORDER BY COALESCE(
                             i.source_created_at,
                             i.registered_at,
                             le.created_at
                         ) DESC,
                         le.id DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 2000)),),
            ).fetchall()
        results = [dict(row) for row in rows]
        states = InquiryRepository(self.database).learning_states([
            int(row["inquiry_id"])
            for row in results if row.get("inquiry_id") is not None
        ])
        for row in results:
            row["active"] = bool(row["active"])
            row["validity_active"] = bool(row["validity_active"])
            row["metadata_json"] = deserialize_json(row.get("metadata_json"))
            row["condition_json"] = deserialize_json(row.get("condition_json"))
            metadata = row["metadata_json"]
            row["provenance"] = metadata.get("answer_provenance")
            row["human_verified"] = bool(metadata.get("human_verified"))
            row["signal_type"] = metadata.get(
                "learning_signal_type", "POSITIVE"
            )
            row["validity_status"] = validity_status(row)
            state = states.get(int(row["inquiry_id"])) if row.get("inquiry_id") else None
            row["effective_learning_status"] = (
                (state or {}).get("learning_status") or "NONE"
            )
            row["effective_learning_tooltip"] = (
                (state or {}).get("learning_tooltip") or "Learning 이력 없음"
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
