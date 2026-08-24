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

from services.auto_post_pipeline_service import AutoPostPipelineService
from services.auto_processing_eligibility_service import (
    AutoProcessingEligibility,
    AutoProcessingEligibilityService,
)


# Every reason the gate can actually produce, in the operator's language.
# Codes not listed here are shown as-is rather than guessed at, so a reason
# added later is visible instead of silently mistranslated.
REASON_LABELS: dict[str, str] = {
    # Idempotency
    "ALREADY_ANSWERED_OR_POSTED": "이미 답변이 등록되어 있어 중복 등록하지 않습니다.",
    # Privacy / transport integrity
    "PII_EXPOSURE": "개인정보가 노출될 수 있어 자동 등록을 차단했습니다.",
    "SECRET_EXPOSURE": "인증정보가 노출될 수 있어 자동 등록을 차단했습니다.",
    "FINAL_ANSWER_REQUIRED": "등록할 답변 본문이 비어 있습니다.",
    "UNRESOLVED_PLACEHOLDER": "답변에 치환되지 않은 자리표시자가 남아 있습니다.",
    "PAYLOAD_FINAL_ANSWER_MISMATCH": "등록 payload와 최종 답변이 일치하지 않습니다.",
    "UNSUPPORTED_SOURCE_TYPE": "지원하지 않는 문의 유형이라 자동 등록하지 않습니다.",
    # Validator
    "VALIDATOR_NOT_PASS": "Validator 안전 검증을 통과하지 못했습니다.",
    "VALIDATOR_REVIEW_REQUIRED": "Validator가 직원 확인을 요청했습니다.",
    # Route / policy
    "INTENT_NOT_AUTO_POSTABLE": "이 답변 경로는 자동 등록 대상이 아닙니다.",
    "ANSWER_REQUIRES_MANUAL_REVIEW": "직원 확인이 필요한 답변입니다.",
    "PRODUCT_FACT_NOT_VERIFIED": "상품 정보 확인이 필요합니다.",
    "PRODUCT_COMPATIBILITY_NOT_VERIFIED": "호환 여부가 검증되지 않았습니다.",
    "PROCESSING_PLAN_REQUIRES_REVIEW": "문의 처리 계획상 직원 확인이 필요합니다.",
    "POLICY_OR_HIGH_RISK_REVIEW": "위험·분쟁 가능성이 있어 직원 판단이 필요합니다.",
    "DRAFT_REVIEW_REQUIRED": "답변 초안이 직원 검토 대상으로 판정되었습니다.",
    # Order / DPS
    "REQUIRED_ORDER_ID_MISSING_OR_INVALID": "필요한 주문번호를 확인하지 못했습니다.",
    "ORDER_LOOKUP_NOT_TRUSTED": "주문 조회 결과를 신뢰할 수 없습니다.",
    "DPS_RESULT_NOT_TRUSTED": "DPS 조회 결과를 신뢰할 수 없습니다.",
    "DPS_SNAPSHOT_NOT_VALIDATED": "DPS 설치 일정 스냅샷이 검증되지 않았습니다.",
    # Recorded but not blocking
    "ORDER_ID_REQUESTED_FROM_CUSTOMER": "고객에게 주문번호를 요청하는 답변입니다.",
    "INTENT_CONFIDENCE_LOW": "문의 분류 신뢰도가 낮게 측정되었습니다.",
    "INTENT_CONFIDENCE_UNKNOWN": "문의 분류 신뢰도를 확인하지 못했습니다.",
    "GPT_CONFIDENCE_LOW": "GPT 자체 신뢰도가 낮게 측정되었습니다.",
    "GPT_CONFIDENCE_UNKNOWN": "GPT 자체 신뢰도를 확인하지 못했습니다.",
    "INTENT_UNCLASSIFIED_VALIDATOR_CLEAR": (
        "문의 유형을 분류하지 못했지만 Validator는 통과했습니다."
    ),
    "PRELIMINARY_REVIEW_RESOLVED": (
        "사전 검토 신호가 현재 분석·근거·Validator 확인으로 해소되었습니다."
    ),
}

# The gate builds this one dynamically from the route, so it cannot be a fixed
# key. Anything else unknown is shown verbatim.
_ROUTE_PREFIX = "ROUTE_"

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


def describe_reason(code: str) -> str:
    """The operator-facing sentence for one gate reason code."""

    text = str(code or "").strip()
    if not text:
        return ""
    known = REASON_LABELS.get(text)
    if known:
        return known
    if text.startswith(_ROUTE_PREFIX):
        route = text[len(_ROUTE_PREFIX):] or "UNKNOWN"
        return f"답변 경로 {route} 은(는) 직원 확인 대상입니다."
    return text


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

    @property
    def warning_count(self) -> int:
        """Everything staff should read, advisory notes included."""

        return len(self.advisory) + len(self.review_signals)


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
    )
