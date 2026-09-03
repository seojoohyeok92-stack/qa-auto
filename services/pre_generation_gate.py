"""Skip the provider call for inquiries the final gate is already certain to hold.

This is not a second safety policy.  Publishing is decided in exactly one
place -- ``AutoProcessingEligibilityService`` -- and this module only asks that
same policy a narrower question, early:

    given what is already known, is there a hard reason that *no* generated
    answer could clear?

If yes, composing an answer cannot change the outcome, so the provider call is
pure cost and the inquiry goes straight to staff.  If there is any path by
which generation could clear the hold, the inquiry takes the normal route and
the real gate decides after the validator has seen a real answer.  Getting that
distinction wrong in the permissive direction costs a wasted GPT call; getting
it wrong in the restrictive direction silently stops answering inquiries the
system used to answer, which is why every skip condition below is derived from
the final gate's own predicates rather than restated.

The reason codes are the final gate's codes, unchanged, so an operator sees the
same sentence whether an inquiry was skipped early or held late.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.auto_processing_eligibility_service import (
    AutoProcessingEligibilityService,
)


# Mirrors services/inquiry_analysis_service.py's constant of the same name.
# Duplicated rather than imported to keep this gate free of a service-layer
# import cycle -- the same intentional duplication the learning modules use
# for their shared regexes.
PRE_PURCHASE_DELIVERY_REVIEW_SOURCE = "PRE_PURCHASE_DELIVERY_GUIDANCE"


@dataclass(frozen=True)
class PreGenerationDecision:
    """Whether to compose an answer, and the final gate's reason if not."""

    skip_generation: bool
    stage: str = ""
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "skip_generation": self.skip_generation,
            "stage": self.stage,
            "reasons": list(self.reasons),
        }


_CONTINUE = PreGenerationDecision(False)


class PreGenerationGate:
    """Asks the publishing policy whether generation could ever help."""

    @staticmethod
    def _resolvable_by_generation(
        analysis: dict[str, Any], plan: dict[str, Any]
    ) -> bool:
        """Whether a manual-review signal could still be cleared downstream.

        Mirrors the one precondition every resolution path in the final gate
        shares.  ``_preliminary_review_resolved`` starts by requiring
        ``_current_analysis_clears_review`` -- a re-analysis of the same
        inquiry text by the same deterministic classifier -- and refuses
        outright when the plan found real risk.  Re-analysis here would return
        what ``analysis`` already holds, so the same question is answered from
        it directly, with no second classification and no new policy.

        The relaxation that clears such a hold (``INTENT_UNCLASSIFIED_``
        ``VALIDATOR_CLEAR``) also needs a validator verdict on a real answer,
        which is exactly the case where generation is worth doing.
        """

        if bool(plan.get("is_high_risk")):
            return False
        return AutoProcessingEligibilityService._intent_unclassified(analysis)

    @staticmethod
    def _answer_still_has_value(
        analysis: dict[str, Any], plan: dict[str, Any]
    ) -> bool:
        """Whether a draft is worth writing even though it cannot be posted.

        Publishing is not the only thing generation is for. A compound inquiry
        is decomposed and answered part by part: the grounded sub-questions get
        real answers and the rest are explicitly deferred, which is the draft a
        person then edits and sends. One high-risk part among six does not make
        the other five worthless -- skipping there would hand staff a blank
        holding reply instead of five answered questions, which is a worse
        outcome for the customer than the provider call costs.

        So "cannot be auto-posted" and "not worth generating" are kept apart.
        The hold itself is unchanged either way.

        A delivery or schedule inquiry qualifies for the same reason, and it is
        the case that showed the rule was drawn too narrowly. "사장님 오늘
        주문했는데 해피콜 및 기사님 빠른설치 부탁드릴게요" is a single request
        for staff action, so it was skipped and the customer's own message came
        back with no draft at all -- staff opened the inquiry to a blank reply.
        Yet phase9 owns exactly this inquiry and answers it from a deterministic
        safe template ("요청하신 배송·설치 일정 변경은 담당자 확인이
        필요합니다"), which states no date, promises nothing, and names what the
        customer actually asked for. Writing that is strictly better than
        writing nothing, and it is not the model composing a claim.

        Narrowed to the case where the DPS lookup *cannot* run -- no validated
        order number, so there is nothing to look up. Generation then costs
        nothing outside this process and the safe template is the whole of what
        could ever be said. When the order number is there, the schedule is a
        real lookup: skipping still saves that call, and the inquiry keeps the
        handling it has today rather than gaining a DPS round trip as a side
        effect of wanting a draft.

        Genuinely unanswerable inquiries are still skipped: EMPTY_QUESTION and
        HIGH_RISK_OR_DISPUTE are refused earlier by ``can_generate_answer``,
        and a plan that found real risk is refused here -- "worth drafting"
        never outranks a risk finding, whatever the inquiry is about.
        """

        if bool(plan.get("is_high_risk")):
            return False
        if (
            str(analysis.get("inquiry_subtype") or "").upper()
            == "COMPOUND_MULTI_INTENT"
        ):
            return True
        # A pre-purchase delivery question is held from publishing, not from
        # being written. It is the same case as the schedule request above:
        # there is a deterministic safe answer for it, it states no date and
        # promises nothing, and handing staff that draft to edit beats handing
        # them an empty reply. Holding the *answer* and refusing to *draft* it
        # are different decisions, and only the first is the policy.
        if (
            str(analysis.get("inquiry_subtype") or "").upper()
            == PRE_PURCHASE_DELIVERY_REVIEW_SOURCE
        ):
            return True
        return bool(analysis.get("delivery_question")) and not bool(
            analysis.get("can_execute_dps_lookup")
        )

    @classmethod
    def evaluate_plan(
        cls, *, analysis: dict[str, Any], plan: dict[str, Any]
    ) -> PreGenerationDecision:
        """Decide from the processing plan alone, before any provider call.

        Only the review signals that the final gate raises from the plan and
        the deterministic analysis are considered.  Everything else it can
        raise -- validator findings, evidence gaps, order/DPS trust, route --
        either depends on an answer existing or is still being collected, and
        is deliberately left to the real gate.
        """

        analysis = analysis if isinstance(analysis, dict) else {}
        plan = plan if isinstance(plan, dict) else {}
        manual = bool(analysis.get("manual_review_required"))
        staff_review = bool(plan.get("needs_staff_review"))
        if not (manual or staff_review or plan.get("is_high_risk")):
            return _CONTINUE
        if cls._resolvable_by_generation(analysis, plan):
            return _CONTINUE
        if cls._answer_still_has_value(analysis, plan):
            return _CONTINUE

        reasons: list[str] = []
        if bool(plan.get("is_high_risk")) or manual:
            reasons.append("POLICY_OR_HIGH_RISK_REVIEW")
        if staff_review:
            reasons.append("PROCESSING_PLAN_REQUIRES_REVIEW")
        return PreGenerationDecision(
            True, "PROCESSING_PLAN", tuple(dict.fromkeys(reasons))
        )

    @staticmethod
    def evaluate_evidence(
        learning_context: dict[str, Any]
    ) -> PreGenerationDecision:
        """Decide once retrieval has run but before the provider is called.

        A sub-question whose evidence is in CONFLICT has sources that flatly
        disagree.  Retrieval already withholds every one of them, so the model
        would be composing from nothing while the customer's question is one
        the system demonstrably has contradictory records about -- the answer
        cannot be published whatever it says.  Asking anyway spends a provider
        call to arrive at the hold that is already certain.

        Only CONFLICT skips.  NO_RELIABLE_SOURCE does not: an answer may still
        legitimately ask the customer for what is missing, and NEEDS_DPS is a
        pending lookup, not a verdict.
        """

        context = learning_context if isinstance(learning_context, dict) else {}
        evidence = context.get("subquestion_evidence")
        if not isinstance(evidence, list):
            return _CONTINUE
        conflicted = [
            item for item in evidence
            if isinstance(item, dict)
            and str(item.get("status") or "").upper() == "CONFLICT"
        ]
        if not conflicted:
            return _CONTINUE
        return PreGenerationDecision(True, "EVIDENCE", ("EVIDENCE_CONFLICT",))
