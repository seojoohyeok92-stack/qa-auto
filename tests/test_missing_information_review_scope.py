"""Naming an unknown is not the same as answering unsafely.

Operational inquiry 686504818 (internal 2727), 삼성 50인치 비즈니스TV:

    "부모님댁에 스탠드형50인치 주문해드렸는데 갑자기 벽걸이로 하고싶으시대요ㅜ
     혹시 브라켓 따로 구매해서 벽걸이로 설치 가능한 제품인가요?"

GPT answered it -- "벽걸이 설치가 가능하지만, 별도 구매한 브라켓의 호환 여부는
브라켓 규격과 설치 환경에 따라 달라질 수 있습니다 ... 설치 기사님을 통해 확인해
주세요" -- grounded on two APPROVED learnings with an EXACT_MODEL match plus the
verified product facts. The validator passed all 17 rules with no error and no
review signal, including QUESTION_ANSWER_ALIGNMENT and
SUBQUESTION_EVIDENCE_COVERAGE.

It was still held for staff review. The provider had listed two items under
``missing_information`` -- "구매 예정인 브라켓의 모델 및 상세 규격" and "설치할
벽면 환경" -- and every missing item is REQUIRED_FOR_SAFE_ANSWER unless one
narrow escape applies: the customer must have asked for a *summary*. That
escape encodes the shape of one earlier inquiry, not a safety property, so an
answer naming the bracket the customer has yet to buy and the wall in their
parents' home -- two things no database of ours can ever hold, and which the
answer explicitly deferred to the installer -- was indistinguishable from an
answer that had guessed at a product spec.

The distinction these tests pin is whether the draft *answered* the question:

    2655 "USB-C 65W 충전 가능한가요?"  -> answered=False, NO_RELIABLE_SOURCE
         answer: "...확인하기 어렵습니다"            -> review, correctly
    2692 "HDMI 단자가 몇 개 있나요?"    -> answered=False, NO_RELIABLE_SOURCE
         answer: "...추가 확인이 필요합니다"          -> review, correctly
    2727 "브라켓 사서 벽걸이 되나요?"    -> answered=True,  ANSWERABLE
         answer: "가능하지만 ... 확인해 주세요"       -> auto-post eligible
"""
from __future__ import annotations

from typing import Any

import pytest

from answer.hybrid_models import IntentResult
from services.draft_generation_service import DraftGenerationService


def intent(
    *questions: str, category: str = "COMPOUND_MULTI_INTENT"
) -> IntentResult:
    return IntentResult(
        category=category,
        questions=tuple(questions),
        urgency="NORMAL",
        emotion="NORMAL",
        confidence=0.94,
        requires_review=False,
        reason="test",
    )


def classify(
    *,
    answer: str,
    missing: list[str],
    subquestions: list[dict[str, Any]],
    questions: tuple[str, ...],
    warnings: list[str] | None = None,
    provider_requires_review: bool = False,
) -> dict[str, Any]:
    raw = {
        "answer": answer,
        "confidence": 0.86,
        "missing_information": list(missing),
        "subquestion_results": list(subquestions),
        "warnings": list(warnings or []),
        "requires_review": provider_requires_review,
    }
    return DraftGenerationService._classify_missing_information(
        raw, intent(*questions)
    )


def answered(text: str) -> dict[str, Any]:
    return {"subquestion": text, "answered": True, "status": "ANSWERABLE"}


def unanswered(text: str) -> dict[str, Any]:
    return {
        "subquestion": text, "answered": False, "status": "NO_RELIABLE_SOURCE",
    }


# The operational drafts, reproduced from the persisted provider output.

Q2727_A = "부모님댁에 스탠드형50인치 주문해드렸는데 갑자기 벽걸이로 하고싶으시대요ㅜ"
Q2727_B = "혹시 브라켓 따로 구매해서 벽걸이로 설치 가능한 제품인가요"
A2727 = (
    "해당 제품은 벽걸이 설치가 가능하지만, 별도 구매한 브라켓의 호환 여부는 "
    "브라켓 규격과 설치 환경에 따라 달라질 수 있습니다. 구매 전에 사용할 브라켓의 "
    "상세 규격이 제품과 맞는지 설치 기사님을 통해 확인해 주세요."
)
M2727 = ["구매 예정인 브라켓의 모델 및 상세 규격", "설치할 벽면 환경"]


def classify_2727(**kwargs: Any) -> dict[str, Any]:
    base: dict[str, Any] = dict(
        answer=A2727,
        missing=M2727,
        subquestions=[answered(Q2727_A), answered(Q2727_B)],
        questions=(Q2727_A, Q2727_B),
        warnings=[
            "별도 구매 브라켓의 정확한 호환 여부는 규격과 설치 환경 확인이 필요합니다."
        ],
    )
    base.update(kwargs)
    return classify(**base)


# ==========================================================================
# CASE 1 / CASE 2 -- an answered question whose remaining detail is deferred
# ==========================================================================


def test_case1_operational_686504818_is_not_held_for_review() -> None:
    result = classify_2727()

    assert result["required_missing_information"] == []
    assert result["optional_missing_information"] == M2727
    assert result["requires_review"] is False


def test_case1_severity_is_recorded_per_item() -> None:
    """The reason survives as data, not just as a boolean."""

    details = classify_2727()["missing_information_details"]

    assert [d["text"] for d in details] == M2727
    assert {d["severity"] for d in details} == {"OPTIONAL_DETAIL"}


@pytest.mark.parametrize(
    ("question", "answer", "missing"),
    [
        (
            "스탠드형으로 주문했는데 벽걸이로 사용하려고 합니다. 별도 브라켓을 구매하면 벽걸이 설치 가능한가요?",
            "해당 제품은 벽걸이 설치가 가능합니다. 다만 별도 구매하시는 브라켓의 호환 여부는 "
            "규격에 따라 다를 수 있으니 설치 기사님을 통해 확인해 주세요.",
            ["구매 예정 브라켓의 상세 규격"],
        ),
        (
            "벽걸이 가능한가요? 브라켓도 별도로 구매해야 하나요?",
            "벽걸이 설치가 가능하며 브라켓은 별도 구매가 필요합니다. 사용하실 브라켓 규격은 "
            "제품 설명서를 확인해 주세요.",
            ["고객님이 사용하실 브라켓 종류"],
        ),
    ],
)
def test_case2_compound_information_questions_are_auto_postable(
    question: str, answer: str, missing: list[str]
) -> None:
    result = classify(
        answer=answer,
        missing=missing,
        subquestions=[answered(question)],
        questions=(question,),
    )

    assert result["required_missing_information"] == []
    assert result["requires_review"] is False


# ==========================================================================
# CASE 6 -- a question the draft could not answer still goes to a person
# ==========================================================================


@pytest.mark.parametrize(
    ("case", "question", "answer", "missing"),
    [
        (
            "2655",
            "이 제품은 USB-C로 65W 충전이 가능한가요?",
            "현재 제공된 상품 정보만으로는 USB-C 65W 충전 지원 여부를 확인하기 어렵습니다. "
            "정확한 모델의 상세 사양 확인이 필요합니다.",
            ["정확한 모델 코드", "USB-C 포트의 65W 충전 지원 사양"],
        ),
        (
            "2692",
            "이 제품 HDMI 단자가 몇 개 있나요?",
            "해당 제품의 정확한 HDMI 단자 수는 추가 확인이 필요합니다.",
            ["HDMI 단자 수"],
        ),
        (
            "2702",
            "옆방에서 공부하는 아이에게 방해 안되게 이어폰 기능을 쓰고 싶은데 가능한 방법이 있을지요?",
            "정확한 모델 코드가 확인되지 않아 이어폰 기능 지원 여부를 안내드리기 어렵습니다. "
            "제품 설명서를 확인해 주세요.",
            ["TV 뒷면 라벨에 기재된 정확한 모델 코드"],
        ),
    ],
)
def test_case6_unanswered_subquestions_still_require_review(
    case: str, question: str, answer: str, missing: list[str]
) -> None:
    """The draft said it could not answer. That is a real finding."""

    result = classify(
        answer=answer,
        missing=missing,
        subquestions=[unanswered(question)],
        questions=(question,),
    )

    assert result["required_missing_information"] == missing, case
    assert result["requires_review"] is True, case


def test_a_partly_answered_compound_question_still_requires_review() -> None:
    """One unanswered sub-question is enough to hold the whole answer."""

    result = classify(
        answer="벽걸이 설치는 가능합니다. HDMI 단자 수는 확인이 필요합니다. 설명서를 확인해 주세요.",
        missing=["HDMI 단자 수"],
        subquestions=[
            answered("벽걸이 설치 가능한가요"),
            unanswered("HDMI 단자가 몇 개인가요"),
        ],
        questions=("벽걸이 설치 가능한가요", "HDMI 단자가 몇 개인가요"),
    )

    assert result["required_missing_information"] == ["HDMI 단자 수"]
    assert result["requires_review"] is True


# ==========================================================================
# The safety conditions on the new path
# ==========================================================================


def test_an_answer_that_does_not_defer_still_requires_review() -> None:
    """Answering every sub-question is not on its own enough.

    The answer must visibly hand the unknown detail off. One that simply
    stops talking about it has asserted by omission.
    """

    result = classify(
        answer="해당 제품은 벽걸이 설치가 가능합니다.",
        missing=["구매 예정인 브라켓의 상세 규격"],
        subquestions=[answered("브라켓 사서 벽걸이 되나요")],
        questions=("브라켓 사서 벽걸이 되나요",),
    )

    assert result["required_missing_information"] == [
        "구매 예정인 브라켓의 상세 규격"
    ]
    assert result["requires_review"] is True


def test_provider_asking_for_review_is_never_overridden() -> None:
    result = classify_2727(provider_requires_review=True)

    assert result["requires_review"] is True


@pytest.mark.parametrize(
    "missing_item",
    [
        "정확한 배송 예정일",
        "주문 취소 가능 여부",
        "환불 처리 기간",
        "반품 배송비 금액",
        "설치일 확정 여부",
        "고객 개인정보 확인",
        "A/S 보증 기간",
        "추가 설치 비용",
    ],
)
def test_operational_unknowns_are_never_downgraded(missing_item: str) -> None:
    """Deferring an operational fact to the installer is not an answer.

    These are commitments about an order, a date, or money. A person decides
    them; a safely-worded sentence does not make them safe to publish.
    """

    result = classify(
        answer="문의하신 내용은 설치 기사님을 통해 확인해 주세요. 자세한 내용은 설명서를 참고해 주세요.",
        missing=[missing_item],
        subquestions=[answered("문의")],
        questions=("문의",),
    )

    assert result["required_missing_information"] == [missing_item]
    assert result["requires_review"] is True


def test_missing_subquestion_results_fails_closed() -> None:
    """No evidence that the question was answered means no downgrade."""

    result = classify(
        answer="벽걸이 설치가 가능합니다. 설치 기사님을 통해 확인해 주세요.",
        missing=["구매 예정 브라켓 규격"],
        subquestions=[],
        questions=("브라켓 사서 벽걸이 되나요",),
    )

    assert result["required_missing_information"] == ["구매 예정 브라켓 규격"]
    assert result["requires_review"] is True


def test_no_missing_information_is_left_alone() -> None:
    result = classify(
        answer="벽걸이 설치가 가능합니다.",
        missing=[],
        subquestions=[answered("벽걸이 되나요")],
        questions=("벽걸이 되나요",),
    )

    assert result["required_missing_information"] == []
    assert result["optional_missing_information"] == []
    assert result.get("requires_review") is False


def test_existing_summary_request_path_is_unchanged() -> None:
    """The original escape hatch keeps working on its own terms."""

    question = "조립 방법 간단하게 알려주세요"
    result = classify(
        answer="스탠드를 본체에 맞춘 뒤 나사로 고정하시면 됩니다. 단계별 상세 절차는 "
               "제품 설명서를 확인해 주세요.",
        missing=["단계별 상세 조립 절차"],
        subquestions=[unanswered(question)],
        questions=(question,),
    )

    assert result["required_missing_information"] == []
    assert result["requires_review"] is False


# ==========================================================================
# CASE 3 / 4 / 5 -- a request for somebody to *do* something keeps its hold.
#
# These are decided upstream of the missing-information severity, by the
# classifier, and this fix does not touch that path. The tests are here so
# that a later widening of the severity rule cannot quietly reach them.
# ==========================================================================

from answer.inquiry_analysis import AnswerStrategy  # noqa: E402
from answer.source_adapter import answer_request_from_inquiry  # noqa: E402
from services.inquiry_analysis_service import (  # noqa: E402
    InquiryAnalysisService,
)

PRODUCT = "삼성 125.7cm(50인치) UHD 4K 1등급 비즈니스TV LH50BEFHLGFXKR 스탠드형"
ORDER_NUMBER = "2026082198559811"


def analyse(question: str, *, order_id: str = ORDER_NUMBER):
    return InquiryAnalysisService().analyze(
        answer_request_from_inquiry(
            {
                "id": 1,
                "source_type": "CUSTOMER_INQUIRY",
                "inquiry_type": "CUSTOMER_INQUIRY",
                "source_question_id": "review-scope",
                "external_inquiry_id": "review-scope",
                "title": "문의",
                "content": question,
                "product_name": PRODUCT,
                "order_id": order_id,
                "product_order_id": "",
                "raw_json": {},
                "source_answered": 0,
                "post_status": "NOT_POSTED",
            }
        )
    )


@pytest.mark.parametrize(
    ("case", "question"),
    [
        ("CASE 3", "스탠드형으로 주문했는데 벽걸이형으로 변경해주세요."),
        ("CASE 4", "배송일을 다음 주 금요일로 변경해주세요."),
        ("CASE 4b", "설치 날짜를 좀 앞당겨 주세요."),
        ("CASE 5", "주문 취소해주세요."),
        ("CASE 5b", "환불 처리 부탁드립니다."),
        ("CASE 5c", "반품 신청 부탁드립니다."),
        ("CASE 5d", "교환 요청드립니다."),
    ],
)
def test_operational_action_requests_keep_their_hold(
    case: str, question: str
) -> None:
    analysis = analyse(question)

    assert analysis.manual_review_required is True, case
    assert analysis.answer_strategy is AnswerStrategy.MANUAL_REVIEW, case


@pytest.mark.parametrize(
    ("case", "question"),
    [
        ("CASE 2", "벽걸이 가능한가요? 브라켓도 별도로 구매해야 하나요?"),
        ("CASE 8", "이 제품 벽걸이 설치 가능한가요?"),
        ("CASE 8b", "스탠드 분리 후 다시 장착할 수 있나요?"),
    ],
)
def test_information_questions_are_not_marked_for_review_by_the_classifier(
    case: str, question: str
) -> None:
    """The other half of the line: asking is not asking us to act."""

    analysis = analyse(question, order_id="")

    assert analysis.manual_review_required is False, case
    assert analysis.answer_strategy is not AnswerStrategy.MANUAL_REVIEW, case


def test_compound_multi_intent_is_not_itself_a_review_reason() -> None:
    """A compound question is a shape, not a risk.

    What matters is whether one of the parts asks us to act. Two information
    questions in one message stay two information questions.
    """

    informational = analyse(
        "벽걸이 가능한가요? 브라켓도 별도로 구매해야 하나요?", order_id=""
    )
    with_action = analyse(
        "벽걸이로 변경해주세요. 그리고 배송일도 바꿔주세요.", order_id=""
    )

    assert informational.manual_review_required is False
    assert with_action.manual_review_required is True
