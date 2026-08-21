from __future__ import annotations

import hashlib
from typing import Any

from answer.learning_feedback import (
    CorrectionReason,
    ExclusionReason,
    FeedbackType,
    LearningSignalType,
    normalize_reason,
    normalize_exclusion_reason,
)
from answer.answer_provenance import AnswerProvenance
from answer.answer_format import format_final_answer
from answer.learning_conflict import LearningConflictError
from answer.learning_signal import OriginKind
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.historical_case_repository import HistoricalCaseRepository
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_feedback_repository import LearningFeedbackRepository
from repositories.learning_repository import LearningRepository
from repositories.naver_posted_answer_repository import (
    NaverPostedAnswerRepository,
)
from services.learning_privacy_service import LearningPrivacyService
from services.learning_signal_service import LearningSignalService


class LearningFeedbackService:
    """Store human correction signals separately from positive answer examples."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.answers = AnswerRepository(database)
        self.inquiries = InquiryRepository(database)
        self.historical = HistoricalCaseRepository(database)
        self.repository = LearningFeedbackRepository(database)
        self.privacy = LearningPrivacyService()
        self.signals = LearningSignalService(database)

    @staticmethod
    def _source_key(*parts: object) -> str:
        return hashlib.sha256(
            "|".join(str(part or "") for part in parts).encode("utf-8")
        ).hexdigest()

    def _capture_signal(
        self,
        *,
        origin_kind: OriginKind,
        feedback_id: int | None,
        inquiry: dict[str, Any],
        question: str,
        signal_kind: str,
        signal_content: str,
        fact_scope: str | None,
        actor: str,
        historical_case_id: int | None = None,
        program_answer: str = "",
        final_answer: str = "",
        operator_note: str = "",
    ) -> None:
        if str(signal_kind or "").strip() and str(signal_content or "").strip():
            # Operator explicitly classified this note -- honor it and skip
            # auto-extraction so this event never produces two signals.
            self.signals.capture(
                origin_kind=origin_kind,
                signal_kind=signal_kind,
                content_text=signal_content,
                inquiry=inquiry,
                learning_feedback_id=feedback_id,
                historical_case_id=historical_case_id,
                question=question,
                product_name=inquiry.get("product_name"),
                option_name=inquiry.get("option_name"),
                product_id=inquiry.get("product_id"),
                fact_scope=fact_scope,
                actor=actor,
            )
            return
        if not program_answer and not final_answer and not operator_note:
            return
        self.signals.auto_extract_and_capture(
            origin_kind=origin_kind,
            inquiry=inquiry,
            question=question,
            source_authority="NEGATIVE_REVIEW_STAFF_CORRECTED",
            program_answer=program_answer,
            final_answer=final_answer,
            operator_note=operator_note,
            learning_feedback_id=feedback_id,
            product_name=inquiry.get("product_name"),
            option_name=inquiry.get("option_name"),
            product_id=inquiry.get("product_id"),
            actor="SYSTEM_AUTO_EXTRACTION",
        )

    def _dashboard_answer(
        self,
        *,
        inquiry_id: int,
        original_answer_source: str | AnswerProvenance,
        original_answer_reference_id: int,
    ) -> tuple[dict[str, Any], AnswerProvenance, int, int | None, str]:
        inquiry = self.inquiries.get(int(inquiry_id))
        if inquiry is None:
            raise LookupError("문의를 찾을 수 없습니다.")
        provenance = AnswerProvenance(str(original_answer_source))
        reference_id = int(original_answer_reference_id)
        draft = self.answers.get(reference_id)
        posted = NaverPostedAnswerRepository(self.database).current(int(inquiry_id))
        if provenance is AnswerProvenance.PROGRAM_GENERATED:
            if draft is None or int(draft["inquiry_id"]) != int(inquiry_id):
                raise LookupError("평가할 Program Answer를 찾을 수 없습니다.")
            original, draft_id = str(draft.get("original_answer") or ""), reference_id
        elif provenance is AnswerProvenance.STAFF_EDITED:
            if draft is None or int(draft["inquiry_id"]) != int(inquiry_id):
                raise LookupError("평가할 직원 수정본을 찾을 수 없습니다.")
            original, draft_id = str(draft.get("edited_answer") or ""), reference_id
        elif provenance is AnswerProvenance.FINAL_ANSWER:
            if draft is None or int(draft["inquiry_id"]) != int(inquiry_id):
                raise LookupError("평가할 Final Answer를 찾을 수 없습니다.")
            original, draft_id = str(draft.get("final_answer") or ""), reference_id
        elif provenance is AnswerProvenance.NAVER_POSTED:
            if posted is None or int(posted["id"]) != reference_id:
                raise LookupError("평가할 네이버 실제 등록 답변을 찾을 수 없습니다.")
            original, draft_id = str(posted.get("answer_body") or ""), None
        else:
            raise ValueError("Dashboard에서 평가할 수 없는 답변 출처입니다.")
        original = format_final_answer(original)
        if not original:
            raise ValueError("평가할 답변 본문이 없습니다.")
        return inquiry, provenance, reference_id, draft_id, original

    def _signals(
        self, reason: CorrectionReason, *, excluded: bool = False
    ) -> tuple[LearningSignalType, ...]:
        if excluded:
            return (LearningSignalType.EXCLUDED,)
        if reason is CorrectionReason.ROUTING_ERROR:
            return (
                LearningSignalType.NEGATIVE,
                LearningSignalType.INTENT_CORRECTION,
            )
        return (LearningSignalType.NEGATIVE,)

    def capture_staff_correction(
        self,
        *,
        inquiry_id: int,
        draft_id: int,
        correction_reason: str | CorrectionReason,
        correction_note: str = "",
        corrected_intent: str = "",
        actor: str = "직원",
        signal_kind: str = "",
        signal_content: str = "",
        fact_scope: str | None = None,
    ) -> list[dict[str, Any]]:
        inquiry = self.inquiries.get(int(inquiry_id))
        draft = self.answers.get(int(draft_id))
        if inquiry is None or draft is None:
            raise LookupError("문의 또는 답변 Draft를 찾을 수 없습니다.")
        if int(draft["inquiry_id"]) != int(inquiry_id):
            raise ValueError("답변 Draft가 문의와 일치하지 않습니다.")
        posted_answer = NaverPostedAnswerRepository(
            self.database
        ).current(int(inquiry_id))
        posted_available = bool(
            posted_answer
            and posted_answer.get("fetch_status") == "AVAILABLE"
            and str(posted_answer.get("answer_body") or "").strip()
        )
        original = format_final_answer(
            str(
                posted_answer.get("answer_body")
                if posted_available and posted_answer is not None
                else draft.get("original_answer")
                or ""
            )
        )
        original_source = (
            AnswerProvenance.NAVER_POSTED
            if posted_available
            else AnswerProvenance.PROGRAM_GENERATED
        )
        original_reference_id = (
            int(posted_answer["id"])
            if posted_available and posted_answer is not None
            else int(draft_id)
        )
        corrected = format_final_answer(str(draft.get("edited_answer") or ""))
        if not corrected or corrected == original:
            return []
        reason = normalize_reason(correction_reason)
        intent = str(corrected_intent or "").strip().upper()
        if reason is CorrectionReason.ROUTING_ERROR and not intent:
            raise ValueError("라우팅 오류에는 올바른 문의 유형을 지정해 주세요.")
        question = "\n".join(
            value
            for value in (
                str(inquiry.get("title") or "").strip(),
                str(inquiry.get("content") or "").strip(),
            )
            if value
        )
        common = {
            "feedback_type": FeedbackType.STAFF_CORRECTION.value,
            "correction_reason": reason.value,
            "correction_note": str(correction_note or "").strip() or None,
            "corrected_intent": intent or None,
            "source": "DASHBOARD_STAFF_EDIT",
            "inquiry_id": int(inquiry_id),
            "answer_draft_id": int(draft_id),
            "historical_case_id": None,
            "original_answer_source": original_source.value,
            "original_answer_reference_id": original_reference_id,
            "question_masked": self.privacy.mask(question),
            "original_answer_masked": self.privacy.mask(original),
            "corrected_answer_masked": self.privacy.mask(corrected),
            "metadata_json": {
                "actor": str(actor or "직원"),
                "original_intent": inquiry.get("inquiry_type"),
                "positive_learning_source": "APPROVED_EDITED",
                "evaluated_answer_provenance": original_source.value,
            },
            "active": True,
        }
        self.repository.deactivate_for_draft(int(draft_id))
        saved = [
            self.repository.upsert(
                {
                    **common,
                    "source_key": self._source_key(
                        "STAFF_CORRECTION",
                        inquiry_id,
                        draft_id,
                        signal.value,
                    ),
                    "learning_signal_type": signal.value,
                }
            )
            for signal in self._signals(reason)
        ]
        self._capture_signal(
            origin_kind=OriginKind.NEGATIVE_REVIEW,
            feedback_id=saved[0]["id"] if saved else None,
            inquiry=inquiry,
            question=question,
            signal_kind=signal_kind,
            signal_content=signal_content,
            fact_scope=fact_scope,
            actor=actor,
            program_answer=original,
            final_answer=corrected,
            operator_note=correction_note,
        )
        return saved

    def capture_dashboard_negative(
        self,
        *,
        inquiry_id: int,
        original_answer_source: str | AnswerProvenance,
        original_answer_reference_id: int,
        correction_reason: str | CorrectionReason,
        correction_note: str = "",
        corrected_intent: str = "",
        actor: str = "직원",
        signal_kind: str = "",
        signal_content: str = "",
        fact_scope: str | None = None,
    ) -> list[dict[str, Any]]:
        """Capture a negative review without creating a positive example."""

        inquiry = self.inquiries.get(int(inquiry_id))
        if inquiry is None:
            raise LookupError("문의를 찾을 수 없습니다.")
        provenance = AnswerProvenance(str(original_answer_source))
        reference_id = int(original_answer_reference_id)
        draft = self.answers.get(reference_id)
        posted = NaverPostedAnswerRepository(self.database).current(
            int(inquiry_id)
        )
        if provenance is AnswerProvenance.PROGRAM_GENERATED:
            if draft is None or int(draft["inquiry_id"]) != int(inquiry_id):
                raise LookupError("평가할 Program Answer를 찾을 수 없습니다.")
            original = str(draft.get("original_answer") or "")
            draft_id: int | None = reference_id
        elif provenance is AnswerProvenance.STAFF_EDITED:
            if draft is None or int(draft["inquiry_id"]) != int(inquiry_id):
                raise LookupError("평가할 직원 수정본을 찾을 수 없습니다.")
            original = str(draft.get("edited_answer") or "")
            draft_id = reference_id
        elif provenance is AnswerProvenance.FINAL_ANSWER:
            if draft is None or int(draft["inquiry_id"]) != int(inquiry_id):
                raise LookupError("평가할 Final Answer를 찾을 수 없습니다.")
            original = str(draft.get("final_answer") or "")
            draft_id = reference_id
        elif provenance is AnswerProvenance.NAVER_POSTED:
            if posted is None or int(posted["id"]) != reference_id:
                raise LookupError("평가할 네이버 실제 등록 답변을 찾을 수 없습니다.")
            original = str(posted.get("answer_body") or "")
            draft_id = None
        else:
            raise ValueError("Dashboard에서 평가할 수 없는 답변 출처입니다.")
        original = format_final_answer(original)
        if not original:
            raise ValueError("평가할 답변 본문이 없습니다.")
        masked_original = self.privacy.mask(original)
        positive_provenances = [provenance.value]
        excluded_conflict = next(
            iter(
                self.repository.active_dashboard_feedback(
                    inquiry_id=int(inquiry_id),
                    original_answer_source=provenance.value,
                    original_answer_reference_id=reference_id,
                    signal_types=("EXCLUDED",),
                )
            ),
            None,
        )
        if excluded_conflict is not None:
            raise LearningConflictError(
                "이 답변은 이미 학습 제외로 평가되었습니다. 제외를 취소한 후 평가해 주세요.",
                conflict=excluded_conflict,
            )
        if provenance is AnswerProvenance.FINAL_ANSWER:
            positive_provenances.extend(
                [
                    AnswerProvenance.PROGRAM_GENERATED.value,
                    AnswerProvenance.STAFF_EDITED.value,
                ]
            )
        positive_repository = LearningRepository(self.database)
        positive_conflict = next(
            (
                row
                for candidate_provenance in positive_provenances
                for row in positive_repository.active_human_verified_for_answer(
                    inquiry_id=int(inquiry_id),
                    answer_provenance=candidate_provenance,
                    answer_reference_id=reference_id,
                )
                if str(row.get("final_answer") or "") == masked_original
            ),
            None,
        )
        if positive_conflict is not None:
            raise LearningConflictError(
                "이 답변은 이미 Human Verified Positive Learning으로 승인되었습니다. "
                "동일한 답변을 Negative Learning으로 저장할 수 없습니다.",
                conflict=positive_conflict,
            )
        reason = normalize_reason(correction_reason)
        intent = str(corrected_intent or "").strip().upper()
        if reason is CorrectionReason.ROUTING_ERROR and not intent:
            raise ValueError("라우팅 오류에는 올바른 문의 유형을 지정해 주세요.")
        question = "\n".join(
            value
            for value in (
                str(inquiry.get("title") or "").strip(),
                str(inquiry.get("content") or "").strip(),
            )
            if value
        )
        common = {
            "feedback_type": FeedbackType.STAFF_CORRECTION.value,
            "correction_reason": reason.value,
            "correction_note": str(correction_note or "").strip() or None,
            "corrected_intent": intent or None,
            "source": "DASHBOARD_NEGATIVE_REVIEW",
            "inquiry_id": int(inquiry_id),
            "answer_draft_id": draft_id,
            "historical_case_id": None,
            "original_answer_source": provenance.value,
            "original_answer_reference_id": reference_id,
            "question_masked": self.privacy.mask(question),
            "original_answer_masked": masked_original,
            "corrected_answer_masked": None,
            "metadata_json": {
                "actor": str(actor or "직원"),
                "original_intent": inquiry.get("inquiry_type"),
                "evaluated_answer_provenance": provenance.value,
                "positive_learning_created": False,
            },
            "active": True,
        }
        feedbacks = [
            {
                    **common,
                    "source_key": self._source_key(
                        "DASHBOARD_NEGATIVE",
                        inquiry_id,
                        provenance.value,
                        reference_id,
                        signal.value,
                    ),
                    "learning_signal_type": signal.value,
                }
            for signal in self._signals(reason)
        ]
        saved = self.repository.save_dashboard_evaluation_atomic(
            feedbacks,
            requested_signal="NEGATIVE",
            positive_answer_sources=tuple(positive_provenances),
        )
        self._capture_signal(
            origin_kind=OriginKind.NEGATIVE_REVIEW,
            feedback_id=saved[0]["id"] if saved else None,
            inquiry=inquiry,
            question=question,
            signal_kind=signal_kind,
            signal_content=signal_content,
            operator_note=correction_note,
            fact_scope=fact_scope,
            actor=actor,
        )
        return saved

    def capture_dashboard_excluded(
        self,
        *,
        inquiry_id: int,
        original_answer_source: str | AnswerProvenance,
        original_answer_reference_id: int,
        exclusion_reason: str | ExclusionReason,
        exclusion_note: str = "",
        actor: str = "직원",
        signal_kind: str = "",
        signal_content: str = "",
        fact_scope: str | None = None,
    ) -> dict[str, Any]:
        inquiry, provenance, reference_id, draft_id, original = self._dashboard_answer(
            inquiry_id=int(inquiry_id),
            original_answer_source=original_answer_source,
            original_answer_reference_id=int(original_answer_reference_id),
        )
        masked_original = self.privacy.mask(original)
        positive_provenances = [provenance.value]
        if provenance is AnswerProvenance.FINAL_ANSWER:
            positive_provenances.extend(
                [
                    AnswerProvenance.PROGRAM_GENERATED.value,
                    AnswerProvenance.STAFF_EDITED.value,
                ]
            )
        positive_repository = LearningRepository(self.database)
        positive_conflict = next(
            (
                row
                for candidate_provenance in positive_provenances
                for row in positive_repository.active_for_answer(
                    inquiry_id=int(inquiry_id),
                    answer_provenance=candidate_provenance,
                    answer_reference_id=reference_id,
                )
                if str(row.get("final_answer") or "") == masked_original
            ),
            None,
        )
        if positive_conflict is not None:
            raise LearningConflictError(
                "이 답변에는 활성 Positive Learning이 있습니다. 먼저 승인을 취소해 주세요.",
                conflict=positive_conflict,
            )
        negative_conflict = next(
            (
                row
                for row in self.repository.active_dashboard_feedback(
                    inquiry_id=int(inquiry_id),
                    original_answer_source=provenance.value,
                    original_answer_reference_id=reference_id,
                    signal_types=("NEGATIVE", "INTENT_CORRECTION"),
                )
                if str(row.get("original_answer_masked") or "") == masked_original
            ),
            None,
        )
        if negative_conflict is not None:
            raise LearningConflictError(
                "이 답변은 이미 Negative Learning으로 평가되었습니다.",
                conflict=negative_conflict,
            )
        reason = normalize_exclusion_reason(exclusion_reason)
        question = "\n".join(
            value
            for value in (
                str(inquiry.get("title") or "").strip(),
                str(inquiry.get("content") or "").strip(),
            )
            if value
        )
        feedback = {
                "source_key": self._source_key(
                    "DASHBOARD_EXCLUDED", inquiry_id, provenance.value, reference_id
                ),
                "feedback_type": FeedbackType.STAFF_CORRECTION.value,
                "correction_reason": reason.value,
                "correction_note": str(exclusion_note or "").strip() or None,
                "corrected_intent": None,
                "learning_signal_type": LearningSignalType.EXCLUDED.value,
                "source": "DASHBOARD_EXCLUDED",
                "inquiry_id": int(inquiry_id),
                "answer_draft_id": draft_id,
                "historical_case_id": None,
                "original_answer_source": provenance.value,
                "original_answer_reference_id": reference_id,
                "question_masked": self.privacy.mask(question),
                "original_answer_masked": masked_original,
                "corrected_answer_masked": None,
                "metadata_json": {
                    "actor": str(actor or "직원"),
                    "status": "ACTIVE",
                    "evaluated_answer_provenance": provenance.value,
                    "positive_learning_created": False,
                    "not_a_negative_evaluation": True,
                },
                "active": True,
            }
        saved = self.repository.save_dashboard_evaluation_atomic(
            [feedback],
            requested_signal="EXCLUDED",
            positive_answer_sources=tuple(positive_provenances),
        )[0]
        self._capture_signal(
            origin_kind=OriginKind.EXCLUSION_REVIEW,
            feedback_id=saved.get("id"),
            inquiry=inquiry,
            question=question,
            signal_kind=signal_kind,
            signal_content=signal_content,
            fact_scope=fact_scope,
            actor=actor,
        )
        return saved

    def revoke_dashboard_excluded(
        self, *, feedback_id: int, reason: str, actor: str = "직원"
    ) -> dict[str, Any]:
        return self.repository.revoke_dashboard_exclusion(
            feedback_id=int(feedback_id), reason=reason, actor=actor
        )

    def capture_historical_review(
        self,
        *,
        case_id: int,
        correction_reason: str | CorrectionReason,
        correction_note: str = "",
        corrected_intent: str = "",
        actor: str = "관리자",
        excluded: bool = False,
        signal_kind: str = "",
        signal_content: str = "",
        fact_scope: str | None = None,
    ) -> list[dict[str, Any]]:
        case = self.historical.get(int(case_id))
        if case is None:
            raise LookupError("Historical Case를 찾을 수 없습니다.")
        reason = normalize_reason(correction_reason)
        intent = str(corrected_intent or "").strip().upper()
        if (
            not excluded
            and reason is CorrectionReason.ROUTING_ERROR
            and not intent
        ):
            raise ValueError("라우팅 오류에는 올바른 문의 유형을 지정해 주세요.")
        self.historical.set_learning_enabled(
            int(case_id),
            False,
            reason=f"{reason.value}: {str(correction_note or '').strip()}".rstrip(": "),
            actor=actor,
        )
        self.historical.set_learning_signal_type(
            int(case_id),
            "EXCLUDED"
            if excluded
            else "INTENT_CORRECTION"
            if reason is CorrectionReason.ROUTING_ERROR
            else "NEGATIVE",
        )
        common = {
            "feedback_type": FeedbackType.HISTORICAL_REVIEW.value,
            "correction_reason": reason.value,
            "correction_note": str(correction_note or "").strip() or None,
            "corrected_intent": intent or None,
            "source": "HISTORICAL_CASE_MANAGER",
            "inquiry_id": case.get("inquiry_id"),
            "answer_draft_id": None,
            "historical_case_id": int(case_id),
            "original_answer_source": AnswerProvenance.HISTORICAL_VERIFIED.value,
            "original_answer_reference_id": int(case_id),
            "question_masked": self.privacy.mask(case.get("question")),
            "original_answer_masked": self.privacy.mask(case.get("seller_answer")),
            "corrected_answer_masked": None,
            "metadata_json": {
                "actor": str(actor or "관리자"),
                "original_intent": case.get("classification")
                or case.get("inquiry_type"),
            },
            "active": True,
        }
        self.repository.deactivate_for_historical_case(int(case_id))
        saved = [
            self.repository.upsert(
                {
                    **common,
                    "source_key": self._source_key(
                        "HISTORICAL_REVIEW",
                        case_id,
                        signal.value,
                    ),
                    "learning_signal_type": signal.value,
                }
            )
            for signal in self._signals(reason, excluded=excluded)
        ]
        if signal_kind and str(signal_content or "").strip():
            self.signals.capture(
                origin_kind=OriginKind.HISTORICAL_REVIEW,
                signal_kind=signal_kind,
                content_text=signal_content,
                inquiry={
                    "id": case.get("inquiry_id"),
                    "store_code": case.get("store_code"),
                    "product_name": case.get("product_name"),
                    "product_id": case.get("product_id"),
                },
                learning_feedback_id=saved[0]["id"] if saved else None,
                historical_case_id=int(case_id),
                question=str(case.get("question") or ""),
                product_name=case.get("product_name"),
                product_id=case.get("product_id"),
                fact_scope=fact_scope,
                actor=actor,
            )
        return saved
