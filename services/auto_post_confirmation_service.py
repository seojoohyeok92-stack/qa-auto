from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from config import StoreConfig, get_store_config
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from services.naver_inquiry_normalizer import InquiryNormalizer, NormalizedInquiry
from services.naver_inquiry_sync_service import NaverInquirySyncService


class AutoPostConfirmationError(RuntimeError):
    """A post succeeded locally but could not be proved by a fresh Naver read."""


@dataclass(frozen=True)
class AutoPostConfirmation:
    inquiry_id: int
    source_answered: bool
    body_matched: bool
    sync_status: str


class _CapturingNormalizer:
    """Capture the target answer in memory while preserving the normal sync path."""

    def __init__(self, *, source_type: str, external_id: str) -> None:
        self.base = InquiryNormalizer()
        self.source_type = str(source_type).upper()
        self.external_id = str(external_id)
        self.target: NormalizedInquiry | None = None

    def _remember(self, value: NormalizedInquiry) -> NormalizedInquiry:
        if (
            value.inquiry_type.upper() == self.source_type
            and value.external_inquiry_id == self.external_id
        ):
            self.target = value
        return value

    def product(self, payload: dict[str, Any], *, store_code: str) -> NormalizedInquiry:
        return self._remember(self.base.product(payload, store_code=store_code))

    def customer(self, payload: dict[str, Any], *, store_code: str) -> NormalizedInquiry:
        return self._remember(self.base.customer(payload, store_code=store_code))


SyncFactory = Callable[[Database, _CapturingNormalizer], Any]


# How many times the remote read is attempted before the confirmation gives up,
# and how long it waits between attempts.
#
# A PUT that returned 204 is not immediately readable: Naver's read model
# publishes the answer a moment after the write is accepted. Measured on a real
# auto-post run, the confirmation read fired 1.1s after the 204 and came back
# with the answer absent (``fetch_status=NOT_FETCHED``, empty body); the next
# ordinary sync, minutes later, returned that exact body. The single-shot read
# therefore reported a body mismatch for an answer that had posted correctly.
#
# Three attempts spanning ~7s cover the propagation gap without making a run
# meaningfully slower -- posts are sequential, and only an unconfirmed one
# pays the wait. Anything still invisible after that is left for the next
# sync to reconcile rather than being declared a mismatch.
CONFIRMATION_ATTEMPTS = 3
CONFIRMATION_BACKOFF_SECONDS: tuple[float, ...] = (2.0, 5.0)


def _canonical_body(value: object) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


class AutoPostConfirmationService:
    """Re-sync one recent target and prove both answered state and body equality."""

    def __init__(
        self,
        database: Database,
        *,
        store_resolver: Callable[[str], StoreConfig] = get_store_config,
        sync_factory: SyncFactory | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        attempts: int = CONFIRMATION_ATTEMPTS,
        backoff_seconds: tuple[float, ...] = CONFIRMATION_BACKOFF_SECONDS,
    ) -> None:
        self.database = database
        self.inquiries = InquiryRepository(database)
        self.answers = AnswerRepository(database)
        self.logs = LogRepository(database)
        self.store_resolver = store_resolver
        self.sleeper = sleeper
        self.attempts = max(1, int(attempts))
        self.backoff_seconds = tuple(backoff_seconds) or (0.0,)
        self.sync_factory = sync_factory or (
            lambda db, normalizer: NaverInquirySyncService(
                db, normalizer=normalizer
            )
        )

    def confirm(self, inquiry_id: int, *, run_id: str) -> AutoPostConfirmation:
        inquiry = self.inquiries.get(int(inquiry_id))
        draft = self.answers.active_for_inquiry(int(inquiry_id))
        if inquiry is None or draft is None:
            raise AutoPostConfirmationError("REMOTE_TARGET_NOT_FOUND")
        source_type = str(inquiry.get("source_type") or "").upper()
        external_id = str(
            inquiry.get("external_inquiry_id")
            or inquiry.get("source_question_id")
            or ""
        )
        store_code = str(inquiry.get("store_code") or "").upper()
        final_answer = _canonical_body(draft.get("final_answer"))
        if (
            source_type not in {"PRODUCT_INQUIRY", "CUSTOMER_INQUIRY"}
            or not external_id
            or not store_code
            or not final_answer
        ):
            raise AutoPostConfirmationError("REMOTE_TARGET_NOT_FOUND")

        # A remote read that does not yet show the answer is not evidence that
        # the wrong body was posted. The four outcomes are kept apart:
        #
        #   REMOTE_TARGET_NOT_FOUND     the inquiry itself was not in the sync
        #   SOURCE_ANSWERED_MISMATCH    the inquiry is still marked unanswered
        #   REMOTE_ANSWER_NOT_VISIBLE   answered, but the body has not published
        #   REMOTE_ANSWER_MISMATCH      a real body, and it differs
        #
        # Only the last is a content failure, and only it keeps the immediate
        # global pause in ``AutoPostPipelineService._pause_for_system_error``.
        # The first three are retried; if they persist they are reported as
        # themselves, so an answer that simply has not appeared yet no longer
        # stops every other inquiry's auto-post.
        state = "REMOTE_TARGET_NOT_FOUND"
        sync_status = "UNKNOWN"
        attempt_used = 0
        for attempt in range(self.attempts):
            attempt_used = attempt + 1
            if attempt:
                index = min(attempt - 1, len(self.backoff_seconds) - 1)
                self.sleeper(self.backoff_seconds[index])
            capture = _CapturingNormalizer(
                source_type=source_type, external_id=external_id
            )
            now = datetime.now(UTC)
            result = self.sync_factory(self.database, capture).sync_inquiries(
                stores=[self.store_resolver(store_code)],
                inquiry_types=[source_type],
                from_datetime=now - timedelta(days=7),
                to_datetime=now,
                sync_type="AUTO_POST_CONFIRMATION",
                owner_id=str(run_id)[:100],
            )
            sync_status = str(result.status)
            target = capture.target
            if target is None:
                state = "REMOTE_TARGET_NOT_FOUND"
                continue
            if target.answered is not True:
                state = "SOURCE_ANSWERED_MISMATCH"
                continue
            remote_body = _canonical_body(target.seller_answer)
            if not remote_body:
                state = "REMOTE_ANSWER_NOT_VISIBLE"
                continue
            if remote_body != final_answer:
                # A body that is present and different is a real finding.
                # Retrying cannot change it, so stop here and keep the
                # existing blocking behaviour intact.
                state = "REMOTE_ANSWER_MISMATCH"
                break
            state = "CONFIRMED"
            break

        if state != "CONFIRMED":
            self.logs.record_inquiry(
                int(inquiry_id),
                "AUTO_POST_REMOTE_UNCONFIRMED",
                "네이버 재동기화에서 게시 본문을 확인하지 못했습니다.",
                level="WARNING",
                details={
                    "auto_post_run_id": str(run_id)[:100],
                    "sync_status": sync_status,
                    "state": state,
                    "attempts": attempt_used,
                },
            )
            raise AutoPostConfirmationError(state)

        self.logs.record_inquiry(
            int(inquiry_id),
            "AUTO_POST_REMOTE_CONFIRMED",
            "네이버 재동기화에서 답변 상태와 본문 일치를 확인했습니다.",
            details={
                "auto_post_run_id": str(run_id)[:100],
                "sync_status": sync_status,
                "source_answered": True,
                "body_matched": True,
                "attempts": attempt_used,
            },
        )
        return AutoPostConfirmation(
            inquiry_id=int(inquiry_id),
            source_answered=True,
            body_matched=True,
            sync_status=sync_status,
        )
