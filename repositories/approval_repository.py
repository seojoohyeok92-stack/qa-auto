from __future__ import annotations

from typing import Any

from answer.exceptions import AnswerAlreadyPostedError
from repositories.database import Database
from repositories.inquiry_repository import utc_now


class ApprovalRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _row(row: Any) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def get_inquiry_approval(self, inquiry_id: int) -> dict[str, Any]:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT id, approval_status, approved_at, approved_by,
                       workflow_status, post_status
                FROM inquiries WHERE id = ?
                """,
                (inquiry_id,),
            ).fetchone()
        result = self._row(row)
        if result is None:
            raise LookupError(f"Inquiry not found: {inquiry_id}")
        return result

    def history_for_inquiry(
        self, inquiry_id: int, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM approval_history
                WHERE inquiry_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (inquiry_id, max(1, min(int(limit), 1_000))),
            ).fetchall()
        return [dict(row) for row in rows]

    def dashboard_states(self) -> list[dict[str, Any]]:
        """KPI와 문의 목록에 필요한 최소 상태만 한 번에 조회합니다."""

        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT i.id, i.store_code, i.source_type,
                       i.source_question_id, i.workflow_status,
                       i.answer_status, i.post_status, i.approval_status,
                       i.approved_at, i.approved_by,
                       EXISTS (
                           SELECT 1 FROM answer_drafts d
                           WHERE d.inquiry_id = i.id
                       ) AS has_draft,
                       (
                           SELECT d.review_status FROM answer_drafts d
                           WHERE d.inquiry_id = i.id
                           ORDER BY d.created_at DESC, d.id DESC LIMIT 1
                       ) AS latest_review_status
                FROM inquiries i
                """
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            result = dict(row)
            result["has_draft"] = bool(result["has_draft"])
            results.append(result)
        return results

    def record_action(
        self,
        *,
        inquiry_id: int,
        answer_draft_id: int | None,
        action: str,
        actor: str,
        reason: str | None,
        previous_status: str | None,
        new_status: str,
    ) -> dict[str, Any]:
        normalized_action = str(action).strip().upper()
        allowed = {
            "EDIT_SAVED",
            "APPROVED",
            "APPROVAL_CANCELLED",
            "RESET",
        }
        if normalized_action not in allowed:
            raise ValueError(f"Invalid approval action: {action!r}")
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO approval_history (
                    inquiry_id, answer_draft_id, action, actor, reason,
                    previous_status, new_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    inquiry_id,
                    answer_draft_id,
                    normalized_action,
                    str(actor).strip() or "관리자",
                    str(reason).strip()[:1_000] if reason else None,
                    previous_status,
                    new_status,
                ),
            )
            row_id = int(cursor.lastrowid)
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM approval_history WHERE id = ?",
                (row_id,),
            ).fetchone()
        result = self._row(row)
        assert result is not None
        return result

    def approve(
        self,
        *,
        inquiry_id: int,
        draft_id: int,
        final_answer: str,
        actor: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        clean_answer = str(final_answer or "").strip()
        if not clean_answer:
            raise ValueError("Final answer must not be empty.")
        now = utc_now()
        clean_actor = str(actor or "").strip() or "관리자"
        with self.database.transaction() as connection:
            inquiry = connection.execute(
                """
                SELECT post_status, approval_status
                FROM inquiries WHERE id = ?
                """,
                (inquiry_id,),
            ).fetchone()
            draft = connection.execute(
                """
                SELECT inquiry_id, posted, review_status
                FROM answer_drafts WHERE id = ?
                """,
                (draft_id,),
            ).fetchone()
            if inquiry is None:
                raise LookupError(f"Inquiry not found: {inquiry_id}")
            if draft is None or int(draft["inquiry_id"]) != inquiry_id:
                raise LookupError(f"Answer draft not found: {draft_id}")
            if str(inquiry["post_status"]).upper() == "POSTED" or bool(
                draft["posted"]
            ):
                raise AnswerAlreadyPostedError(
                    "등록 완료된 문의는 승인 상태를 변경할 수 없습니다."
                )
            previous = str(inquiry["approval_status"] or "PENDING")
            connection.execute(
                """
                UPDATE answer_drafts
                SET final_answer = ?, review_status = 'APPROVED',
                    updated_at = ?
                WHERE id = ?
                """,
                (clean_answer, now, draft_id),
            )
            connection.execute(
                """
                UPDATE inquiries
                SET approval_status = 'APPROVED', approved_at = ?,
                    approved_by = ?, workflow_status = 'READY_TO_POST',
                    updated_at = ?
                WHERE id = ?
                """,
                (now, clean_actor, now, inquiry_id),
            )
            cursor = connection.execute(
                """
                INSERT INTO approval_history (
                    inquiry_id, answer_draft_id, action, actor,
                    previous_status, new_status
                ) VALUES (?, ?, 'APPROVED', ?, ?, 'APPROVED')
                """,
                (inquiry_id, draft_id, clean_actor, previous),
            )
            history_id = int(cursor.lastrowid)
        return self._reload_draft(draft_id), self._reload_history(history_id)

    def cancel_approval(
        self,
        *,
        inquiry_id: int,
        draft_id: int,
        actor: str,
        reason: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        clean_actor = str(actor or "").strip() or "관리자"
        clean_reason = str(reason or "").strip()
        if not clean_reason:
            raise ValueError("승인 취소 사유를 입력해 주세요.")
        now = utc_now()
        with self.database.transaction() as connection:
            inquiry = connection.execute(
                """
                SELECT post_status, approval_status
                FROM inquiries WHERE id = ?
                """,
                (inquiry_id,),
            ).fetchone()
            draft = connection.execute(
                """
                SELECT inquiry_id, posted, review_status
                FROM answer_drafts WHERE id = ?
                """,
                (draft_id,),
            ).fetchone()
            if inquiry is None:
                raise LookupError(f"Inquiry not found: {inquiry_id}")
            if draft is None or int(draft["inquiry_id"]) != inquiry_id:
                raise LookupError(f"Answer draft not found: {draft_id}")
            if str(inquiry["post_status"]).upper() == "POSTED" or bool(
                draft["posted"]
            ):
                raise AnswerAlreadyPostedError(
                    "등록 완료된 문의는 승인을 취소할 수 없습니다."
                )
            if str(inquiry["approval_status"]) != "APPROVED":
                raise ValueError("승인 완료 상태에서만 승인 취소가 가능합니다.")
            connection.execute(
                """
                UPDATE answer_drafts
                SET final_answer = NULL, review_status = 'IN_REVIEW',
                    updated_at = ?
                WHERE id = ?
                """,
                (now, draft_id),
            )
            connection.execute(
                """
                UPDATE inquiries
                SET approval_status = 'PENDING', approved_at = NULL,
                    approved_by = NULL, workflow_status = 'REVIEW_PENDING',
                    updated_at = ?
                WHERE id = ?
                """,
                (now, inquiry_id),
            )
            cursor = connection.execute(
                """
                INSERT INTO approval_history (
                    inquiry_id, answer_draft_id, action, actor, reason,
                    previous_status, new_status
                ) VALUES (
                    ?, ?, 'APPROVAL_CANCELLED', ?, ?,
                    'APPROVED', 'PENDING'
                )
                """,
                (
                    inquiry_id,
                    draft_id,
                    clean_actor,
                    clean_reason[:1_000],
                ),
            )
            history_id = int(cursor.lastrowid)
        return self._reload_draft(draft_id), self._reload_history(history_id)

    def _reload_draft(self, draft_id: int) -> dict[str, Any]:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM answer_drafts WHERE id = ?",
                (draft_id,),
            ).fetchone()
        result = self._row(row)
        if result is None:
            raise LookupError(f"Answer draft not found: {draft_id}")
        result["posted"] = bool(result["posted"])
        return result

    def _reload_history(self, history_id: int) -> dict[str, Any]:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM approval_history WHERE id = ?",
                (history_id,),
            ).fetchone()
        result = self._row(row)
        if result is None:
            raise LookupError(f"Approval history not found: {history_id}")
        return result
