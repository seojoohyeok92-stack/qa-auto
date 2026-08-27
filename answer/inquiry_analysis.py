from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class InquiryType(str, Enum):
    DELIVERY_INSTALLATION_STATUS = "DELIVERY_INSTALLATION_STATUS"
    ORDER_STATUS = "ORDER_STATUS"
    ORDER_INFO_REQUIRED = "ORDER_INFO_REQUIRED"
    PRODUCT_GENERAL = "PRODUCT_GENERAL"
    INSTALLATION_GENERAL = "INSTALLATION_GENERAL"
    CANCEL_RETURN_EXCHANGE = "CANCEL_RETURN_EXCHANGE"
    INFORMATION_INSUFFICIENT = "INFORMATION_INSUFFICIENT"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"


class OrderIdStatus(str, Enum):
    NOT_REQUIRED = "NOT_REQUIRED"
    MISSING = "MISSING"
    CANDIDATE_FOUND = "CANDIDATE_FOUND"
    VALIDATED = "VALIDATED"
    INVALID = "INVALID"
    AMBIGUOUS = "AMBIGUOUS"


class AnswerStrategy(str, Enum):
    DIRECT_FACT_ANSWER = "DIRECT_FACT_ANSWER"
    REQUEST_ORDER_ID = "REQUEST_ORDER_ID"
    REQUEST_ADDITIONAL_INFORMATION = "REQUEST_ADDITIONAL_INFORMATION"
    GENERAL_GUIDANCE = "GENERAL_GUIDANCE"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    BLOCKED_UNVERIFIED_FACT = "BLOCKED_UNVERIFIED_FACT"


@dataclass(frozen=True)
class InquiryAnalysis:
    inquiry_type: InquiryType
    inquiry_subtype: str
    requires_order_lookup: bool
    requires_dps_lookup: bool
    requires_order_id: bool
    order_id_present: bool
    order_id_validated: bool
    order_id_status: OrderIdStatus
    answer_strategy: AnswerStrategy
    selected_fact_keys: tuple[str, ...]
    confidence: float
    reasons: tuple[str, ...]
    manual_review_required: bool
    auto_answerable: bool
    detected_intent: str = "GENERAL"
    # Which finding(s) raised ``manual_review_required``. On a compound inquiry
    # the flag is OR-ed across sub-questions, so the flag alone cannot say
    # whether a person is needed because the message carries real risk or
    # because one sub-question missed the keyword tables. One entry per
    # contributing sub-question, holding that sub-question's own subtype.
    # Empty whenever no review is required -- and deliberately empty on any
    # analysis built outside this classifier, so a caller that cannot see the
    # cause must treat the hold as unexplained.
    manual_review_sources: tuple[str, ...] = ()
    # One record per atomic question the customer actually asked, each holding
    # that question's own verdict. ``manual_review_required`` above is the OR
    # across them and is what the safety gates read; it answers "does a person
    # need to see this inquiry", which is a different question from "which of
    # these can we answer".
    #
    # Conflating the two is what produced the reported failure: a four-question
    # inquiry where one part needed a person came back with nothing answered at
    # all. Keeping the per-question verdicts lets a draft answer what it can
    # and name what it cannot, while the inquiry still goes to review.
    #
    # Runtime only -- serialised into existing JSON metadata, never a column.
    # Empty on any analysis built outside this classifier, so a caller that
    # cannot see the breakdown must not assume there was only one question.
    subquestion_analyses: tuple[dict[str, Any], ...] = ()

    @property
    def answerable_subquestions(self) -> tuple[dict[str, Any], ...]:
        """The atomic questions this classifier believes can be answered."""

        return tuple(
            item
            for item in self.subquestion_analyses
            if not item.get("manual_review_required")
        )

    @property
    def unresolved_subquestions(self) -> tuple[dict[str, Any], ...]:
        """The atomic questions that need a person, with their own reason."""

        return tuple(
            item
            for item in self.subquestion_analyses
            if item.get("manual_review_required")
        )

    @property
    def delivery_question(self) -> bool:
        return self.detected_intent in {
            "DELIVERY_DATE",
            "DELIVERY_TIME",
            "INSTALLATION_DATE",
            "INSTALLATION_TIME",
            "DELIVERY_STATUS",
            "SCHEDULE_CHANGE",
        } or self.inquiry_subtype in {
            "DELIVERY_OR_INSTALLATION_SCHEDULE",
            "LEGACY_DELIVERY_CATEGORY",
            "SCHEDULE_CHANGE_REQUEST",
        }

    @property
    def delivery_related(self) -> bool:
        return self.delivery_question or self.inquiry_type in {
            InquiryType.DELIVERY_INSTALLATION_STATUS,
            InquiryType.ORDER_INFO_REQUIRED,
        }

    @property
    def needs_delivery_lookup(self) -> bool:
        # This represents the inquiry's need for delivery lookup. Whether DPS
        # can be attempted now remains available separately as
        # requires_dps_lookup and depends on a validated general order_id.
        return self.delivery_question

    @property
    def can_execute_dps_lookup(self) -> bool:
        """Whether the required DPS lookup can be called right now."""

        return self.requires_dps_lookup and self.order_id_validated

    @property
    def can_generate_answer(self) -> bool:
        """Whether the pipeline has a safe answer route for this inquiry."""

        # MISSING_ITEM_REPORT joins these because no wording resolves it. What
        # the customer needs is someone checking the outbound record, and an
        # automatic reply of any kind marks the inquiry answered on Naver --
        # removing it from the list staff use to find the ones still open.
        if self.inquiry_subtype in {
            "EMPTY_QUESTION", "HIGH_RISK_OR_DISPUTE", "MISSING_ITEM_REPORT",
        }:
            return False
        return True

    @property
    def question_category(self) -> str:
        return self.inquiry_type.value

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["inquiry_type"] = self.inquiry_type.value
        result["order_id_status"] = self.order_id_status.value
        result["answer_strategy"] = self.answer_strategy.value
        result["selected_fact_keys"] = list(self.selected_fact_keys)
        result["reasons"] = list(self.reasons)
        result["manual_review_sources"] = list(self.manual_review_sources)
        result["subquestion_analyses"] = [
            dict(item) for item in self.subquestion_analyses
        ]
        result["delivery_question"] = self.delivery_question
        result["delivery_related"] = self.delivery_related
        result["needs_delivery_lookup"] = self.needs_delivery_lookup
        result["can_execute_dps_lookup"] = self.can_execute_dps_lookup
        result["can_generate_answer"] = self.can_generate_answer
        result["question_category"] = self.question_category
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "InquiryAnalysis":
        return cls(
            inquiry_type=InquiryType(str(value["inquiry_type"])),
            inquiry_subtype=str(value.get("inquiry_subtype") or ""),
            requires_order_lookup=bool(value.get("requires_order_lookup")),
            requires_dps_lookup=bool(value.get("requires_dps_lookup")),
            requires_order_id=bool(value.get("requires_order_id")),
            order_id_present=bool(value.get("order_id_present")),
            order_id_validated=bool(value.get("order_id_validated")),
            order_id_status=OrderIdStatus(
                str(value.get("order_id_status") or "NOT_REQUIRED")
            ),
            answer_strategy=AnswerStrategy(
                str(value.get("answer_strategy") or "MANUAL_REVIEW")
            ),
            selected_fact_keys=tuple(
                str(item) for item in value.get("selected_fact_keys", ())
            ),
            confidence=float(value.get("confidence", 0)),
            reasons=tuple(str(item) for item in value.get("reasons", ())),
            manual_review_required=bool(
                value.get("manual_review_required")
            ),
            auto_answerable=bool(value.get("auto_answerable")),
            detected_intent=str(value.get("detected_intent") or "GENERAL"),
            subquestion_analyses=tuple(
                dict(item)
                for item in value.get("subquestion_analyses", ())
                if isinstance(item, dict)
            ),
        )
