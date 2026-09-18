"""Register one Coupang answer, on an operator's explicit click.

Manual posting only.  Nothing schedules this, nothing retries it, and the
auto-post pipeline cannot reach it: Coupang is absent from ``POST_MARKETS``,
which is what scopes that queue.

A confirmed registration notifies the Coupang operations room, through the one
notification path Naver uses.  Only success does, because that is the only
notification the Naver post service sends: a failed post is recorded and shown
on the screen the operator is already looking at.  Holds and "직원 확인 필요"
come from the answer pipeline, which already routes by store code, so they
needed nothing here.

The decision of *whether* this answer may go out is not re-invented here.  It
is the Naver manual decision, made by the same code:

    AnswerRepository.posting_answer(manual=True)   which text is posted
    AutoPostTechnicalValidator.validate_answer     personal data, secrets, …
    NaverPostRepository.acquire(...)               approval/duplicate/state
    WorkflowRepository                             the POST step

``acquire`` is the important one.  Despite the table's name its columns are
market-neutral, and it already refuses a post whose inquiry is answered at the
marketplace, whose draft is posted, whose answer changed under the operator,
or whose external id is already POSTING/POSTED elsewhere.  Reusing it is what
makes a second click write nothing, without a new idempotency table.

What is Coupang's own is only the last stretch: which account signs, which
vendor the path names, and the documented reply body.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from api.coupang_post_client import (
    CoupangPostClient,
    CoupangPostError,
    build_reply_path,
)
from config import get_coupang_account
from kakao_notify import notify_qna_safely
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from repositories.naver_post_repository import (
    NaverPostAlreadyAnsweredError,
    NaverPostRepository,
    NaverPostStateError,
)
from repositories.workflow_repository import WorkflowRepository
from services.auto_post_validation_service import AutoPostTechnicalValidator
from services.market_policy import (
    COUPANG,
    account_of_store,
    is_store_manual_post_enabled,
    market_of,
)
from workflow.models import StepCode, StepStatus

ENDPOINT_KIND = "COUPANG_ONLINE_INQUIRY_REPLY"


@dataclass(frozen=True)
class CoupangPostResult:
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


class CoupangPostService:
    """The manual Coupang counterpart of ``NaverPostService.post``."""

    def __init__(
        self,
        database: Database,
        *,
        client: CoupangPostClient | None = None,
        account_resolver: Any = get_coupang_account,
    ) -> None:
        self.database = database
        self.inquiries = InquiryRepository(database)
        self.answers = AnswerRepository(database)
        self.posts = NaverPostRepository(database)
        self.logs = LogRepository(database)
        self.workflows = WorkflowRepository(database)
        self.technical_validator = AutoPostTechnicalValidator()
        self.account_resolver = account_resolver
        self._client = client

    # -- helpers -----------------------------------------------------------

    def _blocked(self, inquiry_id: int, code: str, message: str) -> CoupangPostResult:
        self.logs.record_inquiry(
            inquiry_id,
            "COUPANG_POST_BLOCKED",
            message,
            level="WARNING",
            details={"error_code": code, "network_call_count": 0},
        )
        return CoupangPostResult(
            "BLOCKED", inquiry_id, None, None, code, message, 0
        )

    def _complete_workflow(self, inquiry_id: int, *, attempt_id: int, actor: str) -> None:
        try:
            self.workflows.initialize_steps(inquiry_id)
            step = self.workflows.get_step(inquiry_id, StepCode.NAVER_POST)
            status = StepStatus(step["step_status"])
            if status is StepStatus.PENDING:
                self.workflows.start_step(inquiry_id, StepCode.NAVER_POST)
            elif status in {StepStatus.FAILED, StepStatus.NEEDS_REVIEW}:
                self.workflows.retry_step(inquiry_id, StepCode.NAVER_POST)
            step = self.workflows.get_step(inquiry_id, StepCode.NAVER_POST)
            if StepStatus(step["step_status"]) is not StepStatus.COMPLETED:
                self.workflows.complete_step(
                    inquiry_id,
                    StepCode.NAVER_POST,
                    metadata={"attempt_id": attempt_id, "actor": actor, "manual": True},
                )
        except Exception as error:  # noqa: BLE001 - the post already succeeded
            self.logs.record_inquiry(
                inquiry_id,
                "COUPANG_POST_WORKFLOW_WARNING",
                "등록은 성공했지만 Workflow 표시 갱신을 확인해야 합니다.",
                level="WARNING",
                details={"error_type": error.__class__.__name__},
            )

    # -- what the screen may ask without sending anything ------------------

    def preflight(
        self, inquiry_id: int, *, retry_requested: bool = False
    ) -> dict[str, Any]:
        """Whether this inquiry could be registered right now.  Writes nothing.

        Shaped like the Naver dry run so the same panel can render either.
        """

        resolved = self._resolve(int(inquiry_id), retry_requested=retry_requested)
        if isinstance(resolved, tuple):
            code, message = resolved
            return {
                "eligible": False,
                "inquiry_id": int(inquiry_id),
                "reasons": [message],
                "error_code": code,
                "endpoint_kind": ENDPOINT_KIND,
                "method": "POST",
                "network_call": False,
                "network_call_count": 0,
            }
        return {
            "eligible": True,
            "inquiry_id": int(inquiry_id),
            "reasons": [],
            "error_code": None,
            "endpoint_kind": ENDPOINT_KIND,
            "method": "POST",
            "account_code": resolved["account"].account_code,
            "vendor_id": resolved["account"].vendor_id,
            "external_id": resolved["external_id"],
            "path": resolved["path"],
            # Neither the WING id nor any key is echoed here; the screen only
            # needs to know the account resolved, not what it signs with.
            "reply_by_configured": True,
            "answer_field": resolved["answer_field"],
            "network_call": False,
            "network_call_count": 0,
        }

    # -- the one public entry point ---------------------------------------

    def post(
        self, inquiry_id: int, *, actor: str, confirmed: bool,
        retry_requested: bool = False,
    ) -> CoupangPostResult:
        inquiry_id = int(inquiry_id)
        self.logs.record_inquiry(
            inquiry_id,
            "COUPANG_POST_REQUESTED",
            "직원이 쿠팡 실제 등록을 요청했습니다.",
            details={"actor": actor, "confirmed": bool(confirmed), "manual": True},
        )
        if not confirmed:
            return self._blocked(
                inquiry_id, "CONFIRMATION_REQUIRED", "실제 등록 확인이 필요합니다."
            )
        resolved = self._resolve(inquiry_id, retry_requested=retry_requested)
        if isinstance(resolved, tuple):
            code, message = resolved
            return self._blocked(inquiry_id, code, message)
        inquiry = resolved["inquiry"]
        draft = resolved["draft"]
        account = resolved["account"]
        external_id = resolved["external_id"]
        answer_field = resolved["answer_field"]
        post_answer = resolved["post_answer"]
        store_code = str(inquiry.get("store_code") or "")
        answer_hash = resolved["answer_hash"]
        path = resolved["path"]
        return self._send(
            inquiry_id=inquiry_id, inquiry=inquiry, draft=draft, account=account,
            external_id=external_id, answer_field=answer_field,
            post_answer=post_answer, store_code=store_code,
            answer_hash=answer_hash, path=path, actor=actor,
        )

    # -- internals ---------------------------------------------------------

    def _resolve(
        self, inquiry_id: int, *, retry_requested: bool
    ) -> tuple[str, str] | dict[str, Any]:
        """Every check that runs before anything is sent.

        Returns ``(code, message)`` for the first refusal, otherwise the
        resolved target.  One definition, so the panel's preflight and the
        registration itself can never disagree.
        """

        inquiry = self.inquiries.get(inquiry_id)
        if inquiry is None:
            return ("LOCAL_STATE_MISSING", "문의가 없습니다.")
        store_code = str(inquiry.get("store_code") or "")
        if market_of(store_code) != COUPANG:
            return ("NOT_COUPANG", "쿠팡 문의가 아닙니다.")
        if not is_store_manual_post_enabled(store_code):
            return ("MANUAL_POST_DISABLED", "수동 등록이 비활성화된 매장입니다.")
        if bool(inquiry.get("source_answered")):
            return ("ALREADY_ANSWERED", "쿠팡에 이미 답변이 등록된 문의입니다.")
        if str(inquiry.get("post_status") or "").upper() in {"POSTED", "POSTING", "POST_UNKNOWN"}:
            return ("ALREADY_POSTED", "이미 등록 처리된 문의입니다.")
        if str(inquiry.get("post_status") or "").upper() == "POST_FAILED" and not retry_requested:
            return (
                "EXPLICIT_RETRY_REQUIRED",
                "이전 실패 건은 명시적 재시도 승인 없이는 다시 전송할 수 없습니다.",
            )
        # The reply the marketplace already holds, read from the stored
        # payload the screen shows.  Its presence means a person already
        # answered in Wing even if ``source_answered`` has not caught up.
        from services.coupang_seller_answer_learning_service import (
            marketplace_seller_answer,
        )

        if marketplace_seller_answer(inquiry).strip():
            return ("SELLER_ANSWER_EXISTS", "이미 판매자 답변이 있는 문의입니다.")
        draft = (
            self.answers.active_for_inquiry(inquiry_id)
            or self.answers.latest_for_inquiry(inquiry_id)
        )
        if draft is None:
            return ("LOCAL_STATE_MISSING", "Draft가 없습니다.")
        answer_field, _answer_source = self.answers.posting_answer(draft, manual=True)
        post_answer = str(draft.get(answer_field) or "").strip()
        if bool(draft.get("posted")):
            return ("ALREADY_POSTED", "이미 등록된 Draft입니다.")
        if not post_answer:
            return ("POST_ANSWER_REQUIRED", "등록할 답변이 없습니다.")
        technical = self.technical_validator.validate_answer(post_answer)
        if not technical.passed:
            return (
                technical.errors[0] if technical.errors else "VALIDATOR_NOT_PASS",
                "등록 조건 검증 실패: "
                + ", ".join(technical.errors or ("VALIDATOR_NOT_PASS",)),
            )
        external_id = str(
            inquiry.get("external_inquiry_id")
            or inquiry.get("source_question_id")
            or ""
        ).strip()
        if not external_id:
            return ("EXTERNAL_ID_REQUIRED", "쿠팡 문의 ID가 없습니다.")
        account_code = str(
            (inquiry.get("source_metadata_json") or {}).get("account_code")
            if isinstance(inquiry.get("source_metadata_json"), dict) else ""
        ).strip() or str(account_of_store(store_code) or "").strip()
        if not account_code:
            return ("ACCOUNT_UNRESOLVED", "쿠팡 판매자 계정을 확인할 수 없습니다.")
        try:
            account = self.account_resolver(account_code)
        except ValueError as error:
            return ("ACCOUNT_CONFIG_INVALID", str(error)[:200])
        if not account.post_configured:
            # Fail closed, and name what is missing.  A WING id absent from the
            # environment is a configuration gap, never a reason to guess one
            # or to borrow the other account's.
            return (
                "COUPANG_POST_NOT_CONFIGURED",
                "쿠팡 등록 설정이 없습니다: "
                + ", ".join(account.missing_post_variables()),
            )
        answer_hash = hashlib.sha256(
            post_answer.replace("\r\n", "\n").replace("\r", "\n").strip().encode("utf-8")
        ).hexdigest()
        try:
            path = build_reply_path(
                vendor_id=account.vendor_id, inquiry_id=external_id
            )
        except CoupangPostError as error:
            return (error.code, error.message or "등록 대상 확인 실패")
        return {
            "inquiry": inquiry,
            "draft": draft,
            "account": account,
            "external_id": external_id,
            "answer_field": answer_field,
            "post_answer": post_answer,
            "answer_hash": answer_hash,
            "path": path,
        }

    def _send(
        self, *, inquiry_id: int, inquiry: dict[str, Any], draft: dict[str, Any],
        account: Any, external_id: str, answer_field: str, post_answer: str,
        store_code: str, answer_hash: str, path: str, actor: str,
    ) -> CoupangPostResult:
        try:
            attempt = self.posts.acquire(
                inquiry_id=inquiry_id,
                draft_id=int(draft["id"]),
                idempotency_key=str(uuid.uuid4()),
                external_id=external_id,
                store_code=store_code,
                source_type=str(inquiry.get("source_type") or ""),
                method="POST",
                endpoint_kind=ENDPOINT_KIND,
                final_answer_hash=answer_hash,
                payload_hash=hashlib.sha256(path.encode("utf-8")).hexdigest(),
                actor=str(actor or "").strip() or "operator",
                answer_field=answer_field,
                # The click is the approval, exactly as on the Naver manual
                # path; this must not reapply the automatic Final Answer gate.
                allow_unapproved=True,
            )
        except NaverPostAlreadyAnsweredError:
            return self._blocked(
                inquiry_id, "ALREADY_ANSWERED", "쿠팡에 이미 답변이 등록된 문의입니다."
            )
        except NaverPostStateError as error:
            return self._blocked(
                inquiry_id, str(error)[:100] or "POST_STATE_BLOCKED", "등록 상태 검증 실패."
            )
        attempt_id = int(attempt["id"])
        client = self._client or CoupangPostClient(
            access_key=account.access_key, secret_key=account.secret_key,
        )
        try:
            result = client.reply(
                vendor_id=account.vendor_id,
                inquiry_id=external_id,
                content=post_answer,
                reply_by=account.wing_id,
            )
        except CoupangPostError as error:
            # NETWORK_ERROR leaves the outcome unknown, so it is recorded as
            # such and never retried on its own: Coupang refuses a duplicate
            # reply, and a blind retry could still race the first request.
            status = "POST_UNKNOWN" if error.code == "NETWORK_ERROR" else "POST_FAILED"
            self.posts.fail(
                attempt_id=attempt_id,
                inquiry_id=inquiry_id,
                status=status,
                error_code=error.code,
                error_message=error.message,
                http_status=error.http_status,
            )
            self.logs.record_inquiry(
                inquiry_id,
                "COUPANG_POST_FAILED",
                "쿠팡 답변 등록에 실패했습니다.",
                level="ERROR",
                details={
                    "error_code": error.code,
                    "http_status": error.http_status,
                    "attempt_id": attempt_id,
                    "network_call_count": 1,
                },
            )
            return CoupangPostResult(
                status, inquiry_id, attempt_id, error.http_status,
                error.code, error.message or "쿠팡 등록 실패", 1,
            )
        self.posts.succeed(
            attempt_id=attempt_id,
            inquiry_id=inquiry_id,
            draft_id=int(draft["id"]),
            http_status=result.http_status,
            response_id=None,
            final_answer_hash=answer_hash,
            actor=str(actor or "").strip() or "operator",
        )
        self._complete_workflow(inquiry_id, attempt_id=attempt_id, actor=actor)
        # The same notification the Naver post service sends on the same
        # event, keyed on the immutable attempt id so a repeated call cannot
        # duplicate it.  ``notify_qna_safely`` resolves the room from the
        # store code, so both Coupang accounts reach the one Coupang room and
        # a market that may not be notified still sends nothing.
        try:
            notify_qna_safely(
                title="[쿠팡 Q&A 답변 등록 완료]",
                store_code=store_code,
                product=str(inquiry.get("product_name") or ""),
                option_name=str(inquiry.get("option_name") or ""),
                question=str(inquiry.get("content") or inquiry.get("title") or ""),
                # The text Coupang just acknowledged, not the draft it came from.
                answer=post_answer,
                action="posted",
                inquiry_id=str(inquiry_id),
                notify_key=f"coupang-posted:{inquiry_id}:{attempt_id}",
            )
        except Exception as error:  # noqa: BLE001 - the post is already confirmed
            # Outbox trouble must never roll back a registered answer.
            self.logs.record_inquiry(
                inquiry_id,
                "KAKAO_POSTED_NOTIFICATION_WARNING",
                "쿠팡 등록 성공 알림을 대기열에 추가하지 못했습니다.",
                level="WARNING",
                details={"exception_type": error.__class__.__name__},
            )
        self.logs.record_inquiry(
            inquiry_id,
            "COUPANG_POST_SUCCEEDED",
            "쿠팡 답변을 등록했습니다.",
            details={
                "attempt_id": attempt_id,
                "http_status": result.http_status,
                "actor": actor,
                "network_call_count": 1,
                # source_answered stays marketplace truth: the next Coupang
                # inquiry sync observes the reply and sets it.  Posting here
                # records that this system sent one, not that Coupang shows it.
                "source_answered_owner": "COUPANG_SYNC",
            },
        )
        return CoupangPostResult(
            "POSTED", inquiry_id, attempt_id, result.http_status, None,
            "쿠팡 등록 완료", 1,
        )
