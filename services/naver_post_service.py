from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from typing import Any, Callable

from api.auth import get_access_token
from api.naver_answer_client import (
    NaverAlreadyAnsweredError,
    NaverAnswerClient,
    NaverAnswerClientError,
)
from config import (
    NaverPostSettings,
    StoreConfig,
    get_store_config,
)
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from repositories.naver_post_repository import (
    NON_RETRYABLE_TARGET_ERRORS,
    NaverPostAlreadyAnsweredError,
    NaverPostRepository,
    NaverPostStateError,
)
from repositories.naver_sync_repository import NaverSyncRepository
from repositories.workflow_repository import WorkflowRepository
from services.naver_post_dry_run_service import NaverPostDryRunService
from services.naver_post_payload_builder import NaverPostPayloadBuilder
from services.naver_post_payload_builder import NaverPostTargetError
from services.learning_service import LearningService
from services.auto_post_validation_service import AutoPostTechnicalValidator
from repositories.post_review_repository import PostReviewRepository
from answer.answer_format import format_final_answer
from kakao_notify import notify_qna_safely
from workflow.models import StepCode, StepStatus


@dataclass(frozen=True)
class NaverPostResult:
    status: str
    inquiry_id: int
    attempt_id: int | None
    http_status: int | None
    error_code: str | None
    message: str
    network_call_count: int
    response_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class NaverPostService:
    """Shared manual/automatic Naver answer posting and correction service."""

    def __init__(
        self,
        database: Database,
        *,
        settings: NaverPostSettings | None = None,
        store_resolver: Callable[[str | None], StoreConfig] = get_store_config,
        token_provider: Callable[..., str] = get_access_token,
        client: NaverAnswerClient | None = None,
        payload_builder: NaverPostPayloadBuilder | None = None,
    ) -> None:
        self.database = database
        self.settings = settings or NaverPostSettings.from_environment()
        self.store_resolver = store_resolver
        self.token_provider = token_provider
        self.payload_builder = payload_builder or NaverPostPayloadBuilder()
        self.client = client or NaverAnswerClient(
            timeout=(
                self.settings.connect_timeout,
                self.settings.read_timeout,
            )
        )
        self.inquiries = InquiryRepository(database)
        self.answers = AnswerRepository(database)
        self.posts = NaverPostRepository(database)
        self.logs = LogRepository(database)
        self.workflows = WorkflowRepository(database)
        self.technical_validator = AutoPostTechnicalValidator()
        self.reviews = PostReviewRepository(database)

    def _blocked(
        self, inquiry_id: int, code: str, message: str, *, automatic: bool = False
    ) -> NaverPostResult:
        self.logs.record_inquiry(
            inquiry_id,
            "NAVER_POST_BLOCKED",
            message,
            level="WARNING",
            details={"error_code": code, "network_call_count": 0},
        )
        if automatic:
            self.logs.record_inquiry(
                inquiry_id,
                "AUTO_POST_FAILED",
                "자동등록 최소 기술 검증 또는 전송 준비 단계에서 차단되었습니다.",
                level="ERROR",
                details={"error_code": code, "network_call_count": 0},
            )
        return NaverPostResult(
            "BLOCKED", inquiry_id, None, None, code, message, 0
        )

    def _complete_workflow(
        self, inquiry_id: int, *, attempt_id: int, actor: str,
        automatic: bool = False,
    ) -> None:
        try:
            self.workflows.initialize_steps(inquiry_id)
            step = self.workflows.get_step(inquiry_id, StepCode.NAVER_POST)
            status = StepStatus(step["step_status"])
            if status is StepStatus.PENDING:
                self.workflows.start_step(inquiry_id, StepCode.NAVER_POST)
            elif status in {
                StepStatus.FAILED,
                StepStatus.NEEDS_REVIEW,
            }:
                self.workflows.retry_step(inquiry_id, StepCode.NAVER_POST)
            step = self.workflows.get_step(inquiry_id, StepCode.NAVER_POST)
            if StepStatus(step["step_status"]) is not StepStatus.COMPLETED:
                self.workflows.complete_step(
                    inquiry_id,
                    StepCode.NAVER_POST,
                    metadata={
                        "attempt_id": attempt_id,
                        "actor": actor,
                        "manual": not automatic,
                    },
                )
        except Exception as error:
            self.logs.record_inquiry(
                inquiry_id,
                "NAVER_POST_WORKFLOW_WARNING",
                "등록은 성공했지만 Workflow 표시 갱신을 확인해야 합니다.",
                level="WARNING",
                details={"error_type": error.__class__.__name__},
            )

    def post(
        self,
        inquiry_id: int,
        *,
        actor: str,
        confirmed: bool,
        retry_requested: bool = False,
        automatic: bool = False,
        auto_post_run_id: str | None = None,
    ) -> NaverPostResult:
        inquiry_id = int(inquiry_id)
        self.logs.record_inquiry(
            inquiry_id,
            (
                "NAVER_POST_RETRY_REQUESTED"
                if retry_requested
                else "AUTO_POST_QUEUED" if automatic else "NAVER_POST_REQUESTED"
            ),
            "자동등록 대기열에서 네이버 답변 등록을 요청했습니다."
            if automatic else "직원이 네이버 실제 등록을 요청했습니다.",
            details={
                "actor": actor,
                "confirmed": bool(confirmed),
                "manual": not automatic,
                "auto_post_run_id": auto_post_run_id,
            },
        )
        if not confirmed:
            return self._blocked(
                inquiry_id,
                "CONFIRMATION_REQUIRED",
                "실제 등록 확인이 필요합니다.",
            )
        if not self.settings.enabled:
            self.logs.record_inquiry(
                inquiry_id,
                "NAVER_POST_LOCKED",
                "네이버 실제 등록 기능이 전역 설정으로 잠겨 있습니다.",
                level="WARNING",
                details={
                    "error_code": "NAVER_POST_DISABLED",
                    "network_call_count": 0,
                },
            )
            return self._blocked(
                inquiry_id,
                "NAVER_POST_DISABLED",
                "네이버 실제 등록 기능이 잠겨 있습니다.",
            )
        preflight_inquiry = self.inquiries.get(inquiry_id) or {}
        if bool(preflight_inquiry.get("source_answered")):
            return self._blocked(
                inquiry_id,
                "ALREADY_ANSWERED",
                "네이버에 이미 답변이 등록된 문의입니다.",
                automatic=automatic,
            )
        if (
            str(preflight_inquiry.get("post_status") or "").upper()
            == "POST_FAILED"
            and not retry_requested
        ):
            return self._blocked(
                inquiry_id,
                "EXPLICIT_RETRY_REQUIRED",
                "이전 실패 건은 명시적 재시도 승인 없이는 다시 전송할 수 없습니다.",
                automatic=automatic,
            )
        if retry_requested:
            latest_attempt = self.posts.latest(inquiry_id) or {}
            if str(latest_attempt.get("error_code") or "").upper() in NON_RETRYABLE_TARGET_ERRORS:
                return self._blocked(
                    inquiry_id,
                    "RETRY_PROHIBITED_TARGET_ERROR",
                    "대상 식별 오류는 자동 또는 추측 재시도할 수 없습니다.",
                    automatic=automatic,
                )
        if not automatic:
            dry = NaverPostDryRunService(
                self.database,
                store_resolver=self.store_resolver,
                settings=self.settings,
                payload_builder=self.payload_builder,
            ).run(inquiry_id)
            if not dry.eligible:
                detailed_code = next(
                    (
                        reason
                        for reason in dry.reasons
                        if reason in NON_RETRYABLE_TARGET_ERRORS
                    ),
                    "DRY_RUN_FAILED",
                )
                return self._blocked(
                    inquiry_id,
                    detailed_code,
                    "등록 조건 검증 실패: " + ", ".join(dry.reasons),
                )
        inquiry = self.inquiries.get(inquiry_id)
        draft = (
            self.answers.active_for_inquiry(inquiry_id)
            or self.answers.latest_for_inquiry(inquiry_id)
        )
        if inquiry is None or draft is None:
            return self._blocked(
                inquiry_id, "LOCAL_STATE_MISSING", "문의 또는 Draft가 없습니다."
            )
        try:
            target = self.payload_builder.resolve_target(
                inquiry,
                require_remote_snapshot=True,
                latest_sync_started_at=str(
                    (
                        NaverSyncRepository(self.database).latest_success_for(
                            store_code=str(inquiry.get("store_code") or ""),
                            inquiry_type=str(inquiry.get("source_type") or ""),
                        )
                        or {}
                    ).get("started_at")
                    or ""
                ),
            )
            request = self.payload_builder.build_for_target(
                target=target,
                final_answer=str(draft.get("final_answer") or ""),
            )
        except NaverPostTargetError as error:
            return self._blocked(
                inquiry_id,
                error.code,
                "네이버 문의 대상 ID·유형·원본 근거 검증에 실패했습니다.",
                automatic=automatic,
            )
        except (TypeError, ValueError) as error:
            return self._blocked(
                inquiry_id,
                str(error)[:100] or "PAYLOAD_BUILD_FAILED",
                "네이버 등록 Payload 생성에 실패했습니다.",
                automatic=automatic,
            )
        if automatic:
            validation = self.technical_validator.validate_payload(
                final_answer=request.final_answer,
                payload=request.payload,
                source_type=request.source_type,
            )
            if not validation.passed:
                return self._blocked(
                    inquiry_id,
                    validation.errors[0],
                    "자동등록 최소 기술 검증에 실패했습니다.",
                    automatic=True,
                )
        store = self.store_resolver(request.store)
        if str(store.code or "").upper() != str(target.store).upper():
            return self._blocked(
                inquiry_id,
                "STORE_CREDENTIAL_MISMATCH",
                "문의 Store와 인증 Store가 일치하지 않습니다.",
                automatic=automatic,
            )
        try:
            attempt = self.posts.acquire(
                inquiry_id=inquiry_id,
                draft_id=int(draft["id"]),
                idempotency_key=str(uuid.uuid4()),
                external_id=request.external_id,
                store_code=request.store,
                source_type=request.source_type,
                method=request.method,
                endpoint_kind=request.endpoint_kind,
                final_answer_hash=request.final_answer_hash,
                payload_hash=request.payload_hash,
                actor=str(actor or "").strip() or "operator",
                allow_unapproved=automatic,
                auto_post_run_id=auto_post_run_id,
            )
        except NaverPostAlreadyAnsweredError:
            return self._blocked(
                inquiry_id,
                "ALREADY_ANSWERED",
                "네이버에서 이미 답변된 문의입니다.",
            )
        except (NaverPostStateError, ValueError) as error:
            return self._blocked(
                inquiry_id, str(error), "현재 등록 상태에서는 전송할 수 없습니다."
            )

        attempt_id = int(attempt["id"])
        self.logs.record_inquiry(
            inquiry_id,
            "AUTO_POST_STARTED" if automatic else "NAVER_POST_STARTED",
            "네이버 자동 답변 등록을 시작했습니다."
            if automatic else "네이버 실제 답변 등록을 시작했습니다.",
            details={
                "actor": actor,
                "attempt_id": attempt_id,
                "external_id": request.external_id,
                "store": request.store,
                "source_type": request.source_type,
                "method": request.method,
                "endpoint_kind": request.endpoint_kind,
                "target_endpoint": request.endpoint,
                "target_id_source": target.external_id_source,
                "expected_success_status": target.expected_success_status,
                "auto_post_run_id": auto_post_run_id,
            },
        )
        try:
            access_token = self.token_provider(store=store)
            response = self.client.send(
                request, access_token=access_token
            )
        except NaverAlreadyAnsweredError as error:
            self.posts.mark_already_answered(
                attempt_id=attempt_id,
                inquiry_id=inquiry_id,
                http_status=error.http_status,
            )
            self.logs.record_inquiry(
                inquiry_id,
                "NAVER_ALREADY_ANSWERED",
                "네이버에서 이미 답변된 문의로 확인되어 등록하지 않았습니다.",
                level="WARNING",
                details={
                    "attempt_id": attempt_id,
                    "http_status": error.http_status,
                    "network_call_count": 1,
                },
            )
            return NaverPostResult(
                "ALREADY_ANSWERED",
                inquiry_id,
                attempt_id,
                error.http_status,
                error.code,
                "네이버에서 이미 답변된 문의입니다.",
                1,
            )
        except NaverAnswerClientError as error:
            target = "POST_UNKNOWN" if error.uncertain else "POST_FAILED"
            self.posts.fail(
                attempt_id=attempt_id,
                inquiry_id=inquiry_id,
                status=target,
                error_code=error.code,
                error_message="Naver answer request failed.",
                http_status=error.http_status,
            )
            event = (
                "AUTO_POST_UNKNOWN" if automatic and target == "POST_UNKNOWN"
                else "AUTO_POST_FAILED" if automatic
                else "NAVER_POST_UNKNOWN"
                if target == "POST_UNKNOWN"
                else "NAVER_POST_FAILED"
            )
            self.logs.record_inquiry(
                inquiry_id,
                event,
                (
                    "요청 도달 여부를 확인할 수 없어 자동 재시도를 차단했습니다."
                    if target == "POST_UNKNOWN"
                    else "네이버 실제 등록에 실패했습니다."
                ),
                level="ERROR",
                details={
                    "attempt_id": attempt_id,
                    "http_status": error.http_status,
                    "error_code": error.code,
                    "retryable": error.retryable,
                    "network_call_count": 1,
                },
            )
            return NaverPostResult(
                target,
                inquiry_id,
                attempt_id,
                error.http_status,
                error.code,
                str(error),
                1,
            )
        except Exception as error:
            self.posts.fail(
                attempt_id=attempt_id,
                inquiry_id=inquiry_id,
                status="POST_FAILED",
                error_code=error.__class__.__name__.upper(),
                error_message="Authentication or request preparation failed.",
            )
            self.logs.record_inquiry(
                inquiry_id,
                "AUTO_POST_FAILED" if automatic else "NAVER_POST_FAILED",
                "인증 또는 등록 준비 단계에서 실패했습니다.",
                level="ERROR",
                details={
                    "attempt_id": attempt_id,
                    "error_type": error.__class__.__name__,
                    "network_call_count": 0,
                },
            )
            return NaverPostResult(
                "POST_FAILED",
                inquiry_id,
                attempt_id,
                None,
                error.__class__.__name__.upper(),
                "인증 또는 등록 준비 실패",
                0,
            )

        self.posts.succeed(
            attempt_id=attempt_id,
            inquiry_id=inquiry_id,
            draft_id=int(draft["id"]),
            http_status=response.http_status,
            response_id=response.response_id,
            final_answer_hash=request.final_answer_hash,
            actor=str(actor or "").strip() or "operator",
        )
        self._complete_workflow(
            inquiry_id, attempt_id=attempt_id, actor=actor,
            automatic=automatic,
        )
        self.logs.record_inquiry(
            inquiry_id,
            "AUTO_POST_SUCCEEDED" if automatic else "NAVER_POST_SUCCEEDED",
            "네이버 자동 답변 등록을 완료했습니다."
            if automatic else "네이버 실제 답변 등록을 완료했습니다.",
            details={
                "actor": actor,
                "attempt_id": attempt_id,
                "external_id": request.external_id,
                "store": request.store,
                "source_type": request.source_type,
                "method": request.method,
                "endpoint_kind": request.endpoint_kind,
                "http_status": response.http_status,
                "response_id": response.response_id,
                "final_answer_hash": request.final_answer_hash,
                "draft_id": draft["id"],
                "network_call_count": 1,
                "auto_post_run_id": auto_post_run_id,
            },
        )
        try:
            LearningService(self.database).mark_posted(
                inquiry_id,
                posted_at=str(draft.get("posted_at") or "") or None,
                auto_posted=automatic,
            )
        except Exception as error:
            self.logs.record_inquiry(
                inquiry_id,
                "LEARNING_POST_STATUS_FAILED",
                "등록 상태의 Learning 반영에 실패했지만 등록 결과는 유지됩니다.",
                level="WARNING",
                details={"exception_type": error.__class__.__name__},
            )
        # A draft-generation notification is deliberately not evidence that
        # Naver accepted an answer.  Only this confirmed success path has the
        # exact payload that was registered, so enqueue a separate, stable
        # completion notification here.  The outbox key includes the immutable
        # attempt id: retrying the same notification cannot duplicate it.
        try:
            notify_qna_safely(
                title=(
                    "[네이버 Q&A 자동등록 완료]"
                    if automatic
                    else "[네이버 Q&A 답변 등록 완료]"
                ),
                product=str(
                    inquiry.get("product_name") or inquiry.get("product") or ""
                ),
                option_name=str(inquiry.get("option_name") or ""),
                question=str(
                    inquiry.get("content") or inquiry.get("question") or ""
                ),
                # request.final_answer is the technically validated, hashed
                # payload just acknowledged by Naver.
                answer=request.final_answer,
                action="posted",
                inquiry_id=str(inquiry_id),
                notify_key=f"naver-posted:{inquiry_id}:{attempt_id}",
            )
        except Exception as error:
            # Notification/outbox trouble must never roll back a confirmed
            # Naver post; it is recorded for retry/operations diagnosis.
            self.logs.record_inquiry(
                inquiry_id,
                "KAKAO_POSTED_NOTIFICATION_WARNING",
                "네이버 등록 성공 알림을 대기열에 추가하지 못했습니다.",
                level="WARNING",
                details={"exception_type": error.__class__.__name__},
            )
        return NaverPostResult(
            "POSTED",
            inquiry_id,
            attempt_id,
            response.http_status,
            None,
            "네이버 등록 완료",
            1,
            response.response_id,
        )

    def correct(
        self,
        inquiry_id: int,
        *,
        edited_answer: str,
        actor: str,
        answer_content_id: str | None = None,
    ) -> NaverPostResult:
        inquiry_id = int(inquiry_id)
        inquiry = self.inquiries.get(inquiry_id)
        review = self.reviews.get(inquiry_id)
        if inquiry is None or review is None:
            return self._blocked(
                inquiry_id, "POST_REVIEW_NOT_FOUND", "사후검토 상태가 없습니다."
            )
        if not self.settings.enabled:
            return self._blocked(
                inquiry_id, "NAVER_POST_DISABLED", "네이버 실제 등록 기능이 잠겨 있습니다."
            )
        clean = format_final_answer(edited_answer)
        validation = self.technical_validator.validate_answer(clean)
        if not validation.passed:
            return self._blocked(
                inquiry_id, validation.errors[0], "수정 답변 최소 기술 검증에 실패했습니다."
            )
        external_id = str(
            inquiry.get("external_inquiry_id")
            or inquiry.get("source_question_id")
            or ""
        )
        resolved_answer_id = str(
            answer_content_id or inquiry.get("post_response_id") or ""
        ) or None
        try:
            request = self.payload_builder.build_correction(
                source_type=str(inquiry.get("source_type") or ""),
                external_id=external_id,
                store=str(inquiry.get("store_code") or ""),
                final_answer=clean,
                answer_content_id=resolved_answer_id,
            )
        except ValueError as error:
            return self._blocked(inquiry_id, str(error), "수정 Payload 생성에 실패했습니다.")
        payload_validation = self.technical_validator.validate_payload(
            final_answer=request.final_answer,
            payload=request.payload,
            source_type=request.source_type,
        )
        if not payload_validation.passed:
            return self._blocked(
                inquiry_id, payload_validation.errors[0], "수정 Payload 검증에 실패했습니다."
            )
        version, correction = self.reviews.begin_correction(
            inquiry_id=inquiry_id,
            answer=clean,
            actor=actor,
            answer_content_id=resolved_answer_id,
        )
        correction_id = int(correction["id"])
        self.logs.record_inquiry(
            inquiry_id,
            "POST_CORRECTION_REQUESTED",
            "직원이 자동등록 답변의 네이버 수정을 요청했습니다.",
            details={"actor": actor, "version_id": version["id"]},
        )
        self.logs.record_inquiry(
            inquiry_id,
            "POST_CORRECTION_STARTED",
            "네이버 답변 수정을 시작했습니다.",
            details={"correction_id": correction_id, "endpoint_kind": request.endpoint_kind},
        )
        store = self.store_resolver(request.store)
        try:
            token = self.token_provider(store=store)
            response = self.client.send(request, access_token=token)
        except NaverAnswerClientError as error:
            target = "UNKNOWN" if error.uncertain else "FAILED"
            self.reviews.fail_correction(
                correction_id=correction_id,
                status=target,
                error_code=error.code,
                error_message="Naver correction request failed.",
                http_status=error.http_status,
            )
            self.logs.record_inquiry(
                inquiry_id,
                "POST_CORRECTION_FAILED",
                "네이버 답변 수정에 실패했습니다.",
                level="ERROR",
                details={"correction_id": correction_id, "error_code": error.code},
            )
            return NaverPostResult(
                "POST_UNKNOWN" if error.uncertain else "CORRECTION_FAILED",
                inquiry_id, correction_id, error.http_status, error.code,
                "네이버 답변 수정 실패", 1,
            )
        except Exception as error:
            code = error.__class__.__name__.upper()[:100]
            self.reviews.fail_correction(
                correction_id=correction_id,
                status="FAILED",
                error_code=code,
                error_message="Correction authentication or preparation failed.",
            )
            self.logs.record_inquiry(
                inquiry_id, "POST_CORRECTION_FAILED",
                "인증 또는 준비 오류로 네이버 답변 수정에 실패했습니다.",
                level="ERROR", details={"error_type": error.__class__.__name__},
            )
            return NaverPostResult(
                "CORRECTION_FAILED", inquiry_id, correction_id, None,
                code, "네이버 답변 수정 준비 실패", 0,
            )
        applied, _ = self.reviews.complete_correction(
            correction_id=correction_id,
            http_status=response.http_status,
            response_id=response.response_id,
            payload_hash=request.payload_hash,
        )
        self.logs.record_inquiry(
            inquiry_id,
            "POST_CORRECTION_SUCCEEDED",
            "직원 수정 답변을 네이버에 반영했습니다.",
            details={
                "correction_id": correction_id,
                "version_id": applied["id"],
                "http_status": response.http_status,
                "answer_hash": request.final_answer_hash,
            },
        )
        self.logs.record_inquiry(
            inquiry_id,
            "POST_CORRECTION_RECORDED",
            "직원 수정 이력을 기록했습니다. Learning은 관리자 판단으로만 반영됩니다.",
            details={
                "version_id": applied["id"],
                "learning_saved": False,
                "learning_policy": "MANUAL_DECISION_ONLY",
            },
        )
        return NaverPostResult(
            "CORRECTED_AND_REPOSTED", inquiry_id, correction_id,
            response.http_status, None, "네이버 답변 수정 완료", 1,
            response.response_id,
        )
