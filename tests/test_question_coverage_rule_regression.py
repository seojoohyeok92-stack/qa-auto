from __future__ import annotations

from answer.engine import AnswerEngine
from answer.models import AnswerStatus
from services.semantic_coverage_service import FAIL, PARTIAL, SemanticCoverageService


def test_event_context_does_not_answer_order_number_identification() -> None:
    result = AnswerEngine().answer(
        "Samsung display",
        "삼성 감사페스티벌 신청했는데 SH로 시작하는 주문번호가 뭔가요?",
        "",
    )
    assert not result.answer
    assert result.status is not AnswerStatus.GENERATED
    assert result.match_kind == "FIXED_EVENT_ONNURI"


def test_generic_event_guide_remains_available_for_actual_event_question() -> None:
    result = AnswerEngine().answer(
        "Samsung display", "감사페스티벌 신청 방법 알려주세요.", ""
    )
    assert result.answer
    assert result.status


def test_other_purchased_product_never_uses_current_listing_model() -> None:
    result = AnswerEngine().answer(
        "M5 monitor + FMS stand",
        "제가 2개 구매했는데 다른 제품 모델명이 뭔가요?",
        "",
    )
    assert not result.answer
    assert result.status is not AnswerStatus.GENERATED


def test_partial_compound_answer_is_not_coverage_pass() -> None:
    coverage = SemanticCoverageService().evaluate(
        question=(
            "스탠드와 모니터를 따로 작성해야 하나요?\n"
            "제가 2개 구매했는데 다른 제품 모델명도 알려주세요."
        ),
        answer="스탠드는 FMS 모델로 출고됩니다.",
    )
    assert coverage.status in {FAIL, PARTIAL}
    assert coverage.uncovered >= 1
