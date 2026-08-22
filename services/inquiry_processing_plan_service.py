from __future__ import annotations

import re
import uuid
from typing import Any

from answer.inquiry_processing_plan import InquiryProcessingPlan
from answer.source_adapter import answer_request_from_inquiry
from dps.dates import STALE_DPS_SCHEDULE, is_schedule_stale
from repositories.database import Database
from repositories.dps_repository import DpsRepository
from repositories.workflow_repository import WorkflowRepository
from services.inquiry_analysis_service import InquiryAnalysisService
from services.phase9_answer_policy import build_delivery_answer_context
from workflow.models import StepCode


GENERAL_ORDER_ID = re.compile(r"\d{16}")
FAILED_DPS_STATES = {
    "AGENT_OFFLINE",
    "TIMEOUT",
    "PARSE_ERROR",
    "AUTOMATION_ERROR",
    "NETWORK_ERROR",
    "CACHE_CORRUPTION",
    "STALE_CACHE",
    "CANCELLED",
    "FAILED",
}


class InquiryProcessingPlanService:
    """Build the single routing/workflow contract for one inquiry action."""

    def __init__(
        self,
        database: Database,
        *,
        analysis: InquiryAnalysisService | None = None,
    ) -> None:
        self.database = database
        self.analysis = analysis or InquiryAnalysisService()
        self.dps = DpsRepository(database)
        self.workflows = WorkflowRepository(database)

    @staticmethod
    def _raw(inquiry: dict[str, Any]) -> dict[str, Any]:
        value = inquiry.get("raw_json")
        return dict(value) if isinstance(value, dict) else {}

    def _workflow_status(self, inquiry_id: int, code: StepCode) -> str:
        try:
            return str(
                self.workflows.get_step(inquiry_id, code)["step_status"]
            ).upper()
        except (LookupError, ValueError):
            return "PENDING"

    @staticmethod
    def _order_result_status(result: dict[str, Any] | None) -> str | None:
        if not result:
            return None
        if result.get("success") and result.get("orders"):
            return "SUCCESS"
        code = str(result.get("error_code") or "").upper()
        if code in {
            "EMPTY_RESULT",
            "ORDER_NOT_FOUND",
            "NOT_FOUND",
            "NO_RESULTS",
        }:
            return "NOT_FOUND"
        return "FAILED"

    def create(
        self,
        inquiry: dict[str, Any],
        *,
        template_preferred: bool = True,
        correlation_id: str | None = None,
        order_lookup_result: dict[str, Any] | None = None,
        dps_override: dict[str, Any] | None = None,
    ) -> InquiryProcessingPlan:
        request = answer_request_from_inquiry(inquiry)
        analysis = self.analysis.analyze(request)
        inquiry_id = int(inquiry["id"])
        order_id = str(request.order_id or "").strip()
        product_order_id = str(request.product_order_id or "").strip()
        if GENERAL_ORDER_ID.fullmatch(order_id):
            order_id_status = "VALID"
        elif order_id:
            order_id_status = "INVALID"
        elif product_order_id:
            order_id_status = "AMBIGUOUS_PRODUCT_ORDER_ONLY"
        else:
            order_id_status = "MISSING"

        raw = self._raw(inquiry)
        snapshot = (
            dict(raw.get("order_lookup"))
            if isinstance(raw.get("order_lookup"), dict)
            else {}
        )
        snapshot_order_id = str(snapshot.get("order_id") or "").strip()
        valid_order_snapshot = bool(
            order_id_status == "VALID"
            and snapshot_order_id == order_id
            and (snapshot.get("lookup_at") or inquiry.get("order_lookup_at"))
            and (
                snapshot.get("order_date")
                or snapshot.get("order_status")
                or snapshot.get("product_name")
            )
        )
        explicit_order_status = self._order_result_status(order_lookup_result)
        workflow_order = self._workflow_status(
            inquiry_id, StepCode.NAVER_ORDER_LOOKUP
        )
        if not analysis.requires_order_lookup:
            order_lookup_status = "NOT_REQUIRED"
        elif order_id_status != "VALID":
            order_lookup_status = "CUSTOMER_INFORMATION_REQUIRED"
        elif explicit_order_status:
            order_lookup_status = explicit_order_status
        elif valid_order_snapshot:
            order_lookup_status = "SUCCESS"
        elif workflow_order == "FAILED":
            order_lookup_status = "FAILED"
        else:
            # A stale SKIPPED/COMPLETED step without a matching snapshot is
            # not evidence that the current inquiry was looked up.
            order_lookup_status = "NOT_STARTED"

        latest_dps: dict[str, Any] | None = None
        dps_status = "NOT_REQUIRED"
        if analysis.requires_dps_lookup and order_id_status == "VALID":
            if dps_override is not None:
                latest_dps = dict(dps_override)
            else:
                try:
                    latest_dps = self.dps.get_preferred_for_inquiry_and_order(
                        inquiry_id, order_id
                    )
                except Exception:
                    latest_dps = {"lookup_status": "CACHE_CORRUPTION"}
            dps_status = str(
                (latest_dps or {}).get("lookup_status") or "NOT_STARTED"
            ).upper()
        request.metadata["dps"] = dict(latest_dps or {})
        context = build_delivery_answer_context(request, analysis)
        # A successful lookup can still return the schedule of an already
        # completed delivery. The lookup result stays SUCCESS -- it really did
        # succeed -- but a date that had already passed when the customer
        # wrote in is not the schedule they are asking about, so the snapshot
        # is not answer-authoritative and auto-post is withheld.
        stale_dps_schedule = bool(
            latest_dps
            and dps_status == "SUCCESS"
            and is_schedule_stale(
                (latest_dps or {}).get("installation_date")
                or (latest_dps or {}).get("required_delivery_date"),
                registered_at=request.metadata.get("registered_at"),
                created_at=request.metadata.get("created_at"),
            )
        )
        if stale_dps_schedule:
            request.metadata["dps"] = {
                **request.metadata["dps"],
                "schedule_validity": STALE_DPS_SCHEDULE,
            }
        valid_dps_snapshot = dps_status == "SUCCESS" and not stale_dps_schedule
        if (
            analysis.requires_order_lookup
            and order_id_status == "VALID"
            and order_lookup_status == "NOT_STARTED"
            and valid_dps_snapshot
        ):
            # A current DPS result is already scoped by inquiry + validated
            # general order ID and is sufficient to avoid a redundant Naver
            # lookup on regeneration.
            order_lookup_status = "SUCCESS"
            valid_order_snapshot = True

        if not analysis.requires_order_lookup:
            order_action = "SKIP"
        elif order_id_status != "VALID":
            order_action = "WAIT_FOR_CUSTOMER"
        elif order_lookup_status == "SUCCESS":
            order_action = "USE_SNAPSHOT"
        elif order_lookup_status in {"FAILED", "NOT_FOUND"}:
            order_action = "RETRY_OPTIONAL"
        else:
            order_action = "FETCH"

        if not analysis.requires_dps_lookup:
            dps_action = "SKIP"
        elif order_id_status != "VALID":
            dps_action = "SKIP"
        elif order_lookup_status != "SUCCESS":
            dps_action = "WAIT_FOR_ORDER_LOOKUP"
        elif valid_dps_snapshot:
            dps_action = "USE_CACHE"
        elif dps_status in FAILED_DPS_STATES or dps_status == "NOT_FOUND":
            dps_action = "RETRY_OPTIONAL"
        else:
            dps_action = "FETCH"

        is_high_risk = analysis.inquiry_subtype == "HIGH_RISK_OR_DISPUTE"
        can_generate = bool(request.question.strip()) and not is_high_risk
        if is_high_risk:
            route = "BLOCKED_REVIEW_REQUIRED"
            reason = "HIGH_RISK_BLOCKED"
        elif not analysis.delivery_question:
            route = "GPT_FALLBACK" if template_preferred else "GPT_DIRECT"
            reason = "GENERAL_ROUTE_PENDING_CONTENT_MATCH"
        elif order_id_status != "VALID":
            route = "ORDER_ID_REQUEST"
            reason = order_id_status
        elif order_lookup_status == "NOT_FOUND":
            route = "DELIVERY_ORDER_NOT_FOUND"
            reason = "ORDER_NOT_FOUND"
        elif order_lookup_status != "SUCCESS":
            route = "ORDER_LOOKUP_FAILED"
            reason = "ORDER_LOOKUP_REQUIRED_OR_FAILED"
        elif dps_status == "SUCCESS" and context.installation_date_display:
            route = "DELIVERY_WITH_INSTALLATION_DATE"
            reason = "CONFIRMED_INSTALLATION_DATE"
        elif dps_status == "SUCCESS":
            route = "DELIVERY_DATE_UNCONFIRMED"
            reason = "DPS_SUCCESS_WITHOUT_DATE"
        elif dps_status == "NOT_FOUND":
            route = "DELIVERY_ORDER_NOT_FOUND"
            reason = "DPS_ORDER_NOT_FOUND"
        else:
            route = "DPS_LOOKUP_FAILED"
            reason = "DPS_LOOKUP_REQUIRED_OR_FAILED"

        if not analysis.requires_order_lookup:
            workflow_order_status = "SKIPPED"
        elif order_id_status != "VALID":
            workflow_order_status = "CUSTOMER_INFORMATION_REQUIRED"
        elif order_lookup_status == "SUCCESS":
            workflow_order_status = "COMPLETED"
        elif order_lookup_status == "NOT_STARTED":
            workflow_order_status = "READY"
        else:
            workflow_order_status = order_lookup_status

        if not analysis.requires_dps_lookup or order_id_status != "VALID":
            workflow_dps_status = "SKIPPED"
        elif order_lookup_status != "SUCCESS":
            workflow_dps_status = "WAITING_FOR_ORDER_LOOKUP"
        elif dps_status == "SUCCESS":
            workflow_dps_status = "COMPLETED"
        elif dps_status == "NOT_STARTED":
            workflow_dps_status = "READY"
        else:
            workflow_dps_status = "FAILED"

        return InquiryProcessingPlan(
            inquiry_id=inquiry_id,
            inquiry_type=str(inquiry.get("inquiry_type") or ""),
            normalized_text=request.question,
            detected_intent=analysis.detected_intent,
            is_delivery=analysis.delivery_question,
            is_installation=analysis.detected_intent.startswith("INSTALLATION"),
            is_high_risk=is_high_risk,
            order_id=order_id,
            product_order_id=product_order_id,
            order_id_status=order_id_status,
            requires_order_lookup=analysis.requires_order_lookup,
            requires_dps_lookup=analysis.requires_dps_lookup,
            order_lookup_action=order_action,
            dps_lookup_action=dps_action,
            order_lookup_status=order_lookup_status,
            dps_lookup_status=dps_status,
            valid_order_snapshot_available=valid_order_snapshot,
            valid_dps_snapshot_available=valid_dps_snapshot,
            installation_date_raw=context.installation_date_raw,
            installation_date_display=context.installation_date_display,
            selected_answer_route=route,
            can_generate_draft=can_generate,
            needs_staff_review=route in {
                "ORDER_ID_REQUEST",
                "ORDER_LOOKUP_FAILED",
                "DELIVERY_ORDER_NOT_FOUND",
                "DPS_LOOKUP_FAILED",
                "DELIVERY_DATE_UNCONFIRMED",
            },
            workflow_order_status=workflow_order_status,
            workflow_dps_status=workflow_dps_status,
            workflow_answer_status="READY" if can_generate else "BLOCKED",
            template_preferred=bool(template_preferred),
            template_id=None,
            generation_mode=("RULE" if analysis.delivery_question else route),
            reason_code=reason,
            correlation_id=correlation_id or str(uuid.uuid4()),
            analysis=analysis,
        )
