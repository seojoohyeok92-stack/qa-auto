from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Sequence

from api.auth import get_access_token
from api.customer_inquiry import get_customer_inquiries
from api.naver_read_client import (
    ERROR_MESSAGES,
    NaverSyncError,
    classified_error,
)
from api.qna import get_qna_list
from config import (
    NaverSyncSettings,
    StoreConfig,
    get_configured_stores,
    get_store_config,
)
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from repositories.naver_sync_repository import NaverSyncRepository
from repositories.workflow_repository import WorkflowRepository
from services.inquiry_sync_service import InquirySyncService
from services.automatic_draft_service import AutomaticDraftService
from services.inquiry_sync_trace import InquirySyncTrace
from services.naver_inquiry_normalizer import InquiryNormalizer


SUPPORTED_INQUIRY_TYPES = ("PRODUCT_INQUIRY", "CUSTOMER_INQUIRY")


@dataclass(frozen=True)
class NaverInquirySyncResult:
    sync_id: str
    status: str
    requested_store_count: int
    successful_store_count: int
    inquiry_types: tuple[str, ...]
    requested_from: str
    requested_to: str
    fetched_count: int
    inserted_count: int
    updated_count: int
    unchanged_count: int
    skipped_count: int
    failed_count: int
    started_at: str
    completed_at: str
    duration_ms: int
    error_code: str | None
    error_message: str | None
    errors: tuple[dict[str, Any], ...]

    @property
    def created_count(self) -> int:
        return self.inserted_count

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["inquiry_types"] = list(self.inquiry_types)
        result["errors"] = [dict(item) for item in self.errors]
        result["created_count"] = self.inserted_count
        result["completed_at"] = self.completed_at
        return result


class NaverInquirySyncService:
    """UI-independent, read-only Naver inquiry synchronization service."""

    def __init__(
        self,
        database: Database,
        *,
        settings: NaverSyncSettings | None = None,
        token_provider: Callable[..., str] = get_access_token,
        product_fetch: Callable[..., dict[str, Any]] = get_qna_list,
        customer_fetch: Callable[..., dict[str, Any]] = get_customer_inquiries,
        normalizer: InquiryNormalizer | None = None,
        clock: Callable[[], float] = time.monotonic,
        automatic_drafts: AutomaticDraftService | None = None,
    ) -> None:
        self.database = database
        self.settings = settings or NaverSyncSettings.from_environment()
        self.token_provider = token_provider
        self.product_fetch = product_fetch
        self.customer_fetch = customer_fetch
        self.normalizer = normalizer or InquiryNormalizer()
        self.clock = clock
        self.runs = NaverSyncRepository(database)
        self.logs = LogRepository(database)
        self.inquiries = InquiryRepository(database)
        self.sync = InquirySyncService(
            self.inquiries,
            WorkflowRepository(database),
            self.logs,
            automatic_drafts=automatic_drafts,
        )

    @staticmethod
    def _safe_error(
        error: Exception,
        *,
        store_id: str,
        inquiry_type: str | None,
        page: int | None,
    ) -> dict[str, Any]:
        if isinstance(error, NaverSyncError):
            code = error.code
            status_code = error.status_code
            endpoint = error.endpoint
        elif isinstance(error, (TimeoutError,)):
            code, status_code, endpoint = "API_TIMEOUT", None, None
        elif isinstance(error, (ValueError, TypeError)):
            code, status_code, endpoint = "API_RESPONSE_INVALID", None, None
        else:
            code, status_code, endpoint = "UNKNOWN_ERROR", None, None
        return {
            "store_id": store_id,
            "inquiry_type": inquiry_type,
            "page": page,
            "error_code": code,
            "status_code": status_code,
            "endpoint": endpoint,
            "message": ERROR_MESSAGES.get(code, ERROR_MESSAGES["UNKNOWN_ERROR"]),
        }

    def _token(self, store: StoreConfig) -> str:
        kwargs = {
            "store": store,
            "timeout": (
                self.settings.connect_timeout,
                self.settings.read_timeout,
            ),
            "max_retries": min(1, self.settings.max_retries),
            "backoff_seconds": self.settings.retry_backoff_seconds,
        }
        try:
            return self.token_provider(**kwargs)
        except TypeError:
            return self.token_provider(store=store)

    def _fetch_page(
        self,
        inquiry_type: str,
        *,
        token: str,
        page: int,
        from_datetime: datetime,
        to_datetime: datetime,
    ) -> dict[str, Any]:
        common = {
            "access_token": token,
            "days": max(
                1, int((to_datetime - from_datetime).total_seconds() / 86400)
            ),
            "page": page,
            "size": self.settings.page_size,
            "answered": None,
            "timeout": (
                self.settings.connect_timeout,
                self.settings.read_timeout,
            ),
            "max_retries": self.settings.max_retries,
            "backoff_seconds": self.settings.retry_backoff_seconds,
        }
        fetch = (
            self.product_fetch
            if inquiry_type == "PRODUCT_INQUIRY"
            else self.customer_fetch
        )
        if inquiry_type == "PRODUCT_INQUIRY":
            common.update(
                {
                    "from_date": from_datetime,
                    "to_date": to_datetime,
                }
            )
        else:
            common.update(
                {
                    "from_datetime": from_datetime,
                    "to_datetime": to_datetime,
                }
            )
        try:
            return fetch(**common)
        except TypeError as error:
            # Keep injected legacy fetchers usable in tests and scheduler code.
            if not any(
                key in str(error)
                for key in (
                    "from_date",
                    "from_datetime",
                    "to_datetime",
                    "timeout",
                    "max_retries",
                    "backoff_seconds",
                )
            ):
                raise
            legacy = {
                key: common[key]
                for key in (
                    "access_token",
                    "days",
                    "page",
                    "size",
                    "answered",
                )
            }
            if inquiry_type == "PRODUCT_INQUIRY":
                legacy["to_date"] = to_datetime
            return fetch(**legacy)

    def _normalize(
        self,
        inquiry_type: str,
        payload: dict[str, Any],
        *,
        store_code: str,
    ) -> dict[str, Any]:
        normalized = (
            self.normalizer.product(payload, store_code=store_code)
            if inquiry_type == "PRODUCT_INQUIRY"
            else self.normalizer.customer(payload, store_code=store_code)
        )
        return normalized.to_work_item()

    def _assert_runtime(self, started_clock: float) -> None:
        if self.clock() - started_clock > self.settings.max_runtime_seconds:
            raise classified_error("MAX_RUNTIME_EXCEEDED")

    def sync_inquiries(
        self,
        *,
        store_id: str | None = None,
        stores: Sequence[StoreConfig] | None = None,
        inquiry_types: Sequence[str] = SUPPORTED_INQUIRY_TYPES,
        from_datetime: datetime | None = None,
        to_datetime: datetime | None = None,
        sync_type: str = "MANUAL",
        owner_id: str | None = None,
    ) -> NaverInquirySyncResult:
        if not self.settings.enabled:
            raise RuntimeError(
                "네이버 문의 동기화가 비활성화되어 있습니다. "
                "NAVER_SYNC_ENABLED=true로 설정해주세요."
            )
        to_datetime = to_datetime or datetime.now(UTC)
        from_datetime = from_datetime or (
            to_datetime - timedelta(days=self.settings.lookback_days)
        )
        if (
            from_datetime.tzinfo is None
            or to_datetime.tzinfo is None
            or from_datetime >= to_datetime
        ):
            raise classified_error("INVALID_DATE_RANGE")
        requested_types = tuple(
            dict.fromkeys(str(value).upper() for value in inquiry_types)
        )
        invalid_types = set(requested_types).difference(
            SUPPORTED_INQUIRY_TYPES
        )
        if invalid_types or not requested_types:
            raise ValueError(
                "지원하지 않는 inquiry_type: "
                + ", ".join(sorted(invalid_types))
            )
        if stores is not None and store_id is not None:
            raise ValueError("store_id와 stores는 동시에 지정할 수 없습니다.")
        targets = (
            list(stores)
            if stores is not None
            else [get_store_config(store_id)]
            if store_id
            else get_configured_stores()
        )
        if not targets:
            raise ValueError("동기화할 네이버 스토어 설정이 없습니다.")

        sync_id = str(uuid.uuid4())
        started_at = datetime.now(UTC).isoformat(timespec="milliseconds")
        started_clock = self.clock()
        requested_from = from_datetime.isoformat(timespec="seconds")
        requested_to = to_datetime.isoformat(timespec="seconds")
        trace = InquirySyncTrace(self.logs, sync_id)
        store_label = ",".join(store.code for store in targets)
        type_label = ",".join(requested_types)
        self.runs.start(
            sync_id=sync_id,
            store_id=store_label,
            inquiry_type=type_label,
            requested_from=requested_from,
            requested_to=requested_to,
        )
        common = {
            "sync_id": sync_id,
            "store_id": store_label,
            "inquiry_type": type_label,
            "requested_from": requested_from,
            "requested_to": requested_to,
            "sync_type": str(sync_type or "MANUAL").upper(),
        }
        trace.emit("NAVER_SYNC_STARTED", common)

        fetched = inserted = updated = unchanged = skipped = failed = 0
        successful_stores: set[str] = set()
        successful_sources = 0
        errors: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, str, str]] = set()
        acquired_stores: list[str] = []
        lock_skipped_stores: list[str] = []
        try:
            for store in targets:
                try:
                    acquired = self.runs.acquire_lock(
                        store_id=store.code,
                        sync_id=sync_id,
                        ttl_seconds=max(
                            self.settings.lock_ttl_seconds,
                            int(self.settings.max_runtime_seconds) + 30,
                        ),
                        sync_type=sync_type,
                        owner_id=owner_id,
                    )
                except Exception:
                    errors.append(
                        self._safe_error(
                            classified_error("LOCK_FAILED"),
                            store_id=store.code,
                            inquiry_type=None,
                            page=None,
                        )
                    )
                    failed += 1
                    continue
                if not acquired:
                    skipped += 1
                    lock_skipped_stores.append(store.code)
                    trace.emit(
                        "NAVER_SYNC_SKIPPED",
                        {
                            **common,
                            "store_id": store.code,
                            "status": "SKIPPED",
                            "reason": "SYNC_IN_PROGRESS",
                            "skipped_count": skipped,
                            "failed_count": failed,
                        },
                    )
                    continue
                acquired_stores.append(store.code)
                try:
                    token = self._token(store)
                except Exception as error:
                    safe = self._safe_error(
                        error,
                        store_id=store.code,
                        inquiry_type=None,
                        page=None,
                    )
                    if safe["error_code"] == "AUTH_FAILED":
                        safe["error_code"] = "TOKEN_FAILED"
                        safe["message"] = ERROR_MESSAGES["TOKEN_FAILED"]
                    errors.append(safe)
                    failed += 1
                    continue

                store_had_success = False
                reauthenticated = False
                for inquiry_type in requested_types:
                    page = 1
                    previous_signature: tuple[str, ...] | None = None
                    source_failed = False
                    while page <= self.settings.max_pages:
                        self._assert_runtime(started_clock)
                        try:
                            result = self._fetch_page(
                                inquiry_type,
                                token=token,
                                page=page,
                                from_datetime=from_datetime,
                                to_datetime=to_datetime,
                            )
                        except NaverSyncError as error:
                            if error.code == "AUTH_FAILED" and not reauthenticated:
                                reauthenticated = True
                                try:
                                    token = self._token(store)
                                    continue
                                except Exception as token_error:
                                    error = classified_error(
                                        "TOKEN_FAILED",
                                        status_code=getattr(
                                            token_error, "status_code", None
                                        ),
                                    )
                            safe = self._safe_error(
                                error,
                                store_id=store.code,
                                inquiry_type=inquiry_type,
                                page=page,
                            )
                            errors.append(safe)
                            failed += 1
                            source_failed = True
                            break
                        except Exception as error:
                            safe = self._safe_error(
                                error,
                                store_id=store.code,
                                inquiry_type=inquiry_type,
                                page=page,
                            )
                            errors.append(safe)
                            failed += 1
                            source_failed = True
                            break

                        content_key = (
                            "contents"
                            if inquiry_type == "PRODUCT_INQUIRY"
                            else "content"
                        )
                        contents = result.get(content_key)
                        if not isinstance(contents, list):
                            errors.append(
                                self._safe_error(
                                    classified_error(
                                        "API_RESPONSE_INVALID"
                                    ),
                                    store_id=store.code,
                                    inquiry_type=inquiry_type,
                                    page=page,
                                )
                            )
                            failed += 1
                            source_failed = True
                            break
                        fetched += len(contents)
                        id_fields = (
                            ("questionId",)
                            if inquiry_type == "PRODUCT_INQUIRY"
                            else ("inquiryNo", "inquiryId")
                        )
                        signature = tuple(
                            str(
                                next(
                                    (
                                        item.get(name)
                                        for name in id_fields
                                        if isinstance(item, dict)
                                        and item.get(name) not in (None, "")
                                    ),
                                    f"invalid-{index}",
                                )
                            )
                            for index, item in enumerate(contents)
                        )
                        if contents and signature == previous_signature:
                            errors.append(
                                self._safe_error(
                                    classified_error(
                                        "PAGINATION_FAILED"
                                    ),
                                    store_id=store.code,
                                    inquiry_type=inquiry_type,
                                    page=page,
                                )
                            )
                            failed += 1
                            source_failed = True
                            break
                        previous_signature = signature
                        page_items: list[dict[str, Any]] = []
                        for payload in contents:
                            if not isinstance(payload, dict):
                                failed += 1
                                errors.append(
                                    self._safe_error(
                                        classified_error(
                                            "NORMALIZATION_FAILED"
                                        ),
                                        store_id=store.code,
                                        inquiry_type=inquiry_type,
                                        page=page,
                                    )
                                )
                                continue
                            try:
                                item = self._normalize(
                                    inquiry_type,
                                    payload,
                                    store_code=store.code,
                                )
                                key = (
                                    store.code,
                                    inquiry_type,
                                    str(item["external_inquiry_id"]),
                                )
                                if key in seen_keys:
                                    skipped += 1
                                    continue
                                seen_keys.add(key)
                                page_items.append(item)
                            except Exception:
                                failed += 1
                                errors.append(
                                    self._safe_error(
                                        classified_error(
                                            "NORMALIZATION_FAILED"
                                        ),
                                        store_id=store.code,
                                        inquiry_type=inquiry_type,
                                        page=page,
                                    )
                                )
                        def emit_item_event(
                            event_code: str,
                            details: dict[str, Any] | None = None,
                            *,
                            level: str = "INFO",
                            persist: bool = True,
                        ) -> None:
                            trace.emit(
                                event_code,
                                {
                                    **common,
                                    "store_id": store.code,
                                    "inquiry_type": inquiry_type,
                                    "page": page,
                                    "fetched_count": fetched,
                                    "inserted_count": inserted,
                                    "updated_count": updated,
                                    "unchanged_count": unchanged,
                                    "skipped_count": skipped,
                                    "failed_count": failed,
                                    "duration_ms": int(
                                        (self.clock() - started_clock) * 1000
                                    ),
                                    "status": "RUNNING",
                                    "error_code": None,
                                    **(details or {}),
                                },
                                level=level,
                                persist=persist,
                            )

                        page_result = self.sync.sync(
                            page_items,
                            correlation_id=sync_id,
                            event_callback=emit_item_event,
                        )
                        inserted += page_result["new"]
                        updated += page_result["updated"]
                        unchanged += page_result["unchanged"]
                        failed += page_result["failed"]
                        if page_result["failed"]:
                            errors.append(
                                self._safe_error(
                                    classified_error("DB_WRITE_FAILED"),
                                    store_id=store.code,
                                    inquiry_type=inquiry_type,
                                    page=page,
                                )
                            )
                            source_failed = True
                        trace.emit(
                            "NAVER_SYNC_PAGE_FETCHED",
                            {
                                **common,
                                "store_id": store.code,
                                "inquiry_type": inquiry_type,
                                "page": page,
                                "fetched_count": len(contents),
                                "inserted_count": page_result["new"],
                                "updated_count": page_result["updated"],
                                "unchanged_count": page_result["unchanged"],
                                "skipped_count": skipped,
                                "failed_count": page_result["failed"],
                                "status": "SUCCESS",
                            },
                        )
                        store_had_success = True
                        total_pages = int(result.get("totalPages") or 1)
                        if (
                            not contents
                            or bool(result.get("last"))
                            or page >= total_pages
                        ):
                            break
                        page += 1
                    if page > self.settings.max_pages:
                        errors.append(
                            self._safe_error(
                                classified_error("PAGINATION_FAILED"),
                                store_id=store.code,
                                inquiry_type=inquiry_type,
                                page=page,
                            )
                        )
                        failed += 1
                        source_failed = True
                    if not source_failed:
                        successful_sources += 1
                if store_had_success:
                    successful_stores.add(store.code)
        except Exception as error:
            errors.append(
                self._safe_error(
                    error,
                    store_id=store_label,
                    inquiry_type=None,
                    page=None,
                )
            )
            failed += 1
        finally:
            self.runs.release_locks(sync_id)

        duration_ms = max(0, int((self.clock() - started_clock) * 1000))
        expected_sources = len(targets) * len(requested_types)
        if errors:
            persisted_count = inserted + updated + unchanged
            status = (
                "PARTIAL_SYNC"
                if successful_sources or persisted_count
                else "FAILED"
            )
        elif lock_skipped_stores and not acquired_stores:
            status = "SKIPPED"
        else:
            status = "SUCCESS"
        error_code = (
            errors[0]["error_code"]
            if errors
            else "SYNC_IN_PROGRESS"
            if status == "SKIPPED"
            else None
        )
        error_message = (
            errors[0]["message"]
            if errors
            else ERROR_MESSAGES["SYNC_IN_PROGRESS"]
            if status == "SKIPPED"
            else None
        )
        details = {
            **common,
            "successful_source_count": successful_sources,
            "expected_source_count": expected_sources,
            "skipped_store_count": len(lock_skipped_stores),
            "skipped_stores": lock_skipped_stores,
            "errors": errors,
        }
        self.runs.finish(
            sync_id,
            status=status,
            fetched_count=fetched,
            inserted_count=inserted,
            updated_count=updated,
            unchanged_count=unchanged,
            skipped_count=skipped,
            failed_count=failed,
            duration_ms=duration_ms,
            error_code=error_code,
            error_message=error_message,
            details=details,
        )
        completed_at = datetime.now(UTC).isoformat(timespec="milliseconds")
        final_details = {
            **common,
            "fetched_count": fetched,
            "inserted_count": inserted,
            "updated_count": updated,
            "unchanged_count": unchanged,
            "skipped_count": skipped,
            "failed_count": failed,
            "duration_ms": duration_ms,
            "status": status,
            "error_code": error_code,
        }
        if status == "SUCCESS":
            trace.emit("NAVER_SYNC_COMPLETED", final_details)
        elif status == "SKIPPED":
            trace.emit("NAVER_SYNC_SKIPPED", final_details)
        elif status == "PARTIAL_SYNC":
            trace.emit(
                "NAVER_SYNC_PARTIAL_FAILURE",
                final_details,
                level="WARNING",
            )
        else:
            trace.emit("NAVER_SYNC_FAILED", final_details, level="ERROR")
        return NaverInquirySyncResult(
            sync_id=sync_id,
            status=status,
            requested_store_count=len(targets),
            successful_store_count=len(successful_stores),
            inquiry_types=requested_types,
            requested_from=requested_from,
            requested_to=requested_to,
            fetched_count=fetched,
            inserted_count=inserted,
            updated_count=updated,
            unchanged_count=unchanged,
            skipped_count=skipped,
            failed_count=failed,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=duration_ms,
            error_code=error_code,
            error_message=error_message,
            errors=tuple(errors),
        )
