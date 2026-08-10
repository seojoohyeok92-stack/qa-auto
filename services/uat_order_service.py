from __future__ import annotations

from typing import Any, Callable

from api.auth import get_access_token
from config import get_store_config
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from repositories.workflow_repository import WorkflowRepository
from services.order_service import OrderLookupResult, lookup_order_number
from workflow.models import StepCode, StepStatus


TokenProvider = Callable[..., str]
Lookup = Callable[..., OrderLookupResult]


class UatOrderService:
    def __init__(
        self,
        database: Database,
        *,
        token_provider: TokenProvider = get_access_token,
        lookup: Lookup = lookup_order_number,
    ) -> None:
        self.inquiries = InquiryRepository(database)
        self.logs = LogRepository(database)
        self.workflow = WorkflowRepository(database)
        self.token_provider = token_provider
        self.lookup = lookup

    def _start_workflow_step(self, inquiry_id: int) -> bool:
        self.workflow.initialize_steps(inquiry_id)
        step = self.workflow.get_step(
            inquiry_id, StepCode.NAVER_ORDER_LOOKUP
        )
        status = StepStatus(str(step["step_status"]))
        if status is StepStatus.SKIPPED:
            self.workflow.reopen_skipped_step(
                inquiry_id,
                StepCode.NAVER_ORDER_LOOKUP,
                metadata={"reason": "LATEST_PROCESSING_PLAN_REQUIRES_STEP"},
            )
            status = StepStatus.PENDING
        if status is StepStatus.PENDING:
            self.workflow.start_step(
                inquiry_id, StepCode.NAVER_ORDER_LOOKUP
            )
            return True
        if status in {StepStatus.FAILED, StepStatus.NEEDS_REVIEW}:
            self.workflow.retry_step(
                inquiry_id, StepCode.NAVER_ORDER_LOOKUP
            )
            return True
        return status is StepStatus.RUNNING

    def lookup_for_inquiry(
        self,
        inquiry_id: int,
        *,
        force_refresh: bool = False,
        correlation_id: str | None = None,
    ) -> OrderLookupResult:
        inquiry = self.inquiries.get(inquiry_id)
        if inquiry is None:
            raise LookupError(f"Inquiry not found: {inquiry_id}")
        number = str(
            inquiry.get("order_id") or inquiry.get("product_order_id") or ""
        ).strip()
        if not number:
            result: OrderLookupResult = {
                "success": False,
                "lookup_number": None,
                "lookup_type": None,
                "orders": [],
                "error_code": "NO_ORDER_NUMBER",
                "error_message": "문의에 주문 식별자가 없습니다.",
                "cached": False,
                "queried_at": "",
            }
            return result
        workflow_running = self._start_workflow_step(inquiry_id)
        store = get_store_config(str(inquiry.get("store_code") or ""))
        try:
            token = self.token_provider(store=store)
            result = self.lookup(
                token,
                number,
                store_code=store.code,
                force_refresh=force_refresh,
            )
        except Exception as error:
            if workflow_running:
                self.workflow.fail_step(
                    inquiry_id,
                    StepCode.NAVER_ORDER_LOOKUP,
                    "NAVER_ORDER_LOOKUP_FAILED",
                    "네이버 주문 조회에 실패했습니다.",
                )
            self.logs.record_inquiry(
                inquiry_id,
                "NAVER_ORDER_LOOKUP_FAILED",
                "네이버 주문정보 조회에 실패했습니다.",
                level="WARNING",
                details={
                    "component": "NaverOrder",
                    "operation": "lookup",
                    "exception_type": error.__class__.__name__,
                    "retryable": True,
                    "correlation_id": correlation_id,
                },
            )
            return {
                "success": False,
                "lookup_number": number,
                "lookup_type": None,
                "orders": [],
                "error_code": "NAVER_ORDER_LOOKUP_FAILED",
                "error_message": "네이버 주문 조회에 실패했습니다. 인증과 주문번호를 확인해 주세요.",
                "cached": False,
                "queried_at": "",
            }
        self.logs.record_inquiry(
            inquiry_id,
            (
                "NAVER_ORDER_LOOKUP_SUCCEEDED"
                if result["success"]
                else "NAVER_ORDER_LOOKUP_FAILED"
            ),
            (
                "네이버 주문정보를 확인했습니다."
                if result["success"]
                else "네이버 주문정보를 찾지 못했습니다."
            ),
            level="INFO" if result["success"] else "WARNING",
            details={
                "lookup_type": result.get("lookup_type"),
                "result_count": len(result.get("orders") or []),
                "error_code": result.get("error_code"),
                "correlation_id": correlation_id,
            },
        )
        if workflow_running:
            if result["success"]:
                self.workflow.complete_step(
                    inquiry_id,
                    StepCode.NAVER_ORDER_LOOKUP,
                    metadata={
                        "lookup_type": result.get("lookup_type"),
                        "result_count": len(result.get("orders") or []),
                        "correlation_id": correlation_id,
                    },
                )
            else:
                self.workflow.fail_step(
                    inquiry_id,
                    StepCode.NAVER_ORDER_LOOKUP,
                    str(result.get("error_code") or "ORDER_NOT_FOUND"),
                    "네이버 주문 정보를 확인하지 못했습니다.",
                )
        if result["success"] and result.get("orders"):
            selected_order = dict(result["orders"][0])
            persisted = self.inquiries.update_order_snapshot(
                inquiry_id,
                order_id=(
                    str(selected_order.get("order_id")).strip()
                    if selected_order.get("order_id")
                    else None
                ),
                product_order_id=(
                    str(selected_order.get("product_order_id")).strip()
                    if selected_order.get("product_order_id")
                    else None
                ),
                order_date=(
                    selected_order.get("order_date")
                    or selected_order.get("order_created_at")
                ),
                product_name=selected_order.get("product_name"),
                order_status=selected_order.get("product_order_status"),
                lookup_at=str(result.get("queried_at") or ""),
                lookup_type=result.get("lookup_type"),
                cached=bool(result.get("cached")),
            )
            result["selected_order"] = {
                "order_id": persisted.get("order_id"),
                "product_order_id": persisted.get("product_order_id"),
                "order_date": persisted.get("order_date"),
                "product_name": persisted.get("product_name"),
                "order_status": persisted.get("order_status"),
                "lookup_at": persisted.get("order_lookup_at"),
            }
            self.logs.record_inquiry(
                inquiry_id,
                "NAVER_ORDER_SNAPSHOT_SAVED",
                "DPS 조회용 주문 정보를 저장했습니다.",
                details={
                    "lookup_type": result.get("lookup_type"),
                    "has_order_id": bool(persisted.get("order_id")),
                    "has_order_date": bool(persisted.get("order_date")),
                    "cached": bool(result.get("cached")),
                    "correlation_id": correlation_id,
                },
            )
        safe_orders = []
        for order in result.get("orders") or []:
            safe_orders.append(
                {
                    key: value
                    for key, value in order.items()
                    if key
                    not in {
                        "receiver_name",
                        "receiver_tel",
                        "base_address",
                        "detailed_address",
                        "shipping_memo",
                    }
                }
            )
        result["orders"] = safe_orders
        return result
