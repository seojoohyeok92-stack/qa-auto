from __future__ import annotations

from typing import Any

from answer.answer_format import format_final_answer
from answer.exceptions import AnswerAlreadyPostedError
from answer.models import AnswerResult, AnswerStatus
from repositories.database import Database
from repositories.inquiry_repository import (
    deserialize_json,
    serialize_json,
    utc_now,
)


class AnswerRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _row_to_dict(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["posted"] = bool(result["posted"])
        for field in (
            "metadata_json",
            "inquiry_analysis_json",
            "selected_facts_json",
            "validator_result_json",
        ):
            if field in result:
                result[field] = deserialize_json(result[field])
        return result

    def _assert_inquiry_accepts_drafts(
        self,
        connection: Any,
        inquiry_id: int,
    ) -> None:
        inquiry = connection.execute(
            "SELECT post_status FROM inquiries WHERE id = ?",
            (inquiry_id,),
        ).fetchone()
        if inquiry is None:
            raise LookupError(f"Inquiry not found: {inquiry_id}")
        posted_draft = connection.execute(
            """
            SELECT 1 FROM answer_drafts
            WHERE inquiry_id = ? AND posted = 1
            LIMIT 1
            """,
            (inquiry_id,),
        ).fetchone()
        if str(inquiry["post_status"]).upper() == "POSTED" or posted_draft:
            raise AnswerAlreadyPostedError(
                "이미 등록된 문의는 답변 초안을 다시 생성할 수 없습니다."
            )

    def create_program_draft(
        self,
        inquiry_id: int,
        result: AnswerResult,
        *,
        order_id: str | None = None,
        dps_lookup_id: int | None = None,
        prompt_version: str | None = None,
        facts_version: str = "installation-date-v1",
    ) -> dict[str, Any]:
        wrapped_answer = format_final_answer(result.answer)
        if not wrapped_answer:
            raise ValueError("Draft answer must not be empty.")
        review_status = (
            "PENDING"
            if result.status is AnswerStatus.GENERATED
            else "NEEDS_REVIEW"
        )
        metadata = dict(result.metadata)
        hybrid = (
            metadata.get("hybrid")
            if isinstance(metadata.get("hybrid"), dict)
            else {}
        )
        fallback = bool(hybrid.get("fallback_used"))
        answer_source = str(metadata.get("answer_source") or "").lower()
        source = (
            "GPT"
            if answer_source == "openai"
            or (
                not answer_source
                and not fallback
                and str(result.provider or "").lower()
                not in {
                    "rules",
                    "rule",
                    "rule_provider",
                    "phase9_policy",
                }
            )
            else "RULE"
        )
        validation = (
            hybrid.get("validation")
            if isinstance(hybrid.get("validation"), dict)
            else {}
        )
        validation_status = (
            str(validation.get("status") or "PASSED")
            if validation.get("passed") and not fallback
            else str(validation.get("status") or "FAILED")
            if validation
            else "NOT_RUN"
        )
        phase9 = (
            metadata.get("phase9")
            if isinstance(metadata.get("phase9"), dict)
            else {}
        )
        analysis = (
            phase9.get("analysis")
            if isinstance(phase9.get("analysis"), dict)
            else {}
        )
        selected_facts = (
            phase9.get("selected_facts")
            if isinstance(phase9.get("selected_facts"), dict)
            else {}
        )
        answer_strategy = analysis.get("answer_strategy")
        now = utc_now()
        with self.database.transaction() as connection:
            self._assert_inquiry_accepts_drafts(connection, inquiry_id)
            current = connection.execute(
                """
                SELECT id, edited_answer, final_answer, review_status,
                       source
                FROM answer_drafts
                WHERE inquiry_id = ? AND is_active = 1
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (inquiry_id,),
            ).fetchone()
            protected = bool(
                current
                and (
                    str(current["final_answer"] or "").strip()
                    or str(current["review_status"] or "").upper()
                    == "APPROVED"
                )
            )
            # Every regeneration is a new immutable history row. Staff edits
            # remain on the previous row, while only approved/final answers
            # keep their active identity and approval state protected.
            activate = not protected
            if activate:
                connection.execute(
                    """
                    UPDATE answer_drafts SET is_active = 0
                    WHERE inquiry_id = ?
                    """,
                    (inquiry_id,),
                )
            cursor = connection.execute(
                """
                INSERT INTO answer_drafts (
                    inquiry_id, program_status, category, reason, provider,
                    original_answer, edited_answer, final_answer,
                    review_status, posted, posted_at, created_at, updated_at,
                    metadata_json, order_id, source, validation_status,
                    dps_lookup_id, prompt_version, facts_version, is_active,
                    stale, stale_reason, answer_strategy,
                    inquiry_analysis_json, selected_facts_json,
                    validator_result_json
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, NULL, NULL, ?, 0, NULL, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, ?, ?
                )
                """,
                (
                    inquiry_id,
                    result.status.value,
                    result.category,
                    result.reason,
                    result.provider,
                    wrapped_answer,
                    review_status,
                    now,
                    now,
                    serialize_json(metadata),
                    str(order_id) if order_id else None,
                    source,
                    validation_status,
                    dps_lookup_id,
                    prompt_version,
                    facts_version,
                    1 if activate else 0,
                    answer_strategy,
                    serialize_json(analysis),
                    serialize_json(selected_facts),
                    serialize_json(validation),
                ),
            )
            draft_id = int(cursor.lastrowid)
        try:
            from repositories.learning_provenance_repository import (
                LearningProvenanceRepository,
            )
            LearningProvenanceRepository(self.database).attach_latest_context(
                inquiry_id=int(inquiry_id), draft_id=draft_id
            )
        except Exception:
            # Observability is optional and must never block answer creation.
            pass
        draft = self.get(draft_id)
        if draft is None:  # pragma: no cover - defensive
            raise RuntimeError("Created answer draft could not be reloaded.")
        return draft

    def get(self, draft_id: int) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM answer_drafts WHERE id = ?",
                (draft_id,),
            ).fetchone()
        return self._row_to_dict(row)

    def latest_for_inquiry(
        self,
        inquiry_id: int,
    ) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM answer_drafts
                WHERE inquiry_id = ?
                ORDER BY is_active DESC, created_at DESC, id DESC
                LIMIT 1
                """,
                (inquiry_id,),
            ).fetchone()
        return self._row_to_dict(row)

    def active_for_inquiry(
        self,
        inquiry_id: int,
    ) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM answer_drafts
                WHERE inquiry_id = ? AND is_active = 1
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (inquiry_id,),
            ).fetchone()
        return self._row_to_dict(row)

    def mark_unposted_drafts_stale(
        self,
        inquiry_id: int,
        *,
        reason: str,
    ) -> int:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE answer_drafts
                SET stale = 1, stale_reason = ?, updated_at = ?
                WHERE inquiry_id = ? AND posted = 0
                """,
                (str(reason)[:200], utc_now(), int(inquiry_id)),
            )
        return int(cursor.rowcount)

    def activate_draft(
        self,
        inquiry_id: int,
        draft_id: int,
    ) -> dict[str, Any]:
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT inquiry_id, posted FROM answer_drafts WHERE id = ?
                """,
                (int(draft_id),),
            ).fetchone()
            if row is None or int(row["inquiry_id"]) != int(inquiry_id):
                raise LookupError(f"Answer draft not found: {draft_id}")
            if bool(row["posted"]):
                raise AnswerAlreadyPostedError(
                    "Posted drafts cannot be reactivated."
                )
            connection.execute(
                "UPDATE answer_drafts SET is_active = 0 WHERE inquiry_id = ?",
                (int(inquiry_id),),
            )
            connection.execute(
                """
                UPDATE answer_drafts
                SET is_active = 1, stale = 0, stale_reason = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (utc_now(), int(draft_id)),
            )
        value = self.get(draft_id)
        if value is None:  # pragma: no cover
            raise RuntimeError("Activated draft could not be reloaded.")
        return value

    def history_for_inquiry(
        self,
        inquiry_id: int,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM answer_drafts
                WHERE inquiry_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (inquiry_id, max(1, int(limit))),
            ).fetchall()
        return [
            self._row_to_dict(row)
            for row in rows
            if row is not None
        ]

    def is_inquiry_posted(self, inquiry_id: int) -> bool:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT
                    i.post_status,
                    EXISTS(
                        SELECT 1 FROM answer_drafts a
                        WHERE a.inquiry_id = i.id AND a.posted = 1
                    ) AS has_posted_draft
                FROM inquiries i
                WHERE i.id = ?
                """,
                (inquiry_id,),
            ).fetchone()
        if row is None:
            raise LookupError(f"Inquiry not found: {inquiry_id}")
        return (
            str(row["post_status"]).upper() == "POSTED"
            or bool(row["has_posted_draft"])
        )

    def _update_unposted(
        self,
        draft_id: int,
        field_name: str,
        value: str | None,
    ) -> dict[str, Any]:
        allowed_fields = {
            "edited_answer",
            "final_answer",
            "review_status",
        }
        if field_name not in allowed_fields:
            raise ValueError(f"Unsupported draft field: {field_name}")
        stored_value = value
        if field_name in {"edited_answer", "final_answer"} and value is not None:
            stored_value = format_final_answer(value)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT posted FROM answer_drafts WHERE id = ?",
                (draft_id,),
            ).fetchone()
            if row is None:
                raise LookupError(f"Answer draft not found: {draft_id}")
            if bool(row["posted"]):
                raise AnswerAlreadyPostedError(
                    "등록된 답변 초안은 변경할 수 없습니다."
                )
            connection.execute(
                f"""
                UPDATE answer_drafts
                SET {field_name} = ?, updated_at = ?
                WHERE id = ?
                """,
                (stored_value, utc_now(), draft_id),
            )
        updated = self.get(draft_id)
        if updated is None:  # pragma: no cover - defensive
            raise RuntimeError("Updated answer draft could not be reloaded.")
        return updated

    def save_edited_answer(
        self,
        draft_id: int,
        edited_answer: str | None,
    ) -> dict[str, Any]:
        return self._update_unposted(
            draft_id,
            "edited_answer",
            edited_answer,
        )

    def save_final_answer(
        self,
        draft_id: int,
        final_answer: str | None,
    ) -> dict[str, Any]:
        return self._update_unposted(
            draft_id,
            "final_answer",
            final_answer,
        )

    def update_review_status(
        self,
        draft_id: int,
        review_status: str,
    ) -> dict[str, Any]:
        normalized = str(review_status or "").strip().upper()
        if normalized not in {
            "PENDING",
            "NEEDS_REVIEW",
            "IN_REVIEW",
            "APPROVED",
            "REJECTED",
        }:
            raise ValueError(f"Invalid review status: {review_status!r}")
        return self._update_unposted(
            draft_id,
            "review_status",
            normalized,
        )
