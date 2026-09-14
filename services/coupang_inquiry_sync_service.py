"""Read-only Coupang inquiry synchronization using existing persistence."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Any

from api.coupang_read_client import CoupangReadClient
from services.coupang_inquiry_normalizer import CoupangInquiryNormalizer
from services.inquiry_sync_service import InquirySyncService


@dataclass
class CoupangInquirySyncResult:
    fetched: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0
    online_pages: int = 0
    contact_center_pages: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


class CoupangInquirySyncService:
    """Fetch both documented inquiry feeds and persist through InquirySyncService.

    The caller supplies explicit dates; incremental scheduling/checkpointing is
    intentionally outside Phase 1-A.
    """

    def __init__(
        self,
        read_client: CoupangReadClient,
        inquiry_sync: InquirySyncService,
        normalizer: CoupangInquiryNormalizer | None = None,
    ) -> None:
        self.read_client = read_client
        self.inquiry_sync = inquiry_sync
        self.normalizer = normalizer or CoupangInquiryNormalizer()

    def sync_inquiries(
        self,
        *,
        start_date: date,
        end_date: date,
        include_contact_center: bool = True,
    ) -> CoupangInquirySyncResult:
        if end_date < start_date:
            raise ValueError("end_date must not be earlier than start_date")
        result = CoupangInquirySyncResult()
        for window_start, window_end in self._date_chunks(start_date, end_date):
            self._sync_online_window(window_start, window_end, result)
            if include_contact_center:
                self._sync_contact_window(window_start, window_end, result)
        return result

    @staticmethod
    def _date_chunks(start_date: date, end_date: date):
        current = start_date
        while current <= end_date:
            window_end = min(current + timedelta(days=7), end_date)
            yield current, window_end
            current = window_end + timedelta(days=1)

    def _sync_online_window(
        self,
        start_date: date,
        end_date: date,
        result: CoupangInquirySyncResult,
    ) -> None:
        page = 1
        while True:
            payload = self.read_client.list_online_inquiries(
                inquiry_start_at=start_date,
                inquiry_end_at=end_date,
                page_num=page,
                page_size=50,
                answered_type="ALL",
            )
            content, total_pages = self._page(payload)
            self._persist(content, self.normalizer.online, result)
            result.online_pages += 1
            if page >= total_pages:
                return
            page += 1

    def _sync_contact_window(
        self,
        start_date: date,
        end_date: date,
        result: CoupangInquirySyncResult,
    ) -> None:
        page = 1
        while True:
            payload = self.read_client.list_contact_center_inquiries(
                inquiry_start_at=start_date,
                inquiry_end_at=end_date,
                page_num=page,
                page_size=30,
                partner_counseling_status="NONE",
            )
            content, total_pages = self._page(payload)
            self._persist(content, self.normalizer.contact_center, result)
            result.contact_center_pages += 1
            if page >= total_pages:
                return
            page += 1

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
        content: list[dict[str, Any]],
        normalize: Any,
        result: CoupangInquirySyncResult,
    ) -> None:
        items = [normalize(payload).to_work_item() for payload in content]
        persisted = self.inquiry_sync.sync(items)
        result.fetched += len(content)
        result.new += persisted["new"]
        result.updated += persisted["updated"]
        result.unchanged += persisted["unchanged"]
        result.failed += persisted["failed"]
