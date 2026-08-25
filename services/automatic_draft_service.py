from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from answer.exceptions import AutoAnswerProhibitedError
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from services.answer_service import AnswerService, is_valid_draft


@dataclass(frozen=True)
class AutomaticDraftOutcome:
    status: str
    inquiry_id: int
    draft_id: int | None = None
    route: str | None = None
    error_code: str | None = None


class AutomaticDraftService:
    """Ensure an unanswered inquiry already has a Program Answer.

    Manual regeneration remains a separate later operation.  This service is
    idempotent and never replaces an existing active Draft.
    """

    def __init__(
        self,
        database: Database,
        *,
        answer_service: AnswerService | None = None,
    ) -> None:
        self.database = database
        self.inquiries = InquiryRepository(database)
        self.answers = AnswerRepository(database)
        self.logs = LogRepository(database)
        self.answer_service = answer_service or AnswerService(database)

    def ensure_for_inquiry(
        self,
        inquiry_id: int,
        *,
        correlation_id: str | None = None,
    ) -> AutomaticDraftOutcome:
        inquiry = self.inquiries.get(int(inquiry_id))
        if inquiry is None:
            raise LookupError(f"Inquiry not found: {inquiry_id}")
        if (
            bool(inquiry.get("source_answered"))
            or str(inquiry.get("answer_status") or "").upper() == "ANSWERED"
            or str(inquiry.get("post_status") or "").upper() == "POSTED"
        ):
            return AutomaticDraftOutcome(
                status="SKIPPED_ALREADY_ANSWERED",
                inquiry_id=int(inquiry_id),
            )
        active = self.answers.active_for_inquiry(int(inquiry_id))
        if active and is_valid_draft(active.get("original_answer")):
            return AutomaticDraftOutcome(
                status="EXISTING",
                inquiry_id=int(inquiry_id),
                draft_id=int(active["id"]),
                route=str(
                    (active.get("metadata_json") or {}).get(
                        "selected_answer_route"
                    )
                    or "EXISTING"
                ),
            )

        trace_id = correlation_id or str(uuid.uuid4())
        self.logs.record_inquiry(
            int(inquiry_id),
            "AUTOMATIC_DRAFT_STARTED",
            "문의 수집 후 초기 Program Answer 자동 생성을 시작했습니다.",
            details={"correlation_id": trace_id, "template_preferred": True},
        )
        try:
            generated = self.answer_service.generate_for_inquiry(
                int(inquiry_id),
                prefer_template=True,
                correlation_id=trace_id,
            )
            active = self.answers.active_for_inquiry(int(inquiry_id))
            if (
                active is None
                or not is_valid_draft(active.get("original_answer"))
            ):
                raise RuntimeError("Active Draft was not created")
            route = str(
                generated.result.metadata.get("selected_answer_route")
                or generated.result.metadata.get("generation_mode")
                or "UNKNOWN"
            )
            self.logs.record_inquiry(
                int(inquiry_id),
                "AUTOMATIC_DRAFT_COMPLETED",
                "초기 Program Answer 자동 생성과 활성화를 완료했습니다.",
                details={
                    "correlation_id": trace_id,
                    "draft_id": int(active["id"]),
                    "selected_answer_route": route,
                    "draft_length": len(
                        str(active.get("original_answer") or "").strip()
                    ),
                },
            )
            return AutomaticDraftOutcome(
                status="CREATED",
                inquiry_id=int(inquiry_id),
                draft_id=int(active["id"]),
                route=route,
            )
        except AutoAnswerProhibitedError as blocked:
            # The gate did its job. Reporting it as an ERROR made a correct
            # high-risk block indistinguishable from an outage, so it is
            # recorded as the deliberate policy decision it is. The outcome is
            # unchanged: no draft, no auto-post, staff review.
            self.logs.record_inquiry(
                int(inquiry_id),
                "AUTOMATIC_DRAFT_POLICY_BLOCKED",
                "정책상 자동 답변 생성이 금지된 문의로 직원 검토 대상입니다.",
                level="WARNING",
                details={
                    "correlation_id": trace_id,
                    "policy_blocked": True,
                    "policy_reason": blocked.policy_reason
                    or "AUTO_ANSWER_PROHIBITED",
                    "safe_error_code": blocked.reason_code
                    or "AUTO_ANSWER_PROHIBITED",
                    "selected_answer_route": "BLOCKED_REVIEW_REQUIRED",
                },
            )
            return AutomaticDraftOutcome(
                status="POLICY_BLOCKED",
                inquiry_id=int(inquiry_id),
                route="BLOCKED_REVIEW_REQUIRED",
                error_code=blocked.reason_code or "AUTO_ANSWER_PROHIBITED",
            )
        except Exception as error:
            error_code = error.__class__.__name__.upper()[:100]
            self.logs.record_inquiry(
                int(inquiry_id),
                "AUTOMATIC_DRAFT_FAILED",
                "초기 Program Answer 자동 생성 중 시스템 오류가 발생했습니다.",
                level="ERROR",
                details={
                    "correlation_id": trace_id,
                    "safe_error_code": error_code,
                },
            )
            return AutomaticDraftOutcome(
                status="FAILED",
                inquiry_id=int(inquiry_id),
                error_code=error_code,
            )

