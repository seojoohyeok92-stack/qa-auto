"""Regression tests for the upstream causes of the 827/840 review holds.

The ids are intentionally absent from production code.  These fixtures use no
database, provider, DPS, Naver, or auto-post integration.
"""
from __future__ import annotations

import pytest

from answer.hybrid_models import Emotion, IntentResult
from answer.source_adapter import answer_request_from_inquiry
from answer.text_utils import split_subquestions
from services.auto_processing_eligibility_service import (
    AutoProcessingEligibilityService,
)
from services.draft_generation_service import DraftGenerationService
from services.inquiry_analysis_service import InquiryAnalysisService


SERVICE = AutoProcessingEligibilityService()
SAFE_ANSWER = (
    "제품 설명서에 따라 직접 설치할 수 있습니다. "
    "세부 조립 순서와 체결 방법은 제품 설명서를 확인해 주세요."
)


def _inquiry(title: str, body: str, **overrides: object) -> dict:
    value = {
        "id": 1,
        "source_type": "PRODUCT_INQUIRY",
        "inquiry_type": "PRODUCT_INQUIRY",
        "source_question_id": "fixture",
        "external_inquiry_id": "fixture",
        "title": title,
        "content": body,
        "product_name": "테스트 제품",
        "raw_json": {},
        "source_answered": 0,
        "post_status": "NOT_POSTED",
    }
    value.update(overrides)
    return value


def _analysis(inquiry: dict) -> dict:
    request = answer_request_from_inquiry(inquiry)
    return InquiryAnalysisService().analyze(request).to_dict()


def _validator(status: str = "PASS") -> dict:
    passed = status in {"PASS", "PASS_WITH_WARNING"}
    return {
        "passed": passed,
        "status": status,
        "errors": [] if passed else ["VALIDATOR_REJECTED"],
        "review_signals": (
            ["STAFF_REVIEW_REQUIRED"] if status == "REVIEW_REQUIRED" else []
        ),
        "warnings": [],
        "rules": [],
    }


def _draft(
    inquiry: dict,
    *,
    route: str,
    validator_status: str = "PASS",
    stored_analysis: dict | None = None,
    hybrid: dict | None = None,
    review_status: str = "PENDING",
    plan_high_risk: bool = False,
    needs_staff_review: bool = False,
    product_fact_guard: dict | None = None,
    requires_order_lookup: bool = False,
    requires_dps_lookup: bool = False,
) -> dict:
    validator = _validator(validator_status)
    analysis = dict(stored_analysis or _analysis(inquiry))
    metadata = {
        # Reproduce a persisted preliminary signal without inventing a new
        # safety finding.  The current deterministic analysis is recomputed by
        # the production eligibility service.
        "requires_manual_review": bool(
            analysis.get("manual_review_required")
        ),
        "generation_mode": route,
        "selected_answer_route": route,
        "processing_plan": {
            "analysis": analysis,
            "needs_staff_review": needs_staff_review,
            "is_high_risk": plan_high_risk,
            "requires_order_lookup": requires_order_lookup,
            "requires_dps_lookup": requires_dps_lookup,
            "order_id_status": "MISSING" if requires_order_lookup else "NOT_REQUIRED",
            "dps_lookup_status": "NOT_REQUIRED",
        },
        "hybrid": hybrid
        if hybrid is not None
        else {"validation": validator, "provider": "template_validator"},
    }
    if product_fact_guard is not None:
        metadata["product_fact_guard"] = product_fact_guard
    return {
        "id": 1,
        "original_answer": SAFE_ANSWER,
        "validation_status": validator_status,
        "validator_result_json": validator,
        "review_status": review_status,
        "posted": False,
        "metadata_json": metadata,
    }


@pytest.mark.parametrize(
    "title", ["상품 문의", "상품문의", "설치 문의", "배송 문의"]
)
def test_generic_channel_title_is_not_a_subquestion(title: str) -> None:
    body = "이 제품 혼자 설치할 수 있나요?"
    request = answer_request_from_inquiry(_inquiry(title, body))

    assert request.question == body
    assert split_subquestions(request.question) == ("이 제품 혼자 설치할 수 있나요",)


def test_827_generic_title_leaves_only_the_two_customer_questions() -> None:
    body = "이 제품 혼자 설치할 수 있나요? 설치 방법도 간단히 알려주세요."
    inquiry = _inquiry("상품 문의", body)
    request = answer_request_from_inquiry(inquiry)

    assert split_subquestions(request.question) == (
        "이 제품 혼자 설치할 수 있나요",
        "설치 방법도 간단히 알려주세요.",
    )
    analysis = InquiryAnalysisService().analyze(request)
    assert analysis.manual_review_required is False
    assert analysis.auto_answerable is True
    assert analysis.confidence > 0.45


def test_840_generic_title_does_not_inflate_safe_compound_count() -> None:
    """제목이 분해를 부풀리지 않는다는 것이 이 테스트의 요지이고 그대로다.

    다만 두 번째 하위질문 "주문하면 배송은 보통 얼마나 걸리나요"는 구매 전
    고객의 실제 배송 소요 기간 문의(유형 A)라, 확정된 운영정책상 직원 검토로
    간다. 분해 결과와 confidence 는 영향받지 않는다.
    """

    body = "이 제품 혼자 설치 가능한가요? 그리고 주문하면 배송은 보통 얼마나 걸리나요?"
    inquiry = _inquiry("상품 문의", body)
    request = answer_request_from_inquiry(inquiry)

    assert len(split_subquestions(request.question)) == 2
    analysis = InquiryAnalysisService().analyze(request)
    assert analysis.confidence > 0.45
    assert analysis.manual_review_required is True
    assert analysis.requires_order_lookup is False
    assert analysis.requires_dps_lookup is False


def test_meaningful_title_is_preserved() -> None:
    inquiry = _inquiry("벽걸이 설치 가능 여부", "콘크리트 벽입니다.")
    request = answer_request_from_inquiry(inquiry)

    assert request.question == "벽걸이 설치 가능 여부\n콘크리트 벽입니다."
    assert split_subquestions(request.question) == (
        "벽걸이 설치 가능 여부",
        "콘크리트 벽입니다.",
    )


@pytest.mark.parametrize(
    ("title", "body"),
    [
        ("이 제품 혼자 설치할 수 있나요?", "이 제품 혼자 설치할 수 있나요?"),
        (
            "이 제품 혼자 설치할 수 있나요",
            "이 제품 혼자 설치할 수 있나요? 설치 방법도 알려주세요.",
        ),
    ],
)
def test_title_body_duplicates_do_not_create_a_subquestion(
    title: str, body: str
) -> None:
    request = answer_request_from_inquiry(_inquiry(title, body))
    parts = split_subquestions(request.question)

    assert request.question == body
    assert parts.count("이 제품 혼자 설치할 수 있나요") == 1


def _intent(question: str) -> IntentResult:
    return IntentResult(
        category="INSTALLATION_GENERAL",
        questions=(question,),
        emotion=Emotion.NORMAL,
        urgency="NORMAL",
        confidence=0.9,
        requires_review=False,
        reason="fixture",
    )


def _classified_draft(missing: str, *, answer: str = SAFE_ANSWER):
    raw = {
        "answer": answer,
        "confidence": 0.9,
        "used_facts": ["learning:1"],
        "missing_information": [missing],
        "requires_review": True,
        "warnings": [],
    }
    classified = DraftGenerationService._classify_missing_information(
        raw,
        _intent("설치 방법도 간단히 알려주세요."),
    )
    return DraftGenerationService.parse(classified)


def test_optional_manual_detail_does_not_force_review() -> None:
    draft = _classified_draft("부품별 세부 조립 순서와 체결 방법")

    assert draft.missing_information == ("부품별 세부 조립 순서와 체결 방법",)
    assert draft.required_missing_information == ()
    assert draft.optional_missing_information == (
        "부품별 세부 조립 순서와 체결 방법",
    )
    assert draft.missing_information_details == (
        {
            "text": "부품별 세부 조립 순서와 체결 방법",
            "severity": "OPTIONAL_DETAIL",
        },
    )
    assert draft.provider_requires_review is True
    assert draft.requires_review is False
    assert draft.has_required_missing_information is False


@pytest.mark.parametrize(
    "missing",
    ["브라켓 호환 정보", "설치 예정일", "배송 기간", "제품 모델 확인"],
)
def test_customer_impacting_missing_fact_remains_required(missing: str) -> None:
    draft = _classified_draft(missing)

    assert draft.required_missing_information == (missing,)
    assert draft.optional_missing_information == ()
    assert draft.requires_review is True
    assert draft.has_required_missing_information is True


def test_optional_detail_without_safe_deferral_remains_required() -> None:
    draft = _classified_draft(
        "부품별 세부 조립 순서와 체결 방법",
        answer="직접 설치할 수 있습니다.",
    )

    assert draft.required_missing_information
    assert draft.requires_review is True


def _grounded_hybrid_metadata(
    questions: tuple[str, ...], *, required_missing: bool
) -> dict:
    missing = ["부품별 세부 조립 순서와 체결 방법"]
    severity = (
        "REQUIRED_FOR_SAFE_ANSWER" if required_missing else "OPTIONAL_DETAIL"
    )
    validator = _validator()
    return {
        "enabled": True,
        "draft": {
            "requires_review": required_missing,
            "missing_information": missing,
            "required_missing_information": missing if required_missing else [],
            "optional_missing_information": [] if required_missing else missing,
            "missing_information_details": [
                {"text": missing[0], "severity": severity}
            ],
        },
        "self_review": {"requires_review": False},
        "validation": validator,
        "subquestion_evidence": [
            {
                "subquestion": question,
                "status": "ANSWERABLE",
                "evidence_coverage": "SUPPORTED",
            }
            for question in questions
        ],
    }


def test_827_optional_only_missing_resolves_derivative_review_flags() -> None:
    body = "이 제품 혼자 설치할 수 있나요? 설치 방법도 간단히 알려주세요."
    inquiry = _inquiry("상품 문의", body)
    questions = split_subquestions(answer_request_from_inquiry(inquiry).question)
    result = SERVICE.evaluate(
        inquiry=inquiry,
        draft=_draft(
            inquiry,
            route="GPT_DIRECT",
            stored_analysis=_stale_analysis(inquiry),
            hybrid=_grounded_hybrid_metadata(
                questions, required_missing=False
            ),
            review_status="NEEDS_REVIEW",
            needs_staff_review=True,
        ),
        route="GPT_DIRECT",
    )

    assert result.safe is True
    assert result.reasons == ()
    assert "PRELIMINARY_REVIEW_RESOLVED" in result.soft_reasons


def test_827_required_missing_information_remains_review_required() -> None:
    body = "이 제품 혼자 설치할 수 있나요? 설치 방법도 간단히 알려주세요."
    inquiry = _inquiry("상품 문의", body)
    questions = split_subquestions(answer_request_from_inquiry(inquiry).question)
    result = SERVICE.evaluate(
        inquiry=inquiry,
        draft=_draft(
            inquiry,
            route="GPT_DIRECT",
            stored_analysis=_stale_analysis(inquiry),
            hybrid=_grounded_hybrid_metadata(
                questions, required_missing=True
            ),
            review_status="NEEDS_REVIEW",
            needs_staff_review=True,
        ),
        route="GPT_DIRECT",
    )

    assert result.safe is False
    assert "PROCESSING_PLAN_REQUIRES_REVIEW" in result.reasons
    assert "DRAFT_REVIEW_REQUIRED" in result.reasons


def _stale_analysis(inquiry: dict) -> dict:
    analysis = _analysis(inquiry)
    analysis.update(
        {
            "inquiry_type": "PRODUCT_INQUIRY",
            "inquiry_subtype": "COMPOUND_MULTI_INTENT",
            "answer_strategy": "MANUAL_REVIEW",
            "confidence": 0.45,
            "manual_review_required": True,
            "auto_answerable": False,
        }
    )
    return analysis


def test_template_pass_uses_template_contract_not_hybrid_metadata() -> None:
    inquiry = _inquiry(
        "상품 문의",
        "이 제품 혼자 설치 가능한가요? 그리고 주문하면 배송은 보통 얼마나 걸리나요?",
    )
    result = SERVICE.evaluate(
        inquiry=inquiry,
        draft=_draft(
            inquiry,
            route="TEMPLATE",
            stored_analysis=_stale_analysis(inquiry),
        ),
        route="TEMPLATE",
    )

    # 하위질문 하나가 구매 전 배송 소요 기간 문의(유형 A)라 정책상 보류된다.
    # 이 테스트가 지키려던 것 -- Template 경로가 hybrid metadata 가 아니라
    # template contract 를 읽는다는 것 -- 은 그대로 유지된다: 보류 사유가
    # 정책이지 stale metadata 가 아니다.
    assert result.decision == "REVIEW_REQUIRED"
    assert "POLICY_OR_HIGH_RISK_REVIEW" in result.reasons
    assert "PRELIMINARY_REVIEW_RESOLVED" not in result.reasons


@pytest.mark.parametrize("status", ["REVIEW_REQUIRED", "BLOCK"])
def test_template_validator_review_or_block_still_blocks(status: str) -> None:
    inquiry = _inquiry("상품 문의", "이 제품 혼자 설치 가능한가요?")
    result = SERVICE.evaluate(
        inquiry=inquiry,
        draft=_draft(
            inquiry,
            route="TEMPLATE",
            validator_status=status,
            stored_analysis=_stale_analysis(inquiry),
        ),
        route="TEMPLATE",
    )

    assert result.safe is False
    assert (
        "VALIDATOR_REVIEW_REQUIRED" in result.reasons
        if status == "REVIEW_REQUIRED"
        else "VALIDATOR_NOT_PASS" in result.reasons
    )


def test_hybrid_metadata_absence_remains_fail_closed() -> None:
    inquiry = _inquiry("상품 문의", "이 제품 혼자 설치 가능한가요?")
    result = SERVICE.evaluate(
        inquiry=inquiry,
        draft=_draft(
            inquiry,
            route="GPT_DIRECT",
            stored_analysis=_stale_analysis(inquiry),
        ),
        route="GPT_DIRECT",
    )

    assert result.safe is False
    assert "POLICY_OR_HIGH_RISK_REVIEW" in result.reasons
    assert "PRELIMINARY_REVIEW_RESOLVED" not in result.soft_reasons


def test_high_risk_compound_remains_review_required() -> None:
    inquiry = _inquiry(
        "상품 문의",
        "설치 방법을 알려주세요. 배송 중 파손 책임은 누가 지나요?",
    )
    current = _analysis(inquiry)
    assert current["manual_review_required"] is True

    result = SERVICE.evaluate(
        inquiry=inquiry,
        draft=_draft(
            inquiry,
            route="TEMPLATE",
            stored_analysis=current,
            plan_high_risk=True,
            needs_staff_review=True,
        ),
        route="TEMPLATE",
    )
    assert result.safe is False
    assert "POLICY_OR_HIGH_RISK_REVIEW" in result.reasons


def test_product_fact_not_verified_remains_an_independent_hard_reason() -> None:
    inquiry = _inquiry("상품 문의", "이 브라켓과 호환되나요?")
    result = SERVICE.evaluate(
        inquiry=inquiry,
        draft=_draft(
            inquiry,
            route="TEMPLATE",
            product_fact_guard={
                "sensitive": True,
                "current_fact_verified": False,
            },
        ),
        route="TEMPLATE",
    )

    assert result.safe is False
    assert "PRODUCT_FACT_NOT_VERIFIED" in result.reasons


@pytest.mark.parametrize("lookup", ["order", "dps"])
def test_required_order_or_dps_lookup_remains_blocking(lookup: str) -> None:
    inquiry = _inquiry("상품 문의", "제 주문의 설치 예정일을 알려주세요.")
    result = SERVICE.evaluate(
        inquiry=inquiry,
        draft=_draft(
            inquiry,
            route="TEMPLATE",
            requires_order_lookup=lookup == "order",
            requires_dps_lookup=lookup == "dps",
        ),
        route="TEMPLATE",
    )

    assert result.safe is False
    assert any("ORDER" in reason or "DPS" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("고객 전화번호는 010-1234-5678입니다.", "PII_EXPOSURE"),
        ("api_key=sk-secret-value", "SECRET_EXPOSURE"),
    ],
)
def test_privacy_and_secret_guard_remain_blocking(
    answer: str, expected: str
) -> None:
    inquiry = _inquiry("상품 문의", "설치 가능한가요?")
    draft = _draft(inquiry, route="TEMPLATE")
    draft["original_answer"] = answer

    result = SERVICE.evaluate(inquiry=inquiry, draft=draft, route="TEMPLATE")

    assert result.safe is False
    assert expected in result.reasons
