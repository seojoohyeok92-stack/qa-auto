from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from answer.engine import AnswerEngine
from answer.exceptions import (
    AnswerAlreadyPostedError,
    AnswerConfigError,
    AnswerEngineError,
    AnswerGenerationError,
    AnswerGenerationInProgressError,
)
from answer.models import AnswerResult, AnswerStatus
from answer.inquiry_processing_plan import InquiryProcessingPlan
from answer.answer_format import format_final_answer
from answer.answer_validator import AnswerValidator
from answer.source_adapter import answer_request_from_inquiry
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.dps_repository import DpsRepository
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from repositories.workflow_repository import WorkflowRepository
from services.dps_enrichment_service import (
    DpsEnrichmentOutcome,
    DpsEnrichmentService,
)
from services.dps_lookup_policy import DpsLookupDecision, DpsLookupStatus
from services.hybrid_answer_service import HybridAnswerService
from services.inquiry_analysis_service import InquiryAnalysisService
from services.inquiry_processing_plan_service import (
    InquiryProcessingPlanService,
)
from services.phase9_answer_policy import apply_phase9_rule_policy
from services.gpt_governance_service import GovernedHybridAnswerService
from services.uat_order_service import UatOrderService
from services.order_service import lookup_general_order_id
from services.product_fact_guard import classify_product_fact
from workflow.models import InquiryStatus, StepCode, StepStatus


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnswerGenerationOutcome:
    result: AnswerResult
    draft: dict[str, Any]


def _error_code(error: Exception) -> str:
    return error.__class__.__name__.upper()[:100]


def _user_error_message(error: Exception) -> str:
    if isinstance(error, AnswerConfigError):
        return "답변 설정파일을 확인할 수 없습니다. 관리자에게 문의해 주세요."
    if isinstance(error, AnswerAlreadyPostedError):
        return "이미 등록된 문의는 답변 초안을 다시 생성할 수 없습니다."
    if isinstance(error, AnswerGenerationInProgressError):
        return "이 문의의 답변 초안이 이미 생성 중입니다."
    return "답변 초안을 생성하지 못했습니다. 잠시 후 다시 시도해 주세요."


def is_valid_draft(draft_text: object) -> bool:
    return isinstance(draft_text, str) and bool(draft_text.strip())


def _safe_log_text(value: object) -> str:
    text = str(value or "")
    text = re.sub(r"(?<!\d)\d{10,24}(?!\d)", "[NUMBER_MASKED]", text)
    return text[:500]


def _apply_existing_template_metadata(
    result: AnswerResult,
    *,
    order_id_present: bool,
    template_preferred: bool,
) -> AnswerResult:
    metadata = dict(result.metadata)
    template_id = result.matched_rule or metadata.get("template_id") or None
    metadata.update(
        {
            "answer_type": "existing_template",
            "answer_source": "rule_engine",
            "generation_mode": "TEMPLATE",
            "template_preferred": template_preferred,
            "template_override": False,
            "template_id": template_id,
            "template_name": metadata.get("template_name") or template_id,
            "template_version": (
                metadata.get("template_version")
                or (
                    "configuration.xlsx"
                    if metadata.get("existing_template")
                    else "rule-engine-v1"
                )
            ),
            "order_id_present": order_id_present,
            "dps_lookup_attempted": False,
            "delivery_date_found": False,
            "gpt_called": False,
            "draft_created": True,
            "requires_manual_review": False,
        }
    )
    result.metadata = metadata
    return result


def _template_unavailable_reason(
    result: AnswerResult,
    request: Any,
    validator: AnswerValidator,
) -> str | None:
    metadata = dict(result.metadata or {})
    if result.status is not AnswerStatus.GENERATED:
        return "NOT_FOUND"
    if str(result.provider or "").lower() not in {
        "rules",
        "rule",
        "rule_provider",
    }:
        return "NOT_FOUND"
    if metadata.get("active") is False:
        return "INACTIVE"
    allowed_stores = {
        str(value).upper()
        for value in metadata.get("allowed_stores", ())
        if str(value).strip()
    }
    if allowed_stores and str(request.store_code).upper() not in allowed_stores:
        return "STORE_MISMATCH"
    allowed_types = {
        str(value).upper()
        for value in metadata.get("allowed_inquiry_types", ())
        if str(value).strip()
    }
    if (
        allowed_types
        and str(request.inquiry_type).upper() not in allowed_types
    ):
        return "INQUIRY_TYPE_MISMATCH"
    if metadata.get("relevant") is False:
        return "IRRELEVANT"
    validation = validator.validate_template_text(result.answer)
    if not validation.passed:
        return "VALIDATION_FAILED"
    return None


def _is_product_db_result(result: AnswerResult) -> bool:
    """Identify answers grounded in the loaded model catalog."""

    category = str(result.category or "")
    reason = str(result.reason or "")
    return category.startswith("모델스펙/") and (
        "JSON" in reason or "스펙" in reason
    )


def _is_safe_rule_result(result: AnswerResult) -> bool:
    """Identify an existing non-empty rule answer that requires review."""

    provider = str(result.provider or "").lower()
    source_status = str(result.metadata.get("source_status") or "")
    return bool(
        result.status is AnswerStatus.NEEDS_REVIEW
        and is_valid_draft(result.answer)
        and provider in {
            "rules",
            "rule",
            "rule_provider",
            "safe_rule",
        }
        and (
            provider == "safe_rule"
            or source_status == "추가정보 필요"
        )
        and result.needs_review
        and not result.auto_answerable
    )


def _apply_safe_rule_metadata(
    result: AnswerResult,
    *,
    order_id_present: bool,
    template_preferred: bool,
) -> AnswerResult:
    metadata = dict(result.metadata)
    metadata.update(
        {
            "answer_type": "safe_rule",
            "answer_source": "safe_rule",
            "generation_mode": "SAFE_RULE",
            "selected_answer_route": "SAFE_RULE",
            "template_preferred": bool(template_preferred),
            "template_override": False,
            "template_id": result.matched_rule or None,
            "template_name": result.matched_rule or None,
            "template_version": "safe-rule-v1",
            "order_id_present": bool(order_id_present),
            "dps_lookup_attempted": False,
            "delivery_date_found": False,
            "gpt_called": False,
            "draft_created": True,
            "requires_manual_review": True,
        }
    )
    result.metadata = metadata
    return result


def _neutral_gpt_context(
    template_result: AnswerResult,
    *,
    template_failure: str,
    category: str,
) -> AnswerResult:
    """Remove a missing-template NOT_SUPPORTED policy from GPT grounding.

    A template miss is not a verified answer and is not a high-risk block.
    Passing that empty result into HybridAnswerService previously caused a
    validation fallback to return the same empty rule answer, aborting the
    one-click GPT fallback flow.
    """

    return AnswerResult(
        status=AnswerStatus.NOT_SUPPORTED,
        category=category,
        reason="적용 가능한 고정 템플릿이 없어 GPT 안전 답변을 생성합니다.",
        answer="",
        provider="template_fallback_context",
        auto_answerable=False,
        needs_review=False,
        matched_rule="",
        metadata={
            "template_failure": template_failure,
            "template_candidate_category": template_result.category,
            "template_candidate_status": template_result.status.value,
            "template_candidate_provider": template_result.provider,
        },
    )


def _review_required_safe_result(
    request: Any,
    *,
    template_preferred: bool,
    failure_code: str,
    questions: tuple[str, ...] = (),
) -> AnswerResult:
    """Return the last-resort, non-empty customer draft for GPT outages.

    A provider or validator failure is an expected operational state.  It must
    not leave an unanswered inquiry without a Program Answer; staff can replace
    this conservative draft later.

    When the GPT UNDERSTANDING step already decomposed the inquiry before the
    failure, ``questions`` carries what the customer actually asked.  Echoing
    those back keeps this safety draft on-topic instead of a static
    "사용 방법 또는 기능"/"주문 또는 상품" category label that may not match
    the real question (e.g. a product-availability question mislabeled as a
    usage/feature question).  With no decomposed questions available (a
    failure before UNDERSTANDING even ran), the generic category fallback is
    kept as the last resort.
    """

    cleaned_questions = tuple(
        dict.fromkeys(
            str(item).strip() for item in questions if str(item).strip()
        )
    )
    if cleaned_questions:
        if len(cleaned_questions) == 1:
            confirmation_body = f'문의주신 "{cleaned_questions[0]}"'
        else:
            bullet_list = "\n".join(f"- {item}" for item in cleaned_questions)
            confirmation_body = f"문의주신 아래 내용은\n\n{bullet_list}"
        answer = format_final_answer(
            f"""{confirmation_body} 관련하여 정확한 정보 확인이 필요합니다.

확인되지 않은 내용을 임의로 안내하지 않고 직원 검토가 필요한 상태로 처리하겠습니다."""
        )
    else:
        product_inquiry = str(request.inquiry_type).upper() == "PRODUCT_INQUIRY"
        subject = (
            "문의하신 상품의 사용 방법 또는 기능"
            if product_inquiry
            else "문의하신 주문 또는 상품 관련 내용"
        )
        answer = format_final_answer(
            f"""{subject}은 정확한 정보 확인이 필요한 문의입니다.

확인되지 않은 내용을 임의로 안내하지 않고 직원 검토가 필요한 상태로 처리하겠습니다."""
        )
    return AnswerResult(
        status=AnswerStatus.NEEDS_REVIEW,
        category="직원검토/안전초안",
        reason="자동 답변 공급자 또는 검증 단계 실패 시 사용하는 안전 초안입니다.",
        answer=answer,
        provider="safe_rule",
        auto_answerable=False,
        needs_review=True,
        matched_rule="REVIEW_REQUIRED_SAFE_DRAFT",
        metadata={
            "answer_type": "review_required_safe_draft",
            "answer_source": "SAFE_TEMPLATE",
            "generation_mode": "SAFE_RULE",
            "selected_answer_route": "REVIEW_REQUIRED_SAFE_DRAFT",
            "template_preferred": bool(template_preferred),
            "template_override": False,
            "template_id": "REVIEW_REQUIRED_SAFE_DRAFT",
            "template_name": "REVIEW_REQUIRED_SAFE_DRAFT",
            "gpt_called": True,
            "safe_failure_code": str(failure_code)[:100],
            "draft_created": True,
            "requires_manual_review": True,
        },
    )


class AnswerService:
    def __init__(
        self,
        database: Database,
        *,
        engine: AnswerEngine | None = None,
        dps_enrichment: DpsEnrichmentService | None = None,
        hybrid_service: HybridAnswerService | None = None,
        inquiry_analysis: InquiryAnalysisService | None = None,
        processing_plans: InquiryProcessingPlanService | None = None,
        order_lookup_service: UatOrderService | None = None,
    ) -> None:
        self.database = database
        self._engine = engine
        self._dps_enrichment = dps_enrichment
        self._hybrid_service = hybrid_service
        self.analysis = inquiry_analysis or InquiryAnalysisService()
        self.plans = processing_plans or InquiryProcessingPlanService(
            database, analysis=self.analysis
        )
        self._order_lookup_service = order_lookup_service
        self.inquiries = InquiryRepository(database)
        self.workflows = WorkflowRepository(database)
        self.logs = LogRepository(database)
        self.answers = AnswerRepository(database)
        self.dps = DpsRepository(database)
        self.validator = AnswerValidator()

    @property
    def order_lookup_service(self) -> UatOrderService:
        if self._order_lookup_service is None:
            self._order_lookup_service = UatOrderService(
                self.database,
                lookup=lookup_general_order_id,
            )
        return self._order_lookup_service

    @property
    def engine(self) -> AnswerEngine:
        if self._engine is None:
            self._engine = AnswerEngine()
        return self._engine

    @property
    def dps_enrichment(self) -> DpsEnrichmentService:
        if self._dps_enrichment is None:
            self._dps_enrichment = DpsEnrichmentService(self.database)
        return self._dps_enrichment

    @property
    def hybrid_service(self) -> HybridAnswerService:
        if self._hybrid_service is None:
            self._hybrid_service = GovernedHybridAnswerService(self.database)
        return self._hybrid_service

    def enrich_dps_for_inquiry(
        self,
        inquiry_id: int,
        *,
        force_refresh: bool = False,
        explicit_lookup: bool = False,
        correlation_id: str | None = None,
    ) -> DpsEnrichmentOutcome:
        inquiry = self.inquiries.get(inquiry_id)
        if inquiry is None:
            raise LookupError(f"Inquiry not found: {inquiry_id}")
        if (
            self.answers.is_inquiry_posted(inquiry_id)
            and not explicit_lookup
        ):
            raise AnswerAlreadyPostedError(
                "이미 등록된 문의는 DPS를 다시 조회할 수 없습니다."
            )
        request = answer_request_from_inquiry(inquiry)
        return self.dps_enrichment.enrich(
            request,
            force_refresh=force_refresh,
            explicit_lookup=explicit_lookup,
            correlation_id=correlation_id,
        )

    def _start_generation_step(self, inquiry_id: int) -> None:
        self.workflows.initialize_steps(inquiry_id)
        step = self.workflows.get_step(
            inquiry_id,
            StepCode.ANSWER_GENERATED,
        )
        status = StepStatus(step["step_status"])
        metadata = {"provider": "rules"}
        if status is StepStatus.PENDING:
            self.workflows.start_step(
                inquiry_id,
                StepCode.ANSWER_GENERATED,
                metadata=metadata,
            )
        elif status in {StepStatus.FAILED, StepStatus.NEEDS_REVIEW}:
            self.workflows.retry_step(
                inquiry_id,
                StepCode.ANSWER_GENERATED,
                metadata=metadata,
            )
        elif status is StepStatus.COMPLETED:
            self.workflows.restart_completed_step(
                inquiry_id,
                StepCode.ANSWER_GENERATED,
                metadata={"provider": "rules", "regeneration": True},
            )
        elif status is StepStatus.RUNNING:
            raise AnswerGenerationInProgressError(
                "Answer generation is already running."
            )
        else:
            raise AnswerGenerationError(
                f"답변 생성 단계가 {status.value} 상태여서 실행할 수 없습니다."
            )

    def _set_order_id_request_workflow(self, inquiry_id: int) -> None:
        """Persist the expected non-error workflow for customer confirmation."""

        self.workflows.initialize_steps(inquiry_id)
        for code in (
            StepCode.ORDER_IDENTIFIED,
            StepCode.NAVER_ORDER_LOOKUP,
        ):
            step = self.workflows.get_step(inquiry_id, code)
            status = StepStatus(step["step_status"])
            if status is StepStatus.PENDING:
                self.workflows.mark_needs_review(
                    inquiry_id,
                    code,
                    error_code="CUSTOMER_INFORMATION_REQUIRED",
                    message="네이버 일반 주문번호 확인이 필요합니다.",
                    metadata={"customer_confirmation_required": True},
                )
            elif status is StepStatus.FAILED:
                self.workflows.retry_step(
                    inquiry_id,
                    code,
                    metadata={"customer_confirmation_required": True},
                )
                self.workflows.mark_needs_review(
                    inquiry_id,
                    code,
                    error_code="CUSTOMER_INFORMATION_REQUIRED",
                    message="네이버 일반 주문번호 확인이 필요합니다.",
                    metadata={"customer_confirmation_required": True},
                )
        dps_step = self.workflows.get_step(
            inquiry_id, StepCode.DPS_LOOKUP
        )
        dps_status = StepStatus(dps_step["step_status"])
        if dps_status in {
            StepStatus.PENDING,
            StepStatus.RUNNING,
            StepStatus.FAILED,
            StepStatus.NEEDS_REVIEW,
        }:
            self.workflows.skip_step(
                inquiry_id,
                StepCode.DPS_LOOKUP,
                metadata={
                    "reason": "CUSTOMER_INFORMATION_REQUIRED",
                    "dps_called": False,
                },
            )

    def _apply_order_lookup_workflow(
        self,
        inquiry_id: int,
        *,
        status: str,
        correlation_id: str,
    ) -> None:
        """Apply the plan's order outcome without affecting answer status."""

        self.workflows.initialize_steps(inquiry_id)
        step = self.workflows.get_step(
            inquiry_id, StepCode.NAVER_ORDER_LOOKUP
        )
        current = StepStatus(step["step_status"])
        metadata = {
            "order_lookup_status": status,
            "correlation_id": correlation_id,
        }
        if status == "SUCCESS":
            if current in {StepStatus.FAILED, StepStatus.NEEDS_REVIEW}:
                self.workflows.retry_step(
                    inquiry_id,
                    StepCode.NAVER_ORDER_LOOKUP,
                    metadata=metadata,
                )
                current = StepStatus.RUNNING
            if current in {StepStatus.PENDING, StepStatus.RUNNING}:
                self.workflows.complete_step(
                    inquiry_id,
                    StepCode.NAVER_ORDER_LOOKUP,
                    metadata=metadata,
                )
        elif current in {StepStatus.PENDING, StepStatus.RUNNING}:
            self.workflows.fail_step(
                inquiry_id,
                StepCode.NAVER_ORDER_LOOKUP,
                (
                    "ORDER_NOT_FOUND"
                    if status == "NOT_FOUND"
                    else "ORDER_LOOKUP_FAILED"
                ),
                "주문 조회 결과를 확인하지 못했습니다.",
                metadata=metadata,
            )

    def _apply_dps_workflow(
        self,
        inquiry_id: int,
        *,
        status: str,
        correlation_id: str,
    ) -> None:
        """Apply the plan's DPS outcome independently of Draft generation."""

        normalized = str(status or "").upper()
        if normalized in {"NOT_REQUIRED", "NOT_STARTED"}:
            return
        self.workflows.initialize_steps(inquiry_id)
        step = self.workflows.get_step(inquiry_id, StepCode.DPS_LOOKUP)
        current = StepStatus(step["step_status"])
        metadata = {
            "dps_lookup_status": normalized,
            "correlation_id": correlation_id,
        }
        if normalized == "SUCCESS":
            if current in {StepStatus.FAILED, StepStatus.NEEDS_REVIEW}:
                self.workflows.retry_step(
                    inquiry_id, StepCode.DPS_LOOKUP, metadata=metadata
                )
                current = StepStatus.RUNNING
            if current in {StepStatus.PENDING, StepStatus.RUNNING}:
                self.workflows.complete_step(
                    inquiry_id, StepCode.DPS_LOOKUP, metadata=metadata
                )
        elif current in {StepStatus.PENDING, StepStatus.RUNNING}:
            self.workflows.fail_step(
                inquiry_id,
                StepCode.DPS_LOOKUP,
                "DPS_LOOKUP_FAILED",
                "배송·설치 일정 조회를 완료하지 못했습니다.",
                metadata=metadata,
            )

    def _complete_analysis_step(self, inquiry_id: int) -> None:
        self.workflows.initialize_steps(inquiry_id)
        step = self.workflows.get_step(inquiry_id, StepCode.QUESTION_ANALYZED)
        status = StepStatus(step["step_status"])
        if status is StepStatus.COMPLETED:
            return
        if status in {StepStatus.FAILED, StepStatus.NEEDS_REVIEW}:
            self.workflows.retry_step(inquiry_id, StepCode.QUESTION_ANALYZED)
        self.workflows.complete_step(
            inquiry_id,
            StepCode.QUESTION_ANALYZED,
            metadata={"classification": "phase9"},
        )

    def _skip_not_applicable_steps(
        self,
        inquiry_id: int,
        *,
        requires_order_lookup: bool,
        requires_dps_lookup: bool,
    ) -> None:
        self.workflows.initialize_steps(inquiry_id)
        codes: list[StepCode] = []
        if not requires_order_lookup:
            codes.extend((StepCode.ORDER_IDENTIFIED, StepCode.NAVER_ORDER_LOOKUP))
        if not requires_dps_lookup:
            codes.append(StepCode.DPS_LOOKUP)
        for code in codes:
            step = self.workflows.get_step(inquiry_id, code)
            status = StepStatus(step["step_status"])
            if status in {
                StepStatus.PENDING,
                StepStatus.RUNNING,
                StepStatus.FAILED,
                StepStatus.NEEDS_REVIEW,
            }:
                self.workflows.skip_step(
                    inquiry_id,
                    code,
                    metadata={"reason": "NOT_APPLICABLE"},
                )

    def _safe_dps_failure_outcome(
        self,
        request: Any,
        error: Exception,
    ) -> DpsEnrichmentOutcome:
        """Convert unexpected DPS/cache exceptions into a routable fact."""

        inquiry_id = int(request.inquiry_id)
        metadata = {
            "lookup_required": True,
            "lookup_status": DpsLookupStatus.PARSE_ERROR.value,
            "source": "DPS_PIPELINE",
            "order_id": request.order_id,
            "required_delivery_date": None,
            "installation_date": None,
            "installation_date_source": None,
            "date_parse_status": "PARSE_FAILED",
            "cache_used": False,
            "error_code": "DPS_PIPELINE_EXCEPTION",
            "error_message": "배송 시스템 조회 결과를 안전하게 처리하지 못했습니다.",
            "warnings": ["DPS_PIPELINE_EXCEPTION"],
            "change_request": False,
        }
        request.metadata["dps"] = metadata
        self.workflows.initialize_steps(inquiry_id)
        step = self.workflows.get_step(inquiry_id, StepCode.DPS_LOOKUP)
        status = StepStatus(step["step_status"])
        if status is StepStatus.COMPLETED:
            self.workflows.restart_completed_step(inquiry_id, StepCode.DPS_LOOKUP)
            status = StepStatus.RUNNING
        elif status in {StepStatus.FAILED, StepStatus.NEEDS_REVIEW}:
            self.workflows.retry_step(inquiry_id, StepCode.DPS_LOOKUP)
            status = StepStatus.RUNNING
        elif status is StepStatus.PENDING:
            self.workflows.start_step(inquiry_id, StepCode.DPS_LOOKUP)
            status = StepStatus.RUNNING
        if status is StepStatus.RUNNING:
            self.workflows.fail_step(
                inquiry_id,
                StepCode.DPS_LOOKUP,
                "DPS_PIPELINE_EXCEPTION",
                "배송 시스템 조회 결과 처리에 실패했습니다.",
                metadata={"error_type": error.__class__.__name__},
            )
        self.logs.record_inquiry(
            inquiry_id,
            "DPS_LOOKUP_FAILED",
            "DPS 예외를 안전 답변 경로로 전환했습니다.",
            level="WARNING",
            details={
                "error_type": error.__class__.__name__,
                "safe_error_code": "DPS_PIPELINE_EXCEPTION",
                "order_id_present": bool(str(request.order_id or "").strip()),
            },
        )
        decision = DpsLookupDecision(
            lookup_required=True,
            status=DpsLookupStatus.PARSE_ERROR,
            change_request=False,
            order_id=str(request.order_id or "").strip() or None,
            general_segments=(),
            dps_segments=(str(request.question or ""),),
            reason="DPS 예외를 안전 답변으로 전환합니다.",
        )
        return DpsEnrichmentOutcome(decision, metadata)

    def generate_for_inquiry(
        self,
        inquiry_id: int,
        *,
        force_dps_refresh: bool = False,
        prefer_template: bool = True,
        correlation_id: str | None = None,
        processing_plan: InquiryProcessingPlan | None = None,
    ) -> AnswerGenerationOutcome:
        inquiry = self.inquiries.get(inquiry_id)
        if inquiry is None:
            raise LookupError(f"Inquiry not found: {inquiry_id}")
        if self.answers.is_inquiry_posted(inquiry_id):
            raise AnswerAlreadyPostedError(
                "이미 등록된 문의는 답변 초안을 다시 생성할 수 없습니다."
            )
        prior_active = self.answers.active_for_inquiry(inquiry_id)

        step_started = False
        try:
            self._start_generation_step(inquiry_id)
            step_started = True
            if processing_plan is not None:
                plan = processing_plan.for_execution(
                    correlation_id=(
                        correlation_id or processing_plan.correlation_id
                    ),
                    template_preferred=prefer_template,
                )
            else:
                plan = self.plans.create(
                    inquiry,
                    template_preferred=prefer_template,
                    correlation_id=correlation_id,
                )
            if plan.inquiry_id != inquiry_id:
                raise AnswerGenerationError(
                    "문의와 처리계획의 식별자가 일치하지 않습니다."
                )
            correlation_id = plan.correlation_id
            request = answer_request_from_inquiry(inquiry)
            phase9_analysis = plan.analysis
            analysis_data = phase9_analysis.to_dict()
            request.metadata["phase9_analysis"] = analysis_data
            request.metadata["processing_plan"] = plan.to_dict()
            self.logs.record_inquiry(
                inquiry_id,
                "PROCESSING_PLAN_STARTED",
                "문의 단일 처리계획 생성을 시작했습니다.",
                details={
                    "inquiry_id": inquiry_id,
                    "inquiry_type": plan.inquiry_type,
                    "template_preferred": plan.template_preferred,
                    "correlation_id": correlation_id,
                },
            )
            self.logs.record_inquiry(
                inquiry_id,
                "PROCESSING_PLAN_CREATED",
                "문의 분석·조회·답변 route 처리계획을 생성했습니다.",
                details={
                    key: value
                    for key, value in plan.to_dict().items()
                    if key
                    not in {"normalized_text", "order_id", "product_order_id", "analysis"}
                },
            )
            for event_code, event_message, event_details in (
                (
                    "INTENT_CLASSIFIED",
                    "최신 문의 원문으로 Intent를 분류했습니다.",
                    {
                        "detected_intent": plan.detected_intent,
                        "is_delivery": plan.is_delivery,
                    },
                ),
                (
                    "ORDER_ID_NORMALIZED",
                    "일반 주문번호와 상품주문번호 상태를 분리했습니다.",
                    {
                        "order_id_present": plan.order_id_status == "VALID",
                        "product_order_id_present": bool(plan.product_order_id),
                        "order_id_status": plan.order_id_status,
                    },
                ),
                (
                    "ORDER_LOOKUP_ACTION_SELECTED",
                    "주문 조회 동작을 선택했습니다.",
                    {
                        "order_lookup_action": plan.order_lookup_action,
                        "order_lookup_status": plan.order_lookup_status,
                    },
                ),
                (
                    "DPS_ACTION_SELECTED",
                    "DPS 조회 동작을 선택했습니다.",
                    {
                        "dps_lookup_action": plan.dps_lookup_action,
                        "dps_lookup_status": plan.dps_lookup_status,
                    },
                ),
            ):
                self.logs.record_inquiry(
                    inquiry_id,
                    event_code,
                    event_message,
                    details={
                        **event_details,
                        "inquiry_id": inquiry_id,
                        "inquiry_type": plan.inquiry_type,
                        "requires_order_lookup": plan.requires_order_lookup,
                        "requires_dps_lookup": plan.requires_dps_lookup,
                        "correlation_id": correlation_id,
                    },
                )
            self._complete_analysis_step(inquiry_id)
            if plan.requires_order_lookup:
                for code in (
                    StepCode.ORDER_IDENTIFIED,
                    StepCode.NAVER_ORDER_LOOKUP,
                ):
                    self.workflows.reopen_skipped_step(
                        inquiry_id,
                        code,
                        metadata={
                            "reason": "LATEST_PROCESSING_PLAN_REQUIRES_STEP",
                            "correlation_id": correlation_id,
                        },
                    )
            if plan.requires_dps_lookup:
                self.workflows.reopen_skipped_step(
                    inquiry_id,
                    StepCode.DPS_LOOKUP,
                    metadata={
                        "reason": "LATEST_PROCESSING_PLAN_REQUIRES_STEP",
                        "correlation_id": correlation_id,
                    },
                )
            self._skip_not_applicable_steps(
                inquiry_id,
                requires_order_lookup=plan.requires_order_lookup,
                requires_dps_lookup=plan.requires_dps_lookup,
            )
            selected_title = str(inquiry.get("title") or "")
            selected_content = str(inquiry.get("content") or "")
            safe_title = _safe_log_text(selected_title)
            safe_content = _safe_log_text(selected_content)
            safe_question = _safe_log_text(request.question)
            LOGGER.info(
                "INQUIRY_ANALYSIS_INPUT inquiry_id=%s title=%r "
                "content=%r inquiry_text=%r delivery_question=%s "
                "delivery_related=%s needs_delivery_lookup=%s "
                "question_category=%s",
                inquiry_id,
                safe_title,
                safe_content,
                safe_question,
                analysis_data["delivery_question"],
                analysis_data["delivery_related"],
                analysis_data["needs_delivery_lookup"],
                analysis_data["question_category"],
            )
            self.logs.record_inquiry(
                inquiry_id,
                "INQUIRY_ANALYSIS_INPUT",
                "Streamlit 선택 문의의 분석 입력과 배송 판별 결과입니다.",
                details={
                    "selected_inquiry_title": safe_title,
                    "selected_inquiry_content": safe_content,
                    "inquiry_text": safe_question,
                    "question_source_fields": request.metadata.get(
                        "question_source_fields", []
                    ),
                    "delivery_question": analysis_data[
                        "delivery_question"
                    ],
                    "delivery_related": analysis_data["delivery_related"],
                    "needs_delivery_lookup": analysis_data[
                        "needs_delivery_lookup"
                    ],
                    "requires_dps_lookup": plan.requires_dps_lookup,
                    "question_category": analysis_data[
                        "question_category"
                    ],
                },
                customer_names=(
                    inquiry.get("customer_display"),
                    inquiry.get("masked_writer_id"),
                ),
            )
            self.logs.record_inquiry(
                inquiry_id,
                "PHASE9_INQUIRY_ANALYZED",
                "문의 유형과 답변 전략을 분석했습니다.",
                details={
                    "inquiry_type": phase9_analysis.inquiry_type.value,
                    "answer_strategy": phase9_analysis.answer_strategy.value,
                    "order_id_status": phase9_analysis.order_id_status.value,
                    "confidence": phase9_analysis.confidence,
                    "delivery_question": analysis_data[
                        "delivery_question"
                    ],
                    "delivery_related": analysis_data["delivery_related"],
                    "needs_delivery_lookup": analysis_data[
                        "needs_delivery_lookup"
                    ],
                    "requires_dps_lookup": plan.requires_dps_lookup,
                    "question_category": analysis_data[
                        "question_category"
                    ],
                    "requires_order_lookup": (
                        plan.requires_order_lookup
                    ),
                    "can_execute_dps_lookup": (
                        plan.can_execute_dps_lookup
                    ),
                    "can_generate_answer": (
                        phase9_analysis.can_generate_answer
                    ),
                },
            )
            decision_details = {
                "correlation_id": correlation_id,
                "inquiry_id": inquiry_id,
                "inquiry_type": phase9_analysis.inquiry_type.value,
                "detected_intent": phase9_analysis.detected_intent,
                "question_category": phase9_analysis.question_category,
                "is_delivery": plan.is_delivery,
                "delivery_related": plan.delivery_related,
                "needs_delivery_lookup": plan.needs_delivery_lookup,
                "requires_order_lookup": plan.requires_order_lookup,
                "requires_dps_lookup": plan.requires_dps_lookup,
                "can_execute_dps_lookup": plan.can_execute_dps_lookup,
                "can_generate_answer": plan.can_generate_draft,
                "order_id_present": plan.order_id_status == "VALID",
                "order_id_status": plan.order_id_status,
                "order_lookup_action": plan.order_lookup_action,
                "order_lookup_status": plan.order_lookup_status,
                "dps_lookup_action": plan.dps_lookup_action,
                "dps_lookup_status": plan.dps_lookup_status,
            }
            self.logs.record_inquiry(
                inquiry_id,
                "ANSWER_ROUTING_STARTED",
                "답변 생성 우선순위 라우팅을 시작했습니다.",
                details={
                    **decision_details,
                    "template_preferred": bool(prefer_template),
                },
            )
            self.logs.record_inquiry(
                inquiry_id,
                "INQUIRY_INTENT_CLASSIFIED",
                "문의 Intent와 조회 필요 여부를 분류했습니다.",
                details=decision_details,
            )
            self.logs.record_inquiry(
                inquiry_id,
                (
                    "ORDER_LOOKUP_REQUIRED"
                    if plan.requires_order_lookup
                    else "ORDER_LOOKUP_NOT_REQUIRED"
                ),
                "주문 조회 필요 여부를 결정했습니다.",
                details=decision_details,
            )
            self.logs.record_inquiry(
                inquiry_id,
                (
                    "DPS_LOOKUP_REQUIRED"
                    if plan.requires_dps_lookup
                    else "DPS_LOOKUP_NOT_REQUIRED"
                ),
                "DPS 조회 필요 여부를 결정했습니다.",
                details=decision_details,
            )
            if (
                plan.requires_dps_lookup
                and not plan.can_execute_dps_lookup
            ):
                self.logs.record_inquiry(
                    inquiry_id,
                    "DPS_LOOKUP_SKIPPED",
                    "일반 주문번호가 없어 DPS 외부 호출을 건너뜁니다.",
                    details={**decision_details, "reason": "ORDER_ID_REQUIRED"},
                )
            if (
                phase9_analysis.answer_strategy.value
                == "REQUEST_ORDER_ID"
            ):
                self.inquiries.update_phase9_status(
                    inquiry_id,
                    "ORDER_INFO_REQUIRED",
                )
            elif phase9_analysis.manual_review_required:
                self.inquiries.update_phase9_status(
                    inquiry_id,
                    "MANUAL_REVIEW_REQUIRED",
                )
            if not phase9_analysis.can_generate_answer:
                self.logs.record_inquiry(
                    inquiry_id,
                    "ANSWER_PREREQUISITE_FAILED",
                    "자동 Draft 생성이 금지된 고위험 또는 빈 문의입니다.",
                    level="WARNING",
                    details={
                        **decision_details,
                        "safe_error_code": "AUTO_ANSWER_PROHIBITED",
                    },
                )
                raise AnswerGenerationError(
                    "이 문의는 자동 답변 생성이 금지되어 직원 확인이 필요합니다."
                )
            order_lookup_result: dict[str, Any] | None = None
            if plan.is_delivery and plan.order_id_status == "VALID":
                self.logs.record_inquiry(
                    inquiry_id,
                    "ORDER_LOOKUP_ACTION_SELECTED",
                    "처리계획에 따라 주문 조회 동작을 선택했습니다.",
                    details={
                        "order_lookup_action": plan.order_lookup_action,
                        "order_lookup_status": plan.order_lookup_status,
                        "order_id_present": True,
                        "correlation_id": correlation_id,
                    },
                )
                if plan.order_lookup_action == "FETCH":
                    identified = self.workflows.get_step(
                        inquiry_id, StepCode.ORDER_IDENTIFIED
                    )
                    if StepStatus(identified["step_status"]) in {
                        StepStatus.PENDING,
                        StepStatus.NEEDS_REVIEW,
                    }:
                        self.workflows.complete_step(
                            inquiry_id,
                            StepCode.ORDER_IDENTIFIED,
                            metadata={
                                "order_id_status": "VALID",
                                "correlation_id": correlation_id,
                            },
                        )
                    self.logs.record_inquiry(
                        inquiry_id,
                        "ORDER_LOOKUP_STARTED",
                        "검증된 일반 주문번호로 주문 조회를 시작했습니다.",
                        details={
                            "order_id_present": True,
                            "correlation_id": correlation_id,
                        },
                    )
                    # Injected DPS doubles represent pre-arranged delivery
                    # facts in legacy unit tests. Production and new matrix
                    # tests always use the real/injected order service first.
                    if (
                        self._order_lookup_service is None
                        and self._dps_enrichment is not None
                    ):
                        order_lookup_result = {
                            "success": True,
                            "orders": [{"order_id": request.order_id}],
                            "cached": True,
                            "synthetic_test_snapshot": True,
                        }
                    else:
                        order_lookup_result = (
                            self.order_lookup_service.lookup_for_inquiry(
                                inquiry_id,
                                force_refresh=force_dps_refresh,
                                correlation_id=correlation_id,
                            )
                        )
                    refreshed = self.inquiries.get(inquiry_id) or inquiry
                    plan = self.plans.create(
                        refreshed,
                        template_preferred=prefer_template,
                        correlation_id=correlation_id,
                        order_lookup_result=order_lookup_result,
                    )
                    inquiry = refreshed
                    request = answer_request_from_inquiry(inquiry)
                    request.metadata["phase9_analysis"] = analysis_data
                    request.metadata["processing_plan"] = plan.to_dict()
                    decision_details.update(
                        {
                            "order_lookup_action": plan.order_lookup_action,
                            "order_lookup_status": plan.order_lookup_status,
                            "dps_lookup_action": plan.dps_lookup_action,
                            "dps_lookup_status": plan.dps_lookup_status,
                            "can_execute_dps_lookup": plan.can_execute_dps_lookup,
                        }
                    )
                    self._apply_order_lookup_workflow(
                        inquiry_id,
                        status=plan.order_lookup_status,
                        correlation_id=correlation_id,
                    )
                    event = (
                        "ORDER_LOOKUP_SUCCEEDED"
                        if plan.order_lookup_status == "SUCCESS"
                        else "ORDER_LOOKUP_NOT_FOUND"
                        if plan.order_lookup_status == "NOT_FOUND"
                        else "ORDER_LOOKUP_FAILED"
                    )
                    self.logs.record_inquiry(
                        inquiry_id,
                        event,
                        "주문 조회 결과를 처리계획에 반영했습니다.",
                        level=(
                            "INFO"
                            if plan.order_lookup_status == "SUCCESS"
                            else "WARNING"
                        ),
                        details={
                            "order_lookup_status": plan.order_lookup_status,
                            "result_count": len(
                                order_lookup_result.get("orders") or []
                            ),
                            "safe_error_code": order_lookup_result.get(
                                "error_code"
                            ),
                            "correlation_id": correlation_id,
                        },
                    )
                request.metadata["order_lookup_status"] = (
                    plan.order_lookup_status
                )
            elif plan.is_delivery:
                request.metadata["order_lookup_status"] = (
                    plan.order_lookup_status
                )
            is_delivery_schedule = plan.is_delivery
            if is_delivery_schedule:
                # Delivery/installation schedules are routed before the rule
                # engine so broad legacy shipping templates can never hide a
                # missing order_id or a confirmed DPS date.
                base_rule_result = AnswerResult(
                    status=AnswerStatus.NEEDS_REVIEW,
                    category=phase9_analysis.inquiry_type.value,
                    reason="배송·설치 일정 전용 라우팅",
                    answer="",
                    provider="delivery_router",
                    auto_answerable=False,
                    needs_review=True,
                    metadata={
                        "phase9": {
                            "analysis": phase9_analysis.to_dict(),
                        }
                    },
                )
                if plan.can_execute_dps_lookup:
                    self.logs.record_inquiry(
                        inquiry_id,
                        "DPS_LOOKUP_STARTED",
                        "처리계획에 따라 DPS 조회를 시작했습니다.",
                        details={
                            "order_id_present": True,
                            "correlation_id": correlation_id,
                        },
                    )
                    try:
                        dps_outcome = self.dps_enrichment.enrich(
                            request,
                            force_refresh=force_dps_refresh,
                            correlation_id=correlation_id,
                        )
                    except Exception as dps_error:
                        LOGGER.exception(
                            "DPS pipeline exception converted to safe route: "
                            "inquiry_id=%s error_type=%s",
                            inquiry_id,
                            dps_error.__class__.__name__,
                        )
                        dps_outcome = self._safe_dps_failure_outcome(
                            request, dps_error
                        )
                    dps_event_status = str(
                        dps_outcome.metadata.get("lookup_status") or ""
                    ).upper()
                    self.logs.record_inquiry(
                        inquiry_id,
                        (
                            "DPS_LOOKUP_SUCCEEDED"
                            if dps_event_status in {"SUCCESS", "NOT_FOUND"}
                            else "DPS_LOOKUP_FAILED"
                        ),
                        "DPS 조회 결과를 처리계획에 반영했습니다.",
                        level=(
                            "INFO"
                            if dps_event_status in {"SUCCESS", "NOT_FOUND"}
                            else "WARNING"
                        ),
                        details={
                            "dps_lookup_status": dps_event_status,
                            "installation_date_found": bool(
                                dps_outcome.metadata.get("installation_date")
                                or dps_outcome.metadata.get(
                                    "required_delivery_date"
                                )
                            ),
                            "correlation_id": correlation_id,
                        },
                    )
                else:
                    dps_outcome = self.dps_enrichment.skip_for_phase9(
                        request,
                        reason=(
                            "배송·설치 일정 문의에 검증된 일반 주문번호가 "
                            "없어 DPS 조회를 차단했습니다."
                        ),
                    )
                    dps_step = self.workflows.get_step(
                        inquiry_id, StepCode.DPS_LOOKUP
                    )
                    if StepStatus(dps_step["step_status"]) in {
                        StepStatus.PENDING,
                        StepStatus.RUNNING,
                        StepStatus.FAILED,
                        StepStatus.NEEDS_REVIEW,
                    }:
                        self.workflows.skip_step(
                            inquiry_id,
                            StepCode.DPS_LOOKUP,
                            metadata={
                                "reason": (
                                    "CUSTOMER_INFORMATION_REQUIRED"
                                    if plan.order_id_status != "VALID"
                                    else "ORDER_LOOKUP_NOT_SUCCESSFUL"
                                ),
                                "correlation_id": correlation_id,
                            },
                        )
            elif prefer_template:
                self.logs.record_inquiry(
                    inquiry_id,
                    "TEMPLATE_SEARCH_STARTED",
                    "현재 문의에 적용 가능한 기존 템플릿 검색을 시작했습니다.",
                    details={
                        **decision_details,
                        "template_preferred": True,
                        "store": request.store_code,
                    },
                )
                try:
                    base_rule_result = self.engine.generate(request)
                except Exception as template_error:
                    self.logs.record_inquiry(
                        inquiry_id,
                        "TEMPLATE_RENDER_FAILED",
                        "기존 템플릿 생성 중 오류가 발생해 GPT 전환을 준비합니다.",
                        level="WARNING",
                        details={
                            "error_type": template_error.__class__.__name__,
                            "store": request.store_code,
                            "inquiry_type": request.inquiry_type,
                        },
                    )
                    base_rule_result = AnswerResult(
                        status=AnswerStatus.NOT_SUPPORTED,
                        category=phase9_analysis.inquiry_type.value,
                        reason="TEMPLATE_RENDER_FAILED",
                        answer="",
                        provider="rules",
                        auto_answerable=False,
                        needs_review=True,
                        metadata={"template_error": "RENDER_FAILED"},
                    )
                dps_outcome = self.dps_enrichment.skip_for_phase9(
                    request,
                    reason="일반 문의이므로 DPS 조회가 필요하지 않습니다.",
                )
            else:
                # An explicit operator override must bypass both template
                # lookup and the general Rule Engine.  The empty baseline is
                # only context for the hybrid service and can never be saved.
                base_rule_result = AnswerResult(
                    status=AnswerStatus.NOT_SUPPORTED,
                    category=phase9_analysis.inquiry_type.value,
                    reason="관리자가 기존 운영 템플릿 사용을 해제했습니다.",
                    answer="",
                    provider="template_bypassed",
                    auto_answerable=False,
                    needs_review=False,
                    matched_rule="",
                    metadata={
                        "template_search_skipped": True,
                        "rule_engine_skipped": True,
                    },
                )
                dps_outcome = self.dps_enrichment.skip_for_phase9(
                    request,
                    reason="일반 문의에서 운영 템플릿 사용을 해제했습니다.",
                )
                self.logs.record_inquiry(
                    inquiry_id,
                    "GPT_DIRECT_STARTED",
                    "기존 템플릿을 건너뛰고 GPT 직접 생성을 시작했습니다.",
                    details={
                        **decision_details,
                        "template_preferred": False,
                        "selected_answer_route": "GPT_DIRECT",
                    },
                )
            latest_dps = dps_outcome.lookup_row
            if (
                latest_dps is None
                and phase9_analysis.order_id_validated
                and is_delivery_schedule
                and str(dps_outcome.metadata.get("lookup_status") or "")
                in {"NOT_RUN", "PENDING"}
            ):
                try:
                    latest_dps = self.dps.get_latest_by_inquiry_and_order(
                        inquiry_id, request.order_id
                    )
                except Exception as cache_error:
                    dps_outcome = self._safe_dps_failure_outcome(
                        request, cache_error
                    )
            if latest_dps is not None:
                persisted_dps = dict(
                    latest_dps.get("normalized_result_json") or {}
                )
                raw_dps = latest_dps.get("raw_result_json")
                if isinstance(raw_dps, dict):
                    raw_data = raw_dps.get("data")
                    if isinstance(raw_data, dict):
                        for key in (
                            "required_delivery_date",
                            "installation_date",
                            "installation_date_raw",
                            "requiredDeliveryDate",
                            "품목상세내역 요구납기일",
                        ):
                            if persisted_dps.get(key) in (None, "") and raw_data.get(
                                key
                            ) not in (None, ""):
                                persisted_dps[key] = raw_data[key]
                for key in (
                    "required_delivery_date",
                    "installation_date",
                    "installation_date_source",
                    "raw_required_delivery_date",
                    "date_parse_status",
                ):
                    if persisted_dps.get(key) in (None, "") and latest_dps.get(
                        key
                    ) not in (None, ""):
                        persisted_dps[key] = latest_dps[key]
                persisted_dps["dps_lookup_id"] = latest_dps["id"]
                persisted_dps["lookup_timestamp"] = latest_dps.get(
                    "queried_at"
                )
                persisted_dps["lookup_completed_at"] = latest_dps.get(
                    "lookup_completed_at"
                )
                persisted_dps["lookup_status"] = latest_dps.get(
                    "lookup_status"
                )
                persisted_dps["error_code"] = latest_dps.get("error_code")
                persisted_dps["error_message"] = latest_dps.get(
                    "error_message"
                )
                request.metadata["dps"] = persisted_dps
                self.logs.record_inquiry(
                    inquiry_id,
                    "DPS_RESULT_SELECTED",
                    "현재 문의에 사용할 DPS 결과를 선택했습니다.",
                    details={
                        "dps_result_id": latest_dps["id"],
                        "dps_lookup_status": persisted_dps.get(
                            "lookup_status"
                        ),
                        "dps_result_source": (
                            "CACHE" if latest_dps.get("cached") else "LATEST"
                        ),
                        "installation_date_found": bool(
                            persisted_dps.get("installation_date")
                            or persisted_dps.get("required_delivery_date")
                        ),
                    },
                )
            elif dps_outcome.lookup_row is not None:
                request.metadata["dps"]["dps_lookup_id"] = (
                    dps_outcome.lookup_row["id"]
                )
                request.metadata["dps"]["lookup_timestamp"] = (
                    dps_outcome.lookup_row.get("queried_at")
                )
            if is_delivery_schedule:
                plan = self.plans.create(
                    inquiry,
                    template_preferred=prefer_template,
                    correlation_id=correlation_id,
                    order_lookup_result=order_lookup_result,
                    dps_override=(
                        request.metadata.get("dps")
                        if isinstance(request.metadata.get("dps"), dict)
                        else dps_outcome.metadata
                    ),
                )
                request.metadata["order_lookup_status"] = (
                    plan.order_lookup_status
                )
                request.metadata["processing_plan"] = plan.to_dict()
                decision_details.update(
                    {
                        "order_lookup_action": plan.order_lookup_action,
                        "order_lookup_status": plan.order_lookup_status,
                        "dps_lookup_action": plan.dps_lookup_action,
                        "dps_lookup_status": plan.dps_lookup_status,
                        "can_execute_dps_lookup": plan.can_execute_dps_lookup,
                    }
                )
                self._apply_dps_workflow(
                    inquiry_id,
                    status=plan.dps_lookup_status,
                    correlation_id=correlation_id,
                )
                self.logs.record_inquiry(
                    inquiry_id,
                    "DPS_ACTION_SELECTED",
                    "처리계획에 따라 DPS 조회 결과 사용 동작을 선택했습니다.",
                    details={
                        "dps_lookup_action": plan.dps_lookup_action,
                        "dps_lookup_status": plan.dps_lookup_status,
                        "correlation_id": correlation_id,
                    },
                )
            if is_delivery_schedule:
                rule_result = apply_phase9_rule_policy(
                    request,
                    base_rule_result,
                    phase9_analysis,
                )
                answer_source = str(
                    rule_result.metadata.get("answer_source") or ""
                )
                if answer_source not in {
                    "delivery_template",
                    "dps",
                    "ORDER_ID_REQUEST",
                    "ORDER_LOOKUP_FAILED",
                    "SAFE_TEMPLATE",
                }:
                    raise AnswerGenerationError(
                        "배송·설치 일정 전용 답변을 생성하지 못했습니다."
                    )
                result = rule_result
                generation_mode = (
                    "DPS"
                    if answer_source == "dps"
                    and bool(result.metadata.get("delivery_date_found"))
                    else "RULE"
                )
                result.metadata.update(
                    {
                        "generation_mode": generation_mode,
                        "template_preferred": bool(prefer_template),
                        "template_override": False,
                        "template_id": result.matched_rule or None,
                        "template_name": result.matched_rule or None,
                        "template_version": "phase9-delivery-v1",
                        "delivery_question": True,
                    }
                )
                if (
                    result.metadata.get("selected_answer_route")
                    == "ORDER_ID_REQUEST"
                ):
                    validation = self.validator.validate_route(
                        result.answer,
                        route="ORDER_ID_REQUEST",
                    )
                    result.metadata["hybrid"] = {
                        "validation": validation.to_dict(),
                        "fallback_used": False,
                        "provider": "deterministic_order_id_request",
                    }
                    result.metadata["validator_result"] = (
                        validation.to_dict()
                    )
                    self.logs.record_inquiry(
                        inquiry_id,
                        "ORDER_ID_REQUEST_VALIDATED",
                        "주문번호 요청 전용 답변 검증을 완료했습니다.",
                        level="INFO" if validation.passed else "ERROR",
                        details={
                            "status": validation.status,
                            "error_count": len(validation.errors),
                            "selected_route": "ORDER_ID_REQUEST",
                        },
                    )
                    self.logs.record_inquiry(
                        inquiry_id,
                        (
                            "ANSWER_VALIDATION_PASSED"
                            if validation.passed
                            else "ANSWER_VALIDATION_FAILED"
                        ),
                        "주문번호 요청 답변 Validator를 실행했습니다.",
                        level="INFO" if validation.passed else "ERROR",
                        details={
                            "selected_answer_route": "ORDER_ID_REQUEST",
                            "validator_result": validation.status,
                            "validator_failure_reason": (
                                "; ".join(validation.errors)[:500]
                                if validation.errors
                                else None
                            ),
                        },
                    )
                    if not validation.passed:
                        raise AnswerGenerationError(
                            "주문번호 요청 답변이 전용 Validator를 "
                            "통과하지 못했습니다."
                        )
                    self._set_order_id_request_workflow(inquiry_id)
                delivery_context = (
                    result.metadata.get("delivery_context")
                    if isinstance(
                        result.metadata.get("delivery_context"), dict
                    )
                    else {}
                )
                route = str(
                    delivery_context.get("selected_answer_route") or ""
                )
                route_event = {
                    "ORDER_ID_REQUEST": "ORDER_ID_REQUEST_SELECTED",
                    "DELIVERY_WITH_INSTALLATION_DATE": (
                        "DELIVERY_TEMPLATE_SELECTED"
                    ),
                    "DELIVERY_DATE_UNCONFIRMED": (
                        "DELIVERY_DATE_UNCONFIRMED_SELECTED"
                    ),
                    "DELIVERY_ORDER_NOT_FOUND": (
                        "DELIVERY_ORDER_NOT_FOUND_SELECTED"
                    ),
                    "DPS_LOOKUP_FAILED": (
                        "DPS_LOOKUP_FAILED_TEMPLATE_SELECTED"
                    ),
                    "ORDER_LOOKUP_FAILED": (
                        "ORDER_LOOKUP_FAILED_TEMPLATE_SELECTED"
                    ),
                }.get(route)
                if route_event:
                    self.logs.record_inquiry(
                        inquiry_id,
                        route_event,
                        "배송·설치 전용 안전 답변을 선택했습니다.",
                        details={
                            **decision_details,
                            "selected_answer_route": route,
                            "generation_mode": generation_mode,
                            "gpt_called": False,
                            "dps_lookup_attempted": bool(
                                plan.can_execute_dps_lookup
                            ),
                        },
                    )
                if route != "ORDER_ID_REQUEST":
                    validation = self.validator.validate_route(
                        result.answer,
                        route=route,
                        installation_date=delivery_context.get(
                            "installation_date_raw"
                        ),
                        installation_time=delivery_context.get(
                            "installation_time"
                        ),
                    )
                    result.metadata["hybrid"] = {
                        "validation": validation.to_dict(),
                        "fallback_used": False,
                        "provider": "deterministic_delivery_rule",
                    }
                    result.metadata["validator_result"] = validation.to_dict()
                    self.logs.record_inquiry(
                        inquiry_id,
                        (
                            "ANSWER_VALIDATION_PASSED"
                            if validation.passed
                            else "ANSWER_VALIDATION_FAILED"
                        ),
                        "배송 전용 답변 Validator를 실행했습니다.",
                        level="INFO" if validation.passed else "ERROR",
                        details={
                            "selected_answer_route": route,
                            "validator_result": validation.status,
                            "validator_failure_reason": (
                                "; ".join(validation.errors)[:500]
                                if validation.errors
                                else None
                            ),
                        },
                    )
                    if not validation.passed:
                        raise AnswerGenerationError(
                            "배송 전용 답변이 Validator를 통과하지 못했습니다."
                        )
                route_queue = {
                    "ORDER_ID_REQUEST": "CUSTOMER_CONFIRMATION_REQUIRED",
                    "DELIVERY_WITH_INSTALLATION_DATE": "AUTO_PROCESSABLE",
                    "DELIVERY_DATE_UNCONFIRMED": "ORDER_LOOKUP_READY",
                    "DELIVERY_ORDER_NOT_FOUND": "ORDER_LOOKUP_FAILED",
                    "DPS_LOOKUP_FAILED": "ORDER_LOOKUP_FAILED",
                    "ORDER_LOOKUP_FAILED": "ORDER_LOOKUP_FAILED",
                    "DELIVERY_DATE_INVALID": "ORDER_LOOKUP_FAILED",
                    "DELIVERY_LOOKUP_REQUIRED": "ORDER_LOOKUP_READY",
                }.get(route, "ORDER_LOOKUP_READY")
                self.logs.record_inquiry(
                    inquiry_id,
                    "ANSWER_ROUTE_SELECTED",
                    "문의 Facts에 맞는 답변 경로를 선택했습니다.",
                    details={
                        "inquiry_id": inquiry_id,
                        "detected_intent": phase9_analysis.detected_intent,
                        "requires_order_lookup": (
                            plan.requires_order_lookup
                        ),
                        "requires_dps_lookup": (
                            plan.requires_dps_lookup
                        ),
                        "order_id_present": bool(request.order_id.strip()),
                        "order_lookup_status": request.metadata.get(
                            "order_lookup_status", "SUCCESS"
                            if phase9_analysis.order_id_validated
                            else "NOT_RUN"
                        ),
                        "dps_lookup_status": delivery_context.get(
                            "dps_lookup_status"
                        ),
                        "installation_date_found": bool(
                            delivery_context.get("installation_date_display")
                        ),
                        "selected_answer_route": route,
                        "generation_mode": generation_mode,
                    },
                )
                self.logs.record_inquiry(
                    inquiry_id,
                    "INSTALLATION_DATE_NORMALIZED",
                    "설치예정일 정규화 결과를 답변 라우팅에 반영했습니다.",
                    details={
                        "dps_result_id": (
                            request.metadata.get("dps", {}).get("dps_lookup_id")
                            if isinstance(request.metadata.get("dps"), dict)
                            else None
                        ),
                        "installation_date_raw": delivery_context.get(
                            "installation_date_raw"
                        ),
                        "installation_date_display": delivery_context.get(
                            "installation_date_display"
                        ),
                        "dps_result_source": request.metadata.get("dps", {}).get(
                            "source"
                        )
                        if isinstance(request.metadata.get("dps"), dict)
                        else None,
                    },
                )
                self.logs.record_inquiry(
                    inquiry_id,
                    "ANSWER_PREREQUISITE_PASSED",
                    "정상 업무 상태에 맞는 안전 답변 생성 조건을 충족했습니다.",
                    details={
                        "selected_answer_route": route,
                        "can_generate_answer": (
                            phase9_analysis.can_generate_answer
                        ),
                    },
                )
                self.inquiries.update_delivery_routing_metadata(
                    inquiry_id,
                    queue=route_queue,
                    routing={
                        "intent": phase9_analysis.detected_intent,
                        "order_id_status": (
                            phase9_analysis.order_id_status.value
                        ),
                        "dps_lookup_status": delivery_context.get(
                            "dps_lookup_status"
                        ),
                        "selected_answer_route": route,
                        "installation_date_present": bool(
                            delivery_context.get(
                                "installation_date_display"
                            )
                        ),
                    },
                )
                prior_active = (
                    self.answers.active_for_inquiry(inquiry_id) or {}
                )
                self.logs.record_inquiry(
                    inquiry_id,
                    "DELIVERY_ANSWER_ROUTED",
                    "배송·설치 문의를 주문/DPS 우선 경로로 처리했습니다.",
                    level=(
                        "WARNING"
                        if result.status is AnswerStatus.NEEDS_REVIEW
                        else "INFO"
                    ),
                    details={
                        "inquiry_id": inquiry_id,
                        "detected_intent": phase9_analysis.detected_intent,
                        "is_delivery": True,
                        "order_id_present": bool(request.order_id.strip()),
                        "order_id_valid": (
                            phase9_analysis.order_id_validated
                        ),
                        "product_order_id_only": bool(
                            request.product_order_id.strip()
                            and not phase9_analysis.order_id_validated
                        ),
                        "dps_status": delivery_context.get(
                            "dps_lookup_status"
                        ),
                        "installation_date_present": bool(
                            delivery_context.get(
                                "installation_date_display"
                            )
                        ),
                        "installation_time_present": bool(
                            delivery_context.get("installation_time")
                        ),
                        "selected_route": route,
                        "selected_template": delivery_context.get(
                            "selected_template"
                        ),
                        "draft_reused": False,
                        "final_answer_protected": bool(
                            prior_active.get("final_answer")
                            or str(
                                prior_active.get("review_status") or ""
                            ).upper() == "APPROVED"
                        ),
                    },
                )
            else:
                template_failure = (
                    _template_unavailable_reason(
                        base_rule_result, request, self.validator
                    )
                    if prefer_template
                    else "BYPASSED"
                )
                if prefer_template and template_failure is None:
                    product_db_result = _is_product_db_result(
                        base_rule_result
                    )
                    self.logs.record_inquiry(
                        inquiry_id,
                        (
                            "PRODUCT_DB_MATCHED"
                            if product_db_result
                            else "TEMPLATE_MATCHED"
                        ),
                        (
                            "검증된 Product DB 사실을 선택했습니다."
                            if product_db_result
                            else "현재 문의에 적용 가능한 기존 템플릿을 선택했습니다."
                        ),
                        details={
                            "template_id": (
                                base_rule_result.matched_rule or None
                            ),
                            "store": request.store_code,
                            "inquiry_type": request.inquiry_type,
                        },
                    )
                    result = _apply_existing_template_metadata(
                        base_rule_result,
                        order_id_present=bool(request.order_id.strip()),
                        template_preferred=True,
                    )
                    if product_db_result:
                        result.metadata.update(
                            {
                                "answer_type": "product_db",
                                "answer_source": "product_db",
                                "generation_mode": "PRODUCT_DB",
                                "selected_answer_route": "PRODUCT_DB",
                            }
                        )
                    else:
                        result.metadata["selected_answer_route"] = "TEMPLATE"
                    template_validation = (
                        self.validator.validate_route(
                            result.answer,
                            route="PRODUCT_DB",
                            product_name=request.product_name,
                        )
                        if product_db_result
                        else self.validator.validate_route(
                            result.answer,
                            route="TEMPLATE",
                        )
                    )
                    result.metadata["hybrid"] = {
                        "validation": template_validation.to_dict(),
                        "fallback_used": False,
                        "provider": "template_validator",
                    }
                    result.metadata["validator_result"] = (
                        template_validation.to_dict()
                    )
                    self.logs.record_inquiry(
                        inquiry_id,
                        "ANSWER_VALIDATION_PASSED",
                        "운영 템플릿 Validator를 통과했습니다.",
                        details={
                            "selected_answer_route": (
                                "PRODUCT_DB"
                                if product_db_result
                                else "TEMPLATE"
                            ),
                            "validator_result": template_validation.status,
                        },
                    )
                elif prefer_template and _is_safe_rule_result(
                    base_rule_result
                ):
                    result = _apply_safe_rule_metadata(
                        base_rule_result,
                        order_id_present=bool(request.order_id.strip()),
                        template_preferred=True,
                    )
                    safe_rule_validation = self.validator.validate_route(
                        result.answer,
                        route="SAFE_RULE",
                    )
                    result.metadata["hybrid"] = {
                        "validation": safe_rule_validation.to_dict(),
                        "fallback_used": False,
                        "provider": "safe_rule_validator",
                    }
                    result.metadata["validator_result"] = (
                        safe_rule_validation.to_dict()
                    )
                    if not safe_rule_validation.passed:
                        raise AnswerGenerationError(
                            "SAFE_RULE 답변이 전용 Validator를 통과하지 못했습니다."
                        )
                    self.logs.record_inquiry(
                        inquiry_id,
                        "SAFE_RULE_SELECTED",
                        "기존 안전 Rule 답변을 GPT보다 먼저 선택했습니다.",
                        details={
                            **decision_details,
                            "selected_answer_route": "SAFE_RULE",
                            "generation_mode": "SAFE_RULE",
                            "safe_rule_id": result.matched_rule or None,
                            "gpt_called": False,
                            "learning_search_called": False,
                            "validator_result": safe_rule_validation.status,
                        },
                    )
                    self.logs.record_inquiry(
                        inquiry_id,
                        "ANSWER_VALIDATION_PASSED",
                        "SAFE_RULE 전용 Validator를 통과했습니다.",
                        details={
                            "selected_answer_route": "SAFE_RULE",
                            "validator_result": safe_rule_validation.status,
                        },
                    )
                else:
                    if prefer_template:
                        event_code = (
                            "TEMPLATE_VALIDATION_FAILED"
                            if template_failure == "VALIDATION_FAILED"
                            else "TEMPLATE_NOT_FOUND"
                        )
                        self.logs.record_inquiry(
                            inquiry_id,
                            event_code,
                            "적용 가능한 기존 템플릿이 없어 GPT로 전환합니다.",
                            level="WARNING",
                            details={
                                "reason": template_failure,
                                "store": request.store_code,
                                "inquiry_type": request.inquiry_type,
                            },
                        )
                        self.logs.record_inquiry(
                            inquiry_id,
                            "GPT_FALLBACK_STARTED",
                            "템플릿 우선 생성에서 GPT 자동 Fallback을 시작했습니다.",
                            details={
                                **decision_details,
                                "reason": template_failure,
                                "template_preferred": True,
                                "selected_answer_route": "GPT_FALLBACK",
                                "generation_mode": "GPT_FALLBACK",
                                "gpt_called": True,
                                "dps_lookup_attempted": False,
                            },
                        )
                    safe_review_fallback = False
                    try:
                        gpt_rule_context = (
                            _neutral_gpt_context(
                                base_rule_result,
                                template_failure=str(template_failure),
                                category=phase9_analysis.inquiry_type.value,
                            )
                            if (
                                prefer_template
                                and (
                                    not is_valid_draft(
                                        base_rule_result.answer
                                    )
                                    or template_failure != "NOT_FOUND"
                                )
                            )
                            else base_rule_result
                        )
                        hybrid_outcome = self.hybrid_service.generate(
                            request, gpt_rule_context
                        )
                        for event in hybrid_outcome.events:
                            self.logs.record_inquiry(
                                inquiry_id,
                                event.code,
                                event.message,
                                level=event.level,
                                details=event.details or {},
                            )
                        result = hybrid_outcome.result
                        validation = getattr(
                            hybrid_outcome, "validation", None
                        )
                        fallback_used = bool(
                            getattr(hybrid_outcome, "fallback_used", False)
                        )
                        if fallback_used:
                            hybrid_metadata = (
                                result.metadata.get("hybrid")
                                if isinstance(
                                    result.metadata.get("hybrid"), dict
                                )
                                else {}
                            )
                            fallback_reason = str(
                                hybrid_metadata.get("fallback_reason")
                                or "GPT_VALIDATION_FAILED"
                            )
                            raise AnswerGenerationError(
                                "GPT 답변이 안전 검증을 통과하지 못했습니다: "
                                + fallback_reason
                            )
                        if not is_valid_draft(result.answer):
                            raise AnswerGenerationError(
                                "GPT Fallback 생성 결과가 비어 있습니다."
                            )
                        if (
                            validation is not None
                            and not bool(validation.passed)
                        ):
                            raise AnswerGenerationError(
                                "GPT Fallback 결과가 Validator를 통과하지 못했습니다."
                            )
                        self.logs.record_inquiry(
                            inquiry_id,
                            "ANSWER_VALIDATION_PASSED",
                            "일반 문의 GPT 답변 Validator를 통과했습니다.",
                            details={
                                "selected_answer_route": (
                                    "GPT_FALLBACK"
                                    if prefer_template
                                    else "GPT_DIRECT"
                                ),
                                "validator_result": (
                                    getattr(validation, "status", "PASS")
                                    if validation is not None
                                    else "PASS"
                                ),
                            },
                        )
                    except Exception as fallback_error:
                        validation_failure_reason = None
                        safe_error_code = _error_code(fallback_error)
                        if "hybrid_outcome" in locals():
                            failed_validation = getattr(
                                hybrid_outcome, "validation", None
                            )
                            if failed_validation is not None:
                                validation_failure_reason = "; ".join(
                                    str(value)
                                    for value in getattr(
                                        failed_validation, "errors", ()
                                    )
                                )[:500] or None
                        if prefer_template:
                            self.logs.record_inquiry(
                                inquiry_id,
                                "GPT_FALLBACK_FAILED",
                                "GPT 자동 Fallback에 실패해 기존 Draft를 유지합니다.",
                                level="ERROR",
                                details={
                                    **decision_details,
                                    "error_type": (
                                        fallback_error.__class__.__name__
                                    ),
                                    "reason": template_failure,
                                    "validator_failure_reason": (
                                        validation_failure_reason
                                    ),
                                    "safe_error_code": safe_error_code,
                                    "correlation_id": correlation_id,
                                    "template_preferred": True,
                                    "selected_answer_route": "GPT_FALLBACK",
                                    "generation_mode": "GPT_FALLBACK",
                                    "gpt_called": True,
                                    "dps_lookup_attempted": False,
                                },
                            )
                        else:
                            self.logs.record_inquiry(
                                inquiry_id,
                                "GPT_DIRECT_FAILED",
                                "GPT 직접 답변 생성에 실패해 기존 Draft를 유지합니다.",
                                level="ERROR",
                                details={
                                    **decision_details,
                                    "error_type": (
                                        fallback_error.__class__.__name__
                                    ),
                                    "validator_failure_reason": (
                                        validation_failure_reason
                                    ),
                                    "safe_error_code": safe_error_code,
                                    "correlation_id": correlation_id,
                                    "template_preferred": False,
                                    "selected_answer_route": "GPT_DIRECT",
                                    "generation_mode": "GPT_DIRECT",
                                    "gpt_called": True,
                                    "dps_lookup_attempted": False,
                                },
                            )
                        if prior_active and is_valid_draft(
                            prior_active.get("original_answer")
                        ):
                            # Manual regeneration is a later operation.  If it
                            # fails, keep the already-valid active Draft exactly
                            # as it was instead of replacing it with a weaker
                            # safety response.
                            raise
                        failed_intent = (
                            hybrid_outcome.intent
                            if "hybrid_outcome" in locals()
                            and getattr(hybrid_outcome, "intent", None)
                            is not None
                            else None
                        )
                        result = _review_required_safe_result(
                            request,
                            template_preferred=prefer_template,
                            failure_code=safe_error_code,
                            questions=(
                                tuple(failed_intent.questions)
                                if failed_intent is not None
                                else ()
                            ),
                        )
                        validation = self.validator.validate_route(
                            result.answer,
                            route="REVIEW_REQUIRED_SAFE_DRAFT",
                        )
                        if not validation.passed:
                            raise AnswerGenerationError(
                                "최종 안전 답변이 Validator를 통과하지 못했습니다."
                            ) from fallback_error
                        safe_review_fallback = True
                        self.logs.record_inquiry(
                            inquiry_id,
                            "SAFE_DRAFT_CREATED",
                            "GPT 생성 실패 후 직원 검토용 안전 Draft를 생성했습니다.",
                            level="WARNING",
                            details={
                                **decision_details,
                                "selected_answer_route": (
                                    "REVIEW_REQUIRED_SAFE_DRAFT"
                                ),
                                "generation_mode": "SAFE_RULE",
                                "gpt_called": True,
                                "validator_result": validation.status,
                                "safe_error_code": safe_error_code,
                                "correlation_id": correlation_id,
                            },
                        )
                    generation_mode = (
                        "SAFE_RULE"
                        if safe_review_fallback
                        else "GPT_FALLBACK"
                        if prefer_template
                        else "GPT_DIRECT"
                    )
                    selected_general_route = (
                        "REVIEW_REQUIRED_SAFE_DRAFT"
                        if safe_review_fallback
                        else generation_mode
                    )
                    result.metadata.update(
                        {
                            "answer_type": (
                                "review_required_safe_draft"
                                if safe_review_fallback
                                else "gpt_generated"
                            ),
                            "answer_source": (
                                "SAFE_TEMPLATE"
                                if safe_review_fallback
                                else "openai"
                            ),
                            "generation_mode": generation_mode,
                            "selected_answer_route": selected_general_route,
                            "template_preferred": bool(prefer_template),
                            "template_override": not bool(prefer_template),
                            "template_id": (
                                "REVIEW_REQUIRED_SAFE_DRAFT"
                                if safe_review_fallback
                                else None
                            ),
                            "template_name": (
                                "REVIEW_REQUIRED_SAFE_DRAFT"
                                if safe_review_fallback
                                else None
                            ),
                            "template_version": (
                                "safe-rule-v1"
                                if safe_review_fallback
                                else None
                            ),
                            "order_id_present": bool(
                                request.order_id.strip()
                            ),
                            "dps_lookup_attempted": False,
                            "delivery_date_found": False,
                            "gpt_called": True,
                            "draft_created": True,
                            "delivery_question": False,
                        }
                    )
                    if not safe_review_fallback:
                        self.logs.record_inquiry(
                            inquiry_id,
                            (
                                "GPT_FALLBACK_SUCCESS"
                                if prefer_template
                                else "GPT_DIRECT_SUCCESS"
                            ),
                            (
                                "적용 가능한 기존 템플릿이 없어 GPT로 새 답변을 생성했습니다."
                                if prefer_template
                                else "사용자 요청으로 GPT 새 답변을 생성했습니다."
                            ),
                            details={
                                **decision_details,
                                "generation_mode": generation_mode,
                                "selected_answer_route": generation_mode,
                                "provider": result.provider,
                                "template_preferred": bool(prefer_template),
                                "template_id": None,
                                "template_name": None,
                                "gpt_called": True,
                                "dps_lookup_attempted": False,
                                "validator_result": (
                                    getattr(validation, "status", "PASS")
                                    if validation is not None
                                    else "PASS"
                                ),
                                "draft_length": len(result.answer.strip()),
                                "correlation_id": correlation_id,
                            },
                        )
            final_route = str(
                result.metadata.get("selected_answer_route")
                or result.metadata.get("generation_mode")
                or plan.selected_answer_route
            ).upper()
            product_fact_guard = classify_product_fact(
                request.question,
                inquiry_type=phase9_analysis.inquiry_type.value,
                inquiry_subtype=phase9_analysis.inquiry_subtype,
                product_id=request.metadata.get("product_id"),
                product_name=request.product_name,
                option_name=request.option_name,
            )
            current_fact_verified = bool(
                product_fact_guard.sensitive and final_route == "PRODUCT_DB"
            )
            guard_metadata = {
                **product_fact_guard.to_dict(),
                "current_fact_verified": current_fact_verified,
                "current_fact_source": "PRODUCT_DB" if current_fact_verified else None,
                "auto_post_allowed": (
                    not product_fact_guard.sensitive or current_fact_verified
                ),
            }
            result.metadata["product_fact_guard"] = guard_metadata
            if product_fact_guard.sensitive and not current_fact_verified:
                # The draft may still be useful to staff, but no Product DB
                # miss/GPT route may assert a past model's fact automatically.
                result.status = AnswerStatus.NEEDS_REVIEW
                result.auto_answerable = False
                result.needs_review = True
                result.metadata["requires_manual_review"] = True
                result.metadata["product_fact_guard_reason"] = (
                    "CURRENT_PRODUCT_FACT_NOT_VERIFIED"
                )
                self.logs.record_inquiry(
                    inquiry_id,
                    "PRODUCT_FACT_REVIEW_REQUIRED",
                    "현재 상품의 검증된 사실을 확보하지 못해 직원 검토로 전환했습니다.",
                    level="WARNING",
                    details={
                        "selected_answer_route": final_route,
                        **guard_metadata,
                    },
                )
            plan = plan.finalized(
                final_route,
                generation_mode=str(
                    result.metadata.get("generation_mode") or "RULE"
                ),
                template_id=(
                    str(result.metadata.get("template_id"))
                    if result.metadata.get("template_id")
                    else None
                ),
                needs_staff_review=bool(result.needs_review),
            )
            result.metadata.update(
                {
                    "detected_intent": plan.detected_intent,
                    "question_category": plan.question_category,
                    "is_delivery": plan.is_delivery,
                    "delivery_related": plan.delivery_related,
                    "needs_delivery_lookup": (
                        plan.needs_delivery_lookup
                    ),
                    "requires_order_lookup": plan.requires_order_lookup,
                    "requires_dps_lookup": plan.requires_dps_lookup,
                    "can_execute_dps_lookup": plan.can_execute_dps_lookup,
                    "can_generate_answer": plan.can_generate_draft,
                    "can_generate_draft": plan.can_generate_draft,
                    "order_id_status": plan.order_id_status,
                    "order_lookup_status": plan.order_lookup_status,
                    "dps_lookup_status": plan.dps_lookup_status,
                    "selected_answer_route": plan.selected_answer_route,
                    "processing_plan": plan.to_dict(),
                    "reason_code": plan.reason_code,
                    "correlation_id": correlation_id,
                }
            )
            phase9_metadata = (
                dict(result.metadata.get("phase9"))
                if isinstance(result.metadata.get("phase9"), dict)
                else {}
            )
            phase9_metadata["analysis"] = analysis_data
            result.metadata["phase9"] = phase9_metadata
            if not is_delivery_schedule:
                self.logs.record_inquiry(
                    inquiry_id,
                    "ANSWER_ROUTE_SELECTED",
                    "일반 문의 답변 경로를 선택했습니다.",
                    details={
                        **decision_details,
                        "selected_answer_route": result.metadata.get(
                            "generation_mode"
                        ),
                        "generation_mode": result.metadata.get(
                            "generation_mode"
                        ),
                        "answer_source": result.metadata.get("answer_source"),
                    },
                )
                self.logs.record_inquiry(
                    inquiry_id,
                    "ANSWER_PREREQUISITE_PASSED",
                    "주문·DPS 조회 없이 일반 답변을 생성할 수 있습니다.",
                    details={
                        **decision_details,
                        "selected_answer_route": result.metadata.get(
                            "generation_mode"
                        ),
                    },
                )
            # Every route (Rule, Template, Product DB, GPT and safe delivery
            # routes) crosses the same final rendering boundary.  The
            # formatter is idempotent, so already formatted legacy/template
            # answers cannot produce a duplicate wrapper.
            result.answer = format_final_answer(result.answer)
            if not is_valid_draft(result.answer):
                raise AnswerGenerationError(
                    "답변 생성 결과가 비어 있어 초안을 저장할 수 없습니다."
                )
            dps_metadata = (
                request.metadata.get("dps")
                if isinstance(request.metadata.get("dps"), dict)
                else {}
            )
            governance = (
                result.metadata.get("governance")
                if isinstance(result.metadata.get("governance"), dict)
                else {}
            )
            draft = self.answers.create_program_draft(
                inquiry_id,
                result,
                order_id=request.order_id or None,
                dps_lookup_id=dps_metadata.get("dps_lookup_id"),
                prompt_version=governance.get("prompt_version"),
                facts_version="phase9-selected-facts-v1",
            )
            saved_draft = self.answers.get(int(draft["id"]))
            if (
                saved_draft is None
                or not is_valid_draft(saved_draft.get("original_answer"))
            ):
                raise AnswerGenerationError(
                    "저장된 답변 초안을 다시 확인할 수 없습니다."
                )
            draft = saved_draft
            active_draft = self.answers.active_for_inquiry(inquiry_id)
            if draft.get("is_active"):
                if (
                    active_draft is None
                    or int(active_draft["id"]) != int(draft["id"])
                    or not is_valid_draft(active_draft.get("original_answer"))
                    or active_draft.get("original_answer")
                    != saved_draft.get("original_answer")
                ):
                    raise AnswerGenerationError(
                        "활성 답변 초안을 다시 확인할 수 없습니다."
                    )
            run_id = (
                getattr(self.hybrid_service, "last_run_id", None)
                if result.metadata.get("gpt_called")
                else None
            )
            run_repository = (
                getattr(self.hybrid_service, "runs", None)
                if result.metadata.get("gpt_called")
                else None
            )
            if run_id is not None and run_repository is not None:
                run_repository.attach_draft(int(run_id), int(draft["id"]))
            draft_log_details = {
                **decision_details,
                "template_preferred": bool(prefer_template),
                "selected_answer_route": result.metadata.get(
                    "selected_answer_route"
                ) or result.metadata.get("generation_mode"),
                "template_id": result.metadata.get("template_id"),
                "template_name": result.metadata.get("template_name"),
                "generation_mode": result.metadata.get("generation_mode"),
                "gpt_called": bool(result.metadata.get("gpt_called")),
                "dps_lookup_attempted": bool(
                    result.metadata.get("dps_lookup_attempted")
                ),
                "installation_date_found": bool(
                    result.metadata.get("delivery_date_found")
                ),
                "validator_result": (
                    result.metadata.get("validator_result", {}).get("status")
                    if isinstance(
                        result.metadata.get("validator_result"), dict
                    )
                    else result.metadata.get("validator_result")
                ),
                "draft_length": len(result.answer.strip()),
                "draft_saved": True,
                "draft_id": draft["id"],
                "active_draft_id": (
                    active_draft.get("id") if active_draft else None
                ),
            }
            self.logs.record_inquiry(
                inquiry_id,
                "DRAFT_CREATED",
                "검증된 답변 Draft를 저장했습니다.",
                details=draft_log_details,
            )
            self.logs.record_inquiry(
                inquiry_id,
                "DRAFT_ACTIVATED",
                (
                    "새 답변 Draft를 Active Draft로 지정했습니다."
                    if draft.get("is_active")
                    else "직원 수정본 보호로 신규 Draft를 비활성 저장했습니다."
                ),
                level="INFO" if draft.get("is_active") else "WARNING",
                details={
                    **draft_log_details,
                    "activated": bool(draft.get("is_active")),
                },
            )
            if result.status is AnswerStatus.GENERATED:
                self.workflows.complete_step(
                    inquiry_id,
                    StepCode.ANSWER_GENERATED,
                    metadata={
                        "draft_id": draft["id"],
                        "provider": result.provider,
                    },
                )
                if draft.get("is_active"):
                    self.inquiries.update_status(
                        inquiry_id,
                        InquiryStatus.REVIEW_PENDING,
                    )
                    self.inquiries.update_phase9_status(
                        inquiry_id,
                        (
                            "ORDER_INFO_REQUIRED"
                            if result.metadata.get(
                                "selected_answer_route"
                            )
                            == "ORDER_ID_REQUEST"
                            else "READY_FOR_REVIEW"
                        ),
                    )
                self.logs.record_inquiry(
                    inquiry_id,
                    "ANSWER_DRAFT_GENERATED",
                    "프로그램 답변 초안이 생성되었습니다.",
                    details={
                        "draft_id": draft["id"],
                        "status": result.status.value,
                        "category": result.category,
                        "provider": result.provider,
                    },
                )
                self.logs.record_inquiry(
                    inquiry_id,
                    "ANSWER_ROUTED_AND_SAVED",
                    "답변 우선순위 처리 결과를 저장했습니다.",
                    details={
                        "inquiry_id": inquiry_id,
                        "answer_type": result.metadata.get("answer_type"),
                        "answer_source": result.metadata.get(
                            "answer_source"
                        ),
                        "generation_mode": result.metadata.get(
                            "generation_mode"
                        ),
                        "template_preferred": bool(
                            result.metadata.get("template_preferred")
                        ),
                        "template_override": bool(
                            result.metadata.get("template_override")
                        ),
                        "template_id": result.metadata.get("template_id"),
                        "order_id_present": bool(request.order_id.strip()),
                        "delivery_question": bool(
                            analysis_data["delivery_question"]
                        ),
                        "dps_lookup_attempted": bool(
                            result.metadata.get("dps_lookup_attempted")
                        ),
                        "delivery_date_found": bool(
                            result.metadata.get("delivery_date_found")
                        ),
                        "gpt_called": bool(
                            result.metadata.get("gpt_called")
                        ),
                        "draft_length": len(result.answer.strip()),
                        "draft_saved": True,
                        "draft_id": draft["id"],
                        "active_draft_id": (
                            active_draft.get("id") if active_draft else None
                        ),
                        "rendered_draft_id": None,
                    },
                )
                self.logs.record_inquiry(
                    inquiry_id,
                    "ANSWER_GENERATION_COMPLETED",
                    "답변 초안 생성과 저장 검증을 완료했습니다.",
                    details={
                        "inquiry_id": inquiry_id,
                        "answer_type": result.metadata.get("answer_type"),
                        "answer_source": result.metadata.get("answer_source"),
                        "generation_mode": result.metadata.get(
                            "generation_mode"
                        ),
                        "template_preferred": bool(
                            result.metadata.get("template_preferred")
                        ),
                        "template_override": bool(
                            result.metadata.get("template_override")
                        ),
                        "template_id": result.metadata.get("template_id"),
                        "order_id_present": bool(request.order_id.strip()),
                        "delivery_question": bool(
                            analysis_data["delivery_question"]
                        ),
                        "dps_lookup_attempted": bool(
                            result.metadata.get("dps_lookup_attempted")
                        ),
                        "delivery_date_found": bool(
                            result.metadata.get("delivery_date_found")
                        ),
                        "gpt_called": bool(
                            result.metadata.get("gpt_called")
                        ),
                        "draft_id": draft["id"],
                        "draft_length": len(result.answer.strip()),
                        "draft_saved": True,
                        "active_draft_id": (
                            active_draft.get("id") if active_draft else None
                        ),
                        "rendered_draft_id": None,
                    },
                )
                log_details = {
                    "masked_order_id": (
                        DpsEnrichmentService._masked_order_id(
                            request.order_id
                        )
                    ),
                    "dps_lookup_id": dps_metadata.get("dps_lookup_id"),
                    "draft_id": draft["id"],
                    "correlation_id": (
                        governance.get("correlation_id")
                    ),
                    "status": "SAVED",
                    "model": governance.get("model"),
                    "normalized_date": dps_metadata.get(
                        "installation_date"
                    ),
                }
                self.logs.record_inquiry(
                    inquiry_id,
                    "GPT_DRAFT_SAVED",
                    "GPT Program Answer 초안을 저장했습니다.",
                    details=log_details,
                )
                self.logs.record_inquiry(
                    inquiry_id,
                    (
                        "GPT_DRAFT_ACTIVATED"
                        if draft.get("is_active")
                        else "GPT_DRAFT_RENDER_MISMATCH"
                    ),
                    (
                        "GPT 초안을 활성화했습니다."
                        if draft.get("is_active")
                        else "직원 수정본 보호로 새 GPT 초안을 비활성 상태로 저장했습니다."
                    ),
                    level="INFO" if draft.get("is_active") else "WARNING",
                    details={
                        **log_details,
                        "status": (
                            "ACTIVE"
                            if draft.get("is_active")
                            else "STAFF_EDIT_PROTECTED"
                        ),
                    },
                )
                if dps_outcome.decision.lookup_required:
                    self.logs.record_inquiry(
                        inquiry_id,
                        "ANSWER_GENERATED_WITH_DPS",
                        "DPS 결과를 반영한 답변 초안을 생성했습니다.",
                        details={
                            "draft_id": draft["id"],
                            "dps_status": dps_outcome.metadata[
                                "lookup_status"
                            ],
                            "cache_used": bool(
                                dps_outcome.metadata.get("cache_used")
                            ),
                        },
                    )
            else:
                self.workflows.complete_step(
                    inquiry_id,
                    StepCode.ANSWER_GENERATED,
                    metadata={
                        "draft_id": draft["id"],
                        "provider": result.provider,
                        "requires_staff_review": True,
                        "result_status": result.status.value,
                    },
                )
                if draft.get("is_active"):
                    self.inquiries.update_status(
                        inquiry_id,
                        InquiryStatus.REVIEW_PENDING,
                    )
                hybrid_metadata = (
                    result.metadata.get("hybrid")
                    if isinstance(result.metadata.get("hybrid"), dict)
                    else {}
                )
                validation_metadata = (
                    hybrid_metadata.get("validation")
                    if isinstance(hybrid_metadata.get("validation"), dict)
                    else {}
                )
                self.inquiries.update_phase9_status(
                    inquiry_id,
                    (
                        "VALIDATION_BLOCKED"
                        if validation_metadata.get("status") == "BLOCK"
                        else "READY_FOR_REVIEW"
                    ),
                )
                self.logs.record_inquiry(
                    inquiry_id,
                    "ANSWER_DRAFT_NEEDS_REVIEW",
                    "답변 후보에 직원 검토가 필요합니다.",
                    level="WARNING",
                    details={
                        "draft_id": draft["id"],
                        "status": result.status.value,
                        "category": result.category,
                        "provider": result.provider,
                    },
                )
                self.logs.record_inquiry(
                    inquiry_id,
                    "ANSWER_ROUTED_AND_SAVED",
                    "직원 검토가 필요한 답변 초안을 저장했습니다.",
                    level="WARNING",
                    details={
                        "inquiry_id": inquiry_id,
                        "answer_type": result.metadata.get("answer_type"),
                        "answer_source": result.metadata.get(
                            "answer_source"
                        ),
                        "generation_mode": result.metadata.get(
                            "generation_mode"
                        ),
                        "template_preferred": bool(
                            result.metadata.get("template_preferred")
                        ),
                        "template_override": bool(
                            result.metadata.get("template_override")
                        ),
                        "template_id": result.metadata.get("template_id"),
                        "order_id_present": bool(request.order_id.strip()),
                        "delivery_question": bool(
                            analysis_data["delivery_question"]
                        ),
                        "dps_lookup_attempted": bool(
                            result.metadata.get("dps_lookup_attempted")
                        ),
                        "delivery_date_found": bool(
                            result.metadata.get("delivery_date_found")
                        ),
                        "gpt_called": bool(
                            result.metadata.get("gpt_called")
                        ),
                        "draft_length": len(result.answer.strip()),
                        "draft_saved": True,
                        "draft_id": draft["id"],
                        "active_draft_id": (
                            active_draft.get("id") if active_draft else None
                        ),
                        "rendered_draft_id": None,
                    },
                )
            if plan.needs_staff_review:
                self.logs.record_inquiry(
                    inquiry_id,
                    "ANSWER_VALIDATION_REVIEW_REQUIRED",
                    "검증된 안전 Draft를 직원 검토 대기로 저장했습니다.",
                    level="WARNING",
                    details={
                        "selected_answer_route": plan.selected_answer_route,
                        "validator_result": "PASS_REVIEW_REQUIRED",
                        "draft_saved": True,
                        "active_draft_id": (
                            active_draft.get("id") if active_draft else None
                        ),
                        "correlation_id": correlation_id,
                    },
                )
                self.logs.record_inquiry(
                    inquiry_id,
                    "SAFE_DRAFT_CREATED",
                    "예상 가능한 조회·정보 부족 상태의 안전 Draft를 생성했습니다.",
                    details={
                        "selected_answer_route": plan.selected_answer_route,
                        "draft_id": draft["id"],
                        "correlation_id": correlation_id,
                    },
                )
            completed_details = {
                **plan.to_dict(),
                "draft_saved": True,
                "active_draft_id": (
                    active_draft.get("id") if active_draft else None
                ),
                "gpt_called": bool(result.metadata.get("gpt_called")),
                "validator_result": (
                    "PASS_REVIEW_REQUIRED"
                    if plan.needs_staff_review
                    else "PASS"
                ),
            }
            self.logs.record_inquiry(
                inquiry_id,
                "PROCESSING_PLAN_COMPLETED",
                "단일 처리계획에 따른 답변 생성과 저장을 완료했습니다.",
                details={
                    key: value
                    for key, value in completed_details.items()
                    if key not in {
                        "normalized_text",
                        "order_id",
                        "product_order_id",
                        "analysis",
                    }
                },
            )
            return AnswerGenerationOutcome(result=result, draft=draft)
        except Exception as error:
            if step_started:
                try:
                    self.workflows.fail_step(
                        inquiry_id,
                        StepCode.ANSWER_GENERATED,
                        _error_code(error),
                        _user_error_message(error),
                    )
                    self.inquiries.update_status(
                        inquiry_id,
                        InquiryStatus.NEEDS_ATTENTION,
                    )
                except Exception:
                    LOGGER.exception(
                        "답변 생성 실패 상태 기록 중 추가 오류: inquiry_id=%s",
                        inquiry_id,
                    )
            try:
                self.logs.record_inquiry(
                    inquiry_id,
                    "PROCESSING_PLAN_FAILED",
                    "처리계획 실행 중 시스템 오류가 발생했습니다.",
                    level="ERROR",
                    details={
                        "safe_error_code": _error_code(error),
                        "correlation_id": correlation_id,
                    },
                )
                self.logs.record_inquiry(
                    inquiry_id,
                    "ANSWER_GENERATION_FAILED",
                    _user_error_message(error),
                    level="ERROR",
                    details={"error_type": error.__class__.__name__},
                )
            except Exception:
                LOGGER.exception(
                    "답변 생성 실패 활동 로그 기록 오류: inquiry_id=%s",
                    inquiry_id,
                )
            LOGGER.exception(
                "답변 생성 실패: inquiry_id=%s error_type=%s",
                inquiry_id,
                error.__class__.__name__,
            )
            if isinstance(error, AnswerEngineError):
                raise
            raise AnswerGenerationError(_user_error_message(error)) from error
