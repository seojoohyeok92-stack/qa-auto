from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from answer.models import AnswerRequest, AnswerResult
from dps.dates import STALE_DPS_SCHEDULE, is_schedule_stale


@dataclass(frozen=True)
class AnswerFacts:
    inquiry: dict[str, Any] = field(default_factory=dict)
    product: dict[str, Any] = field(default_factory=dict)
    order: dict[str, Any] = field(default_factory=dict)
    delivery: dict[str, Any] = field(default_factory=dict)
    installation: dict[str, Any] = field(default_factory=dict)
    dps: dict[str, Any] = field(default_factory=dict)
    rule: dict[str, Any] = field(default_factory=dict)
    activity: tuple[dict[str, Any], ...] = ()
    policy: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_prompt_dict(self) -> dict[str, Any]:
        """GPT에 필요한 사실만 반환하고 업무 식별자와 고객정보는 제외합니다."""

        data = self.to_dict()
        data["inquiry"].pop("inquiry_id", None)
        data["inquiry"].pop("question_id", None)
        data["order"].pop("order_id", None)
        data["order"].pop("product_order_id", None)
        data["dps"].pop("order_id", None)
        data["dps"].pop("sales_number", None)
        return data

    def get_fact(self, path: str) -> Any:
        current: Any = self.to_dict()
        for part in str(path).split("."):
            if not isinstance(current, dict) or part not in current:
                return None
            current = current[part]
        return current


def build_answer_facts(
    request: AnswerRequest,
    rule_result: AnswerResult,
) -> AnswerFacts:
    dps = (
        dict(request.metadata.get("dps"))
        if isinstance(request.metadata.get("dps"), dict)
        else {}
    )
    warnings = tuple(
        dict.fromkeys(
            [
                *[str(item) for item in rule_result.warnings],
                *[str(item) for item in dps.get("warnings", [])],
            ]
        )
    )
    # A date that had already passed when the customer wrote in belongs to a
    # previous delivery, not the one being asked about. The lookup stays
    # SUCCESS, but the date is not exposed as a confirmed schedule, so it can
    # never be handed to the model as "the current installation date" nor
    # asserted to the customer.
    schedule_stale = str(
        dps.get("schedule_validity") or ""
    ).upper() == STALE_DPS_SCHEDULE or is_schedule_stale(
        dps.get("installation_date"),
        registered_at=request.metadata.get("registered_at"),
        created_at=request.metadata.get("created_at"),
    )
    installation_confirmed = bool(
        dps.get("installation_date")
        and dps.get("installation_date_source")
        == "DPS_ITEM_DETAIL_REQUIRED_DELIVERY_DATE"
        and str(dps.get("date_parse_status") or "").upper() == "PARSED"
        and not dps.get("requires_human_review")
        and not schedule_stale
    )
    return AnswerFacts(
        inquiry={
            "inquiry_id": request.inquiry_id,
            "question_id": request.question_id,
            "type": request.inquiry_type,
            "question": request.question,
            "source_type": request.metadata.get("source_type"),
            "registered_at": request.metadata.get("registered_at"),
            "inquiry_subtype": (
                request.metadata.get("phase9_analysis", {}).get("inquiry_subtype")
                if isinstance(request.metadata.get("phase9_analysis"), dict)
                else None
            ),
        },
        product={
            "product_id": request.metadata.get("product_id") or None,
            "name": request.product_name or None,
            "option_name": request.option_name or None,
        },
        order={
            "order_id": request.order_id or None,
            "product_order_id": request.product_order_id or None,
            "order_date": request.metadata.get("order_date") or None,
            "payment_date": request.metadata.get("payment_date") or None,
            "shipping_due_date": request.metadata.get("shipping_due_date")
            or None,
            "order_status": request.metadata.get("order_status") or None,
        },
        delivery={
            "status": dps.get("delivery_status"),
            "queried_at": dps.get("queried_at"),
        },
        installation={
            "status": dps.get("installation_status"),
            "date": (
                dps.get("installation_date")
                if installation_confirmed
                else None
            ),
            "source": (
                dps.get("installation_date_source")
                if installation_confirmed
                else None
            ),
            "required_delivery_date": dps.get(
                "required_delivery_date"
            ),
            "installation_date_confirmed": installation_confirmed,
            "raw_required_delivery_date": dps.get(
                "raw_required_delivery_date"
            ),
            "date_parse_status": dps.get("date_parse_status"),
            "queried_at": (
                dps.get("lookup_timestamp") or dps.get("queried_at")
            ),
            "dps_lookup_id": dps.get("dps_lookup_id"),
            "time_text": dps.get("installation_time_text"),
            "type": dps.get("installation_type"),
        },
        dps={
            key: dps.get(key)
            for key in (
                "lookup_required",
                "lookup_status",
                "source",
                "order_id",
                "sales_number",
                "cache_used",
                "change_request",
                "error_code",
                "error_message",
                "required_delivery_date",
                "installation_date_source",
                "date_parse_status",
                "requires_human_review",
                "dps_lookup_id",
                "lookup_timestamp",
            )
        }
        | {
            "schedule_validity": (
                STALE_DPS_SCHEDULE if schedule_stale else None
            )
        },
        rule={
            "status": rule_result.status.value,
            "category": rule_result.category,
            "reason": rule_result.reason,
            "answer": rule_result.answer,
            "auto_answerable": rule_result.auto_answerable,
            "needs_review": rule_result.needs_review,
            "matched_rule": rule_result.matched_rule,
        },
        activity=tuple(
            item
            for item in request.metadata.get("activity", ())
            if isinstance(item, dict)
        ),
        policy={
            "facts_only": True,
            "must_not_guess": True,
            "requires_review": bool(
                rule_result.needs_review
                or dps.get("change_request")
                or dps.get("requires_human_review")
                or schedule_stale
            ),
            "actual_posting_enabled": False,
            "installation_notification_policy": (
                "최종 설치 일정은 설치 전날 카카오톡으로 안내합니다."
            ),
            "date_may_change": True,
        },
        warnings=warnings,
    )
