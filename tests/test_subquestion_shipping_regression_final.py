"""Regression coverage for prose wrapping and A/S-versus-shipping routing."""
from __future__ import annotations

import pytest

from answer.answer_validator import AnswerValidator
from answer.engine import AnswerEngine
from answer.models import AnswerRequest, AnswerResult, AnswerStatus
from answer.text_utils import split_subquestions
from services.answer_service import _template_unavailable_reason
from services.inquiry_analysis_service import InquiryAnalysisService


PARCEL_PRODUCT = (
    "삼성 삼탠바이미 32인치 M5 스마트 모니터 IPTV + 2in1 이동식 거치대"
)
SHIPPING_ANSWER = (
    "택배배송 상품은 오후 3시 이전 결제 주문에 한해 당일 발송되며, "
    "배송은 보통 1~2영업일 정도 소요됩니다. "
    "도서산간 지역은 1일 정도 추가 소요될 수 있습니다."
)


def analyse(question: str):
    return InquiryAnalysisService().analyze(
        AnswerRequest(
            inquiry_id=1,
            question_id="regression",
            inquiry_type="PRODUCT_INQUIRY",
            question=question,
            product_name=PARCEL_PRODUCT,
        )
    )


@pytest.mark.parametrize(
    "question",
    [
        (
            "구매 후 삼성 페스티벌 신청했는데\n"
            "구매처명을 잘못 입력했다고 보완 요청이 왔습니다.\n"
            "어떤 이름으로 입력해야 하나요?"
        ),
        (
            "3개월 정도 사용했는데\n"
            "영상이 1~2초 나오다가 멈춥니다.\n"
            "AS 받을 수 있나요?"
        ),
        (
            "제품을 사용하고 있는데\n"
            "갑자기 화면이 나오지 않습니다.\n"
            "어떻게 해야 하나요?"
        ),
    ],
)
def test_wrapped_prose_stays_one_question(question: str) -> None:
    assert len(split_subquestions(question)) == 1


def test_two_explicit_questions_stay_separate() -> None:
    assert len(
        split_subquestions("배송은 언제 되나요?\n벽걸이 설치도 가능한가요?")
    ) == 2


def test_numbered_questions_stay_separate() -> None:
    question = (
        "1. 배송은 언제 되나요?\n"
        "2. 폐가전 수거 가능한가요?\n"
        "3. 벽걸이 설치 가능한가요?"
    )
    assert len(split_subquestions(question)) == 3


def test_context_is_attached_without_merging_two_questions() -> None:
    question = (
        "TV 설치 예정입니다.\n"
        "배송은 언제 되나요?\n"
        "기존 TV도 수거해주시나요?"
    )
    parts = split_subquestions(question)
    assert len(parts) == 2
    assert "TV 설치 예정입니다" in parts[0]
    assert "배송은 언제 되나요" in parts[0]
    assert "기존 TV도 수거" in parts[1]

    analysis = analyse(question)
    assert len(analysis.subquestion_analyses) == 2
    assert [item["question"] for item in analysis.subquestion_analyses] == list(parts)


def test_unpunctuated_requests_are_not_merged_into_the_last_question() -> None:
    parts = split_subquestions(
        "배송일을 문의합니다\n폐가전 수거를 요청합니다\n벽걸이 설치도 가능한가요?"
    )
    assert len(parts) == 3


@pytest.mark.parametrize(
    "question",
    [
        (
            "3개월정도썼는데 인터넷연결은 되는데\n"
            "처음 틀면 화면에서 영상이 1-2초나왔다가\n"
            "계속 멈추고 안나와요 ..\n"
            "이거 as 받을수있나요"
        ),
        "고장시 A/S기간 언제까지 인가요? 삼성 서비스센터로 전화하면 되나요?",
        "As무상수리기간이얼마나되나요",
        "삼성 서비스센터에서 무상점검 받을수있는 제품인가요?",
    ],
)
def test_confirmed_as_cases_never_select_shipping(question: str) -> None:
    result = AnswerEngine().answer(PARCEL_PRODUCT, question, "")
    analysis = analyse(question)

    assert result.match_kind != "FIXED_POLICY_SHIPPING"
    assert analysis.detected_intent not in {
        "DELIVERY_DATE",
        "PRE_PURCHASE_DELIVERY",
    }
    assert analysis.inquiry_subtype != "PRE_PURCHASE_DELIVERY_GUIDANCE"


@pytest.mark.parametrize(
    "question",
    [
        "언제 받을 수 있나요?",
        "배송 언제 되나요?",
        "상품을 이번 주에 받을 수 있나요?",
        "배송기간이 얼마나 걸리나요?",
        "택배 배송은 며칠 걸리나요?",
        "TV가 고장이라 새 제품은 언제 받을 수 있나요?",
    ],
)
def test_real_shipping_questions_keep_the_shipping_rule(question: str) -> None:
    result = AnswerEngine().answer(PARCEL_PRODUCT, question, "")
    assert result.match_kind == "FIXED_POLICY_SHIPPING"


def test_as_question_rejects_shipping_only_template() -> None:
    result = AnswerValidator().validate_route(
        SHIPPING_ANSWER,
        route="TEMPLATE",
        question="A/S 받을 수 있나요?",
    )
    assert result.passed is False
    assert result.status == "FAILED_INVALID_CONTENT"
    assert any(
        rule.code == "TEMPLATE_SEMANTIC_ALIGNMENT" and rule.status == "BLOCK"
        for rule in result.rules
    )


def test_as_answer_and_shipping_pair_each_remain_valid() -> None:
    validator = AnswerValidator()
    as_result = validator.validate_route(
        "제품 고장은 삼성전자 서비스센터를 통해 A/S 접수해 주세요.",
        route="TEMPLATE",
        question="A/S 받을 수 있나요?",
    )
    shipping_result = validator.validate_route(
        SHIPPING_ANSWER,
        route="TEMPLATE",
        question="배송은 언제 되나요?",
    )
    assert as_result.passed is True
    assert shipping_result.passed is True


def test_template_selection_cannot_bypass_semantic_alignment() -> None:
    request = AnswerRequest(
        inquiry_id=1,
        question="A/S 받을 수 있나요?",
        product_name=PARCEL_PRODUCT,
        inquiry_type="PRODUCT_INQUIRY",
    )
    wrong = AnswerResult(
        status=AnswerStatus.GENERATED,
        category="배송/택배",
        reason="택배배송 기본 안내입니다.",
        answer=SHIPPING_ANSWER,
        provider="rules",
        auto_answerable=True,
        needs_review=False,
        metadata={"template_match_kind": "FIXED_POLICY_SHIPPING"},
    )
    assert (
        _template_unavailable_reason(wrong, request, AnswerValidator())
        == "VALIDATION_FAILED"
    )
