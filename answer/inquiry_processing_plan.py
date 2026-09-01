from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any

from answer.inquiry_analysis import InquiryAnalysis


@dataclass(frozen=True)
class InquiryProcessingPlan:
    """One immutable decision contract shared by UI and answer generation."""

    inquiry_id: int
    inquiry_type: str
    normalized_text: str
    detected_intent: str
    is_delivery: bool
    is_installation: bool
    is_high_risk: bool
    order_id: str
    product_order_id: str
    order_id_status: str
    requires_order_lookup: bool
    requires_dps_lookup: bool
    order_lookup_action: str
    dps_lookup_action: str
    order_lookup_status: str
    dps_lookup_status: str
    valid_order_snapshot_available: bool
    valid_dps_snapshot_available: bool
    installation_date_raw: str | None
    installation_date_display: str | None
    selected_answer_route: str
    can_generate_draft: bool
    needs_staff_review: bool
    workflow_order_status: str
    workflow_dps_status: str
    workflow_answer_status: str
    template_preferred: bool
    template_id: str | None
    generation_mode: str
    reason_code: str
    correlation_id: str
    analysis: InquiryAnalysis
    # Runtime-only provenance of the semantic understanding that constrained
    # this plan.  Kept in existing metadata JSON; no schema change.
    semantic_routing: dict[str, Any] | None = None

    @property
    def delivery_question(self) -> bool:
        return self.is_delivery

    @property
    def order_id_validated(self) -> bool:
        return self.order_id_status == "VALID"

    @property
    def can_execute_dps_lookup(self) -> bool:
        return (
            self.requires_dps_lookup
            and self.order_id_validated
            and self.order_lookup_status == "SUCCESS"
        )

    @property
    def can_generate_answer(self) -> bool:
        return self.can_generate_draft

    @property
    def question_category(self) -> str:
        return self.analysis.question_category

    @property
    def delivery_related(self) -> bool:
        return self.analysis.delivery_related

    @property
    def needs_delivery_lookup(self) -> bool:
        return self.analysis.needs_delivery_lookup

    def finalized(
        self,
        route: str,
        *,
        generation_mode: str,
        template_id: str | None = None,
        needs_staff_review: bool | None = None,
        reason_code: str | None = None,
    ) -> "InquiryProcessingPlan":
        review_routes = {
            "ORDER_ID_REQUEST",
            "ORDER_LOOKUP_FAILED",
            "DELIVERY_ORDER_NOT_FOUND",
            "DPS_LOOKUP_FAILED",
            "DELIVERY_DATE_UNCONFIRMED",
            "REVIEW_REQUIRED_SAFE_DRAFT",
        }
        return replace(
            self,
            selected_answer_route=route,
            generation_mode=generation_mode,
            template_id=template_id,
            needs_staff_review=(
                route in review_routes
                if needs_staff_review is None
                else needs_staff_review
            ),
            workflow_answer_status="COMPLETED",
            reason_code=reason_code or route,
        )

    def for_execution(
        self,
        *,
        correlation_id: str,
        template_preferred: bool,
    ) -> "InquiryProcessingPlan":
        """Bind a UI preview plan to the single answer-click trace."""

        return replace(
            self,
            correlation_id=correlation_id,
            template_preferred=bool(template_preferred),
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["analysis"] = self.analysis.to_dict()
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "InquiryProcessingPlan":
        """Restore the plan that actually governed a persisted draft."""

        analysis = value.get("analysis")
        if not isinstance(analysis, dict):
            raise ValueError("PROCESSING_PLAN_ANALYSIS_REQUIRED")
        return cls(
            inquiry_id=int(value["inquiry_id"]),
            inquiry_type=str(value.get("inquiry_type") or ""),
            normalized_text=str(value.get("normalized_text") or ""),
            detected_intent=str(value.get("detected_intent") or "GENERAL"),
            is_delivery=bool(value.get("is_delivery")),
            is_installation=bool(value.get("is_installation")),
            is_high_risk=bool(value.get("is_high_risk")),
            order_id=str(value.get("order_id") or ""),
            product_order_id=str(value.get("product_order_id") or ""),
            order_id_status=str(value.get("order_id_status") or "NOT_REQUIRED"),
            requires_order_lookup=bool(value.get("requires_order_lookup")),
            requires_dps_lookup=bool(value.get("requires_dps_lookup")),
            order_lookup_action=str(value.get("order_lookup_action") or "SKIP"),
            dps_lookup_action=str(value.get("dps_lookup_action") or "SKIP"),
            order_lookup_status=str(value.get("order_lookup_status") or "NOT_REQUIRED"),
            dps_lookup_status=str(value.get("dps_lookup_status") or "NOT_REQUIRED"),
            valid_order_snapshot_available=bool(value.get("valid_order_snapshot_available")),
            valid_dps_snapshot_available=bool(value.get("valid_dps_snapshot_available")),
            installation_date_raw=value.get("installation_date_raw"),
            installation_date_display=value.get("installation_date_display"),
            selected_answer_route=str(value.get("selected_answer_route") or ""),
            can_generate_draft=bool(value.get("can_generate_draft")),
            needs_staff_review=bool(value.get("needs_staff_review")),
            workflow_order_status=str(value.get("workflow_order_status") or "SKIPPED"),
            workflow_dps_status=str(value.get("workflow_dps_status") or "SKIPPED"),
            workflow_answer_status=str(value.get("workflow_answer_status") or "PENDING"),
            template_preferred=bool(value.get("template_preferred")),
            template_id=value.get("template_id"),
            generation_mode=str(value.get("generation_mode") or ""),
            reason_code=str(value.get("reason_code") or ""),
            correlation_id=str(value.get("correlation_id") or ""),
            analysis=InquiryAnalysis.from_dict(analysis),
            semantic_routing=(
                dict(value["semantic_routing"])
                if isinstance(value.get("semantic_routing"), dict)
                else None
            ),
        )
