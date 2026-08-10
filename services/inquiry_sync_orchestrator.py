from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Callable, Sequence

from config import StoreConfig, get_configured_stores
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from repositories.workflow_repository import WorkflowRepository
from services.inquiry_sync_service import InquirySyncService
from services.inquiry_sync_trace import InquirySyncTrace
from services.work_queue_service import WorkItem, WorkQueueError, load_work_queue
from services.naver_inquiry_sync_service import NaverInquirySyncService
from services.automatic_draft_service import AutomaticDraftService


@dataclass(frozen=True)
class InquirySyncRunResult:
    requested_store_count: int
    successful_store_count: int
    fetched_count: int
    created_count: int
    updated_count: int
    unchanged_count: int
    failed_count: int
    errors: tuple[WorkQueueError, ...]
    completed_at: str
    correlation_id: str
    api_latest_registered_at: str | None = None
    database_latest_registered_at: str | None = None
    skipped_count: int = 0
    status: str = "SUCCESS"
    requested_from: str | None = None
    requested_to: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict:
        result = asdict(self)
        result["errors"] = [dict(item) for item in self.errors]
        return result


Loader = Callable[..., tuple[list[WorkItem], list[WorkQueueError]]]


class InquirySyncOrchestrator:
    """Dashboard와 UAT가 공유하는 네이버 문의 동기화 진입점."""

    def __init__(
        self, database: Database, *, loader: Loader = load_work_queue
    ) -> None:
        self.database = database
        self.loader = loader
        self.sync_service = InquirySyncService(
            InquiryRepository(database),
            WorkflowRepository(database),
            LogRepository(database),
            automatic_drafts=AutomaticDraftService(database),
        )
        self.logs = LogRepository(database)

    def run(
        self,
        *,
        stores: Sequence[StoreConfig] | None = None,
        days: int = 7,
        answered: bool | None = None,
        sync_type: str = "MANUAL",
        owner_id: str | None = None,
    ) -> InquirySyncRunResult:
        if not 1 <= int(days) <= 90:
            raise ValueError("문의 조회 기간은 1~90일이어야 합니다.")
        targets = list(stores) if stores is not None else get_configured_stores()
        if self.loader is load_work_queue:
            to_datetime = datetime.now(UTC)
            from_datetime = to_datetime - timedelta(days=int(days))
            result = NaverInquirySyncService(
                self.database,
                automatic_drafts=AutomaticDraftService(self.database),
            ).sync_inquiries(
                stores=targets,
                from_datetime=from_datetime,
                to_datetime=to_datetime,
                sync_type=sync_type,
                owner_id=owner_id,
            )
            return InquirySyncRunResult(
                requested_store_count=result.requested_store_count,
                successful_store_count=result.successful_store_count,
                fetched_count=result.fetched_count,
                created_count=result.inserted_count,
                updated_count=result.updated_count,
                unchanged_count=result.unchanged_count,
                failed_count=result.failed_count,
                errors=tuple(
                    {
                        "store_code": str(item.get("store_id") or ""),
                        "store_name": str(item.get("store_id") or ""),
                        "stage": str(item.get("error_code") or "조회"),
                        "source": item.get("inquiry_type"),
                        "inquiry_id": None,
                        "message": str(item.get("message") or ""),
                    }
                    for item in result.errors
                ),
                completed_at=result.completed_at,
                correlation_id=result.sync_id,
                database_latest_registered_at=InquiryRepository(
                    self.database
                ).latest_registered_at(),
                skipped_count=result.skipped_count,
                status=result.status,
                requested_from=result.requested_from,
                requested_to=result.requested_to,
                error_code=result.error_code,
                error_message=result.error_message,
            )
        correlation_id = str(uuid.uuid4())
        trace = InquirySyncTrace(self.logs, correlation_id)
        trace.emit(
            "NAVER_SYNC_BUTTON_REQUEST_ACCEPTED",
            {
                "store_count": len(targets),
                "days": int(days),
                "answered": answered,
            },
        )
        watermarks = InquiryRepository(self.database).sync_watermarks()
        trace.emit(
            "NAVER_SYNC_INCREMENTAL_WINDOW_READY",
            {
                "watermark_count": len(watermarks),
                "mode": "INCREMENTAL" if watermarks else "INITIAL_BACKFILL",
            },
        )
        self.logs.record_system(
            "NAVER_INQUIRY_SYNC_STARTED",
            "네이버 문의 동기화를 시작했습니다.",
            details={
                "store_count": len(targets),
                "days": int(days),
                "correlation_id": correlation_id,
            },
        )
        try:
            items, errors = self.loader(
                stores=targets,
                days=int(days),
                answered=answered,
                event_callback=trace.emit,
                since_by_store_source=watermarks,
            )
        except TypeError as error:
            # Preserve compatibility with injected legacy loaders used by UAT/tests.
            if not any(
                name in str(error)
                for name in ("event_callback", "since_by_store_source")
            ):
                raise
            items, errors = self.loader(
                stores=targets, days=int(days), answered=answered
            )
        for error in errors:
            trace.emit(
                "NAVER_SYNC_SOURCE_FAILED",
                {
                    "store_code": error.get("store_code"),
                    "source": error.get("source"),
                    "stage": error.get("stage"),
                    "error_category": error.get("message"),
                },
                level="ERROR",
            )
        trace.emit(
            "NAVER_SYNC_API_COLLECTION_FINISHED",
            {
                "fetched_count": len(items),
                "source_error_count": len(errors),
            },
        )
        api_latest = max(
            (
                str(item.get("registered_at"))
                for item in items
                if item.get("registered_at") not in (None, "")
            ),
            default=None,
        )
        sync_result = self.sync_service.sync(
            items,
            correlation_id=correlation_id,
            event_callback=trace.emit,
        )
        failed_store_codes = {
            str(error.get("store_code")) for error in errors
        }
        successful_stores = {
            store.code for store in targets
        }.difference(failed_store_codes)
        completed_at = datetime.now().astimezone().isoformat(timespec="seconds")
        database_latest = InquiryRepository(
            self.database
        ).latest_registered_at()
        failed_count = sync_result["failed"] + len(errors)
        event = (
            "NAVER_INQUIRY_SYNC_SUCCEEDED"
            if failed_count == 0
            else "NAVER_INQUIRY_SYNC_PARTIAL"
        )
        self.logs.record_system(
            event,
            "네이버 문의 동기화를 완료했습니다."
            if failed_count == 0
            else "일부 스토어 또는 문의의 동기화에 실패했습니다.",
            level="INFO" if failed_count == 0 else "WARNING",
            details={
                "store_count": len(targets),
                "fetched_count": len(items),
                "created_count": sync_result["new"],
                "updated_count": sync_result["updated"],
                "unchanged_count": sync_result["unchanged"],
                "failed_count": failed_count,
                "completed_at": completed_at,
                "correlation_id": correlation_id,
                "api_latest_registered_at": api_latest,
                "database_latest_registered_at": database_latest,
            },
        )
        trace.emit(
            "NAVER_SYNC_DB_PIPELINE_FINISHED",
            {
                "fetched_count": len(items),
                "created_count": sync_result["new"],
                "updated_count": sync_result["updated"],
                "unchanged_count": sync_result["unchanged"],
                "failed_count": failed_count,
                "status": "SUCCESS" if failed_count == 0 else "PARTIAL",
            },
            level="INFO" if failed_count == 0 else "WARNING",
            persist=False,
        )
        return InquirySyncRunResult(
            requested_store_count=len(targets),
            successful_store_count=len(successful_stores),
            fetched_count=len(items),
            created_count=sync_result["new"],
            updated_count=sync_result["updated"],
            unchanged_count=sync_result["unchanged"],
            failed_count=failed_count,
            errors=tuple(errors),
            completed_at=completed_at,
            correlation_id=correlation_id,
            api_latest_registered_at=api_latest,
            database_latest_registered_at=database_latest,
        )
