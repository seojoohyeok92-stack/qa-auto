"""One vocabulary for "why was this not posted", shared by every surface.

The gate reason codes are produced in exactly one place
(``AutoProcessingEligibilityService``).  Their operator-facing sentences used
to live in the Streamlit presenter, which meant the KakaoTalk notification --
the only place most operators actually read -- had no access to them and fell
back to the answer-generation ``reason`` string.  That string describes how the
draft was written, not why it was held, so a held inquiry reported things like
"GPT 답변 생성" while the real cause (직원 확인 필요) was never shown.

Moved here so the presenter and the notifier say the same sentence for the same
code.  Nothing is invented: the labels are the ones the dashboard already used.
"""
from __future__ import annotations

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
    "INTERNAL_PLACEHOLDER_EXPOSURE": (
        "내부 마스킹 표시가 답변에 남아 있어 자동 등록을 차단했습니다."
    ),
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
    # Evidence
    "EVIDENCE_CONFLICT": (
        "확보된 근거가 서로 일치하지 않아 자동으로 확정할 수 없습니다."
    ),
    # Order / DPS
    "REQUIRED_ORDER_ID_MISSING_OR_INVALID": "필요한 주문번호를 확인하지 못했습니다.",
    "ORDER_LOOKUP_NOT_TRUSTED": "주문 조회 결과를 신뢰할 수 없습니다.",
    "DPS_RESULT_NOT_TRUSTED": "DPS 조회 결과를 신뢰할 수 없습니다.",
    "DPS_SNAPSHOT_NOT_VALIDATED": "DPS 설치 일정 스냅샷이 검증되지 않았습니다.",
    "DELIVERY_DEADLINE_NOT_CONFIRMABLE": "고객이 지정한 날짜까지 배송·설치가 가능한지 확정할 근거가 없습니다.",
    "SEMANTIC_ACTION_MISMATCH": "고객이 요청한 내용과 답변이 다루는 내용이 서로 다릅니다.",
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


def primary_reason(
    hard_reasons: object, soft_reasons: object = ()
) -> str:
    """The one sentence that best answers "why was this not posted".

    Hard reasons come first and soft ones are never allowed to stand in for
    them: "신뢰도가 낮음" beside "직원 확인 필요" describes the wrong problem,
    and it is the sentence an operator acts on.  A soft reason is used only
    when there is no hard reason at all -- which is to say, when nothing
    actually blocked the answer.
    """

    for code in _codes(hard_reasons):
        return describe_reason(code)
    for code in _codes(soft_reasons):
        return describe_reason(code)
    return ""


def _codes(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


# ---------------------------------------------------------------------------
# Staff-facing short labels for the KakaoTalk message.
#
# The sentences above are written for the dashboard, where there is room to
# explain. A phone notification is read in a few seconds by someone deciding
# whether to open the console, so the same reason needs a much shorter form --
# a noun phrase naming the problem, not a sentence describing it.
#
# This is a display vocabulary only. The codes themselves, the gate that
# produces them, and everything written to the database and the logs are
# untouched: the raw codes are still recorded, and only the rendered message
# differs.
STAFF_REASON_LABELS: dict[str, str] = {
    # Idempotency
    "ALREADY_ANSWERED_OR_POSTED": "이미 등록됨",
    # Privacy / transport integrity
    "PII_EXPOSURE": "개인정보 포함",
    "SECRET_EXPOSURE": "인증정보 포함",
    "FINAL_ANSWER_REQUIRED": "답변 본문 없음",
    "UNRESOLVED_PLACEHOLDER": "미완성 문구 포함",
    "INTERNAL_PLACEHOLDER_EXPOSURE": "내부 문구 포함",
    "PAYLOAD_FINAL_ANSWER_MISMATCH": "등록 데이터 불일치",
    "UNSUPPORTED_SOURCE_TYPE": "지원하지 않는 문의 유형",
    # Validator
    "VALIDATOR_NOT_PASS": "검증 실패",
    "VALIDATOR_REVIEW_REQUIRED": "검증 확인 필요",
    # Route / policy
    "INTENT_NOT_AUTO_POSTABLE": "자동등록 대상 아님",
    "ANSWER_REQUIRES_MANUAL_REVIEW": "직원 확인 필요",
    "PROCESSING_PLAN_REQUIRES_REVIEW": "직원 검토 필요",
    "POLICY_OR_HIGH_RISK_REVIEW": "위험·분쟁 가능성",
    "DRAFT_REVIEW_REQUIRED": "초안 검토 필요",
    "PRODUCT_FACT_NOT_VERIFIED": "상품 정보 근거 부족",
    "PRODUCT_COMPATIBILITY_NOT_VERIFIED": "호환 여부 미확인",
    # Evidence
    "EVIDENCE_CONFLICT": "근거 충돌",
    "APPROVED_LEARNING_CONFLICT": "학습 답변 간 충돌",
    "PRODUCT_FACT_VS_LEARNING_CONFLICT": "상품 정보와 학습 답변 충돌",
    # Order / DPS
    "REQUIRED_ORDER_ID_MISSING_OR_INVALID": "주문번호 필요",
    "ORDER_LOOKUP_NOT_TRUSTED": "주문 조회 불가",
    "DPS_RESULT_NOT_TRUSTED": "배송·설치 조회 불가",
    "DPS_SNAPSHOT_NOT_VALIDATED": "설치 일정 미확정",
    "DELIVERY_DEADLINE_NOT_CONFIRMABLE": "지정 날짜 확정 불가",
    "SEMANTIC_ACTION_MISMATCH": "요청과 답변 불일치",
    # Recorded but not blocking
    "INTENT_UNCLASSIFIED_VALIDATOR_CLEAR": "문의 유형 불명확",
    "INTENT_CONFIDENCE_LOW": "분류 신뢰도 낮음",
    "INTENT_CONFIDENCE_UNKNOWN": "분류 신뢰도 확인 불가",
    "GPT_CONFIDENCE_LOW": "답변 신뢰도 낮음",
    "GPT_CONFIDENCE_UNKNOWN": "답변 신뢰도 확인 불가",
}

# Codes that describe how processing went, not why the answer was not posted.
# Listing them under "미등록 사유" invites the reader to act on something that
# is not a problem: PRELIMINARY_REVIEW_RESOLVED literally reports a hold that
# was *lifted*, and ORDER_ID_REQUESTED_FROM_CUSTOMER describes what the answer
# says rather than what stopped it. Hidden from the message only -- both are
# still produced, still recorded, and still shown on the dashboard.
STAFF_HIDDEN_REASONS = frozenset({
    "PRELIMINARY_REVIEW_RESOLVED",
    "ORDER_ID_REQUESTED_FROM_CUSTOMER",
})

# Shown instead of a raw code nobody outside this codebase can read. A reason
# added later must never reach a staff member as "SOME_NEW_CODE"; it should
# read as "something else needs a look", while the code itself stays in the
# logs for whoever is debugging.
STAFF_UNKNOWN_LABEL = "추가 확인 필요"

_STAFF_SEPARATOR = " · "
# Past this many, the message stops being scannable and the tail adds nothing:
# an operator opening the console sees the full list anyway.
_STAFF_MAX_ITEMS = 5


def staff_reason_labels(codes: object) -> tuple[str, ...]:
    """Short Korean labels for one hold, deduplicated, in order.

    Two codes that mean the same thing to a staff member collapse into one
    entry -- several unreadable route codes, or several unrecognised ones,
    would otherwise fill the message with repetition.
    """

    labels: list[str] = []
    for code in _codes(codes):
        if code in STAFF_HIDDEN_REASONS:
            continue
        label = STAFF_REASON_LABELS.get(code)
        if label is None:
            label = (
                "답변 경로 확인 필요"
                if code.startswith(_ROUTE_PREFIX)
                else STAFF_UNKNOWN_LABEL
            )
        labels.append(label)
    return tuple(dict.fromkeys(labels))


def staff_reason_summary(codes: object) -> str:
    """The one-line "세부 사유" a staff member reads on their phone."""

    labels = staff_reason_labels(codes)
    if not labels:
        return ""
    if len(labels) > _STAFF_MAX_ITEMS:
        shown = labels[:_STAFF_MAX_ITEMS]
        return _STAFF_SEPARATOR.join(shown) + f" 외 {len(labels) - len(shown)}건"
    return _STAFF_SEPARATOR.join(labels)
