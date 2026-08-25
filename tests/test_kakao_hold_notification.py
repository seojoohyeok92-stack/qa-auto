"""The unregistered-inquiry notification, from the operator's side.

An operator reading this message on their phone has one question: is this
waiting for me, and why.  The message answered a different one -- it described
how the draft had been written, because ``result.reason`` is a generation
narrative.  So a held inquiry announced its answer route while the thing that
was actually blocking it appeared nowhere, and "판단 사유: GPT 답변 생성" told
an operator nothing they could act on.

The reasons here are not written for the message.  They are the publishing
gate's own codes, translated by the same table the dashboard uses, so the two
surfaces can never drift into describing the same inquiry differently.
"""
from __future__ import annotations

import pytest

from answer.hold_reasons import REASON_LABELS, describe_reason, primary_reason
from kakao_notify import format_qna_message

PRODUCT = "삼성 스마트모니터 M5"
SHIP_Q = "배송 좀 당겨주실 수 없나요?"
AIRPLAY_Q = "아이폰 데이터로 미러링하면 인터넷 연결 없이 가능한가요?"


def hold(**overrides) -> str:
    payload = {
        "product": PRODUCT,
        "option_name": "",
        "question": SHIP_Q,
        "answer": "초안 본문",
        "hold_reason": describe_reason("PROCESSING_PLAN_REQUIRES_REVIEW"),
        "hold_codes": ("PROCESSING_PLAN_REQUIRES_REVIEW",),
        "generation_skipped": True,
    }
    payload.update(overrides)
    return format_qna_message(**payload)


# ------------------------------------------------- the operator's questions
def test_the_message_says_which_inquiry():
    message = hold()
    assert PRODUCT in message
    assert SHIP_Q in message


def test_the_message_says_why_it_was_not_registered():
    message = hold()
    assert "미등록 사유:" in message
    assert "문의 처리 계획상 직원 확인이 필요합니다." in message


def test_the_message_says_whether_an_answer_was_even_written():
    assert "답변 생성: 생략됨" in hold(generation_skipped=True)
    assert "답변 생성: 완료" in hold(generation_skipped=False)


def test_the_message_says_it_is_not_on_naver():
    assert "네이버 등록: 안 됨" in hold()


def test_the_machine_readable_code_is_preserved_beside_the_sentence():
    """The Korean sentence is for a person; the code is for a search."""

    message = hold()
    assert "세부 사유: PROCESSING_PLAN_REQUIRES_REVIEW" in message


def test_several_codes_are_all_shown():
    message = hold(hold_codes=("ANSWER_REQUIRES_MANUAL_REVIEW",
                               "PROCESSING_PLAN_REQUIRES_REVIEW"))
    assert "ANSWER_REQUIRES_MANUAL_REVIEW" in message
    assert "PROCESSING_PLAN_REQUIRES_REVIEW" in message


# ------------------------------------------------------- skipped generation
def test_a_skipped_generation_does_not_show_the_holding_reply_as_the_answer():
    """There is no draft worth reading, and showing one implies there is."""

    message = hold(generation_skipped=True, answer="정확한 정보 확인이 필요합니다.")
    assert "생성 답변(참고):" not in message
    assert "정확한 정보 확인이 필요합니다." not in message


def test_a_generated_but_blocked_answer_is_shown_for_reference():
    message = hold(
        generation_skipped=False,
        answer="HDMI 포트는 3개입니다.",
        hold_reason=describe_reason("VALIDATOR_NOT_PASS"),
        hold_codes=("VALIDATOR_NOT_PASS",),
    )
    assert "생성 답변(참고): HDMI 포트는 3개입니다." in message
    assert "Validator 안전 검증을 통과하지 못했습니다." in message


# ------------------------------------------------------ hard beats soft
def test_a_hard_reason_is_never_replaced_by_a_soft_one():
    sentence = primary_reason(
        ("ANSWER_REQUIRES_MANUAL_REVIEW",), ("INTENT_CONFIDENCE_LOW",)
    )
    assert sentence == REASON_LABELS["ANSWER_REQUIRES_MANUAL_REVIEW"]
    assert sentence != REASON_LABELS["INTENT_CONFIDENCE_LOW"]


def test_a_soft_reason_is_used_only_when_nothing_hard_blocked():
    assert primary_reason((), ("INTENT_CONFIDENCE_LOW",)) == (
        REASON_LABELS["INTENT_CONFIDENCE_LOW"]
    )
    assert primary_reason((), ()) == ""


def test_soft_reasons_are_still_recorded_in_the_message():
    """Demoted, not discarded."""

    codes = ("POLICY_OR_HIGH_RISK_REVIEW", "INTENT_CONFIDENCE_LOW")
    message = hold(
        hold_reason=primary_reason(codes[:1], codes[1:]), hold_codes=codes
    )
    assert "위험·분쟁 가능성이 있어 직원 판단이 필요합니다." in message
    assert "INTENT_CONFIDENCE_LOW" in message


# --------------------------------------------------- success path unchanged
def test_a_posted_answer_keeps_the_message_it_always_had():
    message = format_qna_message(
        product=PRODUCT, option_name="화이트", question="배송 언제 되나요?",
        answer="8월 27일 예정입니다.", action="posted",
    )
    assert message == (
        f"상품명: {PRODUCT}\n"
        "옵션명: 화이트\n"
        "\n"
        "질문: 배송 언제 되나요?\n"
        "\n"
        "답변: 8월 27일 예정입니다."
    )
    assert "미등록" not in message
    assert "네이버 등록" not in message


def test_a_generation_notice_without_a_hold_is_unchanged():
    message = format_qna_message(
        product=PRODUCT, option_name="", question="q", answer="a",
        reason="Rule 기반 답변", action="generated",
    )
    assert "판단 사유: Rule 기반 답변" in message
    assert "답변: a" in message
    assert "미등록 사유" not in message


# ------------------------------------------------- reason vocabulary is shared
def test_the_dashboard_and_the_message_use_one_table():
    from ui.answer_status_presenter import REASON_LABELS as ui_labels
    from ui.answer_status_presenter import describe_reason as ui_describe

    assert ui_labels is REASON_LABELS
    assert ui_describe is describe_reason


@pytest.mark.parametrize(
    "code",
    [
        "POLICY_OR_HIGH_RISK_REVIEW",
        "PROCESSING_PLAN_REQUIRES_REVIEW",
        "ANSWER_REQUIRES_MANUAL_REVIEW",
        "VALIDATOR_NOT_PASS",
        "VALIDATOR_REVIEW_REQUIRED",
        "PRODUCT_FACT_NOT_VERIFIED",
        "INTERNAL_PLACEHOLDER_EXPOSURE",
        "EVIDENCE_CONFLICT",
        "DPS_RESULT_NOT_TRUSTED",
        "REQUIRED_ORDER_ID_MISSING_OR_INVALID",
    ],
)
def test_every_blocking_reason_has_an_operator_sentence(code):
    sentence = describe_reason(code)
    assert sentence and sentence != code, f"{code} would be shown as a raw code"


def test_an_unknown_code_is_shown_rather_than_guessed_at():
    assert describe_reason("SOME_NEW_REASON") == "SOME_NEW_REASON"


def test_a_route_reason_is_rendered_from_the_route():
    assert "BLOCKED_REVIEW_REQUIRED" in describe_reason(
        "ROUTE_BLOCKED_REVIEW_REQUIRED"
    )


# ------------------------------------------- the AirPlay message, end to end
def test_the_airplay_conflict_message_states_the_real_cause():
    message = format_qna_message(
        product=PRODUCT, option_name="", question=AIRPLAY_Q, answer="",
        hold_reason=describe_reason("EVIDENCE_CONFLICT"),
        hold_codes=("EVIDENCE_CONFLICT",),
        generation_skipped=True,
    )
    assert AIRPLAY_Q in message
    assert "확보된 근거가 서로 일치하지 않아 자동으로 확정할 수 없습니다." in message
    assert "답변 생성: 생략됨" in message
    assert "네이버 등록: 안 됨" in message
