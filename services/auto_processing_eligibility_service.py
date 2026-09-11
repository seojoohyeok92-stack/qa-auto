from __future__ import annotations

import json
import re

from dataclasses import dataclass
from typing import Any, Mapping

from answer.source_adapter import answer_request_from_inquiry
from services.auto_post_validation_service import AutoPostTechnicalValidator
from services.inquiry_analysis_service import InquiryAnalysisService


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
    # The keyword classifier had no rule for this wording, and the
    # deterministic validator passed the answer outright. See
    # ``_validator_cleared`` -- a routing gap, not a safety finding.
    "INTENT_UNCLASSIFIED_VALIDATOR_CLEAR",
    # A pre-generation classifier gap can set three derivative review flags
    # (answer metadata, processing plan, and draft status).  When generation
    # later has complete evidence and the deterministic validator clears the
    # actual answer, keep the original signal for audit without counting the
    # same preliminary decision three times as customer-facing risk.
    "PRELIMINARY_REVIEW_RESOLVED",
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


# A delivery duration, a dispatch cut-off, or an arrival date. Read from the
# answer rather than from the question, because that is where the harm is: the
# question can be classified any way at all, and what reaches the customer is
# the sentence.
#
# Measured need. "지금 구매하면 정상적으로 받을 수 있나요" was understood as
# PRE_PURCHASE but not as a schedule question -- defensibly, since it asks
# whether delivery will happen rather than when -- and the answer still came
# back "배송 및 설치까지 약 3~4주 소요될 예정입니다" for a customer with no
# order. No number like that can be right before an order exists: it depends on
# when they pay, on the installer's calendar, and on stock.
_DELIVERY_PERIOD_CLAIM = re.compile(
    r"\d+\s*~\s*\d+\s*(?:일|영업일|주|개월)"
    r"|약\s*\d+\s*(?:일|영업일|주|개월)"
    r"|\d+\s*(?:영업일|주일|개월)"
    r"|\d+\s*(?:주|일)\s*(?:정도|가량|이내|안에|쯤)"
    r"|당일\s*발송|익일\s*발송"
    r"|\d{1,2}\s*월\s*\d{1,2}\s*일에?\s*(?:배송|도착|설치|출고|받)"
)


def delivery_period_claim(answer: object) -> str | None:
    """Which delivery-period claim this answer makes, or None."""

    found = _DELIVERY_PERIOD_CLAIM.search(str(answer or ""))
    return found.group(0) if found else None


# Appended when the coverage evaluator found a question the answer left
# unanswered. Deliberately absent from SOFT_REASONS: unresolved substantive
# questions are the thing auto-post must not publish over.
SEMANTIC_COVERAGE_INCOMPLETE = "SEMANTIC_COVERAGE_INCOMPLETE"

# GPT ②'s own findings about the answer it just wrote. Deliberately absent from
# SOFT_REASONS: an item the model could not settle, or an answer it declined to
# publish, is the finding that used to be inferred from anchor tables, and it is
# now made by the reader that saw the evidence.
GPT_REPORTED_UNRESOLVED = "GPT_REPORTED_UNRESOLVED"
GPT_WITHHELD_AUTO_POST = "GPT_WITHHELD_AUTO_POST"

# No usable understanding of the question. Automatic publication requires that
# GPT ① read the inquiry; the keyword classifier is a routing fallback, not a
# licence to answer a customer unattended.
UNDERSTANDING_UNAVAILABLE = "UNDERSTANDING_UNAVAILABLE"

# The two verdicts that mean "a recognised question went unanswered". UNKNOWN
# stays observational, exactly as it is inside the coverage evaluator: an
# unfamiliar but safe question must not become a false hold.
_COVERAGE_HOLD_STATUSES = frozenset({"FAIL", "PARTIAL"})


# Appended when a question the customer asked has no factual source behind it.
# Absent from SOFT_REASONS: publishing a factual claim with nothing supporting
# it is the thing this gate exists to stop.
EVIDENCE_NOT_SUFFICIENT = "EVIDENCE_NOT_SUFFICIENT"

# Retrieval's own verdict when it found nothing for a sub-question. The other
# statuses -- NEEDS_DPS, DELIVERY_SCHEDULE_REVIEW, CONFLICT -- already carry
# their own review paths and are not re-judged here.
_NO_SOURCE_STATUS = "NO_RELIABLE_SOURCE"

# Routes whose answer comes from a source settled before retrieval ran, so an
# empty retrieval verdict says nothing about them. ORDER_ID_REQUEST is the safe
# "please send your order number" reply, which asserts no fact at all.
_EVIDENCE_EXEMPT_ROUTES = frozenset({
    # A generic template is candidate evidence, not a semantic final answer.
    # It therefore still needs the persisted GPT② evidence decision.  Only
    # fixed mechanical routes below may legitimately bypass that decision.
    "SAFE_RULE", "PRODUCT_DB", "ORDER_ID_REQUEST",
    # A date copied from a validated DPS snapshot is a fixed verified
    # workflow result, not a GPT-composed answer.  It has its own order/DPS
    # trust checks below and must not require a GPT② trace after persistence.
    "DELIVERY_WITH_INSTALLATION_DATE",
})

# What the company does not let the system answer by itself.
#
# Named with GPT ①'s own action labels, from the closed set in
# ``services/semantic_analysis.py``. Not a keyword list: "반품" appearing in a
# question about delivery policy is not a return request, and the
# understanding stage is what tells the two apart -- which is why this reads
# the persisted understanding rather than the inquiry text.
#
# A draft is still written and staff can still send it. What closes is the
# automatic path: a resolved, fully grounded answer about a return, an
# exchange or damage is exactly the case this exists for, so the hold cannot
# depend on GPT ② having withheld anything.
POLICY_STAFF_ONLY_ACTIONS = frozenset({"CANCEL_RETURN", "DAMAGE_REPORT"})
RETURN_OR_DAMAGE_POLICY_REVIEW = "RETURN_OR_DAMAGE_POLICY_REVIEW"

# These values are persisted by ``InquiryProcessingPlanService`` while GPT①'s
# already-authoritative understanding is in scope.  This allowlist deliberately
# prevents arbitrary old metadata (intent, subtype, manual-review flags) from
# becoming a worker-side semantic decision after persistence.
_PERSISTED_WORKFLOW_BLOCKS = frozenset({
    "PRE_PURCHASE_DELIVERY_UNRESOLVED",
})


def _evidence_insufficient(
    metadata: Mapping[str, Any] | None, *, route: str | None,
) -> bool:
    """Is there a question here that nothing factual stands behind?

    Coverage asks whether the reply addresses what was asked. That is a
    different question from whether anything supports it, and the two came
    apart: a four-part inquiry with no retrieved evidence at all was answered
    "현재 확인 가능한 정보가 없어 각각 확인 후 안내가 필요합니다", every topic
    was named, coverage scored PASS, and eligibility returned SAFE with no
    reasons. Nothing was wrong with the sentence; there was simply nothing
    behind it, and the pipeline had no way to say so.

    Writing that a fact needs checking is not a factual source. So the source
    verdict retrieval already recorded per sub-question is read here directly:
    an atom it marked NO_RELIABLE_SOURCE has nothing behind it, and an answer
    containing one cannot publish itself.

    Deterministic routes are exempt because their answer predates retrieval --
    a template that settles the inquiry never needed a Learning row, and its
    empty ``subquestion_evidence`` is silence rather than absence.
    """

    if str(route or "").strip().upper() in _EVIDENCE_EXEMPT_ROUTES:
        return False
    metadata = metadata or {}
    hybrid = metadata.get("hybrid")
    hybrid = hybrid if isinstance(hybrid, Mapping) else {}
    entries = hybrid.get("subquestion_evidence") or ()
    if not entries:
        # Nothing recorded at all -- a draft from before this stage existed, or
        # a route that never built a context. Untouched, as every other gate
        # here treats an absent record.
        return False
    for item in entries:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("status") or "").upper() == _NO_SOURCE_STATUS:
            return True
    return False


def _coverage_incomplete(metadata: Mapping[str, Any] | None) -> bool:
    """Did the coverage evaluator find an unanswered substantive question?"""

    coverage = (metadata or {}).get("semantic_coverage")
    if not isinstance(coverage, Mapping):
        return False
    return str(coverage.get("status") or "").upper() in _COVERAGE_HOLD_STATUSES


class AutoProcessingEligibilityService:
    """Conservative policy gate between answer generation and Auto Post."""

    def __init__(self) -> None:
        self.technical = AutoPostTechnicalValidator()

    @staticmethod
    def _metadata(draft: dict[str, Any]) -> dict[str, Any]:
        value = draft.get("metadata_json")
        if isinstance(value, dict):
            return value
        # The generation path evaluates an in-memory Draft while the worker
        # evaluates the same Draft after AnswerRepository has deserialised it.
        # Accept the persisted JSON representation too, so a lifecycle pass
        # cannot acquire a different decision merely because it crossed the
        # SQLite boundary.  This parses stored data; it does not infer any
        # semantic value or recompute GPT's evidence verdict.
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except (TypeError, ValueError):
                return {}
            return decoded if isinstance(decoded, dict) else {}
        return {}

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
            # CANDIDATE means retrieval attached sources and GPT ② judged them;
            # ANSWERABLE means a deterministic source settled it. Either is an
            # evidence base. NEEDS_DPS, CONFLICT, DELIVERY_SCHEDULE_REVIEW and
            # NO_RELIABLE_SOURCE are not, and still return False here.
            if str(item.get("status") or "").upper() not in {
                "ANSWERABLE", "CANDIDATE",
            }:
                return False
            # ``evidence_coverage`` is no longer consulted. It is the lexical
            # answer-support label, and requiring SUPPORTED here re-imposed the
            # wording test that the retrieval ladder no longer applies -- the
            # gate would have gone on holding answers for the exact reason the
            # pipeline stopped withholding evidence.
        return True

    @staticmethod
    def _validator_cleared(
        validation_status: str, validator: dict[str, Any]
    ) -> bool:
        """True only when the validator passed with nothing at all to report.

        Anything short of that -- a failure, a review signal, a recorded error,
        or a missing verdict -- leaves the answer blocked, so this can never
        relax a real finding. It only distinguishes an answer the validator
        actively cleared from one it never got to check.
        """

        pass_statuses = {"PASS", "PASS_WITH_WARNING"}
        if validation_status not in pass_statuses:
            return False
        if not validator or validator.get("passed") is not True:
            return False
        if str(validator.get("status") or "PASS").upper() not in pass_statuses:
            return False
        return not validator.get("errors") and not validator.get(
            "review_signals"
        )

    @staticmethod
    def _intent_unclassified(analysis: dict[str, Any]) -> bool:
        """True when the classifier found no rule, not when it found risk.

        ``manual_review_required`` is raised by two very different findings.
        "위험·분쟁 관련 표현이 있어 직원 판단이 필요합니다" is a positive risk
        classification and must always hold the answer; falling off the end of
        the keyword tables is not. Only the second is recognised here, and it
        is recognised positively -- the classifier has to say "no rule matched"
        -- so a new review category can never be mistaken for a classifier gap.

        A compound inquiry needs the second form below. Its subtype is
        COMPOUND_MULTI_INTENT and its category comes from the representative
        sub-question, so the single-question shape never matches even when the
        *only* thing asking for review is one unclassified fragment beside a
        perfectly ordinary question. ``manual_review_sources`` carries one
        entry per contributing sub-question, and every one of them must be a
        classifier gap: a single risk, cancel, schedule-change or empty-question
        source keeps the hold. Absent or empty sources mean the cause is
        unknown, which stays blocked.
        """

        if (
            str(analysis.get("question_category") or "").upper()
            == "INFORMATION_INSUFFICIENT"
            and str(analysis.get("inquiry_subtype") or "").upper()
            == "UNCLASSIFIED"
        ):
            return True
        sources = analysis.get("manual_review_sources")
        if not isinstance(sources, (list, tuple)) or not sources:
            return False
        return all(
            str(source or "").upper() == "UNCLASSIFIED" for source in sources
        )

    @classmethod
    def _current_analysis_clears_review(
        cls, inquiry: dict[str, Any]
    ) -> bool:
        """True when a current deterministic analysis supersedes a stale hold.

        Persisted drafts can outlive classifier improvements.  Re-analysis is
        local and deterministic (no provider call), and is used only to prove
        that the old manual-review signal no longer exists.  A current manual
        review result, an unreadable legacy inquiry, or any failure stays
        conservative.
        """

        if not str(inquiry.get("content") or inquiry.get("question") or "").strip():
            return False
        try:
            current = InquiryAnalysisService().analyze(
                answer_request_from_inquiry(inquiry)
            )
        except (KeyError, TypeError, ValueError):
            return False
        current_analysis = current.to_dict()
        return not current.manual_review_required or cls._intent_unclassified(
            current_analysis
        )

    @classmethod
    def _preliminary_review_resolved(
        cls,
        *,
        inquiry: dict[str, Any],
        analysis: dict[str, Any],
        plan: dict[str, Any],
        hybrid: dict[str, Any],
        validation_status: str,
        validator: dict[str, Any],
        route: str = "",
        coverage_metadata: Mapping[str, Any] | None = None,
    ) -> bool:
        """Whether post-generation evidence resolved a classifier-only hold.

        This is deliberately narrower than "validator PASS".  Every
        sub-question must be supported, the generated draft and deterministic
        self-review must expose no unresolved review signal or missing
        information, and the processing plan must not identify real risk.
        Advisory warnings are intentionally not consulted here; they are not
        unsafe claims and remain visible in the persisted validator metadata.
        """

        if (
            not cls._current_analysis_clears_review(inquiry)
            or not cls._validator_cleared(validation_status, validator)
            or bool(plan.get("is_high_risk"))
            # An unanswered question is not a classifier-only hold, so there is
            # nothing here for post-generation evidence to resolve. The hard
            # reason above already blocks; this keeps the reported reason
            # honest rather than showing the hold as resolved.
            or _coverage_incomplete(coverage_metadata)
        ):
            return False
        # Rendered templates are validated by the route-specific template
        # validator and never have GPT-only draft/self-review/evidence blocks.
        # Independent product/order/DPS/privacy and route reasons are still
        # evaluated below and remain hard blockers.
        if str(route or "").upper() == "TEMPLATE":
            return True
        if not cls._evidence_fully_supported(hybrid):
            return False
        generated_value = hybrid.get("draft")
        generated = (
            generated_value if isinstance(generated_value, dict) else None
        )
        review_value = hybrid.get("self_review")
        self_review = review_value if isinstance(review_value, dict) else None
        if generated is None or self_review is None:
            return False
        missing = generated.get("missing_information")
        required_missing = generated.get("required_missing_information")
        classified_missing = isinstance(
            generated.get("missing_information_details"), list
        )
        unresolved_missing = (
            bool(required_missing)
            if classified_missing
            else bool(missing)
        )
        return not (
            bool(generated.get("requires_review"))
            or unresolved_missing
            or bool(self_review.get("requires_review"))
        )

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
        hybrid_value = metadata.get("hybrid")
        hybrid = hybrid_value if isinstance(hybrid_value, dict) else {}
        draft_value = hybrid.get("draft")
        gpt_draft = draft_value if isinstance(draft_value, dict) else {}
        gpt_final_pipeline = (
            hybrid.get("answer_pipeline") == "GPT_UNDERSTAND_RETRIEVE_ANSWER"
        )
        # Historical drafts predate the GPT-first trace and are never silently
        # reclassified on a later worker pass.  Every current production run
        # persists either semantic_routing or an answer_pipeline marker; for
        # those runs a missing GPT-final marker is an explicit workflow hold.
        routing_value = metadata.get("semantic_routing")
        routing = routing_value if isinstance(routing_value, Mapping) else {}
        understanding_value = routing.get("understanding")
        understanding = (
            understanding_value
            if isinstance(understanding_value, Mapping)
            else {}
        )
        # A fixed template/Product route need not invoke GPT②, but it still
        # has a valid GPT① understanding contract.  Treating that persisted
        # contract as unavailable after reload was a worker shadow-path bug.
        # A current GPT-composed route must retain its GPT② decision across
        # persistence.  A fixed verified route may legitimately have only
        # GPT① understanding, but silently treating a lost GPT② trace on a
        # composed route as SAFE would create a worker shadow path.
        requires_gpt_final_decision = (
            normalized_route not in _EVIDENCE_EXEMPT_ROUTES
        )
        # A composed current-run answer without the persisted GPT-first decision
        # is never silently promoted by a worker pass. A historical record that
        # predates all GPT-first markers is not reclassified by this lifecycle
        # pass; that preserves backward compatibility without creating a new
        # semantic decision authority.
        gpt_pipeline_unavailable = bool(
            not gpt_final_pipeline
            and requires_gpt_final_decision
            and ("semantic_routing" in metadata or "answer_pipeline" in hybrid)
        )
        review_status = str(draft.get("review_status") or "").upper()
        # ``manual_review_required`` is legacy semantic routing telemetry.
        # It is not an auto-post authority.  Workflow holds and the persisted
        # GPT② evidence verdict remain observable through their own fields.
        has_preliminary_review = bool(
            False
            # NEEDS_REVIEW persisted by the old path is an output of legacy
            # semantic routing, not an independent workflow fact.  A current
            # GPT-final draft therefore derives its verdict from GPT②'s
            # evidence fields below.  Explicit IN_REVIEW remains lifecycle.
            or False
        )
        preliminary_review_resolved = (
            has_preliminary_review
            and self._preliminary_review_resolved(
                inquiry=inquiry,
                analysis=analysis,
                plan=plan,
                hybrid=hybrid,
                validation_status=validation_status,
                validator=validator,
                route=normalized_route,
                coverage_metadata=metadata,
            )
        )
        # "the validator rejected this" and "the validator passed but asked
        # for a person to look" are different findings. Reporting both as
        # VALIDATOR_NOT_PASS told staff the validator had failed on answers it
        # had actually passed. Both still block; only the stated reason
        # differs.
        if validation_status.startswith("FAIL"):
            reasons.append("VALIDATOR_NOT_PASS")
        if validator:
            if validator.get("passed") is False:
                reasons.append("VALIDATOR_NOT_PASS")
            validator_status = str(validator.get("status") or "").upper()
            # On the GPT-first path a validator review signal is diagnostic
            # evidence, not a second semantic verdict.  GPT② already reports
            # unresolved atoms/conflicts through its persisted draft contract;
            # actual technical safety failures use ``passed=False`` above.
        # A deterministic rule/template may remain useful staff context, but
        # it cannot become a shadow semantic publisher when GPT①/② was not
        # available.  This is an explicit workflow failure, not a judgement
        # about the inquiry's intent or answerability.
        if gpt_pipeline_unavailable:
            reasons.append(UNDERSTANDING_UNAVAILABLE)
        if normalized_route not in AUTO_POSTABLE_ROUTES:
            reasons.append("INTENT_NOT_AUTO_POSTABLE")
        if normalized_route in REVIEW_ROUTES or any(
            marker in normalized_route
            for marker in ("REVIEW", "MANUAL", "BLOCKED", "FAILED", "UNCONFIRMED")
        ):
            reasons.append(f"ROUTE_{normalized_route or 'UNKNOWN'}")
        workflow_blocks = plan.get("workflow_block_reasons")
        if isinstance(workflow_blocks, (list, tuple)):
            reasons.extend(
                str(item).upper()
                for item in workflow_blocks
                if str(item).upper() in _PERSISTED_WORKFLOW_BLOCKS
            )
        product_guard_value = metadata.get("product_fact_guard")
        product_guard = (
            product_guard_value if isinstance(product_guard_value, dict) else {}
        )
        if product_guard.get("sensitive") and not product_guard.get(
            "current_fact_verified"
        ):
            reasons.append("PRODUCT_FACT_NOT_VERIFIED")
        # "this inquiry is high risk" and "the keyword classifier had no rule
        # for this wording" arrived in the same flag, and both hard-blocked.
        # Only the first is a safety finding. Inquiry 686125753 asked
        # "삼성센터AS무상기간알려주세요 / 배송기한얼마나생각하면될까요?" -- both
        # phrasings miss the keyword tables, so the inquiry was UNCLASSIFIED
        # and manual_review_required, and a grounded answer the validator
        # passed outright could still never be published. The classifier is a
        # routing aid; what makes an answer safe to send is the deterministic
        # validator, so an unclassified intent is recorded but stops blocking
        # once that validator has passed with nothing to report. Genuine high
        # risk, and any validator finding at all, still block.
        # Publishing policy, applied to what the answer actually says. The
        # routing rules hold a *question* the understanding recognised as a
        # schedule question; this holds an *answer* that names a delivery
        # period when no order is known to exist, whatever the question was
        # classified as. Confirmed orders are untouched -- for them the period
        # comes from DPS and is a real date.
        # Do not re-promote a legacy intent/subtype verdict here.  GPT② owns
        # evidence sufficiency; only explicit high-risk workflow policy above
        # may create a non-evidence review hold.
        # Compatibility is a fact the customer buys on. An exact fixed
        # template (the catalog's own verified accessory rules, or a Product
        # DB fact) may answer it; anything composed by the model without such
        # a source may be drafted for staff but not published.
        # Compatibility is a fact the customer buys on, and the rule stands.
        # What changed is what counts as a verified source: the keyword
        # classifier raising PRODUCT_COMPATIBILITY was blocking every route but
        # two, including one where GPT ② had read the catalogue rows and named
        # the ones it used. That is a verified source, so it is accepted beside
        # the fixed template and the Product DB. An answer that names no product
        # fact is still held.
        if review_status == "IN_REVIEW":
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

        # ORDER_ID_REQUEST is a terminal, confirmed operational template for
        # the current turn.  DPS may be a business requirement for the
        # customer's eventual schedule answer, but it is neither executable
        # nor evidence for the safe request asking for the missing order id.
        # Other routes retain the full DPS trust/snapshot gates.
        dps_required = bool(plan.get("requires_dps_lookup")) and not order_request_route
        if dps_required and str(plan.get("dps_lookup_status") or "").upper() == "DISABLED":
            reasons.append("DPS_LOOKUP_DISABLED")
        if dps_required and str(plan.get("dps_lookup_status") or "").upper() != "SUCCESS":
            reasons.append("DPS_RESULT_NOT_TRUSTED")
        if dps_required and not bool(plan.get("valid_dps_snapshot_available")):
            reasons.append("DPS_SNAPSHOT_NOT_VALIDATED")

        # The answer addresses a different request than the one that was made.
        #
        # "고장난 기존 tv 수거 요청드려요" was auto-posted with A/S guidance:
        # validator PASS, coverage PASS, no blocking reason -- each gate right
        # by its own definition, and none of them asking whether the answer
        # answered the question. This compares the action the customer asked
        # for against what the answer we produced is able to address.
        #
        # Read from what generation recorded, never re-derived here: this stage
        # has no provider and must not acquire one. A draft with no record, an
        # unlabelled answer, or an understanding the model could not supply all
        # leave this undetermined, and undetermined blocks nothing -- the gate
        # can only ever add a hold.

        # The same shape one level narrower: not which action was asked, but
        # which *property* of it. "비용은 누가 내나요" answered by "유상입니다"
        # passes every gate above -- right product, right subject, right
        # action -- and never says who pays. Read from what generation
        # recorded, never re-derived here; no record holds nothing.
        # Attribute coverage is legacy semantic telemetry. GPT② owns answer
        # sufficiency; deterministic routes are constrained by their verified
        # source/workflow invariants above, not by a second text classifier.

        # Stored Learning was offered as the grounds and nothing verified.
        # "사다리차는 유상입니다" against "비용은 누가 내나요" is the shape: right
        # subject, wrong relation, and every label-level check passes it. The
        # verdict is read from what generation recorded; no record holds nothing.
        # ``route`` is what separates "nothing to verify" from "should have
        # been verified and was not". Without it the gate cannot tell the two
        # apart, which is how the producer stayed missing for a whole release.

        # A substantive question the customer asked that the reply never
        # answers. The coverage evaluator measured this on the finished text
        # and generation already set ``requires_manual_review`` from it -- but
        # that flag is one another branch here can downgrade to a soft reason,
        # and did: a compound inquiry scored PARTIAL, carried the flag, and
        # auto-posted anyway because the TEMPLATE route resolves the hold
        # without ever consulting coverage. A measurement that cannot stop a
        # publish is telemetry, so the verdict is read here directly and is a
        # hard blocker no other resolver can lift.
        # Legacy lexical coverage remains persisted telemetry only.  It is not
        # an evidence verdict and cannot become a non-GPT semantic veto here.

        # A question with no factual source behind it. Read from retrieval's
        # own per-sub-question verdict, so a reply that names the gap in
        # fluent Korean cannot pass for one that answered it.

        # GPT ②'s own verdicts on the answer it wrote. These are the findings
        # the removed anchor-table rules were standing in for, reported by the
        # party that read both the question and the evidence, so they are hard
        # blockers rather than advisory.
        #
        # ``can_auto_post`` is tri-state on the draft: absent means the provider
        # never learned the field, and only an explicit false withholds.
        if gpt_draft.get("unresolved"):
            reasons.append(GPT_REPORTED_UNRESOLVED)
        if gpt_draft.get("can_auto_post") is False:
            reasons.append(GPT_WITHHELD_AUTO_POST)

        # An answer GPT ② composed, for a question GPT ① never read.
        #
        # That combination is the GPT-centred pipeline running on one engine.
        # GPT ① is what decides how the inquiry decomposes and which evidence to
        # fetch; without it, retrieval falls back to the keyword splitter and
        # the model composes from whatever that found, with no one having
        # established what was asked. A draft is still written and staff can
        # still send it -- only the automatic path closes.
        #
        # Scoped to the GPT-composed pipeline on purpose. A deterministic route
        # (a fixed template, a Product DB fact, an order-number request) does
        # not compose anything and is judged by the gates it always had, which
        # this release leaves untouched wherever GPT ① is absent -- see
        # ``HybridAnswerService._gpt_judges_evidence`` and the validator rules
        # that key on it.
        #
        # Legacy drafts predating this record are untouched: the marker has to
        # be present and say the understanding was unusable.
        semantic_routing = metadata.get("semantic_routing")
        semantic_routing = (
            semantic_routing if isinstance(semantic_routing, Mapping) else None
        )
        if gpt_final_pipeline and semantic_routing is not None:
            understanding = semantic_routing.get("understanding")
            understanding = (
                understanding if isinstance(understanding, Mapping) else {}
            )
            if understanding.get("usable") is not True:
                reasons.append(UNDERSTANDING_UNAVAILABLE)

        # Company policy, read from the understanding rather than the wording.
        #
        # Checked outside the ``gpt_final_pipeline`` branch above on purpose:
        # the policy holds whatever route produced the draft, and it must not
        # become reachable only while the GPT path is healthy.
        if semantic_routing is not None:
            policy_understanding = semantic_routing.get("understanding")
            policy_understanding = (
                policy_understanding
                if isinstance(policy_understanding, Mapping)
                else {}
            )
            questions = policy_understanding.get("questions")
            questions = questions if isinstance(questions, (list, tuple)) else ()
            asked_actions = {
                str(item.get("action") or "").strip().upper()
                for item in questions
                if isinstance(item, Mapping)
            }
            if asked_actions & POLICY_STAFF_ONLY_ACTIONS:
                reasons.append(RETURN_OR_DAMAGE_POLICY_REVIEW)

        # Confidence remains operator telemetry only.  It is deliberately in
        # SOFT_REASONS and therefore cannot change publishability.
        confidence = analysis.get("confidence")
        if confidence is not None:
            try:
                if float(confidence) < 0.8:
                    reasons.append("INTENT_CONFIDENCE_LOW")
            except (TypeError, ValueError):
                reasons.append("INTENT_CONFIDENCE_UNKNOWN")

        # A date the customer named, which nothing here can promise.
        #
        # "오늘 주문하면 9일까지 받아볼 수 있을까요?" was answered with the
        # standing visit-installation policy and auto-posted: validator PASS,
        # coverage UNKNOWN, no blocking reason. Topically it is a delivery
        # answer; it simply does not say whether the ninth is possible.
        #
        # A deadline can only be confirmed from a trusted schedule, so the
        # gate is the schedule, not the wording: with a SUCCESS DPS lookup and
        # a validated snapshot the existing schedule routes answer it and this
        # never fires. Without one -- a new purchase, or a lookup that did not
        # land -- there is no basis, and the reply goes to staff rather than
        # to the customer. Checked on every route, because the same
        # unanswerable question also reaches GPT.

        # Evidence + Authority first: a pessimistic confidence number from the
        # provider is not itself a safety finding. When retrieval proved every
        # sub-question answerable from SUPPORTED evidence, and no other reason
        # fired, the confidence score alone must not hold the answer back.
        # An unparseable score is still treated as unknown risk.

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
