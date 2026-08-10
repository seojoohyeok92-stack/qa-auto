from __future__ import annotations

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
    ) -> None:
        self.database = database
        self.inquiries = InquiryRepository(database)
        self.answers = AnswerRepository(database)
        self.logs = LogRepository(database)
        self.store_resolver = store_resolver
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
        target = capture.target
        if target is None:
            raise AutoPostConfirmationError("REMOTE_TARGET_NOT_FOUND")
        if target.answered is not True:
            raise AutoPostConfirmationError("SOURCE_ANSWERED_MISMATCH")
        if _canonical_body(target.seller_answer) != final_answer:
            raise AutoPostConfirmationError("REMOTE_ANSWER_MISMATCH")

        self.logs.record_inquiry(
            int(inquiry_id),
            "AUTO_POST_REMOTE_CONFIRMED",
            "네이버 재동기화에서 답변 상태와 본문 일치를 확인했습니다.",
            details={
                "auto_post_run_id": str(run_id)[:100],
                "sync_status": str(result.status),
                "source_answered": True,
                "body_matched": True,
            },
        )
        return AutoPostConfirmation(
            inquiry_id=int(inquiry_id),
            source_answered=True,
            body_matched=True,
            sync_status=str(result.status),
        )
