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
    AutoAnswerProhibitedError,
)
from answer.hold_reasons import primary_reason
from answer.models import AnswerResult, AnswerStatus
from answer.inquiry_analysis import InquiryAnalysis
from answer.inquiry_processing_plan import InquiryProcessingPlan
from answer.answer_format import extract_answer_body, format_final_answer
from answer.answer_validator import AnswerValidator
from answer.safe_draft import review_required_safe_result as _review_required_safe_result
from answer.source_adapter import answer_request_from_inquiry
from answer.text_utils import restore_question_mark, split_subquestions
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.dps_repository import DpsRepository
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from repositories.workflow_repository import WorkflowRepository
from kakao_notify import notify_qna_safely
from services.dps_enrichment_service import (
    DpsEnrichmentOutcome,
    DpsEnrichmentService,
)
from services.dps_lookup_policy import DpsLookupDecision, DpsLookupStatus
from services.hybrid_answer_service import HybridAnswerService
from services.auto_processing_eligibility_service import (
    AutoProcessingEligibility,
    AutoProcessingEligibilityService,
)
from services.inquiry_analysis_service import InquiryAnalysisService
from services.product_knowledge_service import (
    ProductKnowledgeResult,
    ProductKnowledgeService,
)
from services.inquiry_processing_plan_service import (
    InquiryProcessingPlanService,
)
from services.phase9_answer_policy import apply_phase9_rule_policy
from services.atomic_completeness_service import (
    AtomicCompletenessService,
)
from answer.providers.provider_factory import create_gpt_provider
from services.gpt_semantic_analyzer_service import (
    GptSemanticAnalyzerService,
)
from services.semantic_analysis import (
    SemanticAnalysis,
    is_enabled as semantic_analyzer_enabled,
    route as semantic_route,
)
from services.semantic_coverage_service import (
    SemanticCoverageService,
    is_enabled as semantic_coverage_enabled,
)
from services.gpt_governance_service import GovernedHybridAnswerService
from services.uat_order_service import UatOrderService
from services.order_service import lookup_general_order_id
from services.product_fact_guard import (
    classify_product_fact,
    extract_model_code,
)
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


# Rule matchers whose wording is deterministic for the situation they match:
# fixed company/legal policy text, event announcements, and structured product
# catalog facts. Only these may become the customer-facing final answer without
# the GPT composition step.
#
# Deliberately excluded: KEYWORD_LEARNED_RULE and
# KEYWORD_SIMPLE_PRODUCT_USAGE. Both match on substring keywords only, so they
# can fire on a question they do not actually answer -- e.g. "AS는 삼성서비스
# 센터에서 하나요?" matching an "A/S 접수 전화번호" rule and replying with a
# phone number instead of answering yes. Those results are still generated and
# handed to the GPT step as reference context; they simply no longer decide the
# final answer by themselves.
EXACT_TEMPLATE_MATCH_KINDS = frozenset({
    "FIXED_POLICY_HARD_BLOCK",
    "FIXED_POLICY_STORE_PICKUP",
    "FIXED_EVENT_REVIEW",
    "FIXED_PACKAGE_CODE",
    "FIXED_EVENT_ONNURI",
    "FIXED_POLICY_SHIPPING",
    "FIXED_POLICY_INSTALL",
    "FIXED_POLICY_PICKUP",
    "FIXED_PRODUCT_ACCESSORY",
    "PRODUCT_DB_MODEL_CODE",
    "PRODUCT_DB_MODEL_SPEC",
})


def _template_may_answer(metadata: dict[str, Any]) -> bool:
    """True when a rule result is exact enough to be the final answer.

    An unknown/absent match kind is treated as exact so that callers which
    construct rule results outside AnswerEngine (tests, legacy fixtures,
    injected providers) keep their existing behaviour.
    """

    kind = str(metadata.get("template_match_kind") or "").upper()
    if not kind or kind == "UNKNOWN":
        return True
    return kind in EXACT_TEMPLATE_MATCH_KINDS


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
    if not _template_may_answer(metadata):
        return "NOT_EXACT_MATCH"
    validation = validator.validate_template_text(
        result.answer, question=request.question
    )
    if not validation.passed:
        return "VALIDATION_FAILED"
    return None


# Diagnostics that survive neutralisation. Deliberately not ``answer_type``
# or ``matched_rule``: those identify the draft, and the neutral context must
# not be mistaken for one.
_CARRIED_REJECTION_DIAGNOSTICS = frozenset({
    "semantic_rule_rejected",
    "rejected_rule_category",
    "rejected_template_match_kind",
    "safe_failure_code",
})


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
            # Why the deterministic candidate was discarded is written once,
            # here, and read by nothing but a person looking at the draft
            # afterwards. Neutralising the result must not also erase the
            # reason, or an investigation like 688159337's has nothing to read.
            **{
                key: value
                for key, value in (template_result.metadata or {}).items()
                if key in _CARRIED_REJECTION_DIAGNOSTICS
            },
        },
    )


def _template_candidate_payload(
    result: AnswerResult,
    *,
    source: str,
) -> dict[str, Any] | None:
    """Return an existing rule/Phase9 result as non-binding GPT evidence.

    ``AnswerEngine`` and Phase9 already own the rendered wording and its
    provenance.  The GPT path must not turn that one result into the whole
    answer, but it may show the model the candidate alongside Product and
    Learning evidence.  Keep this deliberately small and serialisable: this
    is a reuse of the existing result, not a second template system.
    """

    answer = str(result.answer or "").strip()
    if not answer:
        return None
    metadata = dict(result.metadata or {})
    return {
        "kind": "PHASE9" if source == "PHASE9" else "RULE_TEMPLATE",
        "source": source,
        "template_id": result.matched_rule or metadata.get("template_id"),
        "category": result.category,
        "reason": result.reason,
        "answer": answer,
        "template_match_kind": metadata.get("template_match_kind"),
        "requires_review": bool(result.needs_review),
        "auto_answerable": bool(result.auto_answerable),
        "metadata": {
            key: metadata[key]
            for key in (
                "answer_source",
                "answer_type",
                "template_variables",
                "delivery_context",
            )
            if key in metadata
        },
    }


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
        product_knowledge: ProductKnowledgeService | None = None,
        semantic_analyzer: GptSemanticAnalyzerService | None = None,
    ) -> None:
        self.database = database
        self._engine = engine
        self._dps_enrichment = dps_enrichment
        # Whether a *caller* supplied these, recorded before anything can
        # lazily fill the attributes in. The synthetic order snapshot below
        # keys off "a test injected a DPS double and no order service", and
        # the lazily-created production instances are indistinguishable from
        # injected ones once ``_dps_enrichment``/``_order_lookup_service``
        # have been populated -- which is how a test-only shortcut became
        # reachable in production. See ``_use_synthetic_order_snapshot``.
        self._dps_enrichment_injected = dps_enrichment is not None
        self._order_lookup_injected = order_lookup_service is not None
        self._hybrid_service = hybrid_service
        self._eligibility: AutoProcessingEligibilityService | None = None
        # Separate knowledge source, read-only, in its own database. Injected
        # so tests can point it at a fixture without touching the real file.
        self.product_knowledge = product_knowledge or ProductKnowledgeService()
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
        self.semantic_coverage = SemanticCoverageService()
        self.completeness = AtomicCompletenessService()
        self._semantic_analyzer_instance = semantic_analyzer

    def _complete_atomic_answer(
        self,
        inquiry_id: int,
        request: AnswerRequest,
        result: AnswerResult,
        analysis: InquiryAnalysis,
    ) -> str:
        """Make sure no part of the question left the draft without a trace.

        The model is told to address every atomic question and usually does;
        when it does not, the part simply vanishes and nothing downstream
        notices, because the validator asks whether the answer is safe and the
        eligibility gate asks whether it may be published. Neither asks whether
        it is complete.

        Only a partial answer is completed, and only with a sentence that
        states no fact. Publication is untouched: the validator still runs on
        the completed text and the eligibility gate still decides on its own
        reasons. A fault here can never fail an answer that was ready to save.
        """

        hybrid = result.metadata.get("hybrid")
        if (
            isinstance(hybrid, dict)
            and hybrid.get("answer_pipeline") == "GPT_UNDERSTAND_RETRIEVE_ANSWER"
        ):
            # The anchors decide this from the customer's wording and the
            # draft's, and they recognise neither side of an inquiry they have
            # no entry for: "삼성기사분이 설치하러 오시나요" and the confirmed
            # answer to it both reduce to no topics at all. Appending a
            # deferral sentence off that reading would tell a customer their
            # answered question still needs a person. GPT ② reports the same
            # finding as ``unresolved``, on questions it actually read.
            result.metadata["legacy_atomic_completeness"] = (
                "PRODUCTION_PATH_UNUSED"
            )
            return result.answer
        body = extract_answer_body(result.answer)
        try:
            completeness = self.completeness.evaluate(
                question=request.question,
                answer=body,
                subquestions=analysis.subquestion_analyses,
            )
        except Exception as error:  # pragma: no cover - defensive only
            self.logs.record_inquiry(
                inquiry_id,
                "ATOMIC_COMPLETENESS_FAILED",
                "질문 완결성 점검에 실패했습니다. 답변 생성에는 영향이 없습니다.",
                level="WARNING",
                details={"error_type": type(error).__name__},
            )
            return result.answer

        payload = completeness.to_dict()
        result.metadata["atomic_completeness"] = payload
        if not completeness.needs_completion:
            self.logs.record_inquiry(
                inquiry_id,
                "ATOMIC_ANSWER_COMPLETE",
                "고객이 물은 각 항목이 답변 또는 확인 필요로 표시되었습니다.",
                level="INFO",
                details=payload,
            )
            return result.answer

        completed = self.completeness.complete(
            body, completeness.deferral_sentence
        )
        self.logs.record_inquiry(
            inquiry_id,
            "ATOMIC_ANSWER_COMPLETED",
            "답변에서 빠진 문의 항목을 확인 필요로 명시했습니다.",
            level="INFO",
            details=payload,
        )
        return completed

    def _record_atomic_questions(
        self,
        inquiry_id: int,
        analysis: InquiryAnalysis,
    ) -> None:
        """Record how the inquiry was split and what each part was judged to be.

        Diagnostic only. ``manual_review_required`` on the aggregate is what
        the safety gates read and is untouched; this records which atomic
        question raised it, so "a person is needed" can be told apart from
        "nothing could be answered" without replaying the inquiry against a
        copy of the operational database.
        """

        records = analysis.subquestion_analyses
        if not records:
            return
        try:
            unresolved = analysis.unresolved_subquestions
            self.logs.record_inquiry(
                inquiry_id,
                "ATOMIC_QUESTION_ANALYZED",
                f"문의를 {len(records)}개 질문으로 나누어 각각 판단했습니다.",
                level="INFO",
                details={
                    "total": len(records),
                    "answerable": len(analysis.answerable_subquestions),
                    "unresolved": len(unresolved),
                    "unresolved_subtypes": [
                        str(item.get("inquiry_subtype") or "")
                        for item in unresolved
                    ],
                    "subquestions": [dict(item) for item in records],
                },
            )
        except Exception as error:  # pragma: no cover - defensive only
            self.logs.record_inquiry(
                inquiry_id,
                "ATOMIC_QUESTION_TRACE_FAILED",
                "질문 분해 진단 기록에 실패했습니다. 답변 생성에는 영향이 없습니다.",
                level="WARNING",
                details={"error_type": type(error).__name__},
            )

    def _semantic_analyzer(self) -> GptSemanticAnalyzerService | None:
        """One analyzer per service, so its cache survives repeat questions."""

        existing = getattr(self, "_semantic_analyzer_instance", None)
        if existing is not None:
            return existing
        try:
            provider = create_gpt_provider()
        except Exception:
            return None
        instance = GptSemanticAnalyzerService(provider)
        self._semantic_analyzer_instance = instance
        return instance

    def _semantic_for_routing(
        self,
        request: AnswerRequest,
        analysis: InquiryAnalysis,
    ) -> tuple[SemanticAnalysis | None, dict[str, Any]]:
        """Run the bounded semantic stage before any Plan/routing decision.

        Provider failure deliberately returns an unusable value and leaves the
        deterministic Plan intact.  The payload is later persisted with the
        draft and reused by the action-support gate, so one inquiry never pays
        for the same semantic understanding twice.
        """

        analysis_dict = analysis.to_dict()
        decision = semantic_route(request.question, analysis=analysis_dict)
        payload: dict[str, Any] = {
            "phase": "PRE_ROUTING",
            "router": decision.to_dict(),
            "called": False,
        }
        # GPT① is the production semantic authority.  The legacy router may
        # remain diagnostic telemetry, but it cannot decide that a new
        # inquiry is not worth understanding.  When the feature is enabled,
        # every inquiry receives the same bounded understanding attempt;
        # provider failure is represented explicitly below as workflow state.
        if not semantic_analyzer_enabled():
            payload["fallback"] = "DETERMINISTIC"
            return None, payload
        analyzer = self._semantic_analyzer()
        if analyzer is None:
            payload["fallback"] = "DETERMINISTIC_PROVIDER_UNAVAILABLE"
            return None, payload
        semantic = analyzer.analyze(request.question)
        payload.update({
            "called": True,
            "semantic": semantic.to_dict(),
            "understanding": self._understanding_contract(semantic),
            "trace": dict(analyzer.last_trace),
            "usable": semantic.usable,
            "fallback": None if semantic.usable else "DETERMINISTIC_UNUSABLE",
        })
        return semantic, payload

    @staticmethod
    def _understanding_contract(semantic: SemanticAnalysis | None) -> dict[str, Any]:
        """Compact GPT ① execution contract, derived from its existing result.

        This deliberately adds no classifier or second semantic model.  The
        contract makes the already-usable understanding consumable by code:
        retrieval services read the source requests and the processing plan
        reads only the Order/DPS execution requests.  A non-usable result is
        explicitly a fallback signal, never an alternative opinion.
        """

        if semantic is None or not semantic.usable:
            return {
                "usable": False,
                "source": "DETERMINISTIC_FALLBACK",
                "need_template": None,
                "need_product": None,
                "offer_product_record": None,
                "need_learning": None,
                "need_order": None,
                "need_dps": None,
                "purchase_state": None,
                "questions": [],
            }
        actions = {str(value).upper() for value in semantic.actions}
        product_actions = {"PRODUCT_SPEC", "PRODUCT_CONCEPT", "PACKAGE_CONTENTS"}
        template_actions = {
            "DELIVERY_STATUS", "DELIVERY_POLICY", "SCHEDULE_REQUEST",
            "SCHEDULE_CHANGE", "INSTALLATION_METHOD", "INSTALLATION_SCHEDULE",
        }
        current_order = semantic.purchase_state == "CURRENT_ORDER"
        need_dps = bool(
            current_order and semantic.requires_delivery_schedule
        )
        need_order = bool(
            current_order and (
                semantic.requires_order_context or need_dps
            )
        )
        return {
            "usable": True,
            "source": "GPT_UNDERSTAND",
            "need_template": bool(actions & template_actions),
            "need_product": bool(actions & product_actions),
            # Whether this inquiry may be shown the product's own record.
            #
            # ``need_product`` cannot answer that. It is a fixed list of three
            # action names, and an action lands in exactly one bucket: an
            # inquiry GPT ① read as INSTALLATION_METHOD is a template action,
            # so "뱅걸이 설치에 추가 비용이 있나요" was answered with the
            # listing's ``installation_method`` and ``installation_fee_applies``
            # withheld -- the two rows in the store that bear on it. Adding
            # INSTALLATION_METHOD to the list would fix that inquiry and leave
            # the next action name to be discovered the same way.
            #
            # So the record is offered on the same terms as Learning, and for
            # the same reason: it is context, the prompt says to use only what
            # the question needs, and which rows bear on the question is GPT
            # ②'s judgement. The one exclusion is the one Learning already
            # makes -- a current-order schedule inquiry is answered from
            # Order/DPS evidence, and a stored record has nothing to add to it.
            #
            # ``need_dps`` is an inquiry-wide flag, and withholding the record on
            # it alone let one clause speak for the others: "해상도가 어떻게
            # 되고, 설치도 해주시나요? 배송은 언제 받을 수 있을까요?" raised
            # need_dps for its third question and the first question's answer --
            # a verified resolution in this product's record -- arrived with one
            # keyword-matched field instead of the record. The exclusion is
            # therefore scoped to the inquiry it was written for: a single
            # question that is only about the customer's current schedule.
            "offer_product_record": not need_dps or len(
                semantic.atomic_questions or ()
            ) > 1,
            # Learning is recall-oriented context.  It is intentionally
            # requested for all non-current-schedule questions so GPT ②,
            # rather than a keyword gate, decides whether it is useful.
            "need_learning": not need_dps,
            "need_order": need_order,
            "need_dps": need_dps,
            "purchase_state": semantic.purchase_state,
            "questions": [
                {
                    "text": item.text,
                    "action": item.action,
                    "requested_information": item.requested_information,
                    "requested_attribute": item.requested_attribute,
                }
                for item in semantic.atomic_questions
            ],
        }

    @staticmethod
    def _attach_semantic_routing(
        request: AnswerRequest,
        semantic: SemanticAnalysis | None,
        payload: dict[str, Any],
    ) -> None:
        request.metadata["semantic_routing"] = dict(payload)
        request.metadata["gpt_understanding"] = dict(
            payload.get("understanding") or {}
        )
        # In-memory only: never serialised directly, and used to prevent a
        # second provider call after the answer has been rendered.
        request.metadata["_semantic_routing_value"] = semantic

    # Rejection reasons that are facts about a stored row rather than
    # judgements about this question. These are the removals CODE owns, and
    # keeping them apart from the rest is what lets a reader see that the
    # pipeline deleted nothing on semantic grounds.
    _DATA_SAFETY_REJECTIONS: tuple[str, ...] = (
        "FILTERED_BY_VALIDITY",
        "REVOKED",
        "NEGATIVE_EXCLUDED",
        "REDACTION_TOKEN_CONTAMINATED",
        "ORDER_SCOPE_MISMATCH",
        "FILTERED_BY_RUNTIME_QUALITY",
    )

    @staticmethod
    def _evidence_provenance(
        learning: dict[str, Any], draft: dict[str, Any]
    ) -> dict[str, Any]:
        """One place that says what happened to the retrieved evidence."""

        def _ids(value: Any) -> list[int]:
            found: list[int] = []
            for item in value or ():
                try:
                    found.append(int(item))
                except (TypeError, ValueError):
                    continue
            return list(dict.fromkeys(found))

        delivered = _ids(learning.get("selected_learning_ids"))
        used = _ids(draft.get("used_learning_ids"))
        rejections = learning.get("rejection_counts")
        rejections = rejections if isinstance(rejections, dict) else {}
        return {
            # Store-wide: every active row the repository offered.
            "retrieved_pool": learning.get("candidate_count"),
            # After the row-fact filters only.
            "data_integrity_pool": learning.get("safe_candidate_count"),
            "delivered_to_gpt": delivered,
            "used_by_gpt": used,
            "not_used": [item for item in delivered if item not in set(used)],
            "filtered_for_data_safety": {
                key: rejections[key]
                for key in AnswerService._DATA_SAFETY_REJECTIONS
                if rejections.get(key)
            },
            "used_source_of_truth": "GPT2_STRUCTURED_OUTPUT",
        }

    @staticmethod
    def _usable_gpt_understanding(request: AnswerRequest) -> dict[str, Any] | None:
        """Return the persisted GPT① contract when it is authoritative."""

        value = request.metadata.get("gpt_understanding")
        if isinstance(value, dict) and value.get("usable") is True:
            return value
        semantic = request.metadata.get("_semantic_routing_value")
        if semantic is not None and getattr(semantic, "usable", False):
            return AnswerService._understanding_contract(semantic)
        return None

    @classmethod
    def _template_candidate_retrieval_requested(
        cls, request: AnswerRequest, *, prefer_template: bool,
    ) -> bool:
        """Whether GPT① requested semantic Template/RULE evidence.

        Legacy deterministic routing remains the fallback if GPT① is absent
        or invalid.  With usable understanding, ``need_template`` is the
        source request; a keyword rule must not independently re-open that
        semantic decision.
        """

        if not prefer_template:
            return False
        understanding = cls._usable_gpt_understanding(request)
        return True if understanding is None else bool(
            understanding.get("need_template")
        )

    @staticmethod
    def _append_template_candidate(
        request: AnswerRequest, payload: dict[str, Any],
    ) -> None:
        candidates = request.metadata.setdefault("template_candidates", [])
        if not isinstance(candidates, list):
            candidates = []
            request.metadata["template_candidates"] = candidates
        identity = (
            payload.get("source"),
            payload.get("template_id"),
            payload.get("answer"),
        )
        if not any(
            isinstance(item, dict)
            and (item.get("source"), item.get("template_id"), item.get("answer"))
            == identity
            for item in candidates
        ):
            candidates.append(payload)

    @classmethod
    def _record_template_candidate(
        cls,
        request: AnswerRequest,
        result: AnswerResult,
        *,
        source: str,
    ) -> None:
        payload = _template_candidate_payload(result, source=source)
        if payload is None:
            return
        cls._append_template_candidate(request, payload)

    def _record_template_candidates(self, request: AnswerRequest) -> int:
        """Ask the rule engine for everything that could apply to this product.

        The rule engine used to be asked for *the* answer, and a miss produced
        nothing at all -- not even a candidate -- because
        ``_template_candidate_payload`` drops a result with an empty body. So an
        inquiry whose wording missed the substring table left GPT ② with no
        Template evidence whatever, while the store's confirmed sentence about
        that exact product sat unrendered in the engine.

        ``candidates`` answers the question the engine can actually answer --
        which of our standing statements are about this product -- and GPT ②
        does the rest. Never raises: no candidates is a normal outcome.
        """

        try:
            found = self.engine.candidates(request)
        except Exception:  # noqa: BLE001 - retrieval never blocks generation
            return 0
        for item in found:
            if isinstance(item, dict) and str(item.get("answer") or "").strip():
                self._append_template_candidate(request, dict(item))
        value = request.metadata.get("template_candidates")
        return len(value) if isinstance(value, list) else 0

    @staticmethod
    def _phase9_shortcut_allowed(request: AnswerRequest) -> bool:
        """Whether Phase9 may finish the *entire* customer inquiry.

        Phase9 owns reliable order/DPS actions and confirmed schedule text.  It
        does not own the meaning of a compound inquiry.  In particular, a
        pre-purchase delivery clause must not discard product or Learning
        evidence requested by another clause.

        Without a usable GPT understanding the inquiry is not handed back to
        the legacy router.  ``is_delivery_schedule`` is itself a keyword
        classification, so letting it finish an inquiry that nothing
        understood is the shadow path this architecture removes: retrieval and
        the GPT answer step still run, and eligibility holds the draft for
        staff through UNDERSTANDING_UNAVAILABLE.
        """

        semantic = request.metadata.get("_semantic_routing_value")
        if semantic is None or not getattr(semantic, "usable", False):
            return False
        questions = [
            str(getattr(item, "text", "") or "").strip()
            for item in (getattr(semantic, "atomic_questions", ()) or ())
        ]
        if len([item for item in questions if item]) > 1:
            return False
        # Phase9 may still complete a genuine current-order workflow (missing
        # order number / confirmed DPS facts).  A pre-purchase policy answer
        # is semantic evidence for GPT②, not an early final response.
        understanding = AnswerService._understanding_contract(semantic)
        return bool(
            understanding.get("need_order") or understanding.get("need_dps")
        )

    @staticmethod
    def _record_pipeline_trace(
        request: AnswerRequest, result: AnswerResult,
    ) -> None:
        """One place an operator can read why an answer came out as it did.

        The existing records are spread across ``hybrid``, ``phase9`` and the
        retrieval traces, and answering "why could GPT not answer this?" meant
        opening three of them and knowing which. This gathers the counts each
        stage actually produced -- what was understood, what was found, what
        was used -- into a flat block beside them.

        Counts, statuses and identifiers only. No prompt text, no candidate
        bodies, no customer data: this is read from dashboards and pasted into
        tickets.

        ``selected_answer_route`` is deliberately untouched. It reads
        ``GPT_FALLBACK`` on the GPT-first path, which is historical wording
        rather than a description, but the value is load-bearing -- the
        publishing gate's ``AUTO_POSTABLE_ROUTES`` and the validator's route
        table both key on it. Renaming it would edit a safety set to improve a
        label, so the accurate name is recorded here instead and the route
        keeps its meaning.
        """

        try:
            hybrid = result.metadata.get("hybrid")
            hybrid = hybrid if isinstance(hybrid, dict) else {}
            draft = hybrid.get("draft")
            draft = draft if isinstance(draft, dict) else {}
            retrieval = hybrid.get("retrieval")
            retrieval = retrieval if isinstance(retrieval, dict) else {}
            learning = retrieval.get("learning")
            learning = learning if isinstance(learning, dict) else {}
            understanding = (
                AnswerService._usable_gpt_understanding(request) or {}
            )
            knowledge = request.metadata.get("product_knowledge")
            templates = request.metadata.get("template_candidates")
            templates = templates if isinstance(templates, list) else []
            evidence = hybrid.get("subquestion_evidence")
            evidence = evidence if isinstance(evidence, list) else []
            result.metadata["pipeline_trace"] = {
                "answer_pipeline": hybrid.get("answer_pipeline"),
                "selected_answer_route": result.metadata.get(
                    "selected_answer_route"
                ),
                "provider_fallback_used": bool(hybrid.get("fallback_used")),
                "understanding": {
                    "usable": bool(understanding),
                    "atomic_question_count": len(
                        understanding.get("questions") or ()
                    ),
                    "need_template": understanding.get("need_template"),
                    "need_product": understanding.get("need_product"),
                    "need_learning": understanding.get("need_learning"),
                    "need_order": understanding.get("need_order"),
                    "need_dps": understanding.get("need_dps"),
                    "purchase_state": understanding.get("purchase_state"),
                },
                "retrieval": {
                    "template_candidates": len(templates),
                    "product_identity_status": getattr(
                        knowledge, "identity_status", None
                    ),
                    "product_candidates": len(
                        getattr(knowledge, "candidate_models", ()) or ()
                    ),
                    "verified_product_facts": len(
                        getattr(knowledge, "safe_facts", ()) or ()
                    ),
                    "learning_pool": learning.get("candidate_count"),
                    "learning_hard_valid": learning.get("safe_candidate_count"),
                    "learning_selected": learning.get("selected_count"),
                    "subquestion_evidence": [
                        {
                            "status": item.get("status"),
                            "source": item.get("source"),
                            "learning_ids": len(item.get("learning_ids") or ()),
                            "historical_ids": len(
                                item.get("historical_case_ids") or ()
                            ),
                        }
                        for item in evidence
                        if isinstance(item, dict)
                    ],
                },
                "answer": {
                    "used_template_ids": list(
                        draft.get("used_template_ids") or ()
                    ),
                    "used_product_facts": list(
                        draft.get("used_product_facts") or ()
                    ),
                    "used_learning_ids": list(
                        draft.get("used_learning_ids") or ()
                    ),
                    "used_historical_ids": list(
                        draft.get("used_historical_ids") or ()
                    ),
                    "ignored_evidence": len(draft.get("ignored_evidence") or ()),
                    "unresolved": len(draft.get("unresolved") or ()),
                    "requires_review": draft.get("requires_review"),
                    "can_auto_post": draft.get("can_auto_post"),
                },
                # Retrieved, delivered and used, told apart.
                #
                # All three numbers existed and none of them were beside each
                # other: the pool size lived in the retrieval diagnostics, what
                # reached the prompt in ``answer_learning_provenance``, and what
                # the model said it used in the draft. A reader comparing "677
                # candidates" with "2 used" could not tell which of the three
                # differences they were looking at, and the store-wide exclusion
                # counts sat in the same block as the per-inquiry ones.
                #
                # Nothing is inferred here. ``used_by_gpt`` is GPT ②'s own
                # structured output and no lexical heuristic guesses at it; a
                # delivered candidate the model did not name is NOT_USED, which
                # is an ordinary outcome and not a fault.
                "evidence_provenance": AnswerService._evidence_provenance(
                    learning, draft
                ),
            }
        except Exception:  # noqa: BLE001 - observability never blocks an answer
            result.metadata["pipeline_trace"] = {"status": "TRACE_FAILED"}

    def _record_semantic_coverage(
        self,
        inquiry_id: int,
        request: AnswerRequest,
        result: AnswerResult,
    ) -> None:
        """Record question/answer coverage observations.

        This legacy lexical measurement is diagnostic only. GPT② owns evidence
        sufficiency for current runs; a deterministic coverage classifier must
        not become a second semantic publisher on an older/fixed route.
        """

        hybrid = result.metadata.get("hybrid")
        if (
            isinstance(hybrid, dict)
            and hybrid.get("answer_pipeline") == "GPT_UNDERSTAND_RETRIEVE_ANSWER"
        ):
            result.metadata["legacy_semantic_coverage"] = "PRODUCTION_PATH_UNUSED"
            return
        if not semantic_coverage_enabled():
            return
        try:
            coverage = self.semantic_coverage.evaluate(
                question=request.question,
                answer=result.answer,
                route=str(result.metadata.get("selected_answer_route") or ""),
            )
            payload = coverage.to_dict()
            result.metadata["semantic_coverage"] = payload
            result.metadata["semantic_coverage_enforced"] = False
            self.logs.record_inquiry(
                inquiry_id,
                f"SEMANTIC_COVERAGE_{coverage.status}",
                "고객 질문과 답변의 대응 여부를 진단 정보로 기록했습니다.",
                level="INFO",
                details=payload,
            )
        except Exception as error:  # pragma: no cover - defensive only
            self.logs.record_inquiry(
                inquiry_id,
                "SEMANTIC_COVERAGE_ERROR",
                "질문 대응 관찰에 실패했습니다. 답변 생성에는 영향이 없습니다.",
                level="WARNING",
                details={"error_type": type(error).__name__},
            )

    def _use_synthetic_order_snapshot(self) -> bool:
        """Whether this instance is a legacy unit test with a DPS double.

        Read only from what the constructor was handed, never from the
        attributes: ``dps_enrichment`` and ``order_lookup_service`` are lazy
        properties that assign to ``_dps_enrichment`` and
        ``_order_lookup_service`` on first use, so the original test asking
        "was a double injected?" silently became "has anything touched DPS
        yet, and is this the first order lookup?" -- which a production run
        satisfies on its first delivery inquiry after start-up.

        The consequence was not a cosmetic one. The synthetic snapshot claims
        ``success`` for whatever number the customer typed, without calling
        Naver at all, so an order that does not exist was recorded as looked
        up and handed to DPS. Real inquiry 686427466 shows it: a number Naver
        answers with 100003 (주문을 찾을 수 없음) produced
        ``ORDER_LOOKUP_SUCCEEDED result_count=1`` 43ms after the lookup
        started, with no Naver request in the log at all.
        """

        return self._dps_enrichment_injected and not self._order_lookup_injected

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
    def eligibility(self) -> AutoProcessingEligibilityService:
        """The single publishing policy, reused for reporting, never re-stated."""

        if self._eligibility is None:
            self._eligibility = AutoProcessingEligibilityService()
        return self._eligibility

    @property
    def hybrid_service(self) -> HybridAnswerService:
        if self._hybrid_service is None:
            self._hybrid_service = GovernedHybridAnswerService(self.database)
        return self._hybrid_service

    def _hold_reason_for(
        self,
        *,
        inquiry: dict[str, Any],
        draft: dict[str, Any],
        result: AnswerResult,
    ) -> tuple[str, tuple[str, ...]]:
        """The publishing gate's own verdict, in the operator's language.

        Soft reasons are never allowed to stand in for a hard one: telling an
        operator "분류 신뢰도가 낮음" when the actual block is "직원 확인 필요"
        describes the wrong problem and invites the wrong action. They are
        still reported after the hard reasons, so nothing recorded is lost.
        """

        try:
            verdict = self.eligibility.evaluate(
                inquiry=inquiry,
                draft=draft,
                route=str(
                    result.metadata.get("selected_answer_route")
                    or result.metadata.get("generation_mode")
                    or ""
                ),
            )
        except Exception:
            LOGGER.exception(
                "미등록 사유 계산 실패: inquiry_id=%s", inquiry.get("id")
            )
            # Eligibility evaluation is an execution dependency. Its failure
            # is never evidence that a held draft became safe, and it must not
            # be papered over by legacy semantic metadata.
            return "ELIGIBILITY_EVALUATION_FAILED", (
                "ELIGIBILITY_EVALUATION_FAILED",
            )
        if verdict.safe:
            return "", ()
        return (
            primary_reason(verdict.reasons, verdict.soft_reasons),
            tuple((*verdict.reasons, *verdict.soft_reasons)),
        )

    def _notify_active_draft_safely(
        self,
        *,
        inquiry_id: int,
        inquiry: dict[str, Any],
        draft: dict[str, Any],
        result: AnswerResult,
        plan: InquiryProcessingPlan,
    ) -> None:
        if not draft.get("is_active"):
            return
        # ``plan.needs_staff_review`` is preliminary legacy semantic
        # telemetry.  It must not revive a review outcome after the persisted
        # GPT② decision has made this draft eligible.
        needs_review = result.status is not AnswerStatus.GENERATED
        # A generated answer is only an intermediate state while the same
        # automatic lifecycle still has to decide and perform Naver posting.
        # The confirmed post path owns the single success notification, so a
        # draft must not emit a second, misleading "generated" notification.
        # A held result, on the other hand, is final for this lifecycle and
        # still needs its one actionable notification.
        if not needs_review:
            return
        # Why the answer was written is what the pipeline records; why it is
        # not on Naver is what an operator has to act on. Those are different
        # questions, and the notification used to answer only the first --
        # ``result.reason`` describes the generation route, so a held inquiry
        # reported how its draft was composed and never said what was blocking
        # it. The publishing gate is asked directly instead, so the message
        # carries the same reason the dashboard shows for the same inquiry.
        hold_reason = ""
        hold_codes: tuple[str, ...] = ()
        generation_skipped = bool(
            result.metadata.get("generation_skipped")
        )
        if needs_review:
            hold_reason, hold_codes = self._hold_reason_for(
                inquiry=inquiry, draft=draft, result=result
            )
            # A stale non-generated status without a current hard/workflow or
            # GPT-evidence hold is not an operator action.
            needs_review = bool(hold_codes)
        if not needs_review:
            return
        try:
            notification_enqueued = notify_qna_safely(
                title=(
                    "[Q&A 미등록 / 직원 확인 필요]"
                    if needs_review
                    else "[네이버 Q&A 답변 생성 완료]"
                ),
                product=str(inquiry.get("product_name") or ""),
                option_name=str(inquiry.get("option_name") or ""),
                question=str(
                    inquiry.get("content")
                    or inquiry.get("title")
                    or ""
                ),
                answer=str(draft.get("original_answer") or result.answer),
                reason=str(result.reason or ""),
                action="needs_review" if needs_review else "generated",
                inquiry_id=str(
                    inquiry.get("external_inquiry_id")
                    or inquiry.get("source_question_id")
                    or inquiry_id
                ),
                notify_key=(
                    # Review is a terminal lifecycle outcome, not a draft
                    # version.  The late Auto Post hold uses this same key so
                    # a draft-stage hold and a later eligibility hold cannot
                    # produce two operator messages.
                    f"review-required:{inquiry_id}"
                ),
                hold_reason=hold_reason,
                hold_codes=hold_codes,
                generation_skipped=generation_skipped,
            )
            if notification_enqueued:
                self.logs.record_inquiry(
                    inquiry_id,
                    "KAKAO_NOTIFICATION_ENQUEUED",
                    "답변 초안을 카카오 공통 전송 대기열에 등록했습니다.",
                    details={
                        "draft_id": draft["id"],
                        "recipient": "staff_qna_room",
                        # The message shows short Korean phrases; the codes
                        # themselves are kept here so a hold can still be
                        # traced back to the exact gate reason that caused it.
                        "hold_reason_codes": list(hold_codes),
                    },
                )
        except Exception as error:
            LOGGER.exception(
                "카카오 알림 대기열 등록 실패: inquiry_id=%s draft_id=%s",
                inquiry_id,
                draft.get("id"),
            )
            try:
                self.logs.record_inquiry(
                    inquiry_id,
                    "KAKAO_NOTIFICATION_ENQUEUE_FAILED",
                    "답변은 정상 저장했지만 카카오 알림 등록에 실패했습니다.",
                    level="WARNING",
                    details={
                        "draft_id": draft.get("id"),
                        "error_type": error.__class__.__name__,
                    },
                )
            except Exception:
                LOGGER.exception(
                    "카카오 알림 실패 로그 기록 오류: inquiry_id=%s",
                    inquiry_id,
                )

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
            initial_request = answer_request_from_inquiry(inquiry)
            deterministic_analysis = self.analysis.analyze(initial_request)
            routing_semantic, semantic_routing = self._semantic_for_routing(
                initial_request, deterministic_analysis,
            )
            if processing_plan is not None and (
                routing_semantic is None or not routing_semantic.usable
            ):
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
                    semantic_analysis=routing_semantic,
                    semantic_routing=semantic_routing,
                    deterministic_analysis=deterministic_analysis,
                )
            if plan.inquiry_id != inquiry_id:
                raise AnswerGenerationError(
                    "문의와 처리계획의 식별자가 일치하지 않습니다."
                )
            correlation_id = plan.correlation_id
            request = answer_request_from_inquiry(inquiry)
            self._attach_semantic_routing(
                request, routing_semantic, semantic_routing,
            )
            phase9_analysis = plan.analysis
            analysis_data = phase9_analysis.to_dict()
            request.metadata["phase9_analysis"] = analysis_data
            request.metadata["processing_plan"] = plan.to_dict()
            # B5: looked up *here*, before any provider call, because a fact
            # the model never read cannot justify anything downstream. The
            # result rides on request.metadata so the hybrid path can put the
            # safe facts in the prompt and in the validator's evidence.
            # Sub-questions are passed separately so a compound inquiry keeps
            # the fields each part asks about.
            # With a usable GPT ① the catalogue is offered whole: the model
            # asked for product evidence, and which rows bear on the question is
            # a judgement GPT ② makes from the rows themselves. Without one, the
            # keyword topic filter remains the only way to keep an unrelated
            # specification out of a delivery prompt.
            understanding = self._usable_gpt_understanding(request)
            product_evidence_requested = bool(
                understanding is not None
                and understanding.get("offer_product_record")
            )
            product_knowledge = self.product_knowledge.facts_for_inquiry(
                product_id=request.metadata.get("product_id"),
                questions=split_subquestions(request.question),
                question=request.question,
                model_code=extract_model_code(request.product_name),
                product_name=request.product_name,
                option_name=request.metadata.get("option_name"),
                include_all_catalog_fields=product_evidence_requested,
            )
            request.metadata["product_knowledge"] = product_knowledge
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
            # The legacy pre-generation gate used to stand here. It read the
            # keyword classifier's intent/subtype/high-risk flags and stopped
            # the inquiry before retrieval, so nothing understood the question
            # and no evidence was ever collected. Meaning and answerability
            # belong to the GPT stages, so the call is gone rather than passed
            # empty arguments. Real policy blocks keep their own raises: an
            # empty question, a missing-item report and a current-order
            # schedule change each still refuse here and below.
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
                    if self._use_synthetic_order_snapshot():
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
                                validated_order_number=request.order_id,
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
                        semantic_analysis=routing_semantic,
                        semantic_routing=semantic_routing,
                        deterministic_analysis=deterministic_analysis,
                    )
                    inquiry = refreshed
                    request = answer_request_from_inquiry(inquiry)
                    self._attach_semantic_routing(
                        request, routing_semantic, semantic_routing,
                    )
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
            template_candidate_requested = (
                self._template_candidate_retrieval_requested(
                    request, prefer_template=prefer_template,
                )
            )
            request.metadata["template_candidate_retrieval"] = {
                "requested": template_candidate_requested,
                "source": (
                    "GPT_UNDERSTAND"
                    if self._usable_gpt_understanding(request) is not None
                    else "DETERMINISTIC_FALLBACK"
                ),
            }
            # Keep current-order actions (Order/DPS) on the delivery plan, but
            # only let Phase9 render the final reply when the semantic
            # understanding says this is one question.  A delivery clause in
            # a compound inquiry is evidence/policy context, not a licence to
            # terminate product or Learning retrieval for the whole inquiry.
            phase9_shortcut = (
                is_delivery_schedule
                and self._phase9_shortcut_allowed(request)
            )
            # Classify product-fact sensitivity before choosing a general
            # answer route. A non-authoritative SAFE_RULE used to skip
            # Approved Learning retrieval and was then rejected by this same
            # guard downstream. Exact templates and PRODUCT_DB keep their
            # existing authority; sensitive prose rules must reach Hybrid so
            # exact-model evidence can actually be evaluated.
            product_fact_guard = classify_product_fact(
                request.question,
                inquiry_type=phase9_analysis.inquiry_type.value,
                inquiry_subtype=phase9_analysis.inquiry_subtype,
                product_id=request.metadata.get("product_id"),
                product_name=request.product_name,
                option_name=request.option_name,
            )
            if is_delivery_schedule:
                # A schedule-change request is an operational action, not a
                # semantic uncertainty.  Until the real order/DPS action is
                # performed, a Q&A draft must not complete or auto-post it.
                dps_action = self.dps_enrichment.policy.decide(request)
                if dps_action.change_request:
                    self.workflows.initialize_steps(inquiry_id)
                    self.workflows.mark_needs_review(
                        inquiry_id,
                        StepCode.DPS_LOOKUP,
                        error_code="CURRENT_ORDER_ACTION_REQUIRED",
                        message="현재 주문의 일정 변경은 실제 처리 확인이 필요합니다.",
                        metadata={"change_request": True},
                    )
                    self.logs.record_inquiry(
                        inquiry_id,
                        "CURRENT_ORDER_ACTION_REQUIRED",
                        "일정 변경 요청은 자동 답변 완료 대상이 아니므로 직원 처리 대기로 전환했습니다.",
                        level="WARNING",
                        details={"change_request": True},
                    )
                    raise AutoAnswerProhibitedError(
                        "현재 주문의 일정 변경은 실제 처리 확인이 필요합니다.",
                        policy_reason="CURRENT_ORDER_ACTION_REQUIRED",
                    )
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
                    # The engine's own result is always kept as evidence.
                    # It no longer grounds the prompt (see ``gpt_rule_context``
                    # below), so a candidate entry is the only way it can reach
                    # the answer step at all -- and dropping it because the
                    # understanding did not ask for Template evidence would
                    # delete a confirmed store sentence that may still bear on
                    # the question.
                    self._record_template_candidate(
                        request, base_rule_result, source="ANSWER_ENGINE",
                    )
                    # The product-wide sweep stays understanding-driven: that
                    # is a retrieval decision, and the request for it is what
                    # makes the extra rows worth the prompt budget.
                    if template_candidate_requested:
                        self._record_template_candidates(request)
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
            if (
                is_delivery_schedule
                and not phase9_shortcut
                and template_candidate_requested
            ):
                # The delivery action above may already have populated Order
                # or DPS evidence.  Retrieve the deterministic candidate as
                # one input to the common GPT path rather than returning the
                # delivery placeholder as the whole answer.
                try:
                    base_rule_result = self.engine.generate(request)
                    self._record_template_candidate(
                        request, base_rule_result, source="ANSWER_ENGINE",
                    )
                    self._record_template_candidates(request)
                except Exception as template_error:
                    self.logs.record_inquiry(
                        inquiry_id,
                        "TEMPLATE_RENDER_FAILED",
                        "Template candidate rendering failed; continuing with evidence retrieval.",
                        level="WARNING",
                        details={"error_type": template_error.__class__.__name__},
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
                    semantic_analysis=routing_semantic,
                    semantic_routing=semantic_routing,
                    deterministic_analysis=deterministic_analysis,
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
            if (
                is_delivery_schedule
                and not phase9_shortcut
                and template_candidate_requested
            ):
                # Phase9 still renders its authoritative workflow/policy text,
                # but for a usable GPT① semantic route it is evidence for
                # GPT② rather than the whole inquiry's final answer.
                phase9_candidate = apply_phase9_rule_policy(
                    request, base_rule_result, phase9_analysis,
                )
                self._record_template_candidate(
                    request, phase9_candidate, source="PHASE9",
                )
            if phase9_shortcut:
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
                    if template_candidate_requested
                    else "BYPASSED"
                )
                if template_candidate_requested:
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
                generation_skipped = False
                try:
                    # A deterministic Rule/Template/Phase9 result is evidence, never
                    # the instruction the answer step must follow.  It reaches the model
                    # as one ``template_candidates`` entry beside Learning, Historical and
                    # the product record, and which of them answers the customer is the
                    # model's judgement.
                    #
                    # This used to be conditional, and every branch of the condition was a
                    # way for a keyword rule to become the grounding.  688159337 arrived
                    # with the understanding asking for Learning and not Template, the
                    # conjunction short-circuited, and the pipeline's own "a person has to
                    # check this" safety draft went on as ``rule.answer``; the provider then
                    # refused all six retrieved candidates because the answer it had been
                    # handed said to confirm first.  325584049 lost an approved answer the
                    # same way.
                    gpt_rule_context = _neutral_gpt_context(
                        base_rule_result,
                        template_failure=str(template_failure),
                        category=phase9_analysis.inquiry_type.value,
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
                        # Keep the reason machine-readable as well as in
                        # the message: a provider limit is a "retry
                        # shortly" for the operator, a validation failure
                        # is not, and the UI can only say so if the cause
                        # survives the raise.
                        raise AnswerGenerationError(
                            "GPT 답변이 안전 검증을 통과하지 못했습니다: "
                            + fallback_reason,
                            reason_code=fallback_reason,
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
                # The GenerationSkippedError clause was removed with the gate that
                # raised it: nothing in the pipeline now decides, before the model is
                # called, that no answer it could write would be publishable.
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
                        # False when the gate stopped ahead of the
                        # provider. Reporting True there would tell an
                        # operator an answer had been composed and
                        # rejected, and would put a phantom call in the
                        # cost telemetry.
                        "gpt_called": not generation_skipped,
                        "generation_skipped": generation_skipped,
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
            # B5: the Product Knowledge DB is a second way to satisfy the same
            # requirement the PRODUCT_DB route already satisfies -- a fact
            # about *this* product that a person verified. It is never a
            # shortcut: the service returns a fact only when it is VERIFIED,
            # not CONFLICT/NEEDS_REVIEW, ACTIVE, non-empty, backed by ACTIVE
            # VERIFIED provenance, and attached to this product_id. A product
            # that merely exists in the DB proves nothing, so the topic the
            # customer asked about has to be among the verified fields.
            # Same lookup the provider was given -- reused, never repeated, so
            # the gate can only ever judge the facts the model actually saw.
            product_knowledge = request.metadata.get("product_knowledge")
            if not isinstance(product_knowledge, ProductKnowledgeResult):
                product_knowledge = self.product_knowledge.facts_for_inquiry(
                    product_id=request.metadata.get("product_id"),
                    question=request.question,
                    model_code=product_fact_guard.model_code,
                    product_name=request.product_name,
                    option_name=request.metadata.get("option_name"),
                )
            # A verified fact only settles the product-fact requirement when
            # the whole evidence chain actually happened: the fact was safe,
            # it reached the provider prompt, and the validator cleared the
            # answer against that same fact. Any link missing and the existing
            # PRODUCT_FACT_NOT_VERIFIED hold stays exactly as it was.
            hybrid_metadata = (
                result.metadata.get("hybrid")
                if isinstance(result.metadata.get("hybrid"), dict) else {}
            )
            prompt_included = bool(
                hybrid_metadata.get("product_catalog_in_prompt")
            )
            validation_metadata = (
                hybrid_metadata.get("validation")
                if isinstance(hybrid_metadata.get("validation"), dict) else {}
            )
            validator_cleared = bool(
                validation_metadata.get("passed") is True
                and not validation_metadata.get("errors")
            )
            knowledge_verified = bool(
                product_fact_guard.sensitive
                and product_knowledge.matched
                and product_knowledge.has_safe_facts
                and product_knowledge.supports_question(request.question)
                and prompt_included
                and validator_cleared
            )
            # A third way to satisfy the same requirement, for the many
            # products whose specification the Product DB has not catalogued
            # yet. The DB having no row for a field says nothing about the
            # product -- treating that silence as "unverified forever" threw
            # away the answers staff wrote and a person approved, which for
            # those fields is the best evidence the system has.
            #
            # It is not a softer test, only a different one: the answer must
            # be approved, resolved to this exact product, actually supporting
            # this question, unhedged, and contradicted by nothing -- and the
            # validator must still have cleared the answer that was written
            # from it. See ``learning_evidence_policy``.
            gpt_understanding_usable = (
                self._usable_gpt_understanding(request) is not None
            )
            learning_evidence = (
                hybrid_metadata.get("approved_learning_evidence")
                if isinstance(
                    hybrid_metadata.get("approved_learning_evidence"), dict
                )
                else {}
            )
            learning_verified = bool(
                product_fact_guard.sensitive
                and learning_evidence.get("usable")
                and not learning_evidence.get("conflict")
                and validator_cleared
            )
            # A fourth way, and on the GPT path the one that means what the
            # others were reaching for. ``knowledge_verified`` asks
            # ``supports_question`` -- a keyword read of the customer's wording
            # -- whether the catalogue covers the claim. GPT ② was given the
            # catalogue rows and reports which ones it actually used, which is
            # the same question answered by the party that read both. Still
            # requires the validator to have cleared the finished answer, so
            # this widens what counts as verified, never what counts as safe.
            gpt_draft_metadata = (
                hybrid_metadata.get("draft")
                if isinstance(hybrid_metadata.get("draft"), dict)
                else {}
            )
            gpt_fact_verified = bool(
                product_fact_guard.sensitive
                and gpt_understanding_usable
                and gpt_draft_metadata.get("used_product_facts")
                and prompt_included
                and validator_cleared
            )
            # The same reading for Learning. ``learning_verified`` above asks
            # for an approved row resolved to this exact product id -- the
            # Product Fact identity standard -- so a same-model answer from a
            # sibling listing could never settle the hold however directly it
            # answered. GPT ② read the rows and named the ones it used; the
            # only checks CODE keeps are that each id was actually delivered
            # and that no verified Product Fact contradicts the Learning.
            delivered_learning = {
                str(item)
                for item in (
                    (hybrid_metadata.get("retrieval") or {}).get("learning") or {}
                ).get("selected_learning_ids") or ()
            }
            gpt_used_learning = {
                str(item)
                for item in gpt_draft_metadata.get("used_learning_ids") or ()
            }
            gpt_learning_verified = bool(
                product_fact_guard.sensitive
                and gpt_understanding_usable
                and delivered_learning & gpt_used_learning
                and not learning_evidence.get("conflict")
                and validator_cleared
            )
            # Three ways a sensitive product claim can be verified, and the
            # fourth is gone. PRODUCT_DB used to be a route that answered the
            # customer directly from the catalogue, so being on it was itself
            # proof that a verified fact had been used. Nothing produces that
            # route now -- the catalogue reaches the model as evidence instead
            # -- so the term could never be true and is removed rather than
            # left to read as a live path.
            current_fact_verified = (
                knowledge_verified or learning_verified or gpt_fact_verified
                or gpt_learning_verified
            )
            guard_metadata = {
                **product_fact_guard.to_dict(),
                "current_fact_verified": current_fact_verified,
                "current_fact_source": (
                    "PRODUCT_CATALOG_JSON" if knowledge_verified
                    else "APPROVED_LEARNING" if learning_verified
                    else "GPT_SELECTED_PRODUCT_FACT" if gpt_fact_verified
                    else "GPT_SELECTED_LEARNING" if gpt_learning_verified
                    else None
                ),
                "approved_learning_evidence": dict(learning_evidence),
                # Read by the eligibility gate. On the GPT-composed path the
                # keyword ``sensitive`` flag no longer holds GPT ②'s answer;
                # what still holds it is a VERIFIED Product Fact contradicting
                # the Learning (data conflict), or GPT ②'s own unresolved.
                "auto_post_allowed": (
                    not product_fact_guard.sensitive
                    or current_fact_verified
                    or (
                        gpt_understanding_usable
                        and hybrid_metadata.get("answer_pipeline")
                        == "GPT_UNDERSTAND_RETRIEVE_ANSWER"
                        and not learning_evidence.get("conflict")
                    )
                ),
                "enforced_by": (
                    "GPT_CONTRACT_AND_GROUNDING"
                    if gpt_understanding_usable
                    else "KEYWORD_PRODUCT_FACT_GUARD"
                ),
                "product_knowledge": product_knowledge.to_dict(),
                "product_catalog_in_prompt": prompt_included,
                "product_catalog_validator_cleared": validator_cleared,
                "product_fact_claims_supported": (
                    product_knowledge.supports_question(request.question)
                ),
            }
            result.metadata["product_fact_guard"] = guard_metadata
            if product_knowledge.has_safe_facts:
                # Carried so the validator can ground a product claim against
                # the same facts, and so staff can see what was relied on.
                result.metadata["product_catalog"] = [
                    item.to_dict() for item in product_knowledge.safe_facts
                ]
                self.logs.record_inquiry(
                    inquiry_id,
                    "PRODUCT_CATALOG_APPLIED",
                    "검증된 상품 Fact를 답변 근거로 사용했습니다.",
                    details={
                        "product_id": product_knowledge.product_id,
                        "listing_id": product_knowledge.listing_id,
                        "fields": sorted(product_knowledge.safe_field_keys()),
                        "safe_count": len(product_knowledge.safe_facts),
                        "excluded_count": len(product_knowledge.excluded_facts),
                        "topics": list(product_knowledge.topics),
                    },
                )
            if (
                product_fact_guard.sensitive
                and not current_fact_verified
                and not gpt_understanding_usable
            ):
                # The draft may still be useful to staff, but no Product DB
                # miss/GPT route may assert a past model's fact automatically.
                #
                # Scoped to the legacy path. ``classify_product_fact`` decides
                # from the customer's wording whether this is a specification
                # question, and then holds the answer for a verified fact --
                # which is the same judgement twice over on the GPT path, made
                # once by a keyword table and once by GPT ①'s ``need_product``
                # and GPT ②'s ``used_product_facts``/``unresolved``. Two
                # readers disagreeing meant the keyword one won.
                #
                # Nothing mechanical is lost. An answer that states a figure or
                # a feature the catalogue does not carry is still caught by
                # ``ungrounded_claims`` and ``ungrounded_feature_claims``
                # against that same catalogue, and an item GPT ② could not
                # settle is still held by GPT_REPORTED_UNRESOLVED.
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
            result.answer = self._complete_atomic_answer(
                inquiry_id, request, result, phase9_analysis
            )
            result.answer = format_final_answer(result.answer)
            if not is_valid_draft(result.answer):
                raise AnswerGenerationError(
                    "답변 생성 결과가 비어 있어 초안을 저장할 수 없습니다."
                )
            # Deterministic coverage gate: record whether the final answer
            # addresses what was asked.  It is placed after the final rendering
            # boundary so every route is measured the same way; clear missing
            # core topics are converted to review before persistence.
            # this key, so a FAIL cannot alter the validator verdict,
            # requires_review, eligibility, auto-post or the approval state.
            # The measurement is never allowed to break generation either: an
            # evaluator fault is recorded and the answer proceeds.
            self._record_atomic_questions(inquiry_id, phase9_analysis)
            self._record_semantic_coverage(inquiry_id, request, result)
            # Persisted on the draft, not only on the in-memory request, so the
            # publishing gate can see whether GPT ① actually read this inquiry.
            # Automatic publication now requires that it did, and a gate that
            # cannot tell would have to guess -- which is how a keyword-only run
            # would quietly keep auto-posting if the semantic stage were off.
            routing = request.metadata.get("semantic_routing")
            if isinstance(routing, dict):
                result.metadata["semantic_routing"] = {
                    key: value
                    for key, value in routing.items()
                    if key != "semantic"
                }
            self._record_pipeline_trace(request, result)
            # Persist the actual eligibility verdict with the draft.  The
            # Dashboard must display this production decision, never rebuild
            # semantic meaning from the inquiry while an operator is viewing
            # it.  Auto-post records its later execution result separately.
            trace_draft = {
                "original_answer": result.answer,
                "validation_status": str(
                    (result.metadata.get("validator_result") or {}).get("status")
                    if isinstance(result.metadata.get("validator_result"), dict)
                    else result.metadata.get("validation_status") or ""
                ),
                "validator_result_json": result.metadata.get("validator_result") or {},
                "review_status": "PENDING",
                "posted": False,
                "metadata_json": result.metadata,
            }
            # This first verdict is persisted for the operator and the later
            # worker.  It must not turn an already-generated draft into a
            # failed generation merely because diagnostic eligibility code is
            # temporarily unavailable: the worker will re-check the actual
            # hard-safety/workflow prerequisites before execution.  Record a
            # fail-closed workflow trace instead, so neither Dashboard nor the
            # worker has to invent a semantic explanation for the failure.
            try:
                trace_verdict = self.eligibility.evaluate(
                    inquiry=inquiry,
                    draft=trace_draft,
                    route=str(result.metadata.get("selected_answer_route") or ""),
                )
            except Exception as error:  # pragma: no cover - defensive boundary
                LOGGER.exception(
                    "자동등록 적격성 trace 기록 실패: inquiry_id=%s", inquiry_id
                )
                trace_verdict = AutoProcessingEligibility(
                    decision="BLOCKED",
                    stage="WORKFLOW",
                    reasons=("WORKFLOW_FAILURE",),
                )
                result.metadata["eligibility_trace_error"] = (
                    error.__class__.__name__
                )
            result.metadata["production_decision_trace"] = {
                "eligibility": trace_verdict.decision,
                "blocking_reason_codes": list(trace_verdict.reasons),
                "soft_reason_codes": list(trace_verdict.soft_reasons),
                "auto_post": "NOT_ATTEMPTED",
            }
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
            self._notify_active_draft_safely(
                inquiry_id=inquiry_id,
                inquiry=inquiry,
                draft=draft,
                result=result,
                plan=plan,
            )
            return AnswerGenerationOutcome(result=result, draft=draft)
        except Exception as error:
            # A policy block unwinds through the same path as a genuine
            # failure, so it used to be written to the workflow, the activity
            # log and the application log as a system error three more times
            # over. The block itself is unchanged -- it still raises, still
            # leaves no draft -- but it is reported as a decision.
            policy_blocked = isinstance(error, AutoAnswerProhibitedError)
            if step_started:
                try:
                    if policy_blocked:
                        self.workflows.skip_step(
                            inquiry_id,
                            StepCode.ANSWER_GENERATED,
                            metadata={
                                "policy_blocked": True,
                                "policy_reason": error.policy_reason
                                or "AUTO_ANSWER_PROHIBITED",
                            },
                        )
                    else:
                        self.workflows.fail_step(
                            inquiry_id,
                            StepCode.ANSWER_GENERATED,
                            _error_code(error),
                            _user_error_message(error),
                        )
                    # NEEDS_ATTENTION is what puts an inquiry in front of a
                    # person: the staff queue selects on
                    # workflow_status IN ('REVIEW_PENDING','NEEDS_ATTENTION').
                    # A blocked high-risk inquiry has no draft, so this is the
                    # only thing keeping it visible -- it must be set for the
                    # policy path too, not just for failures.
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
                if policy_blocked:
                    self.logs.record_inquiry(
                        inquiry_id,
                        "PROCESSING_PLAN_POLICY_BLOCKED",
                        "정책상 자동 답변이 금지되어 처리계획을 중단했습니다.",
                        level="WARNING",
                        details={
                            "policy_blocked": True,
                            "policy_reason": error.policy_reason
                            or "AUTO_ANSWER_PROHIBITED",
                            "safe_error_code": _error_code(error),
                            "correlation_id": correlation_id,
                        },
                    )
                else:
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
            if policy_blocked:
                LOGGER.info(
                    "정책상 자동 답변 금지: inquiry_id=%s policy_reason=%s",
                    inquiry_id,
                    error.policy_reason,
                )
            else:
                LOGGER.exception(
                    "답변 생성 실패: inquiry_id=%s error_type=%s",
                    inquiry_id,
                    error.__class__.__name__,
                )
            if isinstance(error, AnswerEngineError):
                raise
            raise AnswerGenerationError(_user_error_message(error)) from error
