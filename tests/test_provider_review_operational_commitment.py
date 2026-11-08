"""GPT② asked for review and named an order/date/money item it could not confirm.

11331 ("삼성 전용 8% 는 어디서 받는걸까요? 결제가가 90만원이네요") was answered
from two learnings about *other* benefits (a Samsung gift card, an efficiency
refund), GPT② set ``requires_review`` and listed "this product's 8%
eligibility and conditions" among what it could not confirm.
``_apply_reported_resolution`` lowered every item to OPTIONAL because no
sub-question was reported unresolved, so the wrong procedure was auto-posted
and a person corrected it ten minutes later.

The legacy classifier never lowered an operational commitment (order, date,
money, benefit) and never lowered anything when the model asked for review;
the GPT-judged path lost both. Only their conjunction is restored, so a
cautious review preference on its own still publishes.

No database, provider, market, or Kakao call is made here.
"""
from __future__ import annotations

import pytest

from services.auto_processing_eligibility_service import GPT_WITHHELD_AUTO_POST
from services.draft_generation_service import DraftGenerationService
from test_auto_post_gate_server_cases import _draft, _evaluate

ANSWERABLE = [{"subquestion": "질문", "answered": True, "status": "ANSWERABLE"}]


def _resolve(missing, *, provider_review=True, unresolved=(), subquestions=None):
    return DraftGenerationService._apply_reported_resolution({
        "answer": "근거 기반 답변",
        "requires_review": provider_review,
        "can_auto_post": not provider_review,
        "missing_information": list(missing),
        "unresolved": list(unresolved),
        "subquestion_results": ANSWERABLE if subquestions is None else subquestions,
    })


# --------------------------------------------------------------------------
# the incident
# --------------------------------------------------------------------------

MISSING_11331 = [
    "삼성 전용 8% 혜택이 결제 시 즉시 차감되는지 여부",
    "현재 표시된 결제금액 90만원에 혜택이 반영되었는지 여부",
    "현재 상품의 삼성 전용 8% 혜택 대상 여부와 구체적인 지급 조건",
]


def test_11331_a_review_request_about_a_benefit_is_not_lowered() -> None:
    result = _resolve(MISSING_11331)

    assert result["requires_review"] is True
    assert result["can_auto_post"] is False
    assert result["required_missing_information"] == MISSING_11331
    assert result["optional_missing_information"] == []
    assert {item["severity"] for item in result["missing_information_details"]} == {
        "REQUIRED_FOR_SAFE_ANSWER"}
    # The model's own reports are kept as they were for diagnosis.
    assert result["provider_requires_review"] is True
    assert result["provider_can_auto_post"] is False
    assert result["unresolved"] == []


def test_11331_the_parsed_draft_reports_required_missing_information() -> None:
    draft = DraftGenerationService.parse(_resolve(MISSING_11331))

    assert draft.requires_review is True
    assert draft.can_auto_post is False
    assert draft.has_required_missing_information is True


def test_11331_eligibility_holds_it_with_the_existing_reason() -> None:
    def verdict_for(generated):
        draft = _draft()
        # The fixture would infer a hold from missing_information on its own;
        # put the generation verdict in exactly as the service produced it.
        draft["metadata_json"]["hybrid"]["draft"] = generated
        return _evaluate(draft=draft)

    held = verdict_for(_resolve(MISSING_11331))
    assert held.decision == "REVIEW_REQUIRED"
    assert GPT_WITHHELD_AUTO_POST in held.reasons

    # The same draft about a product detail is still published.
    published = verdict_for(_resolve(["고객이 보유한 셋톱박스의 모델과 출력 단자 규격"]))
    assert published.decision == "SAFE", published.reasons


@pytest.mark.parametrize("missing", [
    "A/S 기사 점검 결과 및 제품 불량 판정 여부",                   # 10654
    "별도로 작성된 거래내역서를 요청하는 경우 정확한 결제 금액",   # 3145
])
def test_other_stored_operational_items_hold_the_same_way(missing) -> None:
    assert _resolve([missing])["can_auto_post"] is False


STAIRS = "현장 상황에 따른 계단 배송 추가 비용의 정확한 부과 여부와 금액"


def _resolve_with(missing, *, approved_ids, used_ids, usable=True):
    return DraftGenerationService._apply_reported_resolution(
        {
            "answer": "근거 기반 답변",
            "requires_review": True,
            "missing_information": list(missing),
            "unresolved": [],
            "used_learning_ids": list(used_ids),
            "subquestion_results": ANSWERABLE,
        },
        {"approved_learning_evidence": {
            "usable": usable, "learning_ids": list(approved_ids),
            "reason": "APPROVED_LEARNING_SUPPORTED" if usable else "NO_QUALIFYING_APPROVED_LEARNING",
        }},
    )


def test_11412_an_answer_resting_on_its_approved_policy_still_publishes() -> None:
    """Live replay 2026-11-01: 319369/319370 answered it, GPT② still asked."""

    result = _resolve_with([STAIRS], approved_ids=[319369, 319335],
                           used_ids=[319369, 319370])

    assert result["requires_review"] is False
    assert result["can_auto_post"] is True
    assert result["optional_missing_information"] == [STAIRS]


def test_10654_an_approved_learning_the_answer_did_not_use_does_not_exempt() -> None:
    """10654 carried approved 19266 (an RF-cable answer) but used no Learning."""

    result = _resolve_with(["A/S 기사 점검 결과 및 제품 불량 판정 여부"],
                           approved_ids=[19266], used_ids=[])

    assert result["can_auto_post"] is False


def test_11331_learnings_that_did_not_qualify_do_not_exempt() -> None:
    result = _resolve_with(MISSING_11331, approved_ids=[],
                           used_ids=[154605, 318273], usable=False)

    assert result["can_auto_post"] is False


# --------------------------------------------------------------------------
# what still publishes
# --------------------------------------------------------------------------

@pytest.mark.parametrize("missing", [
    # stored on auto-posted drafts whose answers were grounded
    "모니터의 외부 오디오 출력 단자 유무",
    "고객이 선택하려는 이동형 스탠드 옵션",
    "현재 상품의 정확한 모델 기준으로 검증된 무선 미러링 사양",
    "고객이 보유한 셋톱박스의 모델과 출력 단자 규격",
    "세부 조건",
])
def test_a_cautious_request_about_a_product_detail_still_publishes(missing) -> None:
    result = _resolve([missing])

    assert result["requires_review"] is False
    assert result["can_auto_post"] is True
    assert result["optional_missing_information"] == [missing]


def test_a_money_item_without_a_review_request_is_unchanged() -> None:
    """The model judged the answer publishable; nothing here second-guesses it."""

    result = _resolve(MISSING_11331, provider_review=False)

    assert result["requires_review"] is False
    assert result["can_auto_post"] is True


def test_a_review_request_with_nothing_missing_still_publishes() -> None:
    result = _resolve([])

    assert result["requires_review"] is False
    assert result["can_auto_post"] is True


def test_an_unresolved_sub_question_still_holds_on_its_own() -> None:
    result = _resolve(
        ["신청 절차"], provider_review=False,
        subquestions=[{"subquestion": "별도 신청 절차", "answered": False,
                       "status": "NO_RELIABLE_SOURCE"}],
    )

    assert result["unresolved"] == ["별도 신청 절차"]
    assert result["can_auto_post"] is False
    # The item is not an unresolved sub-question and the model did not ask
    # for review, so it stays optional exactly as before.
    assert result["optional_missing_information"] == ["신청 절차"]
