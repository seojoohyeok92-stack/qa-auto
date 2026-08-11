from __future__ import annotations

from enum import Enum


class CorrectionReason(str, Enum):
    ROUTING_ERROR = "ROUTING_ERROR"
    FACT_ERROR = "FACT_ERROR"
    DELIVERY_INSTALLATION_ERROR = "DELIVERY_INSTALLATION_ERROR"
    PRODUCT_INFO_ERROR = "PRODUCT_INFO_ERROR"
    INTENT_NOT_REFLECTED = "INTENT_NOT_REFLECTED"
    TONE_EXPRESSION = "TONE_EXPRESSION"
    REDUNDANT_ANSWER = "REDUNDANT_ANSWER"
    OTHER = "OTHER"


CORRECTION_REASON_LABELS: dict[CorrectionReason, str] = {
    CorrectionReason.ROUTING_ERROR: "문의 유형/라우팅 오류",
    CorrectionReason.FACT_ERROR: "사실 오류",
    CorrectionReason.DELIVERY_INSTALLATION_ERROR: "배송/설치일 오류",
    CorrectionReason.PRODUCT_INFO_ERROR: "상품정보 오류",
    CorrectionReason.INTENT_NOT_REFLECTED: "질문 의도 미반영",
    CorrectionReason.TONE_EXPRESSION: "말투/표현 문제",
    CorrectionReason.REDUNDANT_ANSWER: "불필요/중복 답변",
    CorrectionReason.OTHER: "기타",
}

CORRECTION_REASON_BY_LABEL = {
    label: reason for reason, label in CORRECTION_REASON_LABELS.items()
}


class LearningSignalType(str, Enum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    INTENT_CORRECTION = "INTENT_CORRECTION"
    EXCLUDED = "EXCLUDED"


class FeedbackType(str, Enum):
    STAFF_CORRECTION = "STAFF_CORRECTION"
    HISTORICAL_REVIEW = "HISTORICAL_REVIEW"


INTENT_OPTIONS: dict[str, str] = {
    "DELIVERY_INSTALLATION_STATUS": "배송/설치 일정",
    "ORDER_STATUS": "주문 상태",
    "ORDER_INFO_REQUIRED": "주문정보 확인 필요",
    "PRODUCT_GENERAL": "상품 일반 문의",
    "INSTALLATION_GENERAL": "설치 일반 문의",
    "CANCEL_RETURN_EXCHANGE": "취소/반품/교환",
    "INFORMATION_INSUFFICIENT": "정보 부족",
    "MANUAL_REVIEW_REQUIRED": "직원 확인 필요",
}


def normalize_reason(value: str | CorrectionReason) -> CorrectionReason:
    if isinstance(value, CorrectionReason):
        return value
    normalized = str(value or "").strip()
    if normalized in CORRECTION_REASON_BY_LABEL:
        return CORRECTION_REASON_BY_LABEL[normalized]
    return CorrectionReason(normalized.upper())
