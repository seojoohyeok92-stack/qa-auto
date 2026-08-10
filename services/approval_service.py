from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from answer.exceptions import AnswerAlreadyPostedError
from repositories.answer_repository import AnswerRepository
from repositories.approval_repository import ApprovalRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from repositories.workflow_repository import WorkflowRepository
from services.quality_metrics_service import QualityMetricsService
from services.learning_service import LearningService
from answer.answer_format import format_final_answer
from workflow.models import InquiryStatus, StepCode, StepStatus


class ApprovalError(RuntimeError):
    pass


class ApprovalLockedError(ApprovalError):
    pass


@dataclass(frozen=True)
class ApprovalOutcome:
    draft: dict[str, Any]
    history: dict[str, Any]


class ApprovalService:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.answers = AnswerRepository(database)
        self.approvals = ApprovalRepository(database)
        self.inquiries = InquiryRepository(database)
        self.workflows = WorkflowRepository(database)
        self.logs = LogRepository(database)
        self.quality = QualityMetricsService(database)

    def _store_quality_metric(
        self,
        *,
        inquiry_id: int,
        draft: dict[str, Any],
        edited_answer: str,
        actor: str,
        approved: bool,
    ) -> None:
        try:
            inquiry = self.inquiries.get(inquiry_id) or {}
            with self.database.connection() as connection:
                regeneration_count = max(
                    0,
                    int(
                        connection.execute(
                            "SELECT COUNT(*) FROM answer_drafts WHERE inquiry_id=?",
                            (inquiry_id,),
                        ).fetchone()[0]
                    )
                    - 1,
                )
            duration: int | None = None
            try:
                created = datetime.fromisoformat(
                    str(draft.get("created_at")).replace("Z", "+00:00")
                )
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
                duration = max(
                    0,
                    int((datetime.now(UTC) - created.astimezone(UTC)).total_seconds()),
                )
            except (TypeError, ValueError):
                pass
            self.quality.calculate_and_store(
                inquiry_id=inquiry_id,
                answer_draft_id=int(draft["id"]),
                actor=actor,
                category=str(inquiry.get("inquiry_type") or "") or None,
                program_answer=str(draft.get("original_answer") or ""),
                staff_answer=edited_answer,
                edit_duration_seconds=duration,
                approved=approved,
                regeneration_count=regeneration_count,
            )
        except Exception as error:
            self.logs.record_inquiry(
                inquiry_id,
                "QUALITY_METRIC_FAILED",
                "수정 지표 계산에 실패했지만 답변 데이터는 정상 보존되었습니다.",
                level="WARNING",
                details={
                    "component": "QualityMetrics",
                    "operation": "calculate",
                    "exception_type": error.__class__.__name__,
                },
            )

    def _assert_editable(
        self, inquiry_id: int, draft_id: int
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        draft = self.answers.get(draft_id)
        if draft is None or int(draft["inquiry_id"]) != inquiry_id:
            raise LookupError(f"Answer draft not found: {draft_id}")
        state = self.approvals.get_inquiry_approval(inquiry_id)
        if self.answers.is_inquiry_posted(inquiry_id):
            raise AnswerAlreadyPostedError(
                "등록 완료된 문의는 답변을 수정할 수 없습니다."
            )
        if state["approval_status"] == "APPROVED":
            raise ApprovalLockedError(
                "승인된 답변은 승인 취소 후 수정할 수 있습니다."
            )
        return draft, state

    def _start_review(self, inquiry_id: int) -> None:
        self.workflows.initialize_steps(inquiry_id)
        step = self.workflows.get_step(inquiry_id, StepCode.STAFF_REVIEW)
        status = StepStatus(step["step_status"])
        if status is StepStatus.PENDING:
            self.workflows.start_step(inquiry_id, StepCode.STAFF_REVIEW)
        elif status in {StepStatus.FAILED, StepStatus.NEEDS_REVIEW}:
            self.workflows.retry_step(inquiry_id, StepCode.STAFF_REVIEW)

    def save_edited_answer(
        self,
        *,
        inquiry_id: int,
        draft_id: int,
        edited_answer: str,
        actor: str = "관리자",
        autosave: bool = False,
    ) -> dict[str, Any]:
        draft, state = self._assert_editable(inquiry_id, draft_id)
        clean = format_final_answer(str(edited_answer or ""))
        current = str(draft.get("edited_answer") or "")
        if clean == current:
            return draft
        updated = self.answers.save_edited_answer(draft_id, clean)
        self.answers.update_review_status(draft_id, "IN_REVIEW")
        self.inquiries.update_status(inquiry_id, InquiryStatus.REVIEW_PENDING)
        self._start_review(inquiry_id)
        history = self.approvals.record_action(
            inquiry_id=inquiry_id,
            answer_draft_id=draft_id,
            action="EDIT_SAVED",
            actor=actor,
            reason="자동 저장" if autosave else "직원 수정 저장",
            previous_status=state["approval_status"],
            new_status="PENDING",
        )
        self.logs.record_inquiry(
            inquiry_id,
            "STAFF_EDIT_AUTOSAVED" if autosave else "STAFF_EDIT_SAVED",
            "직원 수정 답변을 자동 저장했습니다."
            if autosave
            else "직원 수정 답변을 저장했습니다.",
            details={
                "actor": actor,
                "action": history["action"],
                "status": "IN_REVIEW",
                "draft_id": draft_id,
            },
        )
        self._store_quality_metric(
            inquiry_id=inquiry_id,
            draft=draft,
            edited_answer=clean,
            actor=actor,
            approved=False,
        )
        return self.answers.get(draft_id) or updated

    def reset_edited_answer(
        self,
        *,
        inquiry_id: int,
        draft_id: int,
        actor: str = "관리자",
    ) -> dict[str, Any]:
        draft, state = self._assert_editable(inquiry_id, draft_id)
        self.answers.save_edited_answer(draft_id, None)
        updated = self.answers.update_review_status(draft_id, "PENDING")
        history = self.approvals.record_action(
            inquiry_id=inquiry_id,
            answer_draft_id=draft_id,
            action="RESET",
            actor=actor,
            reason="직원 수정본 초기화",
            previous_status=state["approval_status"],
            new_status="PENDING",
        )
        self.logs.record_inquiry(
            inquiry_id,
            "STAFF_EDIT_RESET",
            "직원 수정 답변을 프로그램 원본으로 초기화했습니다.",
            details={
                "actor": actor,
                "action": history["action"],
                "status": "PENDING",
                "draft_id": draft["id"],
            },
        )
        return updated

    def approve(
        self,
        *,
        inquiry_id: int,
        draft_id: int,
        actor: str = "관리자",
    ) -> ApprovalOutcome:
        draft, _ = self._assert_editable(inquiry_id, draft_id)
        self._start_review(inquiry_id)
        final_answer = format_final_answer(
            str(draft.get("edited_answer") or draft.get("original_answer") or "")
        )
        updated, history = self.approvals.approve(
            inquiry_id=inquiry_id,
            draft_id=draft_id,
            final_answer=final_answer,
            actor=actor,
        )
        self.workflows.initialize_steps(inquiry_id)
        step = self.workflows.get_step(inquiry_id, StepCode.STAFF_REVIEW)
        if StepStatus(step["step_status"]) is not StepStatus.COMPLETED:
            self.workflows.complete_step(
                inquiry_id,
                StepCode.STAFF_REVIEW,
                metadata={
                    "draft_id": draft_id,
                    "actor": actor,
                    "approval_status": "APPROVED",
                },
            )
        self.logs.record_inquiry(
            inquiry_id,
            "ANSWER_APPROVED",
            "직원 검토를 완료하고 최종 등록본을 승인했습니다.",
            details={
                "actor": actor,
                "action": "APPROVED",
                "status": "APPROVED",
                "draft_id": draft_id,
            },
        )
        self._store_quality_metric(
            inquiry_id=inquiry_id,
            draft=draft,
            edited_answer=final_answer,
            actor=actor,
            approved=True,
        )
        try:
            LearningService(self.database).capture_approved(
                inquiry_id=inquiry_id,
                draft_id=draft_id,
                history_id=int(history["id"]),
            )
        except Exception as error:
            # Learning is deliberately non-blocking: approval is already
            # committed and must never be rolled back by the optional layer.
            self.logs.record_inquiry(
                inquiry_id,
                "LEARNING_SAVE_FAILED",
                "승인 답변의 Learning 저장에 실패했지만 승인 결과는 유지됩니다.",
                level="WARNING",
                details={"exception_type": error.__class__.__name__},
            )
        return ApprovalOutcome(updated, history)

    def cancel_approval(
        self,
        *,
        inquiry_id: int,
        draft_id: int,
        reason: str,
        actor: str = "관리자",
    ) -> ApprovalOutcome:
        updated, history = self.approvals.cancel_approval(
            inquiry_id=inquiry_id,
            draft_id=draft_id,
            actor=actor,
            reason=reason,
        )
        try:
            LearningService(self.database).deactivate_draft(draft_id)
        except Exception:
            pass
        step = self.workflows.get_step(inquiry_id, StepCode.STAFF_REVIEW)
        if StepStatus(step["step_status"]) is StepStatus.COMPLETED:
            self.workflows.restart_completed_step(
                inquiry_id,
                StepCode.STAFF_REVIEW,
                metadata={
                    "draft_id": draft_id,
                    "actor": actor,
                    "approval_cancelled": True,
                },
            )
        self.logs.record_inquiry(
            inquiry_id,
            "ANSWER_APPROVAL_CANCELLED",
            "답변 승인을 취소했습니다.",
            level="WARNING",
            details={
                "actor": actor,
                "action": "APPROVAL_CANCELLED",
                "status": "PENDING",
                "reason": reason,
                "draft_id": draft_id,
            },
        )
        return ApprovalOutcome(updated, history)
