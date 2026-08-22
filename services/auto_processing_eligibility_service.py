from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.auto_post_validation_service import AutoPostTechnicalValidator


REVIEW_ROUTES = {
    "BLOCKED_REVIEW_REQUIRED",
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
    # A generated "please send us your order number" reply asserts no order
    # fact at all -- it is the safe response to a missing order id, so it is
    # itself auto-postable. The unsafe case (claiming an order fact without a
    # trusted lookup) is still blocked by the order/DPS reasons below.
    "ORDER_ID_REQUEST",
}

# Conditions worth recording, but which do not on their own indicate that
# answering the customer is unsafe. Operating philosophy: auto-post by
# default, hard-block only on an actual risk of customer harm. Anything not
# listed here keeps its blocking behaviour.
SOFT_REASONS = frozenset({
    # A provider's self-reported confidence score is not a safety finding.
    # A real factual risk always surfaces as an evidence/validator/DPS reason.
    "INTENT_CONFIDENCE_LOW",
    "INTENT_CONFIDENCE_UNKNOWN",
    "GPT_CONFIDENCE_LOW",
    "GPT_CONFIDENCE_UNKNOWN",
    # The safe "please send your order number" reply. Asserts no order fact;
    # blocking it would leave the customer with no reply at all.
    "ORDER_ID_REQUESTED_FROM_CUSTOMER",
})


@dataclass(frozen=True)
class AutoProcessingEligibility:
    decision: str
    stage: str
    reasons: tuple[str, ...]
    # Recorded-but-not-blocking findings, preserved for logs/diagnostics so
    # relaxing the gate never means losing the signal.
    soft_reasons: tuple[str, ...] = ()

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

    @staticmethod
    def _evidence_fully_supported(hybrid: dict[str, Any]) -> bool:
        """True only when every sub-question is answerable from strong evidence.

        Mirrors the retrieval-side Evidence Support contract exactly: each
        sub-question must be ANSWERABLE *and* carry SUPPORTED coverage. Any
        CONFLICT, NEEDS_DPS, NO_RELIABLE_SOURCE, or merely partial coverage
        makes this False, so this can never relax a real evidence gap -- it
        only lets a confident-enough evidence base stand on its own when the
        provider's self-reported confidence number is pessimistic.
        """
        evidence = hybrid.get("subquestion_evidence")
        if not isinstance(evidence, list) or not evidence:
            return False
        for item in evidence:
            if not isinstance(item, dict):
                return False
            if str(item.get("status") or "").upper() != "ANSWERABLE":
                return False
            if str(item.get("evidence_coverage") or "").upper() != "SUPPORTED":
                return False
        return True

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
        product_guard_value = metadata.get("product_fact_guard")
        product_guard = (
            product_guard_value if isinstance(product_guard_value, dict) else {}
        )
        if product_guard.get("sensitive") and not product_guard.get(
            "current_fact_verified"
        ):
            reasons.append("PRODUCT_FACT_NOT_VERIFIED")
        if bool(plan.get("needs_staff_review")):
            reasons.append("PROCESSING_PLAN_REQUIRES_REVIEW")
        if bool(plan.get("is_high_risk")) or bool(analysis.get("manual_review_required")):
            reasons.append("POLICY_OR_HIGH_RISK_REVIEW")
        # Compatibility is a fact the customer buys on. An exact fixed
        # template (the catalog's own verified accessory rules, or a Product
        # DB fact) may answer it; anything composed by the model without such
        # a source may be drafted for staff but not published.
        if (
            str(analysis.get("detected_intent") or "").upper()
            == "PRODUCT_COMPATIBILITY"
            and normalized_route not in {"TEMPLATE", "PRODUCT_DB"}
        ):
            reasons.append("PRODUCT_COMPATIBILITY_NOT_VERIFIED")
        if str(draft.get("review_status") or "").upper() in {
            "NEEDS_REVIEW",
            "IN_REVIEW",
        }:
            reasons.append("DRAFT_REVIEW_REQUIRED")

        # An ORDER_ID_REQUEST answer exists precisely *because* the order id is
        # missing, and it only asks the customer for that number -- it states
        # no order fact. Blocking it would leave the customer with no reply at
        # all. The reasons are still recorded (soft) for diagnostics.
        order_request_route = normalized_route == "ORDER_ID_REQUEST"
        order_required = bool(plan.get("requires_order_lookup"))
        if order_required and str(plan.get("order_id_status") or "").upper() != "VALID":
            reasons.append(
                "ORDER_ID_REQUESTED_FROM_CUSTOMER" if order_request_route
                else "REQUIRED_ORDER_ID_MISSING_OR_INVALID"
            )
        if order_required and str(plan.get("order_lookup_status") or "").upper() != "SUCCESS":
            if not order_request_route:
                reasons.append("ORDER_LOOKUP_NOT_TRUSTED")

        dps_required = bool(plan.get("requires_dps_lookup"))
        if dps_required and str(plan.get("dps_lookup_status") or "").upper() != "SUCCESS":
            reasons.append("DPS_RESULT_NOT_TRUSTED")
        if dps_required and not bool(plan.get("valid_dps_snapshot_available")):
            reasons.append("DPS_SNAPSHOT_NOT_VALIDATED")

        hybrid_value = metadata.get("hybrid")
        hybrid = hybrid_value if isinstance(hybrid_value, dict) else {}
        # Evidence + Authority first: a pessimistic confidence number from the
        # provider is not itself a safety finding. When retrieval proved every
        # sub-question answerable from SUPPORTED evidence, and no other reason
        # fired, the confidence score alone must not hold the answer back.
        # An unparseable score is still treated as unknown risk.
        evidence_supported = self._evidence_fully_supported(hybrid)

        confidence = analysis.get("confidence")
        if confidence is not None:
            try:
                if float(confidence) < 0.8 and not evidence_supported:
                    reasons.append("INTENT_CONFIDENCE_LOW")
            except (TypeError, ValueError):
                reasons.append("INTENT_CONFIDENCE_UNKNOWN")

        draft_value = hybrid.get("draft")
        gpt_draft = draft_value if isinstance(draft_value, dict) else {}
        gpt_confidence = gpt_draft.get("confidence")
        if gpt_confidence is not None:
            try:
                if float(gpt_confidence) < 0.8 and not evidence_supported:
                    reasons.append("GPT_CONFIDENCE_LOW")
            except (TypeError, ValueError):
                reasons.append("GPT_CONFIDENCE_UNKNOWN")

        ordered = tuple(dict.fromkeys(reasons))
        hard = tuple(item for item in ordered if item not in SOFT_REASONS)
        soft = tuple(item for item in ordered if item in SOFT_REASONS)
        if hard:
            return AutoProcessingEligibility(
                "REVIEW_REQUIRED", "AUTO_POST_ELIGIBILITY", hard,
                soft_reasons=soft,
            )
        return AutoProcessingEligibility(
            "SAFE", "AUTO_POST_ELIGIBILITY", (), soft_reasons=soft,
        )
