"""Historical Coupang Online Inquiry import into review-only cases.

This service is deliberately separate from the live inquiry synchronizer.  A
backfill must not enqueue AnswerService work, create post events, or call any
write API at Coupang/Naver.  It reads answered Online Inquiries, persists their
common Inquiry provenance directly, then creates *disabled* Historical Case
candidates only when their exact mapped model is currently operated.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Any, Callable

from api.coupang_read_client import CoupangReadClient
from config import CoupangAccountSettings, get_coupang_account, get_coupang_accounts
from repositories.coupang_product_catalog_repository import CoupangProductCatalogRepository
from repositories.coupang_product_mapping_repository import CoupangProductMappingRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.coupang_inquiry_normalizer import CoupangInquiryNormalizer
from services.historical_case_service import HistoricalCaseService
from services.historical_learning_quality_service import classify_historical_candidate


HISTORICAL_START_DATE = date(2024, 9, 1)
ONLINE_INQUIRY_TYPE = "COUPANG_ONLINE_INQUIRY"

ReadClientFactory = Callable[[CoupangAccountSettings], CoupangReadClient]


@dataclass
class CoupangHistoricalBackfillResult:
    account_code: str
    fetched: int = 0
    pages: int = 0
    inquiries_new: int = 0
    inquiries_updated: int = 0
    inquiries_unchanged: int = 0
    candidates_inserted: int = 0
    candidates_updated: int = 0
    candidates_duplicate: int = 0
    skipped_no_answer: int = 0
    skipped_multi_comment_review: int = 0
    skipped_unmapped: int = 0
    skipped_not_currently_operated: int = 0
    skipped_learning_excluded: int = 0
    manual_review_candidates: int = 0
    failed: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CoupangHistoricalInquiryBackfillService:
    """Backfill answered Online Inquiry records without activating Learning."""

    def __init__(
        self,
        database: Database,
        *,
        client_factory: ReadClientFactory | None = None,
        normalizer: CoupangInquiryNormalizer | None = None,
        inquiry_repository: InquiryRepository | None = None,
        historical_cases: HistoricalCaseService | None = None,
        catalog_repository: CoupangProductCatalogRepository | None = None,
        mapping_repository: CoupangProductMappingRepository | None = None,
    ) -> None:
        self.database = database
        self.client_factory = client_factory or self._client_for_account
        self.normalizer = normalizer or CoupangInquiryNormalizer()
        self.inquiries = inquiry_repository or InquiryRepository(database)
        self.historical_cases = historical_cases or HistoricalCaseService(database)
        self.catalog = catalog_repository or CoupangProductCatalogRepository(database)
        self.mappings = mapping_repository or CoupangProductMappingRepository(database)

    @staticmethod
    def _client_for_account(account: CoupangAccountSettings) -> CoupangReadClient:
        return CoupangReadClient(
            access_key=account.access_key,
            secret_key=account.secret_key,
            vendor_id=account.vendor_id,
        )

    @staticmethod
    def _date_chunks(start_date: date, end_date: date):
        cursor = start_date
        while cursor <= end_date:
            window_end = min(cursor + timedelta(days=6), end_date)
            yield cursor, window_end
            cursor = window_end + timedelta(days=1)

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
        return [row for row in content if isinstance(row, dict)], total_pages

    def backfill_configured_accounts(
        self,
        *,
        start_date: date = HISTORICAL_START_DATE,
        end_date: date | None = None,
    ) -> list[CoupangHistoricalBackfillResult]:
        return [
            self.backfill_account(
                account.account_code, start_date=start_date, end_date=end_date
            )
            for account in get_coupang_accounts()
        ]

    def backfill_account(
        self,
        account_code: str,
        *,
        start_date: date = HISTORICAL_START_DATE,
        end_date: date | None = None,
    ) -> CoupangHistoricalBackfillResult:
        finish = end_date or date.today()
        if start_date > finish:
            raise ValueError("start_date must not be later than end_date")
        account = get_coupang_account(account_code)
        result = CoupangHistoricalBackfillResult(account_code=account.account_code)
        client = self.client_factory(account)
        for window_start, window_end in self._date_chunks(start_date, finish):
            page = 1
            while True:
                payload = client.list_online_inquiries(
                    inquiry_start_at=window_start,
                    inquiry_end_at=window_end,
                    answered_type="ANSWERED",
                    page_num=page,
                    page_size=50,
                )
                content, total_pages = self._page(payload)
                result.pages += 1
                for payload_item in content:
                    self._save_item(account.account_code, payload_item, result)
                if page >= total_pages:
                    break
                page += 1
        return result

    def _save_item(
        self,
        account_code: str,
        payload: dict[str, Any],
        result: CoupangHistoricalBackfillResult,
    ) -> None:
        result.fetched += 1
        try:
            normalized = self.normalizer.online(payload, account_code=account_code)
            work_item = normalized.to_work_item()
            upsert = self.inquiries.upsert_work_item(work_item)
            setattr(
                result,
                f"inquiries_{upsert.outcome}",
                getattr(result, f"inquiries_{upsert.outcome}") + 1,
            )

            if normalized.answer_selection_status == "MULTI_COMMENT_REVIEW":
                result.skipped_multi_comment_review += 1
                return
            if not normalized.seller_answer:
                result.skipped_no_answer += 1
                return

            candidate_gate = classify_historical_candidate(
                normalized.content, normalized.seller_answer
            )
            if candidate_gate.decision == "EXCLUDE":
                result.skipped_learning_excluded += 1
                return

            vendor_item_id = str(payload.get("vendorItemId") or "").strip()
            mapping = (
                self.mappings.get_confirmed(
                    account_code=account_code, vendor_item_id=vendor_item_id
                )
                if vendor_item_id else None
            )
            if not mapping or not mapping.get("canonical_model"):
                result.skipped_unmapped += 1
                return
            canonical_model = str(mapping["canonical_model"])
            operation_source = self._operation_source(canonical_model)
            if operation_source is None:
                result.skipped_not_currently_operated += 1
                return

            candidate = dict(work_item)
            candidate.update(
                {
                    "local_inquiry_id": upsert.inquiry_id,
                    "seller_answer": normalized.seller_answer,
                    "source_answered": True,
                    "source_updated_at": normalized.answer_created_at
                    or normalized.source_created_at,
                    "raw_payload": normalized.raw_payload,
                    "historical_metadata": {
                        "candidate_only": True,
                        "market": "COUPANG",
                        # A Coupang answer can describe a bundle, option, or
                        # marketplace procedure that differs elsewhere.  Only
                        # an explicit review decision may make it COMMON.
                        "market_applicability": "COUPANG_ONLY",
                        "origin_market": "COUPANG",
                        "shared_cross_market_learning": False,
                        "account_code": account_code,
                        "canonical_model": canonical_model,
                        "model_code": canonical_model,
                        "current_model_operation_source": operation_source,
                        "seller_product_id": payload.get("sellerProductId"),
                        "vendor_item_id": vendor_item_id,
                        "seller_item_id": payload.get("sellerItemId"),
                        "inquiry_comment_id": normalized.inquiry_comment_id,
                        "seller_answer_selection": normalized.answer_selection_status,
                        "historical_candidate_decision": candidate_gate.decision,
                        "historical_candidate_reason": candidate_gate.primary_reason,
                        "historical_candidate_tags": list(candidate_gate.tags),
                    },
                }
            )
            case = self.historical_cases.prepare_case(
                candidate,
                source_reference=(
                    f"COUPANG_ONLINE_API:{account_code}:{normalized.external_inquiry_id}"
                ),
            )
            _, outcome = self.historical_cases.repository.upsert(case)
            setattr(
                result,
                f"candidates_{outcome}",
                getattr(result, f"candidates_{outcome}") + 1,
            )
            if candidate_gate.decision == "MANUAL_REVIEW":
                result.manual_review_candidates += 1
        except Exception:
            result.failed += 1

    def _operation_source(self, canonical_model: str) -> str | None:
        if self.catalog.is_canonical_model_currently_active(canonical_model):
            return "COUPANG_ACTIVE_CATALOG"
        return None
