from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterable

from repositories.answer_repository import AnswerRepository
from repositories.auto_post_repository import AutoPostRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from repositories.post_review_repository import PostReviewRepository
from services.automatic_draft_service import AutomaticDraftService
from services.naver_post_service import NaverPostService
from services.dps_agent_client import get_dps_session_status
from workflow.models import InquiryStatus


AUTO_POST_ROUTES = {
    "TEMPLATE", "SAFE_RULE", "PRODUCT_DB", "GPT_FALLBACK", "GPT_DIRECT",
    "ORDER_ID_REQUEST", "ORDER_LOOKUP_FAILED", "DELIVERY_ORDER_NOT_FOUND",
    "DPS_LOOKUP_FAILED", "DELIVERY_DATE_UNCONFIRMED",
    "DELIVERY_WITH_INSTALLATION_DATE", "REVIEW_REQUIRED_SAFE_DRAFT",
}


@dataclass(frozen=True)
class AutoPostRunResult:
    processed_count: int = 0
    succeeded_count: int = 0
    failed_count: int = 0
    unknown_count: int = 0
    skipped_count: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


class AutoPostPipelineService:
    """Generate, technically validate, finalize, and post each inquiry independently."""

    def __init__(
        self,
        database: Database,
        *,
        draft_service: AutomaticDraftService | None = None,
        post_service: NaverPostService | None = None,
        confirmation_service: Any | None = None,
        dps_status_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.database = database
        self.inquiries = InquiryRepository(database)
        self.answers = AnswerRepository(database)
        self.auto = AutoPostRepository(database)
        self.reviews = PostReviewRepository(database)
        self.logs = LogRepository(database)
        self.drafts = draft_service or AutomaticDraftService(database)
        self.posts = post_service or NaverPostService(database)
        self.dps_status_provider = dps_status_provider or get_dps_session_status
        if confirmation_service is not None:
            self.confirmation = confirmation_service
        elif post_service is None:
            from services.auto_post_confirmation_service import (
                AutoPostConfirmationService,
            )

            self.confirmation = AutoPostConfirmationService(database)
        else:
            # Injected post services are Mock/DryRun test paths and must never
            # cause an accidental external read.
            self.confirmation = None

    @staticmethod
    def _route(draft: dict[str, Any]) -> str:
        metadata = draft.get("metadata_json")
        data = metadata if isinstance(metadata, dict) else {}
        selected = str(data.get("selected_answer_route") or "").upper()
        if selected:
            return selected
        # Compatibility for pre-ProcessingPlan drafts. This reads an existing
        # safety decision only; it does not re-route or rewrite the Draft.
        if str(data.get("answer_type") or "").lower() == "order_id_required":
            return "ORDER_ID_REQUEST"
        return str(data.get("generation_mode") or "").upper()

    @staticmethod
    def _needs_review(draft: dict[str, Any]) -> bool:
        metadata = draft.get("metadata_json")
        data = metadata if isinstance(metadata, dict) else {}
        plan = data.get("processing_plan")
        plan_data = plan if isinstance(plan, dict) else {}
        return bool(
            data.get("requires_manual_review")
            or plan_data.get("needs_staff_review")
            or str(draft.get("review_status") or "").upper() in {
                "NEEDS_REVIEW", "IN_REVIEW"
            }
        )

    def _dps_session_block_reason(
        self, draft: dict[str, Any]
    ) -> str | None:
        metadata = draft.get("metadata_json")
        data = metadata if isinstance(metadata, dict) else {}
        plan = data.get("processing_plan")
        plan_data = plan if isinstance(plan, dict) else {}
        dps = data.get("dps")
        dps_data = dps if isinstance(dps, dict) else {}
        required = bool(
            plan_data.get("requires_dps_lookup")
            or dps_data.get("lookup_required")
        )
        lookup_status = str(dps_data.get("lookup_status") or "").upper()
        if not required or lookup_status in {"SUCCESS", "NOT_FOUND"}:
            return None
        error_code = str(dps_data.get("error_code") or "").upper()
        direct_markers = (
            "LOGIN_REQUIRED", "OTP_REQUIRED", "CHROME_NOT_FOUND",
            "DPS_TAB_NOT_FOUND", "AGENT_CONNECTION_FAILED",
            "AGENT_CONNECT_TIMEOUT", "AGENT_REQUEST_FAILED",
            "AGENT_START_FAILED", "AGENT_START_TIMEOUT",
        )
        if any(marker in error_code for marker in direct_markers):
            return error_code
        status = self.dps_status_provider()
        session_status = str(status.get("session_status") or "UNKNOWN").upper()
        if session_status in {
            "LOGIN_REQUIRED", "CHROME_NOT_FOUND", "DPS_PAGE_NOT_FOUND",
            "CONNECTION_FAILED",
        }:
            return session_status
        return None

    def _pause_for_system_error(self, error_code: object) -> None:
        code = str(error_code or "").upper()
        immediate = {
            "PII_EXPOSURE", "SECRET_EXPOSURE",
            "PAYLOAD_FINAL_ANSWER_MISMATCH", "FINAL_PAYLOAD_MISMATCH",
            "REMOTE_ANSWER_MISMATCH", "SOURCE_ANSWERED_MISMATCH",
            "LOCK_STORAGE_ERROR", "DB_INTEGRITY_ERROR",
            "SCHEDULER_LEADER_CONFLICT",
        }
        should_pause = code in immediate
        if code == "AUTH_FAILED":
            with self.database.connection() as connection:
                failures = int(connection.execute(
                    """
                    SELECT COUNT(*) FROM naver_post_attempts
                    WHERE error_code='AUTH_FAILED'
                      AND created_at >= datetime('now', '-30 minutes')
                    """
                ).fetchone()[0])
            should_pause = failures >= 2
        if should_pause:
            from services.auto_post_runtime_service import AutoPostRuntimeService

            AutoPostRuntimeService(self.database).pause(code)

    def run_pending(
        self, *, run_id: str, owner_id: str,
        max_retries: int, limit: int = 100,
        inquiry_ids: Iterable[int] | None = None,
    ) -> AutoPostRunResult:
        counters = {
            "processed_count": 0, "succeeded_count": 0,
            "failed_count": 0, "unknown_count": 0, "skipped_count": 0,
        }
        recovered = self.auto.recover_stale_posting()
        if recovered:
            self.logs.record_system(
                "AUTO_POST_UNKNOWN",
                "앱 재시작 전 전송 중이던 건을 POST_UNKNOWN으로 복구했습니다.",
                level="ERROR", details={"recovered_count": recovered},
            )
        for candidate in self.auto.candidates(
            max_retries=max_retries, limit=limit, inquiry_ids=inquiry_ids
        ):
            inquiry_id = int(candidate["id"])
            external_id = str(
                candidate.get("external_inquiry_id")
                or candidate.get("source_question_id") or ""
            )
            locked = self.auto.acquire_inquiry_lock(
                inquiry_id=inquiry_id,
                store_code=str(candidate.get("store_code") or ""),
                external_id=external_id,
                owner_id=owner_id,
                run_id=run_id,
            )
            if not locked:
                counters["skipped_count"] += 1
                continue
            counters["processed_count"] += 1
            try:
                fresh = self.inquiries.get(inquiry_id) or {}
                if (
                    bool(fresh.get("source_answered"))
                    or str(fresh.get("post_status") or "").upper() == "POSTED"
                ):
                    counters["skipped_count"] += 1
                    self.logs.record_inquiry(
                        inquiry_id,
                        "AUTO_POST_SKIPPED_ALREADY_ANSWERED",
                        "이미 답변된 문의이므로 자동등록을 건너뛰었습니다.",
                        details={"auto_post_run_id": run_id},
                    )
                    continue
                self.logs.record_inquiry(
                    inquiry_id, "AUTO_ANSWER_STARTED",
                    "자동 답변 생성 파이프라인을 시작했습니다.",
                    details={"auto_post_run_id": run_id},
                )
                draft_outcome = self.drafts.ensure_for_inquiry(
                    inquiry_id, correlation_id=run_id
                )
                if draft_outcome.status in {"FAILED", "SKIPPED_ALREADY_ANSWERED"}:
                    if draft_outcome.status == "SKIPPED_ALREADY_ANSWERED":
                        counters["skipped_count"] += 1
                        event = "AUTO_POST_SKIPPED_ALREADY_ANSWERED"
                    else:
                        counters["failed_count"] += 1
                        event = "AUTO_ANSWER_FAILED"
                    self.logs.record_inquiry(
                        inquiry_id, event,
                        "자동 답변을 준비하지 못해 해당 문의를 건너뛰었습니다.",
                        level="WARNING",
                        details={
                            "auto_post_run_id": run_id,
                            "error_code": draft_outcome.error_code,
                        },
                    )
                    continue
                draft = self.answers.active_for_inquiry(inquiry_id)
                if draft is None or not str(draft.get("original_answer") or "").strip():
                    raise ValueError("DRAFT_REQUIRED")
                route = self._route(draft)
                if not route or route not in AUTO_POST_ROUTES:
                    raise ValueError("ROUTE_UNIDENTIFIED")
                dps_block = self._dps_session_block_reason(draft)
                if dps_block:
                    counters["skipped_count"] += 1
                    self.inquiries.update_status(
                        inquiry_id, InquiryStatus.NEEDS_ATTENTION
                    )
                    self.logs.record_inquiry(
                        inquiry_id,
                        "AUTO_POST_BLOCKED_DPS_SESSION",
                        "DPS session is unavailable; automatic posting was held for staff review.",
                        level="WARNING",
                        details={
                            "auto_post_run_id": run_id,
                            "safe_error_code": dps_block,
                            "dps_required": True,
                        },
                    )
                    continue
                self.logs.record_inquiry(
                    inquiry_id, "AUTO_ANSWER_SUCCEEDED",
                    "자동 답변 Draft 준비를 완료했습니다.",
                    details={
                        "auto_post_run_id": run_id,
                        "draft_id": draft["id"],
                        "selected_answer_route": route,
                    },
                )
                version, finalized_draft = self.reviews.finalize_auto(
                    inquiry_id=inquiry_id,
                    draft_id=int(draft["id"]),
                    run_id=run_id,
                )
                self.logs.record_inquiry(
                    inquiry_id, "AUTO_FINAL_CREATED",
                    "직원 승인과 구분되는 자동등록용 Final Answer를 생성했습니다.",
                    details={
                        "auto_post_run_id": run_id,
                        "draft_id": finalized_draft["id"],
                        "answer_version_id": version["id"],
                        "finalization_source": "AUTO_POST",
                        "approval_status": "AUTO_FINALIZED",
                        "actor": "SYSTEM_AUTO_POST",
                    },
                )
                result = self.posts.post(
                    inquiry_id,
                    actor="SYSTEM_AUTO_POST",
                    confirmed=True,
                    retry_requested=str(fresh.get("post_status") or "").upper() == "POST_FAILED",
                    automatic=True,
                    auto_post_run_id=run_id,
                )
                if result.status == "POSTED":
                    if self.confirmation is not None:
                        self.confirmation.confirm(inquiry_id, run_id=run_id)
                    counters["succeeded_count"] += 1
                    posted = self.inquiries.get(inquiry_id) or {}
                    self.reviews.create_review_after_post(
                        inquiry_id=inquiry_id,
                        draft_id=int(finalized_draft["id"]),
                        version_id=int(version["id"]),
                        run_id=run_id,
                        route=route,
                        needs_staff_review=self._needs_review(draft),
                        posted_at=posted.get("posted_at"),
                    )
                    self.logs.record_inquiry(
                        inquiry_id, "POST_REVIEW_CREATED",
                        "자동등록 답변의 사후검토 대기 상태를 생성했습니다.",
                        details={
                            "auto_post_run_id": run_id,
                            "review_status": "AUTO_POSTED_UNREVIEWED",
                        },
                    )
                elif result.status == "ALREADY_ANSWERED":
                    counters["skipped_count"] += 1
                    self.logs.record_inquiry(
                        inquiry_id, "AUTO_POST_SKIPPED_ALREADY_ANSWERED",
                        "네이버에 이미 답변이 있어 로컬 상태만 동기화했습니다.",
                        details={"auto_post_run_id": run_id},
                    )
                else:
                    unknown = result.status == "POST_UNKNOWN"
                    counters["unknown_count" if unknown else "failed_count"] += 1
                    self._pause_for_system_error(result.error_code)
                    self.reviews.mark_post_failure(
                        inquiry_id=inquiry_id,
                        draft_id=int(finalized_draft["id"]),
                        version_id=int(version["id"]),
                        run_id=run_id,
                        route=route,
                        status=result.status,
                        needs_staff_review=self._needs_review(draft),
                    )
            except Exception as error:
                counters["failed_count"] += 1
                self._pause_for_system_error(str(error))
                self.logs.record_inquiry(
                    inquiry_id, "AUTO_ANSWER_FAILED",
                    "자동 답변·등록 파이프라인의 기술 단계에서 실패했습니다.",
                    level="ERROR",
                    details={
                        "auto_post_run_id": run_id,
                        "error_type": error.__class__.__name__,
                        "error_code": str(error)[:100],
                    },
                )
            finally:
                self.auto.release_inquiry_lock(
                    inquiry_id=inquiry_id, owner_id=owner_id
                )
        return AutoPostRunResult(**counters)
