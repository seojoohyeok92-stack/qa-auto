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


# Q&A Auto owns Smart Store's 상품문의 board only.  고객문의 is a separate
# Naver endpoint and must never enter this ingestion service: accepting it here
# would create an Inquiry row and enqueue the entire answer/auto-post pipeline.
SUPPORTED_INQUIRY_TYPES = ("PRODUCT_INQUIRY",)

# Source-side deletion tracking.
#
# Naver's 상품문의 list response carries no status or deleted field -- across
# every payload this service has ever stored, the normalizer's whitelisted
# ``status`` key has never once been populated.  Absence from an otherwise
# complete response is therefore the only observable signal, which makes the
# question "when is absence trustworthy?" the whole of the design.
DELETION_TRACKED_INQUIRY_TYPE = "PRODUCT_INQUIRY"

# Three consecutive authoritative syncs.  Measured against the four inquiries
# that vanished together on the production store: they stayed absent for
# 20.5h, and the two before them for 86.8h and 136.7h -- hundreds of ten-minute
# runs each.  Three costs half an hour of latency and removes every
# single-response flicker from consideration.
SOURCE_DELETION_STREAK_THRESHOLD = 3

# Keep away from the lower edge of the requested window.  ``fromDate`` filters
# on the inquiry's own creation time, so a row drifts out of range on its own:
# two inquiries looked "missing" on the production store until their KST
# timestamps were converted, at which point both sat 1 and 8 minutes *outside*
# the window they were last seen in.  A row inside this margin is simply left
# alone for one more run -- the margin can only ever prevent a mark.
_SOURCE_DELETION_WINDOW_MARGIN = timedelta(hours=1)

# How far back the coarse SQL prefilter reaches.  ``source_created_at`` keeps
# the marketplace's own UTC offset, so its date prefix can sit a day either
# side of the UTC window; two days is slack, and exact membership is decided
# on parsed datetimes afterwards.
_SOURCE_DELETION_PREFILTER_SLACK = timedelta(days=2)


def _as_utc(value: Any) -> datetime | None:
    """Parse a stored source timestamp into UTC, or give up.

    Giving up matters: a row whose creation time cannot be read has no
    provable place in the requested window, and is never a deletion candidate.
    """

    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class _SourceObservation:
    """What one (store, inquiry type) actually returned in one sync.

    Kept per source rather than per run so an id collected for one store can
    never count as presence for another -- production runs a single Naver
    store today, and this is the structure that keeps that from mattering.
    """

    store_code: str
    inquiry_type: str
    seen_external_ids: frozenset[str]
    fetched_count: int


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

    def _reconcile_source_deletions(
        self,
        observations: Sequence[_SourceObservation],
        *,
        from_datetime: datetime,
        to_datetime: datetime,
    ) -> dict[str, int]:
        """Compare tracked rows against what an authoritative sync returned.

        Only reached when the whole run finished SUCCESS, and only ever looks
        at rows this build collected itself (``source_deletion_tracked = 1``).
        Rows that predate the feature are not queried, not counted and not
        marked: the back catalogue is out of scope by construction, not by a
        flag that could be flipped later.

        Nothing here writes answer, approval, Learning or post history.  The
        four source-state columns are the entire blast radius.
        """

        summary = {
            "observed_count": 0,
            "missing_count": 0,
            "deleted_count": 0,
            "restored_count": 0,
        }
        window_start = from_datetime + _SOURCE_DELETION_WINDOW_MARGIN
        prefilter = (
            (from_datetime - _SOURCE_DELETION_PREFILTER_SLACK)
            .date()
            .isoformat()
        )
        for observation in observations:
            if observation.inquiry_type != DELETION_TRACKED_INQUIRY_TYPE:
                continue
            # An empty response is not evidence that everything is gone.  The
            # authoritative-run gate already refuses one, and this repeats the
            # condition at the point the writes happen.
            if observation.fetched_count <= 0:
                continue
            candidates = self.inquiries.source_deletion_candidates(
                store_code=observation.store_code,
                source_type=observation.inquiry_type,
                created_on_or_after=prefilter,
            )
            observed: list[int] = []
            missing: list[int] = []
            for row in candidates:
                created = _as_utc(
                    row.get("source_created_at") or row.get("registered_at")
                )
                if created is None:
                    continue
                if not window_start <= created <= to_datetime:
                    continue
                external = str(
                    row.get("external_inquiry_id")
                    or row.get("source_question_id")
                    or ""
                )
                if not external:
                    continue
                inquiry_id = int(row["id"])
                if external in observation.seen_external_ids:
                    # Only write when there is something to undo, so a steady
                    # state costs no UPDATE at all.
                    if (
                        int(row.get("source_missing_streak") or 0)
                        or int(row.get("source_deleted") or 0)
                    ):
                        observed.append(inquiry_id)
                else:
                    missing.append(inquiry_id)
            summary["observed_count"] += len(observed)
            summary["missing_count"] += len(missing)
            if observed:
                restored = self.inquiries.clear_source_deleted(
                    observed, source_type=observation.inquiry_type
                )
                summary["restored_count"] += len(restored)
                for inquiry_id in restored:
                    self.logs.record_inquiry(
                        inquiry_id,
                        "SOURCE_DELETION_CLEARED",
                        "네이버 원본에서 다시 조회되어 미조회 상태를 해제했습니다.",
                        details={
                            "store_code": observation.store_code,
                            "source": observation.inquiry_type,
                            "network_call_count": 0,
                        },
                    )
            if not missing:
                continue
            streaks = self.inquiries.increment_source_missing_streak(
                missing, source_type=observation.inquiry_type
            )
            confirmed = [
                inquiry_id
                for inquiry_id, streak in streaks.items()
                if streak >= SOURCE_DELETION_STREAK_THRESHOLD
            ]
            if not confirmed:
                continue
            detected_at = datetime.now(UTC).isoformat(timespec="milliseconds")
            changed = self.inquiries.mark_source_deleted(
                confirmed,
                source_type=observation.inquiry_type,
                detected_at=detected_at,
            )
            summary["deleted_count"] += len(changed)
            # ``mark_source_deleted`` returns only the 0 -> 1 transitions, so
            # a row already known to be deleted is not logged again on every
            # ten-minute run.
            for inquiry_id in changed:
                self.logs.record_inquiry(
                    inquiry_id,
                    "SOURCE_DELETION_DETECTED",
                    "네이버 원본에서 연속 조회되지 않아 미조회 문의로 확정했습니다.",
                    level="WARNING",
                    details={
                        "store_code": observation.store_code,
                        "source": observation.inquiry_type,
                        "missing_streak": int(streaks.get(inquiry_id) or 0),
                        "detected_at": detected_at,
                        "network_call_count": 0,
                    },
                )
        return summary

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
        source_observations: list[_SourceObservation] = []
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
                    # Per (store, inquiry type), never shared across the run:
                    # presence for this store must be decided only by what
                    # this store's own pages returned.
                    source_seen_ids: set[str] = set()
                    source_fetched = 0
                    source_normalization_failed = False
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
                        source_fetched += len(contents)
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
                                source_normalization_failed = True
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
                                external_id = str(item["external_inquiry_id"])
                                # Presence is recorded before the duplicate
                                # check: an id that appears twice in one
                                # response was still returned by the API.
                                source_seen_ids.add(external_id)
                                key = (
                                    store.code,
                                    inquiry_type,
                                    external_id,
                                )
                                if key in seen_keys:
                                    skipped += 1
                                    continue
                                seen_keys.add(key)
                                page_items.append(item)
                            except Exception:
                                failed += 1
                                # An item that could not be normalized never
                                # reaches the seen set, so it would read as
                                # absent.  Disqualify the whole source instead
                                # of guessing which rows the gap belongs to.
                                source_normalization_failed = True
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
                        inserted_inquiry_ids: list[int] = []

                        def emit_item_event(
                            event_code: str,
                            details: dict[str, Any] | None = None,
                            *,
                            level: str = "INFO",
                            persist: bool = True,
                        ) -> None:
                            # ``InquirySyncService`` already announces each
                            # item's upsert outcome here.  Reading "new" off
                            # that existing contract is how this service knows
                            # which rows it collected itself, without changing
                            # the shared sync layer Coupang also writes through.
                            if event_code == "NAVER_SYNC_ITEM_INSERTED":
                                new_id = (details or {}).get("inquiry_id")
                                if new_id is not None:
                                    inserted_inquiry_ids.append(int(new_id))
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
                        if (
                            inserted_inquiry_ids
                            and inquiry_type == DELETION_TRACKED_INQUIRY_TYPE
                        ):
                            # Tracking begins the moment an inquiry is first
                            # inserted, and only then.  An existing row that
                            # merely reappears in a response is not promoted:
                            # "from here on" means the rows this build
                            # collected, not every row the API still returns.
                            self.inquiries.start_source_deletion_tracking(
                                inserted_inquiry_ids,
                                source_type=inquiry_type,
                            )
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
                        # The authoritative-source gate.  Absence is only
                        # evidence when this (store, type) fetched every page
                        # without an API, pagination or DB error, normalized
                        # every item it received, and received something at
                        # all.  Anything less and no observation is recorded,
                        # so no row's streak can move.
                        if (
                            not source_normalization_failed
                            and source_fetched > 0
                        ):
                            source_observations.append(
                                _SourceObservation(
                                    store_code=store.code,
                                    inquiry_type=inquiry_type,
                                    seen_external_ids=frozenset(
                                        source_seen_ids
                                    ),
                                    fetched_count=source_fetched,
                                )
                            )
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
        # Deletion reconciliation runs here and nowhere else: after the run's
        # own verdict is known, and only on a clean one.  A per-source check
        # alone would let a run that ended PARTIAL_SYNC still write deletion
        # state for the sources that happened to succeed; requiring SUCCESS
        # as well is the stricter reading, and the one that fails closed.
        deletion_summary: dict[str, int] | None = None
        if status == "SUCCESS" and source_observations:
            try:
                deletion_summary = self._reconcile_source_deletions(
                    source_observations,
                    from_datetime=from_datetime,
                    to_datetime=to_datetime,
                )
            except Exception as error:
                # Collection already succeeded and is committed.  A failure in
                # the bookkeeping that follows must not turn a good sync into
                # a reported failure, so it is logged and the run stands.
                deletion_summary = None
                self.logs.record_system(
                    "SOURCE_DELETION_RECONCILE_FAILED",
                    "삭제 상태 동기화 단계에서 오류가 발생했지만 문의 수집은 완료되었습니다.",
                    level="WARNING",
                    details={
                        "sync_id": sync_id,
                        "exception_type": error.__class__.__name__,
                    },
                )
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
            **(
                {"source_deletion": deletion_summary}
                if deletion_summary
                else {}
            ),
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
