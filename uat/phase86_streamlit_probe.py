from __future__ import annotations

import os
from types import SimpleNamespace

from answer.models import AnswerResult, AnswerStatus
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.gpt_provider_run_repository import (
    GptProviderRunRepository,
)
from repositories.inquiry_repository import InquiryRepository
from services.answer_service import AnswerService as RealAnswerService
import ui.review_workspace as workspace


database = Database()
database.initialize()
inquiry_id = int(os.environ["PHASE86_INQUIRY_ID"])
inquiry = InquiryRepository(database).get(inquiry_id)
if inquiry is None:
    raise LookupError(f"Inquiry not found: {inquiry_id}")

panel = os.getenv("PHASE86_PANEL", "answer")
failure = os.getenv("PHASE86_DPS_FAILURE", "")
fake_answer = os.getenv("PHASE86_FAKE_ANSWER", "")
empty_answer = os.getenv("PHASE86_EMPTY_ANSWER", "") == "1"
order_status = os.getenv("PHASE86_ORDER_STATUS", "").upper()
dps_status = os.getenv("PHASE86_DPS_STATUS", "").upper()
dps_date = os.getenv("PHASE86_DPS_DATE", "")
if panel == "dps":
    if failure:
        class FailingOrchestrator:
            def __init__(self, database):
                self.database = database

            def lookup(self, *args, **kwargs):
                if failure == "system_exit":
                    raise SystemExit("simulated")
                raise RuntimeError(failure)

        workspace.DpsLookupOrchestrator = FailingOrchestrator
    workspace._render_dps(database, inquiry)
else:
    # AppTest reuses imported modules in one pytest process. Restore the real
    # service before applying an optional per-run deterministic double.
    workspace.AnswerService = RealAnswerService
    if order_status:
        class DeterministicOrderLookup:
            def lookup_for_inquiry(self, selected_inquiry_id, **kwargs):
                if order_status == "SUCCESS":
                    return {
                        "success": True,
                        "orders": [{"order_id": inquiry.get("order_id")}],
                        "cached": False,
                    }
                return {
                    "success": False,
                    "orders": [],
                    "error_code": (
                        "ORDER_NOT_FOUND"
                        if order_status == "NOT_FOUND"
                        else "ORDER_LOOKUP_FAILED"
                    ),
                }

        class DeterministicDps:
            def enrich(self, request, **kwargs):
                metadata = {
                    "lookup_required": True,
                    "lookup_status": dps_status or "TIMEOUT",
                    "installation_date": dps_date or None,
                    "required_delivery_date": dps_date or None,
                    "date_parse_status": "PARSED" if dps_date else "MISSING",
                }
                request.metadata["dps"] = metadata
                return SimpleNamespace(
                    decision=SimpleNamespace(lookup_required=True),
                    metadata=metadata,
                    lookup_row=None,
                )

            def skip_for_phase9(self, request, **kwargs):
                metadata = {
                    "lookup_required": False,
                    "lookup_status": "NOT_REQUIRED",
                }
                request.metadata["dps"] = metadata
                return SimpleNamespace(
                    decision=SimpleNamespace(lookup_required=False),
                    metadata=metadata,
                    lookup_row=None,
                )

        class PlannedAnswerService(RealAnswerService):
            def __init__(self, selected_database):
                super().__init__(
                    selected_database,
                    order_lookup_service=DeterministicOrderLookup(),
                    dps_enrichment=DeterministicDps(),
                )

        workspace.AnswerService = PlannedAnswerService
    if fake_answer or empty_answer:
        class DeterministicAnswerService:
            def __init__(self, database):
                self.database = database

            def generate_for_inquiry(
                self,
                selected_inquiry_id,
                *,
                prefer_template=True,
                correlation_id=None,
                processing_plan=None,
            ):
                if empty_answer:
                    result = AnswerResult(
                        status=AnswerStatus.GENERATED,
                        category="상품",
                        reason="phase86-empty-ui-test",
                        answer=" \n\r\n",
                        provider="openai_hybrid",
                        auto_answerable=True,
                        needs_review=False,
                    )
                    return SimpleNamespace(
                        result=result,
                        draft={
                            "id": 999999,
                            "original_answer": result.answer,
                            "is_active": False,
                        },
                    )
                result = AnswerResult(
                    status=AnswerStatus.GENERATED,
                    category="배송/설치현황",
                    reason="phase86-ui-test",
                    answer=fake_answer,
                    provider="openai_hybrid",
                    auto_answerable=True,
                    needs_review=False,
                    metadata={
                        "answer_type": "gpt_generated",
                        "answer_source": "openai",
                        "generation_mode": (
                            "GPT_FALLBACK"
                            if prefer_template
                            else "GPT_DIRECT"
                        ),
                        "selected_answer_route": (
                            "GPT_FALLBACK"
                            if prefer_template
                            else "GPT_DIRECT"
                        ),
                        "template_preferred": bool(prefer_template),
                        "template_override": not bool(prefer_template),
                        "template_id": None,
                        "template_name": None,
                        "template_version": None,
                        "order_id_present": bool(inquiry.get("order_id")),
                        "delivery_question": False,
                        "dps_lookup_attempted": False,
                        "delivery_date_found": False,
                        "gpt_called": True,
                        "hybrid": {
                            "fallback_used": False,
                            "validation": {"passed": True},
                        }
                    },
                )
                draft = AnswerRepository(
                    self.database
                ).create_program_draft(
                    selected_inquiry_id,
                    result,
                    order_id=inquiry.get("order_id"),
                )
                GptProviderRunRepository(self.database).create_run(
                    inquiry_id=selected_inquiry_id,
                    draft_id=draft["id"],
                    correlation_id="phase86-ui",
                    provider="openai",
                    model="test-model",
                    mode="ACTIVE",
                    started_at="2026-07-30T10:00:00+09:00",
                    completed_at="2026-07-30T10:00:01+09:00",
                    success=True,
                    validator_passed=True,
                )
                return SimpleNamespace(result=result, draft=draft)

        workspace.AnswerService = DeterministicAnswerService
    workspace._render_answer_panel(database, inquiry)
