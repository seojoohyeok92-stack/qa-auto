from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import uuid

from repositories.database import Database
from repositories.dps_repository import DpsRepository
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from services.answer_service import AnswerService
from services.dps_enrichment_service import DpsEnrichmentOutcome


@dataclass(frozen=True)
class DpsLookupRequest:
    inquiry_id: int
    order_id: str
    order_date: str | None
    force_refresh: bool = False


class DpsLookupOrchestrator:
    """Dashboard와 진단 화면이 공유하는 단일 DPS 조회 진입점."""

    def __init__(
        self,
        database: Database,
        *,
        answer_service: AnswerService | None = None,
    ) -> None:
        self.database = database
        self.inquiries = InquiryRepository(database)
        self.dps = DpsRepository(database)
        self.logs = LogRepository(database)
        self.answer_service = answer_service or AnswerService(database)

    @staticmethod
    def _masked_order_id(order_id: str) -> str:
        value = str(order_id or "").strip()
        if len(value) <= 8:
            return "<masked-order>"
        return f"{value[:4]}****{value[-4:]}"

    @staticmethod
    def request_from_inquiry(
        inquiry: dict[str, Any], *, force_refresh: bool = False
    ) -> DpsLookupRequest:
        order_id = str(inquiry.get("order_id") or "").strip()
        if not order_id:
            raise ValueError(
                "일반 주문번호(order_id)가 없어 DPS 조회를 실행할 수 없습니다."
            )
        raw = (
            inquiry.get("raw_json")
            if isinstance(inquiry.get("raw_json"), dict)
            else {}
        )
        product_order_ids = (
            inquiry.get("product_order_ids_json")
            or raw.get("product_order_ids")
            or []
        )
        if not isinstance(product_order_ids, list):
            product_order_ids = [product_order_ids]
        if inquiry.get("product_order_id"):
            product_order_ids.append(inquiry.get("product_order_id"))
        if order_id in {
            str(value).strip() for value in product_order_ids if str(value).strip()
        }:
            raise ValueError(
                "상품주문번호는 DPS 조회에 사용할 수 없습니다. "
                "일반 주문번호를 확인해 주세요."
            )
        order_lookup = (
            raw.get("order_lookup")
            if isinstance(raw.get("order_lookup"), dict)
            else {}
        )
        order_date = next(
            (
                str(value).strip()
                for value in (
                    inquiry.get("order_date"),
                    order_lookup.get("order_date"),
                    raw.get("order_date"),
                    raw.get("orderDate"),
                )
                if value
            ),
            None,
        )
        if not order_date:
            raise ValueError(
                "DPS 조회에는 실제 주문일(order_date)이 필요합니다. "
                "먼저 주문 조회를 실행해 주세요."
            )
        return DpsLookupRequest(
            inquiry_id=int(inquiry["id"]),
            order_id=order_id,
            order_date=order_date,
            force_refresh=force_refresh,
        )

    def lookup(
        self,
        inquiry_id: int,
        *,
        force_refresh: bool = False,
        correlation_id: str | None = None,
    ) -> DpsEnrichmentOutcome:
        resolved_correlation_id = correlation_id or str(uuid.uuid4())
        inquiry = self.inquiries.get(inquiry_id)
        if inquiry is None:
            raise LookupError(f"Inquiry not found: {inquiry_id}")
        try:
            request = self.request_from_inquiry(
                inquiry, force_refresh=force_refresh
            )
        except ValueError as error:
            self.logs.record_inquiry(
                inquiry_id,
                "DPS_LOOKUP_VALIDATION_FAILED",
                "DPS 조회 입력값 검증에 실패했습니다.",
                level="WARNING",
                details={
                    "correlation_id": resolved_correlation_id,
                    "stage": "validation",
                    "error_category": error.__class__.__name__,
                },
            )
            raise
        self.logs.record_inquiry(
            inquiry_id,
            "DPS_LOOKUP_REQUESTED",
            "Dashboard에서 DPS 조회를 요청했습니다.",
            details={
                "correlation_id": resolved_correlation_id,
                "masked_order_id": self._masked_order_id(request.order_id),
                "has_order_date": bool(request.order_date),
                "force_refresh": force_refresh,
            },
        )
        outcome = self.answer_service.enrich_dps_for_inquiry(
            inquiry_id,
            force_refresh=force_refresh,
            explicit_lookup=True,
            correlation_id=resolved_correlation_id,
        )
        if outcome.lookup_row is not None:
            persisted = self.dps.get_latest_by_inquiry_id(inquiry_id)
            if (
                persisted is None
                or int(persisted["id"]) != int(outcome.lookup_row["id"])
            ):
                self.logs.record_inquiry(
                    inquiry_id,
                    "DPS_RESULT_RELOAD_MISMATCH",
                    "DPS 메모리 결과와 DB 재조회 결과가 다릅니다.",
                    level="WARNING",
                    details={
                        "correlation_id": resolved_correlation_id,
                        "stage": "repository_reload",
                    },
                )
        return outcome
