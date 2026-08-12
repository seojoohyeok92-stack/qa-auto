from __future__ import annotations

from enum import Enum


class PositiveReason(str, Enum):
    CONTENT_ACCURATE = "CONTENT_ACCURATE"
    POLICY_WELL_APPLIED = "POLICY_WELL_APPLIED"
    PRODUCT_INFO_CLEAR = "PRODUCT_INFO_CLEAR"
    DELIVERY_INSTALLATION_CLEAR = "DELIVERY_INSTALLATION_CLEAR"
    INTENT_WELL_REFLECTED = "INTENT_WELL_REFLECTED"
    TONE_EXPRESSION_GOOD = "TONE_EXPRESSION_GOOD"
    REUSE_RECOMMENDED = "REUSE_RECOMMENDED"
    OTHER = "OTHER"


POSITIVE_REASON_LABELS: dict[PositiveReason, str] = {
    PositiveReason.CONTENT_ACCURATE: "내용 정확",
    PositiveReason.POLICY_WELL_APPLIED: "정책 반영 우수",
    PositiveReason.PRODUCT_INFO_CLEAR: "상품정보 설명 우수",
    PositiveReason.DELIVERY_INSTALLATION_CLEAR: "배송/설치 안내 우수",
    PositiveReason.INTENT_WELL_REFLECTED: "질문 의도 반영 우수",
    PositiveReason.TONE_EXPRESSION_GOOD: "말투/표현 우수",
    PositiveReason.REUSE_RECOMMENDED: "재사용 추천",
    PositiveReason.OTHER: "기타",
}

POSITIVE_REASON_BY_LABEL = {
    label: reason for reason, label in POSITIVE_REASON_LABELS.items()
}


def normalize_positive_reason(
    value: str | PositiveReason | None,
) -> PositiveReason | None:
    if value is None or not str(value).strip():
        return None
    if isinstance(value, PositiveReason):
        return value
    normalized = str(value).strip()
    if normalized in POSITIVE_REASON_BY_LABEL:
        return POSITIVE_REASON_BY_LABEL[normalized]
    return PositiveReason(normalized.upper())
