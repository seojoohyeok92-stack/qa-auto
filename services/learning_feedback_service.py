from __future__ import annotations

import hashlib
from typing import Any

from answer.learning_feedback import (
    CorrectionReason,
    FeedbackType,
    LearningSignalType,
    normalize_reason,
)
from answer.answer_provenance import AnswerProvenance
from answer.answer_format import format_final_answer
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.historical_case_repository import HistoricalCaseRepository
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_feedback_repository import LearningFeedbackRepository
from repositories.naver_posted_answer_repository import (
    NaverPostedAnswerRepository,
)
from services.learning_privacy_service import LearningPrivacyService


class LearningFeedbackService:
    """Store human correction signals separately from positive answer examples."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.answers = AnswerRepository(database)
        self.inquiries = InquiryRepository(database)
        self.historical = HistoricalCaseRepository(database)
        self.repository = LearningFeedbackRepository(database)
        self.privacy = LearningPrivacyService()

    @staticmethod
    def _source_key(*parts: object) -> str:
        return hashlib.sha256(
            "|".join(str(part or "") for part in parts).encode("utf-8")
        ).hexdigest()

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
        return [
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
            "original_answer_masked": self.privacy.mask(original),
            "corrected_answer_masked": None,
            "metadata_json": {
                "actor": str(actor or "직원"),
                "original_intent": inquiry.get("inquiry_type"),
                "evaluated_answer_provenance": provenance.value,
                "positive_learning_created": False,
            },
            "active": True,
        }
        self.repository.deactivate_dashboard_evaluation(
            inquiry_id=int(inquiry_id),
            original_answer_source=provenance.value,
            original_answer_reference_id=reference_id,
        )
        return [
            self.repository.upsert(
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
            )
            for signal in self._signals(reason)
        ]

    def capture_historical_review(
        self,
        *,
        case_id: int,
        correction_reason: str | CorrectionReason,
        correction_note: str = "",
        corrected_intent: str = "",
        actor: str = "관리자",
        excluded: bool = False,
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
        return [
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
