from __future__ import annotations

import re
import uuid
import dataclasses
from datetime import datetime, timedelta
from typing import Any

from answer.inquiry_processing_plan import InquiryProcessingPlan
from answer.source_adapter import answer_request_from_inquiry
from dps.dates import STALE_DPS_SCHEDULE, is_schedule_stale
from repositories.database import Database
from repositories.dps_repository import DpsRepository
from repositories.workflow_repository import WorkflowRepository
from services.inquiry_analysis_service import InquiryAnalysisService
from services.semantic_analysis import (
    SemanticAnalysis,
    delivery_schedule_needs_review,
)
from answer.inquiry_analysis import AnswerStrategy, InquiryAnalysis
from repositories.answer_repository import AnswerRepository
from services.market_policy import is_store_dps_enabled
from services.phase9_answer_policy import build_delivery_answer_context
from workflow.models import StepCode


GENERAL_ORDER_ID = re.compile(r"\d{16}")
# A message that is nothing but an order number: the number, the words that
# introduce it, and the courtesies around it. Anything else left over -- a
# question, a request, a topic -- means the customer said something of their
# own, and it is read as that instead.
_ORDER_NUMBER_FILLER = re.compile(
    r"(?:일반|네이버)?\s*주문\s*번호|번호|입니다|이에요|예요|이요|요|"
    r"다시|보내\s*드립니다|보내\s*드려요|보내요|남깁니다|남겨\s*드립니다|"
    r"전달\s*드립니다|드립니다|부탁\s*드립니다|부탁\s*드려요|부탁해요|"
    r"확인\s*(?:부탁|해\s*주세요|해주세요|요청)?|이게|이것|이거|제|저의|"
    r"여기|네|안녕하세요|감사합니다|고맙습니다|[\s.,!?~:·\-()\[\]]"
)
# Delivery intents whose missing piece is exactly an order number. A schedule
# change is not one of them: an order number does not move a date.
_SCHEDULE_READ_INTENTS = frozenset({
    "DELIVERY_DATE", "DELIVERY_TIME", "INSTALLATION_DATE",
    "INSTALLATION_TIME", "DELIVERY_STATUS",
})
ORDER_NUMBER_FOLLOWUP_WINDOW = timedelta(days=7)


def order_number_only(question: object, order_id: str) -> bool:
    """Whether the message is only an order number, and nothing of its own."""

    text = str(question or "")
    if not order_id or order_id not in re.sub(r"\s+", "", text):
        return False
    remainder = re.sub(r"\d", "", text)
    return not _ORDER_NUMBER_FILLER.sub("", remainder)


def _moment(value: object) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else None


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
        self.answers = AnswerRepository(database)

    def _order_number_followup(
        self, inquiry: dict[str, Any], order_id: str,
    ) -> dict[str, Any] | None:
        """The delivery inquiry this bare order number was asked for, if any.

        Not conversation state. It links exactly one shape: the system asked
        this customer for an order number to look up a delivery or
        installation schedule, and the customer's next inquiry on the same
        listing is that number and nothing else. Every clause is required,
        because the customer key is weak -- Naver's writer id is masked to a
        prefix, so it is trusted only together with the same store and the
        same listing, and only for the customer's most recent earlier
        inquiry. Coupang has no DPS and carries no writer id at all, and a
        customer inquiry without one is never linked: an unidentifiable prior
        is no prior.
        """

        store = str(inquiry.get("store_code") or "")
        writer = str(inquiry.get("masked_writer_id") or "").strip()
        listing = str(inquiry.get("product_id") or "").strip()
        registered = _moment(
            inquiry.get("registered_at") or inquiry.get("created_at")
        )
        if not (
            is_store_dps_enabled(store) and writer and listing and registered
        ):
            return None
        if not order_number_only(inquiry.get("content"), order_id):
            return None
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, registered_at, created_at, order_id
                FROM inquiries
                WHERE store_code = ? AND source_type = ?
                  AND masked_writer_id = ? AND product_id = ? AND id <> ?
                """,
                (store, inquiry.get("source_type"), writer, listing,
                 int(inquiry["id"])),
            ).fetchall()
        earlier = sorted(
            (
                (moment, row)
                for row in rows
                if (moment := _moment(row["registered_at"] or row["created_at"]))
                and moment < registered
            ),
            key=lambda pair: pair[0],
        )
        if not earlier:
            return None
        moment, prior = earlier[-1]
        if registered - moment > ORDER_NUMBER_FOLLOWUP_WINDOW:
            return None
        draft = self.answers.latest_for_inquiry(int(prior["id"])) or {}
        metadata = draft.get("metadata_json")
        metadata = metadata if isinstance(metadata, dict) else {}
        prior_plan = metadata.get("processing_plan")
        prior_plan = prior_plan if isinstance(prior_plan, dict) else {}
        intent = str(prior_plan.get("detected_intent") or "").upper()
        if not (
            str(metadata.get("selected_answer_route") or "") == "ORDER_ID_REQUEST"
            and prior_plan.get("is_delivery") is True
            and intent in _SCHEDULE_READ_INTENTS
        ):
            return None
        return {
            "previous_inquiry_id": int(prior["id"]),
            "previous_intent": intent,
            "previous_route": "ORDER_ID_REQUEST",
            "linkage": "STORE+SOURCE+MASKED_WRITER_ID+PRODUCT_ID+MOST_RECENT",
        }

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
        semantic_analysis: SemanticAnalysis | None = None,
        semantic_routing: dict[str, Any] | None = None,
        deterministic_analysis: InquiryAnalysis | None = None,
    ) -> InquiryProcessingPlan:
        request = answer_request_from_inquiry(inquiry)
        analysis = (
            self.analysis._with_semantic(
                deterministic_analysis, semantic_analysis,
            )
            if deterministic_analysis is not None
            else self.analysis.analyze(request, semantic=semantic_analysis)
        )
        # A usable GPT ① result is the meaning authority.  This service still
        # performs mechanical plan work (identifier validation, cache state
        # and workflow actions), but it must not independently rediscover
        # whether customer-specific Order/DPS evidence was requested.
        understanding = (
            (semantic_routing or {}).get("understanding")
            if isinstance(semantic_routing, dict)
            else None
        )
        understanding = understanding if isinstance(understanding, dict) else {}
        gpt_understanding_usable = bool(understanding.get("usable") is True)
        if gpt_understanding_usable:
            analysis = dataclasses.replace(
                analysis,
                requires_order_lookup=bool(understanding.get("need_order")),
                requires_dps_lookup=bool(understanding.get("need_dps")),
                requires_order_id=bool(understanding.get("need_order")),
                purchase_confirmed=(
                    str(understanding.get("purchase_state") or "")
                    == "CURRENT_ORDER"
                ),
            )
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
        # This is a workflow invariant, not a legacy topic/subtype verdict:
        # GPT① established that the question asks for a delivery outcome but
        # the inquiry establishes no current order.  There is therefore no
        # customer-specific schedule that Q&A may complete or publish.  Keep
        # the fact in the immutable plan so the post-persistence worker reads
        # the same decision rather than reclassifying wording later.
        workflow_block_reasons: tuple[str, ...] = ()
        if delivery_schedule_needs_review(
            semantic_analysis,
            order_id_validated=(order_id_status == "VALID"),
        ):
            workflow_block_reasons = ("PRE_PURCHASE_DELIVERY_UNRESOLVED",)
        # A preserved identifier is not an execution requirement.  When the
        # semantic-aware plan says external order evidence is unnecessary,
        # every order-state field must say the same thing so no downstream
        # gate can reinterpret an unrelated blank as an order failure.
        # "This inquiry carries an order number" is not "this inquiry needs an
        # order lookup" -- except when the number is the whole message and it
        # answers the order-number request the system made on this
        # customer's previous delivery inquiry. Then that one intent, and
        # nothing else from the previous inquiry, carries over.
        order_number_followup = None
        if (
            order_id_status == "VALID"
            # GPT① may read a bare number as ORDER_IDENTIFICATION and ask
            # for an order lookup; what it cannot see is the schedule
            # question the number was sent for.
            and not analysis.requires_dps_lookup
            and not analysis.delivery_question
        ):
            order_number_followup = self._order_number_followup(
                inquiry, order_id,
            )
            if order_number_followup is not None:
                analysis = dataclasses.replace(
                    analysis,
                    detected_intent=order_number_followup["previous_intent"],
                    requires_order_lookup=True,
                    requires_dps_lookup=True,
                    requires_order_id=True,
                    purchase_confirmed=True,
                    answer_strategy=AnswerStrategy.DIRECT_FACT_ANSWER,
                    reasons=(
                        *analysis.reasons,
                        "이전 배송 문의에서 요청한 주문번호를 받은 문의입니다.",
                    ),
                )
        if not analysis.requires_order_lookup:
            order_id_status = "NOT_REQUIRED"

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

        # Retain the legacy label for telemetry only.  It is a semantic
        # classifier result, so it cannot suppress GPT retrieval/generation or
        # select a publish-blocking route.  GPT①/② decide evidence sufficiency;
        # deterministic order/DPS/action requirements below remain workflow.
        is_high_risk = analysis.inquiry_subtype == "HIGH_RISK_OR_DISPUTE"
        can_generate = bool(request.question.strip())
        if not analysis.delivery_question:
            route = "GPT_FALLBACK" if template_preferred else "GPT_DIRECT"
            reason = "GENERAL_ROUTE_PENDING_CONTENT_MATCH"
        elif not analysis.requires_order_lookup:
            # The plan has already decided this delivery question needs no
            # customer-specific order evidence. Routing it to ORDER_ID_REQUEST
            # would contradict that decision in the same object -- and for a
            # customer whose purchase is not confirmed, demanding an order
            # number is exactly what the confirmed policy forbids.
            route = "GPT_FALLBACK" if template_preferred else "GPT_DIRECT"
            reason = "DELIVERY_WITHOUT_ORDER_EVIDENCE_REQUIREMENT"
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
            # Intent/subtype classification is routing telemetry.  GPT① may
            # not recognise a wording while GPT② can still resolve every atom
            # from evidence; that must not become an independent publish veto.
            # Keep only workflow routes whose missing external state is a
            # deterministic safety condition.
            needs_staff_review=(
                route in {
                    "ORDER_ID_REQUEST",
                    "ORDER_LOOKUP_FAILED",
                    "DELIVERY_ORDER_NOT_FOUND",
                    "DPS_LOOKUP_FAILED",
                    "DELIVERY_DATE_UNCONFIRMED",
                }
            ),
            workflow_order_status=workflow_order_status,
            workflow_dps_status=workflow_dps_status,
            workflow_answer_status="READY" if can_generate else "BLOCKED",
            template_preferred=bool(template_preferred),
            template_id=None,
            generation_mode=("RULE" if analysis.delivery_question else route),
            reason_code=reason,
            correlation_id=correlation_id or str(uuid.uuid4()),
            analysis=analysis,
            semantic_routing=(dict(semantic_routing) if semantic_routing else None),
            workflow_block_reasons=workflow_block_reasons,
            order_number_followup=order_number_followup,
        )
