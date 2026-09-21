"""Operational Coupang Online Inquiry sync.

The read path is the whole of what this service does itself:

    CoupangReadClient.list_online_inquiries(answered_type="ALL")
      -> CoupangInquiryNormalizer.online(payload, account_code=...)
      -> to_work_item() -> normalize_work_item()
      -> InquiryRepository.upsert_work_item()

``InquirySyncService.sync`` is still deliberately not used.  Around the same
upsert it records a Naver posted answer, runs AutomaticDraftService inline and
writes Naver-shaped activity logs, none of which belongs here.

What it does share is the one durable handover: a newly collected inquiry is
announced on ``auto_sync_events``, the same outbox the Naver sync writes and
the same one ``naver_auto_post_scheduler`` drains.  Without it a Coupang row
was invisible to automatic processing no matter what the market gates said --
nothing ever offered it to a scheduler.  Announcing it is not deciding it:
every gate downstream (automatic generation, eligibility, auto post) still
answers for itself, and a market that may not be posted to simply never leaves
the queue.

Each seller account is its own identity (``COUPANG_OJE_NS`` /
``COUPANG_OJE_PLUS``): inquiry ids are account-local, and a row carries its
account in ``source_metadata_json`` so it joins that account's catalogue only.

This runs inside the Naver auto sync cycle (see ``naver_auto_sync_scheduler``);
the account loop keeps one account's failure from stopping the other.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterator

from api.coupang_read_client import (
    ONLINE_INQUIRIES_PATH,
    CoupangReadClient,
    CoupangReadError,
)
from config import COUPANG_OJE_NS, COUPANG_OJE_PLUS, get_coupang_account
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.coupang_inquiry_normalizer import (
    COUPANG_ONLINE_INQUIRY,
    CoupangInquiryNormalizer,
    coupang_store_code,
)
from services.inquiry_sync_service import normalize_work_item


KST = timezone(timedelta(hours=9))

ACCOUNTS: tuple[str, ...] = (COUPANG_OJE_NS, COUPANG_OJE_PLUS)
# New, still-unanswered and answered inquiries alike -- the client accepts
# ALL, ANSWERED and NOANSWER.
ANSWERED_TYPE = "ALL"
PAGE_SIZE = 50
# Start and end at most six days apart: seven calendar days.  Seven apart passes
# the client's own check and is refused by Coupang with HTTP 400.
WINDOW_SPAN_DAYS = 6
# Every cycle reads the last seven days: new inquiries and recent answers.
RECENT_DAYS = 7
# The API filters by when an inquiry was asked, so an answer given after its
# seven days would never be read again.  A cycle therefore reaches back to the
# oldest inquiry still stored as unanswered -- but no further than this.
UNANSWERED_RECHECK_DAYS = 30


def kst_today() -> date:
    return datetime.now(KST).date()


def date_windows(start: date, end: date) -> Iterator[tuple[date, date]]:
    if end < start:
        raise ValueError("end_date must not be earlier than start_date")
    cursor = start
    while cursor <= end:
        window_end = min(cursor + timedelta(days=WINDOW_SPAN_DAYS), end)
        yield cursor, window_end
        cursor = window_end + timedelta(days=1)


@dataclass
class CoupangInquirySyncResult:
    account_code: str
    start_date: date | None = None
    end_date: date | None = None
    http_requests: int = 0
    fetched: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0
    # Newly collected inquiries handed to the shared auto-processing
    # outbox, and the ones the handover itself could not record.
    announced: int = 0
    announce_failed: int = 0
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def log_line(self) -> str:
        line = (
            f"COUPANG_SYNC {self.account_code} "
            f"range={self.start_date}~{self.end_date} "
            f"requests={self.http_requests} fetched={self.fetched} "
            f"new={self.new} updated={self.updated} "
            f"unchanged={self.unchanged} failed={self.failed}"
        )
        return f"{line} error={self.error}" if self.error else line


def _client_for_account(account_code: str) -> CoupangReadClient:
    account = get_coupang_account(account_code)
    return CoupangReadClient(
        access_key=account.access_key,
        secret_key=account.secret_key,
        vendor_id=account.vendor_id,
    )


def _describe(error: Exception) -> str:
    """Error text safe for a console: never credentials, never the vendor id."""

    if isinstance(error, CoupangReadError):
        return (
            f"{error.code} status={error.status_code} "
            f"endpoint={ONLINE_INQUIRIES_PATH}"
        )
    return error.__class__.__name__


class CoupangInquirySyncService:
    def __init__(
        self,
        database: Database,
        *,
        client_factory: Callable[[str], CoupangReadClient] = _client_for_account,
        normalizer: CoupangInquiryNormalizer | None = None,
        today: Callable[[], date] = kst_today,
    ) -> None:
        self.database = database
        self.inquiries = InquiryRepository(database)
        self.client_factory = client_factory
        self.normalizer = normalizer or CoupangInquiryNormalizer()
        self.today = today

    def lookback_range(self, account_code: str) -> tuple[date, date]:
        """The last seven days, stretched back to the oldest open inquiry.

        Only as far as ``UNANSWERED_RECHECK_DAYS``: with nothing unanswered it
        is one window, and an inquiry abandoned for months cannot make every
        cycle re-read its whole history.
        """

        end = self.today()
        start = end - timedelta(days=RECENT_DAYS - 1)
        floor = end - timedelta(days=UNANSWERED_RECHECK_DAYS - 1)
        with self.database.connection() as connection:
            oldest = connection.execute(
                "SELECT MIN(substr(source_created_at, 1, 10)) FROM inquiries "
                "WHERE store_code = ? AND source_type = ? "
                "AND source_answered = 0 "
                "AND substr(source_created_at, 1, 10) >= ?",
                (
                    coupang_store_code(account_code),
                    COUPANG_ONLINE_INQUIRY,
                    floor.isoformat(),
                ),
            ).fetchone()[0]
        if oldest:
            try:
                start = min(start, date.fromisoformat(str(oldest)))
            except ValueError:
                pass
        return start, end

    def sync_account(
        self,
        account_code: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> CoupangInquirySyncResult:
        """Sync one account.  A failed request raises; a bad item is counted."""

        if start_date is None or end_date is None:
            default_start, default_end = self.lookback_range(account_code)
            start_date = start_date or default_start
            end_date = end_date or default_end
        result = CoupangInquirySyncResult(
            account_code=account_code, start_date=start_date, end_date=end_date
        )
        client = self.client_factory(account_code)
        for window_start, window_end in date_windows(start_date, end_date):
            page = 1
            while True:
                result.http_requests += 1
                payload = client.list_online_inquiries(
                    inquiry_start_at=window_start,
                    inquiry_end_at=window_end,
                    answered_type=ANSWERED_TYPE,
                    page_num=page,
                    page_size=PAGE_SIZE,
                )
                content, total_pages = self._page(payload)
                for item in content:
                    self._persist(account_code, item, result)
                if page >= total_pages:
                    break
                page += 1
        return result

    def sync_accounts(
        self, accounts: tuple[str, ...] = ACCOUNTS
    ) -> list[CoupangInquirySyncResult]:
        """One cycle.  Each account is tried; a failure is kept to its account."""

        results: list[CoupangInquirySyncResult] = []
        for account_code in accounts:
            try:
                result = self.sync_account(account_code)
            except Exception as error:
                result = CoupangInquirySyncResult(
                    account_code=account_code, error=_describe(error)
                )
            # One line per account on the server console.  Printed rather than
            # logged: nothing configures a console handler for service loggers,
            # so an INFO record would never be seen.
            print(result.log_line(), flush=True)
            results.append(result)
        return results

    @staticmethod
    def _page(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("Coupang response is missing data object")
        content = data.get("content")
        pagination = data.get("pagination")
        if not isinstance(content, list) or not isinstance(pagination, dict):
            raise ValueError("Coupang response pagination is invalid")
        total_pages = int(pagination.get("totalPages") or 1)
        if total_pages < 1:
            raise ValueError("Coupang response totalPages is invalid")
        return [item for item in content if isinstance(item, dict)], total_pages

    def _persist(
        self,
        account_code: str,
        payload: dict[str, Any],
        result: CoupangInquirySyncResult,
    ) -> None:
        result.fetched += 1
        try:
            work_item = self.normalizer.online(
                payload, account_code=account_code
            ).to_work_item()
            ready = normalize_work_item(work_item)
            if ready["store_code"] != coupang_store_code(account_code):
                raise ValueError("normalized store_code does not match the account")
            upsert = self.inquiries.upsert_work_item(ready)
            setattr(result, upsert.outcome, getattr(result, upsert.outcome) + 1)
            if upsert.created:
                self._announce(upsert.inquiry_id, ready, result)
        except Exception:
            result.failed += 1

    def _announce(
        self,
        inquiry_id: int,
        ready: dict[str, Any],
        result: CoupangInquirySyncResult,
    ) -> None:
        """Put a newly collected inquiry on the shared auto-processing outbox.

        Only a genuinely new row, so a re-sync of the same inquiry announces
        nothing and cannot queue it twice.  Isolated from the sync the way the
        Naver side isolates it: a queue that cannot be written must never fail
        the collection that succeeded.
        """

        try:
            from repositories.auto_post_event_repository import (
                AutoPostEventRepository,
            )
            from repositories.auto_post_repository import AutoPostRepository

            runtime_enabled = bool(
                AutoPostRepository(self.inquiries.database)
                .settings()
                .get("runtime_auto_post_enabled")
            )
            event = AutoPostEventRepository(self.inquiries.database).create(
                inquiry_id=int(inquiry_id),
                store_code=str(ready.get("store_code") or ""),
                external_id=str(
                    ready.get("external_inquiry_id")
                    or ready.get("source_question_id")
                    or ""
                ),
                source_sync_id=None,
                runtime_enabled=runtime_enabled,
            )
            if event is not None:
                result.announced += 1
        except Exception:  # noqa: BLE001 - the inquiry is collected either way
            result.announce_failed += 1


def run_operational_sync(database: Database) -> list[CoupangInquirySyncResult]:
    """The scheduler's entry: both accounts, default lookback, never raises."""

    return CoupangInquirySyncService(database).sync_accounts()
