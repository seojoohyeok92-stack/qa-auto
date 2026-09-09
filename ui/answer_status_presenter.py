"""Turn the stored answer state into the five things an operator must know.

The dashboard used to answer "직원 검토: 필요" from ``approval_status`` alone,
"자동 답변: 가능" from ``validation.passed`` (which is also True for
REVIEW_REQUIRED), and the strategy caption from the InquiryAnalysis taken
*before* the answer existed. None of them consulted the auto-registration gate,
so a perfectly safe answer that was simply awaiting approval looked identical
to one held back for a safety finding, and an answer already published on Naver
looked like a failure.

This module reads that state and reports, separately:

  * 답변 검증  -- the validator's own verdict, PASS / REVIEW_REQUIRED / BLOCK
  * 직원 검토  -- whether a person actually has to act
  * 자동등록   -- what the Auto Post gate decides, in the gate's own terms
  * 등록 상태  -- Naver's answer vs. this program's own posting
  * 사유       -- the gate's reasons, in words an operator can act on

It computes nothing itself: the verdict comes from the stored validator result
and the decision from :class:`AutoProcessingEligibilityService`, called exactly
as the pipeline calls it. It is a pure read -- no database write, no posting,
no provider call -- so what the screen says and what the pipeline would do
cannot drift apart.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from answer.hold_reasons import REASON_LABELS as _REASON_LABELS
from answer.hold_reasons import describe_reason as _describe_reason
from services.auto_post_pipeline_service import AutoPostPipelineService
from services.auto_processing_eligibility_service import (
    AutoProcessingEligibility,
    AutoProcessingEligibilityService,
)


# The reason vocabulary is shared with the KakaoTalk notifier so the
# dashboard and the message an operator actually reads give the same
# sentence for the same code. Re-exported here under the names this
# module has always published.
REASON_LABELS = _REASON_LABELS
describe_reason = _describe_reason


_VALIDATION_LABELS = {
    "PASS": "PASS · 통과",
    "REVIEW_REQUIRED": "REVIEW_REQUIRED · 직원 확인",
    "BLOCK": "BLOCK · 차단",
}

_APPROVAL_LABELS = {
    "PENDING": "대기",
    "APPROVED": "승인 완료",
    "POSTED": "등록 완료",
}

_POST_LABELS = {
    "POSTED": "등록 완료",
    "POSTING": "등록 중",
    "POST_FAILED": "등록 실패",
    "NOT_POSTED": "미등록",
}

# Registration outcomes, in the order the operator cares about.
ELIGIBLE = "ELIGIBLE"
HELD = "HELD"
BLOCKED = "BLOCKED"
ALREADY_ANSWERED = "ALREADY_ANSWERED"
UNKNOWN = "UNKNOWN"

_REGISTRATION_LABELS = {
    ELIGIBLE: "가능",
    HELD: "보류 · 직원 확인 필요",
    BLOCKED: "차단",
    ALREADY_ANSWERED: "이미 답변됨 · 중복등록 방지",
    UNKNOWN: "초안 없음",
}


def pipeline_route(draft: dict[str, Any] | None) -> str:
    """The route the Auto Post pipeline would derive for this draft.

    Delegated rather than reimplemented: the route decides which gate reasons
    apply, so a second copy of that derivation could drift and make the screen
    describe a decision the pipeline never made.
    """

    if not draft:
        return ""
    return AutoPostPipelineService._route(draft)


@dataclass(frozen=True)
class AnswerStatusView:
    """What the detail panel should say, and why."""

    validation_status: str
    validation_label: str
    staff_review_required: bool
    staff_review_label: str
    approval_label: str
    registration: str
    registration_label: str
    naver_answer_label: str
    program_post_label: str
    blocking_reasons: tuple[tuple[str, str], ...] = ()
    soft_reasons: tuple[tuple[str, str], ...] = ()
    advisory: tuple[str, ...] = ()
    review_signals: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    # Product Knowledge summary for this draft, display only. The gate decides
    # elsewhere; this exists so staff can see which product facts were verified
    # and which were withheld, and therefore why a spec question is on hold.
    product_fact_label: str = ""
    product_facts: tuple[tuple[str, str], ...] = ()
    product_fact_exclusions: tuple[tuple[str, str], ...] = ()

    @property
    def warning_count(self) -> int:
        """Everything staff should read, advisory notes included."""

        return len(self.advisory) + len(self.review_signals)


@dataclass(frozen=True)
class DecisionTraceView:
    """Read-only operator view of the persisted GPT-first decision path.

    This deliberately consumes generation/eligibility/post records only.  It
    never invokes an analyzer or retrieval service, so opening the Dashboard
    cannot create a second semantic decision path.
    """

    gpt1: str
    source: str
    retrieval: str
    gpt2: str
    hard_safety: str
    auto_post: str
    root_stage: str
    root_cause: str
    root_message: str


_TRACE_MESSAGES = {
    "NONE": "최초 실패 원인이 기록되지 않았습니다.",
    "TRACE_NOT_RECORDED": "과거 문의에는 처리 진단 기록이 없습니다.",
    "SOURCE_MISSING": "답변에 필요한 신뢰 가능한 근거가 없습니다.",
    "RETRIEVAL_MISS": "관련 근거가 존재하지만 GPT 검토 후보에 전달되지 않았습니다.",
    "EVIDENCE_UNRESOLVED": "GPT가 제공된 근거만으로 질문을 해결할 수 없다고 판단했습니다.",
    "EVIDENCE_CONFLICT": "제공된 근거 사이에 해결되지 않은 충돌이 있습니다.",
    "HARD_SAFETY_BLOCK": "자동등록 안전조건을 충족하지 못했습니다.",
    "WORKFLOW_FAILURE": "주문·배송 등 필수 처리 상태를 충족하지 못했습니다.",
    "EXECUTION_FAILURE": "자동등록 실행 또는 Naver 등록 과정에서 실패했습니다.",
}


def build_decision_trace(
    *, inquiry: dict[str, Any], draft: dict[str, Any] | None,
    eligibility: AutoProcessingEligibility | None = None,
    route: str = "",
) -> DecisionTraceView:
    """Project stored pipeline facts into a closed, operator-facing trace.

    The precedence is causal: source/retrieval/evidence before later holds,
    and an actual post failure after a successful decision is execution.  A
    missing historical trace is shown as such instead of guessed from legacy
    classifier fields.
    """

    inquiry = _mapping(inquiry)
    if not draft:
        return DecisionTraceView(
            "NOT_RECORDED", "NOT_RECORDED", "NOT_RECORDED", "NOT_RECORDED",
            "NOT_RECORDED", "NOT_ATTEMPTED", "", "TRACE_NOT_RECORDED",
            _TRACE_MESSAGES["TRACE_NOT_RECORDED"],
        )
    metadata = _mapping(draft.get("metadata_json"))
    persisted_decision = _mapping(metadata.get("production_decision_trace"))
    hybrid = _mapping(metadata.get("hybrid"))
    pipeline = str(hybrid.get("answer_pipeline") or "")
    routing = _mapping(metadata.get("semantic_routing"))
    understanding = _mapping(routing.get("understanding"))
    gpt_draft = _mapping(hybrid.get("draft"))
    evidence = hybrid.get("subquestion_evidence")
    evidence_rows = [item for item in evidence or [] if isinstance(item, dict)]
    statuses = {str(item.get("status") or "").upper() for item in evidence_rows}
    unresolved = list(gpt_draft.get("unresolved") or [])
    retrieval = _mapping(hybrid.get("retrieval")).get("learning")
    retrieval = _mapping(retrieval)
    source = "UNKNOWN"
    if evidence_rows:
        if statuses <= {"NO_RELIABLE_SOURCE"}:
            source = "NONE"
        elif "NO_RELIABLE_SOURCE" in statuses:
            source = "PARTIAL"
        else:
            source = "SUFFICIENT"
    delivered = bool(
        gpt_draft.get("used_learning_ids") or gpt_draft.get("used_product_facts")
        or retrieval.get("final_candidate_count") or retrieval.get("candidate_count")
    )
    retrieval_state = "DELIVERED" if delivered else "NOT_RECORDED"
    if source == "SUFFICIENT" and not delivered:
        retrieval_state = "MISSING"
    gpt2 = "UNRESOLVED" if unresolved else ("RESOLVED" if gpt_draft else "NOT_RECORDED")
    if eligibility is None and not persisted_decision:
        eligibility = AutoProcessingEligibilityService().evaluate(
            inquiry=inquiry, draft=draft, route=route
        )
    post_status = str(inquiry.get("post_status") or "").upper()
    persisted_reasons = set(persisted_decision.get("blocking_reason_codes") or ())
    eligibility_reasons = set(eligibility.reasons) if eligibility is not None else persisted_reasons
    safe = eligibility.safe if eligibility is not None else (
        str(persisted_decision.get("eligibility") or "").upper() == "SAFE"
    )
    auto_post = (
        "SUCCESS" if post_status == "POSTED" or inquiry.get("source_answered")
        else "FAILED" if post_status == "POST_FAILED"
        else str(persisted_decision.get("auto_post") or "")
        if persisted_decision.get("auto_post") in {"SUCCESS", "BLOCKED", "FAILED"}
        else "BLOCKED" if not safe else "NOT_ATTEMPTED"
    )
    hard_reasons = eligibility_reasons - {
        "GPT_REPORTED_UNRESOLVED", "GPT_WITHHELD_AUTO_POST",
        # A successfully posted inquiry evaluates as idempotently blocked on
        # a later Dashboard read.  That is not the cause of the original
        # decision and must not turn a success trace into a safety failure.
        "ALREADY_ANSWERED_OR_POSTED",
    }
    hard_safety = "BLOCKED" if hard_reasons else "PASS"
    root_stage, root_cause = "", "NONE"
    if auto_post == "FAILED":
        root_stage, root_cause = "EXECUTION", "EXECUTION_FAILURE"
    elif pipeline == "GPT_PIPELINE_UNAVAILABLE":
        root_stage, root_cause = "WORKFLOW", "WORKFLOW_FAILURE"
    elif source == "NONE":
        root_stage, root_cause = "SOURCE", "SOURCE_MISSING"
    elif retrieval_state == "MISSING":
        root_stage, root_cause = "RETRIEVAL", "RETRIEVAL_MISS"
    elif "CONFLICT" in statuses:
        root_stage, root_cause = "GPT_EVIDENCE", "EVIDENCE_CONFLICT"
    elif gpt2 == "UNRESOLVED":
        root_stage, root_cause = "GPT_EVIDENCE", "EVIDENCE_UNRESOLVED"
    elif hard_reasons:
        workflow = any(
            "ORDER" in reason or "DPS" in reason or "ROUTE" in reason
            for reason in hard_reasons
        )
        root_stage, root_cause = (
            ("WORKFLOW", "WORKFLOW_FAILURE") if workflow
            else ("HARD_SAFETY", "HARD_SAFETY_BLOCK")
        )
    return DecisionTraceView(
        "PASS" if understanding.get("usable") is True else "NOT_RECORDED",
        source, retrieval_state, gpt2, hard_safety, auto_post,
        root_stage, root_cause, _TRACE_MESSAGES[root_cause],
    )


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _validator_status(draft: dict[str, Any]) -> str:
    """The validator's own verdict for this draft.

    ``validation.passed`` is True for REVIEW_REQUIRED as well as PASS, so the
    status is read directly and only derived from ``passed`` when a draft
    predates the status field.
    """

    validator = _mapping(draft.get("validator_result_json"))
    status = str(validator.get("status") or "").upper()
    if status:
        return status
    hybrid = _mapping(_mapping(draft.get("metadata_json")).get("hybrid"))
    status = str(_mapping(hybrid.get("validation")).get("status") or "").upper()
    if status:
        return status
    column = str(draft.get("validation_status") or "").upper()
    if column.startswith("FAIL"):
        return "BLOCK"
    if "REVIEW" in column:
        return "REVIEW_REQUIRED"
    return column


def _findings(draft: dict[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Split what staff should read into advisory, review signals and errors.

    ``warnings`` carries both advisory notes and review signals, so the
    signals are subtracted rather than shown twice -- an advisory note and a
    finding that holds the answer back must not look the same.
    """

    validator = _mapping(draft.get("validator_result_json"))
    hybrid = _mapping(_mapping(draft.get("metadata_json")).get("hybrid"))
    validation = _mapping(hybrid.get("validation"))

    def _seq(*sources: Any) -> tuple[str, ...]:
        seen: list[str] = []
        for source in sources:
            for item in source or []:
                text = str(item).strip()
                if text and text not in seen:
                    seen.append(text)
        return tuple(seen)

    signals = _seq(
        validator.get("review_signals"), validation.get("review_signals")
    )
    warnings = _seq(
        validator.get("warnings"),
        validation.get("warnings"),
        _mapping(hybrid.get("draft")).get("warnings"),
        _mapping(hybrid.get("facts")).get("warnings"),
    )
    advisory = tuple(item for item in warnings if item not in set(signals))
    errors = _seq(validator.get("errors"), validation.get("errors"))
    return advisory, signals, errors


def _registration(eligibility: AutoProcessingEligibility | None) -> str:
    if eligibility is None:
        return UNKNOWN
    if eligibility.safe:
        return ELIGIBLE
    if "ALREADY_ANSWERED_OR_POSTED" in eligibility.reasons:
        return ALREADY_ANSWERED
    if eligibility.decision == "BLOCKED":
        return BLOCKED
    return HELD


def build_answer_status(
    *,
    inquiry: dict[str, Any],
    draft: dict[str, Any] | None,
    route: str,
    eligibility: AutoProcessingEligibility | None = None,
    service: AutoProcessingEligibilityService | None = None,
) -> AnswerStatusView:
    """Read the stored state and say what the operator needs to know.

    ``eligibility`` may be supplied by a caller that already computed it;
    otherwise the same service the pipeline uses is called with the same
    arguments, so the screen can never disagree with the gate.
    """

    inquiry = _mapping(inquiry)
    if not draft:
        answered = bool(inquiry.get("source_answered"))
        return AnswerStatusView(
            validation_status="",
            validation_label="초안 없음",
            staff_review_required=False,
            staff_review_label="초안 없음",
            approval_label=_APPROVAL_LABELS.get(
                str(inquiry.get("approval_status") or "").upper(), "대기"
            ),
            registration=UNKNOWN,
            registration_label=_REGISTRATION_LABELS[UNKNOWN],
            naver_answer_label="답변 완료" if answered else "미답변",
            program_post_label=_POST_LABELS.get(
                str(inquiry.get("post_status") or "").upper(), "미등록"
            ),
        )

    if eligibility is None:
        evaluator = service or AutoProcessingEligibilityService()
        eligibility = evaluator.evaluate(
            inquiry=inquiry, draft=draft, route=route
        )

    status = _validator_status(draft)
    advisory, signals, errors = _findings(draft)
    registration = _registration(eligibility)
    # "A person has to act" is the gate's decision, not the approval queue:
    # an answer merely awaiting approval needs no judgement, and one already
    # answered on Naver needs none either.
    staff_review = registration in {HELD, BLOCKED}

    return AnswerStatusView(
        validation_status=status,
        validation_label=_VALIDATION_LABELS.get(status, status or "확인 불가"),
        staff_review_required=staff_review,
        staff_review_label="필요" if staff_review else "불필요",
        approval_label=_APPROVAL_LABELS.get(
            str(inquiry.get("approval_status") or "").upper(), "대기"
        ),
        registration=registration,
        registration_label=_REGISTRATION_LABELS[registration],
        naver_answer_label=(
            "답변 완료" if inquiry.get("source_answered") else "미답변"
        ),
        program_post_label=_POST_LABELS.get(
            str(inquiry.get("post_status") or "").upper(), "미등록"
        ),
        blocking_reasons=tuple(
            (code, describe_reason(code)) for code in eligibility.reasons
        ),
        soft_reasons=tuple(
            (code, describe_reason(code)) for code in eligibility.soft_reasons
        ),
        advisory=advisory,
        review_signals=signals,
        errors=errors,
        **_product_fact_view(draft),
    )


def _product_fact_view(draft: dict[str, Any]) -> dict[str, Any]:
    """Read back what the Product Knowledge service already decided.

    Purely a read: the safe/unsafe verdict was made by
    ``ProductKnowledgeService`` at generation time and is replayed here, so
    the screen can never disagree with the pipeline about which facts counted.
    """

    guard = _mapping(_mapping(draft.get("metadata_json")).get(
        "product_fact_guard"
    ))
    knowledge = _mapping(guard.get("product_knowledge"))
    if not knowledge:
        return {}
    safe = [item for item in knowledge.get("safe_facts") or [] if isinstance(item, dict)]
    excluded = [
        item for item in knowledge.get("excluded_facts") or []
        if isinstance(item, dict)
    ]
    if not safe and not excluded:
        label = ""
        if knowledge.get("unavailable_reason"):
            label = _PRODUCT_FACT_UNAVAILABLE.get(
                str(knowledge["unavailable_reason"]), ""
            )
        return {"product_fact_label": label} if label else {}
    label = f"VERIFIED · {len(safe)}건"
    if excluded:
        label += f" (제외 {len(excluded)}건)"
    return {
        "product_fact_label": label,
        "product_facts": tuple(
            (
                str(item.get("field_key") or ""),
                "{}{}".format(
                    item.get("value"),
                    f" {item['unit']}" if item.get("unit") else "",
                ),
            )
            for item in safe
        ),
        "product_fact_exclusions": tuple(
            (
                str(item.get("field_key") or ""),
                _PRODUCT_FACT_EXCLUSIONS.get(
                    str(item.get("exclusion_reason") or ""),
                    str(item.get("exclusion_reason") or ""),
                ),
            )
            for item in excluded
        ),
    }


_PRODUCT_FACT_UNAVAILABLE = {
    "PRODUCT_NOT_IN_PRODUCT_DB": "상품DB에 등록되지 않은 상품입니다.",
    "PRODUCT_FACTS_DB_UNAVAILABLE": "상품DB를 사용할 수 없습니다.",
    "NO_PRODUCT_ID": "상품 식별자가 없어 상품DB를 조회하지 못했습니다.",
}
_PRODUCT_FACT_EXCLUSIONS = {
    "VERIFICATION_NEEDS_REVIEW": "상품DB 검증 대기 중이라 근거로 쓰지 않았습니다.",
    "RESOLUTION_CONFLICT": "상품DB 출처 간 값이 충돌해 근거로 쓰지 않았습니다.",
    "RESOLUTION_NEEDS_REVIEW": "상품DB 값 확정 전이라 근거로 쓰지 않았습니다.",
    "VALUE_EMPTY_OR_UNKNOWN": "값이 확인되지 않았습니다(미지원이라는 뜻이 아닙니다).",
    "NO_ACTIVE_PROVENANCE": "출처 기록이 없어 근거로 쓰지 않았습니다.",
    "PROVENANCE_NOT_VERIFIED": "출처가 아직 검증되지 않았습니다.",
    "MODEL_SCOPE_MISMATCH": "다른 모델의 값이라 근거로 쓰지 않았습니다.",
    "SUPERSEDED_BY_LATER_RUN": "최신 수집본으로 대체된 값입니다.",
    "VOLATILE_LISTING_FACT": "가격·재고처럼 자주 바뀌는 값이라 제외했습니다.",
    "NO_SELECTED_VALUE": "확정된 값이 없습니다.",
}
