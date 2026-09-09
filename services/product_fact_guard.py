from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


# Common policy/procedure questions must keep their existing cross-product
# Learning reuse.  These terms take precedence over the product-fact terms
# below unless the question also explicitly asks for a model-specific value.
COMMON_POLICY_TERMS = (
    "거래명세서", "영수증", "세금계산서", "상담", "문의 접수", "접수 방법",
    "반품 절차", "교환 절차", "배송 절차", "회사 정책",
)

PRODUCT_FACT_TERMS = (
    "화면 크기", "인치", "해상도", "주사율", "hdmi", "usb", "포트", "단자",
    "vesa", "베사", "스탠드", "구성품", "크기", "사이즈", "무게", "중량",
    "벽걸이", "호환", "지원", "기능", "색상", "옵션", "설치 조건", "설치조건",
    "액세서리", "악세서리", "부품", "모델", "규격", "스펙", "사양",
)

FACT_QUERY_MARKERS = (
    "몇", "얼마", "어떤", "무엇", "있나요", "되나요", "가능", "포함", "지원",
    "호환", "규격", "사양", "스펙",
)

MODEL_CODE_PATTERN = re.compile(
    r"\b(?=[A-Z0-9-]{5,}\b)(?=[A-Z0-9-]*[A-Z])(?=[A-Z0-9-]*\d)[A-Z0-9-]+\b",
    re.IGNORECASE,
)

# A measurement written without a space -- 125CM, 107.9CM, 50INCH, 180HZ --
# satisfies MODEL_CODE_PATTERN exactly as a real model code does.
# ``learning_compatibility_service`` had already had to learn this and kept its
# own copy; the rule lives here now, beside the pattern it corrects, and that
# module imports it. One rule, one place.
DIMENSION_TOKEN = re.compile(
    r"^\d+(?:[.,]\d+)?(?:CM|MM|M|INCH|IN|KG|G|W|HZ|K|MS)$",
    re.IGNORECASE,
)


def is_dimension_token(value: object) -> bool:
    return bool(DIMENSION_TOKEN.match(str(value or "").strip()))


@dataclass(frozen=True)
class ProductFactGuardDecision:
    sensitive: bool
    reason: str
    product_id: str | None
    model_code: str | None
    product_name: str | None
    option_name: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sensitive": self.sensitive,
            "classification": (
                "PRODUCT_FACT_SENSITIVE" if self.sensitive else "COMMON_OR_NON_PRODUCT_FACT"
            ),
            "reason": self.reason,
            "product_identity": {
                "product_id": self.product_id,
                "model_code": self.model_code,
                "product_name": self.product_name,
                "option_name": self.option_name,
            },
        }


def extract_model_code(product_name: object) -> str | None:
    """The model code in a listing title, or None if it states no model.

    A measurement is not a model. "삼성 삼탠바이미 50인치(125cm) 4K UHD 무빙
    스마트 비즈니스TV 이동식 거치대" names no model at all, and this returned
    ``125CM`` from the parenthesised size. That phantom then travelled into
    ``ProductKnowledgeService`` as ``expected_model``, where every one of the
    listing's own 133 verified facts was rejected for belonging to a different
    model -- the product's specification excluded on the strength of its width.

    ``learning_compatibility_service`` already had to learn this and carries
    ``DIMENSION_TOKEN`` for it; this reuses that rule rather than writing a
    second, differently-wrong copy of it.
    """

    matches = [
        value
        for value in MODEL_CODE_PATTERN.findall(str(product_name or "").upper())
        if not is_dimension_token(value)
    ]
    return max(matches, key=len) if matches else None


def classify_product_fact(
    question: object,
    *,
    inquiry_type: object = "",
    inquiry_subtype: object = "",
    product_id: object = None,
    product_name: object = None,
    option_name: object = None,
) -> ProductFactGuardDecision:
    text = " ".join(str(question or "").lower().split())
    subtype = str(inquiry_subtype or "").upper()
    kind = str(inquiry_type or "").upper()
    common_policy = any(term in text for term in COMMON_POLICY_TERMS)
    fact_terms = [term for term in PRODUCT_FACT_TERMS if term in text]
    fact_query = any(marker in text for marker in FACT_QUERY_MARKERS)
    classified_product_fact = subtype == "PRODUCT_SPEC_OR_FEATURE"

    # A policy question may mention a product but does not become model-fact
    # sensitive merely because the product is present in the inquiry.
    sensitive = bool(
        not common_policy
        and fact_terms
        and (fact_query or classified_product_fact or kind == "PRODUCT_GENERAL")
    )
    reason = (
        "COMMON_POLICY_OR_PROCEDURE"
        if common_policy
        else "EXISTING_PRODUCT_SPEC_SUBTYPE"
        if sensitive and classified_product_fact
        else "PRODUCT_FACT_TERMS"
        if sensitive
        else "NO_PRODUCT_FACT_SIGNAL"
    )
    clean_name = str(product_name or "").strip() or None
    return ProductFactGuardDecision(
        sensitive=sensitive,
        reason=reason,
        product_id=str(product_id or "").strip() or None,
        model_code=extract_model_code(clean_name),
        product_name=clean_name,
        option_name=str(option_name or "").strip() or None,
    )


def same_stable_product(
    *,
    current_product_id: object,
    candidate_product_id: object,
    current_model_code: object = None,
    candidate_model_code: object = None,
) -> bool:
    current_id = str(current_product_id or "").strip()
    candidate_id = str(candidate_product_id or "").strip()
    if current_id and candidate_id:
        return current_id == candidate_id
    current_model = str(current_model_code or "").strip().upper()
    candidate_model = str(candidate_model_code or "").strip().upper()
    return bool(current_model and candidate_model and current_model == candidate_model)
