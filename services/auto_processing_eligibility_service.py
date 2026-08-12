from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.auto_post_validation_service import AutoPostTechnicalValidator


REVIEW_ROUTES = {
    "BLOCKED_REVIEW_REQUIRED",
    "ORDER_ID_REQUEST",
    "ORDER_LOOKUP_FAILED",
    "DELIVERY_ORDER_NOT_FOUND",
    "DPS_LOOKUP_FAILED",
    "DELIVERY_DATE_UNCONFIRMED",
    "REVIEW_REQUIRED_SAFE_DRAFT",
}

AUTO_POSTABLE_ROUTES = {
    "TEMPLATE",
    "SAFE_RULE",
    "PRODUCT_DB",
    "GPT_FALLBACK",
    "GPT_DIRECT",
    "DELIVERY_WITH_INSTALLATION_DATE",
}


@dataclass(frozen=True)
class AutoProcessingEligibility:
    decision: str
    stage: str
    reasons: tuple[str, ...]

    @property
    def safe(self) -> bool:
        return self.decision == "SAFE"


class AutoProcessingEligibilityService:
    """Conservative policy gate between answer generation and Auto Post."""

    def __init__(self) -> None:
        self.technical = AutoPostTechnicalValidator()

    @staticmethod
    def _metadata(draft: dict[str, Any]) -> dict[str, Any]:
        value = draft.get("metadata_json")
        return value if isinstance(value, dict) else {}

    def evaluate(
        self,
        *,
        inquiry: dict[str, Any],
        draft: dict[str, Any],
        route: str,
    ) -> AutoProcessingEligibility:
        metadata = self._metadata(draft)
        plan_value = metadata.get("processing_plan")
        plan = plan_value if isinstance(plan_value, dict) else {}
        analysis_value = plan.get("analysis")
        analysis = analysis_value if isinstance(analysis_value, dict) else {}
        normalized_route = str(route or "").upper()

        if (
            bool(inquiry.get("source_answered"))
            or str(inquiry.get("post_status") or "").upper() == "POSTED"
            or bool(draft.get("posted"))
        ):
            return AutoProcessingEligibility(
                "BLOCKED", "IDEMPOTENCY", ("ALREADY_ANSWERED_OR_POSTED",)
            )

        answer = str(draft.get("original_answer") or "").strip()
        technical = self.technical.validate_answer(answer)
        if not technical.passed:
            privacy_errors = tuple(
                code
                for code in technical.errors
                if code in {"PII_EXPOSURE", "SECRET_EXPOSURE"}
            )
            return AutoProcessingEligibility(
                "BLOCKED" if privacy_errors else "REVIEW_REQUIRED",
                "PRIVACY" if privacy_errors else "VALIDATOR",
                privacy_errors or technical.errors,
            )

        reasons: list[str] = []
        validation_status = str(draft.get("validation_status") or "").upper()
        validator_value = draft.get("validator_result_json")
        validator = validator_value if isinstance(validator_value, dict) else {}
        if validation_status.startswith("FAIL") or "REVIEW" in validation_status:
            reasons.append("VALIDATOR_NOT_PASS")
        if validator and validator.get("passed") is False:
            reasons.append("VALIDATOR_NOT_PASS")
        if normalized_route not in AUTO_POSTABLE_ROUTES:
            reasons.append("INTENT_NOT_AUTO_POSTABLE")
        if normalized_route in REVIEW_ROUTES or any(
            marker in normalized_route
            for marker in ("REVIEW", "MANUAL", "BLOCKED", "FAILED", "UNCONFIRMED")
        ):
            reasons.append(f"ROUTE_{normalized_route or 'UNKNOWN'}")
        if bool(metadata.get("requires_manual_review")):
            reasons.append("ANSWER_REQUIRES_MANUAL_REVIEW")
        if bool(plan.get("needs_staff_review")):
            reasons.append("PROCESSING_PLAN_REQUIRES_REVIEW")
        if bool(plan.get("is_high_risk")) or bool(analysis.get("manual_review_required")):
            reasons.append("POLICY_OR_HIGH_RISK_REVIEW")
        if str(draft.get("review_status") or "").upper() in {
            "NEEDS_REVIEW",
            "IN_REVIEW",
        }:
            reasons.append("DRAFT_REVIEW_REQUIRED")

        order_required = bool(plan.get("requires_order_lookup"))
        if order_required and str(plan.get("order_id_status") or "").upper() != "VALID":
            reasons.append("REQUIRED_ORDER_ID_MISSING_OR_INVALID")
        if order_required and str(plan.get("order_lookup_status") or "").upper() != "SUCCESS":
            reasons.append("ORDER_LOOKUP_NOT_TRUSTED")

        dps_required = bool(plan.get("requires_dps_lookup"))
        if dps_required and str(plan.get("dps_lookup_status") or "").upper() != "SUCCESS":
            reasons.append("DPS_RESULT_NOT_TRUSTED")
        if dps_required and not bool(plan.get("valid_dps_snapshot_available")):
            reasons.append("DPS_SNAPSHOT_NOT_VALIDATED")

        confidence = analysis.get("confidence")
        if confidence is not None:
            try:
                if float(confidence) < 0.8:
                    reasons.append("INTENT_CONFIDENCE_LOW")
            except (TypeError, ValueError):
                reasons.append("INTENT_CONFIDENCE_UNKNOWN")

        hybrid_value = metadata.get("hybrid")
        hybrid = hybrid_value if isinstance(hybrid_value, dict) else {}
        draft_value = hybrid.get("draft")
        gpt_draft = draft_value if isinstance(draft_value, dict) else {}
        gpt_confidence = gpt_draft.get("confidence")
        if gpt_confidence is not None:
            try:
                if float(gpt_confidence) < 0.8:
                    reasons.append("GPT_CONFIDENCE_LOW")
            except (TypeError, ValueError):
                reasons.append("GPT_CONFIDENCE_UNKNOWN")

        if reasons:
            return AutoProcessingEligibility(
                "REVIEW_REQUIRED", "AUTO_POST_ELIGIBILITY", tuple(dict.fromkeys(reasons))
            )
        return AutoProcessingEligibility("SAFE", "AUTO_POST_ELIGIBILITY", ())
