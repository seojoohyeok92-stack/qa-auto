from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from answer.exceptions import AnswerAlreadyPostedError, StaleAnswerStateError
from repositories.answer_repository import AnswerRepository
from repositories.approval_repository import ApprovalRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from repositories.learning_feedback_repository import LearningFeedbackRepository
from repositories.learning_repository import LearningRepository
from repositories.workflow_repository import WorkflowRepository
from services.quality_metrics_service import QualityMetricsService
from services.learning_service import LearningService
from services.learning_feedback_service import LearningFeedbackService
from answer.answer_format import format_final_answer
from answer.answer_provenance import AnswerProvenance
from workflow.models import InquiryStatus, StepCode, StepStatus


class ApprovalError(RuntimeError):
    pass


class ApprovalLockedError(ApprovalError):
    pass


@dataclass(frozen=True)
class ApprovalOutcome:
    draft: dict[str, Any]
    history: dict[str, Any]
    learning: dict[str, Any] | None = None


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
        self,
        inquiry_id: int,
        draft_id: int,
        *,
        allow_source_answered_internal_edit: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        draft = self.answers.get(draft_id)
        if draft is None or int(draft["inquiry_id"]) != inquiry_id:
            raise LookupError(f"Answer draft not found: {draft_id}")
        state = self.approvals.get_inquiry_approval(inquiry_id)
        inquiry = self.inquiries.get(inquiry_id) or {}
        internal_posted_correction = bool(
            allow_source_answered_internal_edit
            and inquiry.get("source_answered")
        )
        if self.answers.is_inquiry_posted(inquiry_id) and not internal_posted_correction:
            raise AnswerAlreadyPostedError(
                "등록 완료된 문의는 답변을 수정할 수 없습니다."
            )
        if (
            state["approval_status"] == "APPROVED"
            and not internal_posted_correction
        ):
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
        correction_reason: str = "",
        correction_note: str = "",
        corrected_intent: str = "",
        correction_signal_kind: str = "",
        correction_signal_content: str = "",
        correction_fact_scope: str | None = None,
        expected_updated_at: str | None = None,
    ) -> dict[str, Any]:
        draft, state = self._assert_editable(
            inquiry_id,
            draft_id,
            allow_source_answered_internal_edit=True,
        )
        if (
            expected_updated_at is not None
            and str(draft.get("updated_at")) != str(expected_updated_at)
        ):
            raise StaleAnswerStateError()
        inquiry = self.inquiries.get(inquiry_id) or {}
        internal_posted_correction = bool(inquiry.get("source_answered"))
        clean = format_final_answer(str(edited_answer or ""))
        current = str(draft.get("edited_answer") or "")
        if clean == current:
            if correction_reason and not autosave:
                LearningFeedbackService(
                    self.database
                ).capture_staff_correction(
                    inquiry_id=inquiry_id,
                    draft_id=draft_id,
                    correction_reason=correction_reason,
                    correction_note=correction_note,
                    corrected_intent=corrected_intent,
                    actor=actor,
                    signal_kind=correction_signal_kind,
                    signal_content=correction_signal_content,
                    fact_scope=correction_fact_scope,
                )
            return draft
        updated = self.answers.save_edited_answer(
            draft_id,
            clean,
            expected_updated_at=expected_updated_at,
        )
        if not internal_posted_correction:
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
            new_status=(
                state["approval_status"]
                if internal_posted_correction
                else "PENDING"
            ),
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
                "status": (
                    "INTERNAL_POSTED_CORRECTION"
                    if internal_posted_correction
                    else "IN_REVIEW"
                ),
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
        if correction_reason and not autosave:
            LearningFeedbackService(self.database).capture_staff_correction(
                inquiry_id=inquiry_id,
                draft_id=draft_id,
                correction_reason=correction_reason,
                correction_note=correction_note,
                corrected_intent=corrected_intent,
                actor=actor,
                signal_kind=correction_signal_kind,
                signal_content=correction_signal_content,
                fact_scope=correction_fact_scope,
            )
        return self.answers.get(draft_id) or updated

    def approve_posted_answer(
        self,
        *,
        inquiry_id: int,
        actor: str = "관리자",
        positive_reason: str = "",
        positive_note: str = "",
        positive_signal_kind: str = "",
        positive_signal_content: str = "",
        positive_fact_scope: str | None = None,
    ) -> dict[str, Any]:
        """Human-verify the Naver answer for Learning without reposting it."""

        inquiry = self.inquiries.get(int(inquiry_id))
        if inquiry is None:
            raise LookupError(f"Inquiry not found: {inquiry_id}")
        if not inquiry.get("source_answered"):
            raise ApprovalError("네이버 답변완료 문의가 아닙니다.")
        saved = LearningService(
            self.database
        ).capture_verified_posted_answer(
            inquiry_id=int(inquiry_id),
            actor=actor,
            positive_reason=positive_reason,
            positive_note=positive_note,
            signal_kind=positive_signal_kind,
            signal_content=positive_signal_content,
            fact_scope=positive_fact_scope,
        )
        if saved is None:
            raise ApprovalError("검증할 네이버 실제 등록 답변이 없습니다.")
        self.logs.record_inquiry(
            int(inquiry_id),
            "NAVER_POSTED_ANSWER_APPROVED_FOR_LEARNING",
            "네이버 실제 등록 답변을 직원 검증 Positive Learning으로 승인했습니다.",
            details={
                "actor": actor,
                "learning_example_id": int(saved["id"]),
                "answer_provenance": "NAVER_POSTED",
                "network_call_count": 0,
            },
        )
        return saved

    def approve_posted_staff_correction(
        self,
        *,
        inquiry_id: int,
        draft_id: int,
        edited_answer: str,
        actor: str = "직원",
        correction_reason: str = "",
        correction_note: str = "",
        corrected_intent: str = "",
        correction_signal_kind: str = "",
        correction_signal_content: str = "",
        correction_fact_scope: str | None = None,
        positive_reason: str = "",
        positive_note: str = "",
        positive_signal_kind: str = "",
        positive_signal_content: str = "",
        positive_fact_scope: str | None = None,
        expected_updated_at: str | None = None,
    ) -> dict[str, Any]:
        """Approve an internal correction without changing the Naver answer."""

        learning = LearningService(self.database)
        learning.assert_positive_allowed(
            inquiry_id=inquiry_id,
            answer_provenance=AnswerProvenance.STAFF_EDITED,
            answer_reference_id=draft_id,
            answer=edited_answer,
        )
        self.save_edited_answer(
            inquiry_id=inquiry_id,
            draft_id=draft_id,
            edited_answer=edited_answer,
            actor=actor,
            correction_reason=correction_reason,
            correction_note=correction_note,
            corrected_intent=corrected_intent,
            correction_signal_kind=correction_signal_kind,
            correction_signal_content=correction_signal_content,
            correction_fact_scope=correction_fact_scope,
            expected_updated_at=expected_updated_at,
        )
        saved = learning.capture_posted_staff_correction(
            inquiry_id=inquiry_id,
            draft_id=draft_id,
            human_verified=True,
            actor=actor,
            positive_reason=positive_reason,
            positive_note=positive_note,
            signal_kind=positive_signal_kind,
            signal_content=positive_signal_content,
            fact_scope=positive_fact_scope,
        )
        if saved is None:
            raise ApprovalError("학습할 직원 수정 답변이 없습니다.")
        self.logs.record_inquiry(
            inquiry_id,
            "NAVER_POSTED_INTERNAL_CORRECTION_APPROVED",
            "네이버 실제 등록 답변의 내부 수정본을 Positive Learning으로 승인했습니다.",
            details={
                "actor": actor,
                "learning_example_id": int(saved["id"]),
                "answer_provenance": "STAFF_EDITED",
                "network_call_count": 0,
            },
        )
        return saved

    def reset_edited_answer(
        self,
        *,
        inquiry_id: int,
        draft_id: int,
        actor: str = "관리자",
    ) -> dict[str, Any]:
        draft, state = self._assert_editable(
            inquiry_id,
            draft_id,
            allow_source_answered_internal_edit=True,
        )
        inquiry = self.inquiries.get(inquiry_id) or {}
        internal_posted_correction = bool(inquiry.get("source_answered"))
        self.answers.save_edited_answer(draft_id, None)
        updated = (
            self.answers.get(draft_id) or draft
            if internal_posted_correction
            else self.answers.update_review_status(draft_id, "PENDING")
        )
        history = self.approvals.record_action(
            inquiry_id=inquiry_id,
            answer_draft_id=draft_id,
            action="RESET",
            actor=actor,
            reason="직원 수정본 초기화",
            previous_status=state["approval_status"],
            new_status=(
                state["approval_status"]
                if internal_posted_correction
                else "PENDING"
            ),
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
        LearningFeedbackRepository(self.database).deactivate_for_draft(draft_id)
        return updated

    def approve(
        self,
        *,
        inquiry_id: int,
        draft_id: int,
        actor: str = "관리자",
        correction_reason: str = "",
        correction_note: str = "",
        corrected_intent: str = "",
        correction_signal_kind: str = "",
        correction_signal_content: str = "",
        correction_fact_scope: str | None = None,
        positive_reason: str = "",
        positive_note: str = "",
        positive_signal_kind: str = "",
        positive_signal_content: str = "",
        positive_fact_scope: str | None = None,
        expected_updated_at: str | None = None,
    ) -> ApprovalOutcome:
        draft, _ = self._assert_editable(inquiry_id, draft_id)
        final_answer = format_final_answer(
            str(draft.get("edited_answer") or draft.get("original_answer") or "")
        )
        learning = LearningService(self.database)
        learning.assert_positive_allowed(
            inquiry_id=inquiry_id,
            answer_provenance=(
                AnswerProvenance.STAFF_EDITED
                if str(draft.get("edited_answer") or "").strip()
                else AnswerProvenance.PROGRAM_GENERATED
            ),
            answer_reference_id=draft_id,
            answer=final_answer,
        )
        self._start_review(inquiry_id)
        updated, history = self.approvals.approve(
            inquiry_id=inquiry_id,
            draft_id=draft_id,
            final_answer=final_answer,
            actor=actor,
            expected_draft_updated_at=expected_updated_at,
            evaluated_answer_sources=(
                (
                    AnswerProvenance.STAFF_EDITED.value
                    if str(draft.get("edited_answer") or "").strip()
                    else AnswerProvenance.PROGRAM_GENERATED.value
                ),
                AnswerProvenance.FINAL_ANSWER.value,
            ),
            evaluated_answer_masked=learning.privacy.mask(final_answer),
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
            learning.capture_approved(
                inquiry_id=inquiry_id,
                draft_id=draft_id,
                history_id=int(history["id"]),
                positive_reason=positive_reason,
                positive_note=positive_note,
                signal_kind=positive_signal_kind,
                signal_content=positive_signal_content,
                fact_scope=positive_fact_scope,
            )
            if correction_reason:
                LearningFeedbackService(
                    self.database
                ).capture_staff_correction(
                    inquiry_id=inquiry_id,
                    draft_id=draft_id,
                    correction_reason=correction_reason,
                    correction_note=correction_note,
                    corrected_intent=corrected_intent,
                    actor=actor,
                    signal_kind=correction_signal_kind,
                    signal_content=correction_signal_content,
                    fact_scope=correction_fact_scope,
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
        draft_id: int | None,
        reason: str,
        actor: str = "관리자",
    ) -> ApprovalOutcome:
        return self.cancel_approval_with_learning(
            inquiry_id=inquiry_id,
            draft_id=draft_id,
            reason=reason,
            actor=actor,
        )

    def cancel_approval_with_learning(
        self,
        *,
        inquiry_id: int,
        draft_id: int | None,
        reason: str,
        actor: str = "관리자",
        learning_id: int | None = None,
        expected_updated_at: str | None = None,
    ) -> ApprovalOutcome:
        clean_reason = str(reason or "").strip()
        if not clean_reason:
            raise ValueError("승인 취소 사유를 입력해 주세요.")
        repository = LearningRepository(self.database)
        target = repository.get(int(learning_id)) if learning_id is not None else None
        if learning_id is not None and target is None:
            raise LookupError(f"Learning not found: {learning_id}")
        if learning_id is None:
            target = next(
                (
                    row
                    for row in repository.for_inquiry(inquiry_id)
                    if row.get("active")
                    and bool((row.get("metadata_json") or {}).get("human_verified"))
                    and (
                        draft_id is None
                        or row.get("answer_draft_id") in {None, int(draft_id)}
                    )
                ),
                None,
            )
        if target is not None and int(target["inquiry_id"]) != int(inquiry_id):
            raise ValueError("승인 취소 Learning이 문의와 일치하지 않습니다.")
        if (
            target is not None
            and draft_id is not None
            and target.get("answer_draft_id") is not None
            and int(target["answer_draft_id"]) != int(draft_id)
        ):
            raise ValueError("승인 취소 Learning이 Answer Draft와 일치하지 않습니다.")
        if target is not None and (
            not target.get("active")
            or not bool((target.get("metadata_json") or {}).get("human_verified"))
        ):
            raise ValueError("활성 Human Verified Positive Learning만 취소할 수 있습니다.")

        state = self.approvals.get_inquiry_approval(inquiry_id)
        if str(state.get("approval_status") or "").upper() == "APPROVED":
            if draft_id is None:
                raise ApprovalError("승인 취소 대상 Draft가 없습니다.")
            updated, history = self.approvals.cancel_approval(
                inquiry_id=inquiry_id,
                draft_id=int(draft_id),
                actor=actor,
                reason=clean_reason,
                expected_draft_updated_at=expected_updated_at,
            )
        elif target is not None:
            history = self.approvals.record_action(
                inquiry_id=inquiry_id,
                answer_draft_id=int(draft_id) if draft_id is not None else None,
                action="APPROVAL_CANCELLED",
                actor=actor,
                reason=clean_reason,
                previous_status="APPROVED",
                new_status="PENDING",
            )
            updated = self.answers.get(int(draft_id)) if draft_id is not None else None
            updated = updated or {}
        else:
            raise ApprovalError("취소할 승인 또는 Human Verified Positive Learning이 없습니다.")

        revoked = None
        if target is not None:
            revoked = repository.revoke_human_verified(
                learning_id=int(target["id"]),
                inquiry_id=inquiry_id,
                reason=clean_reason,
                actor=actor,
                approval_history_id=int(history["id"]),
            )
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
                "learning_example_id": (revoked or {}).get("id"),
            },
        )
        return ApprovalOutcome(updated, history, revoked)
