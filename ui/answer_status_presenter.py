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
