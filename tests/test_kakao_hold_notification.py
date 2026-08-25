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


def test_the_detail_line_is_readable_without_knowing_the_codebase():
    """The code is for a search; this line is for a person on a phone.

    Superseded the earlier behaviour of printing the raw code here: staff
    reported that "PROCESSING_PLAN_REQUIRES_REVIEW" told them nothing and read
    like an error. The code is kept in the activity log instead -- see
    ``test_the_raw_codes_are_still_recorded_internally``.
    """

    message = hold()
    assert "세부 사유: 직원 검토 필요" in message
    assert "PROCESSING_PLAN_REQUIRES_REVIEW" not in message


def test_several_reasons_are_all_shown():
    message = hold(hold_codes=("ANSWER_REQUIRES_MANUAL_REVIEW",
                               "PROCESSING_PLAN_REQUIRES_REVIEW"))
    assert "직원 확인 필요" in message
    assert "직원 검토 필요" in message


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
    # The soft finding is still shown, just not as a raw identifier.
    assert "분류 신뢰도 낮음" in message
    assert "INTENT_CONFIDENCE_LOW" not in message


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


# ===================================================== staff-readable reasons
# The message is read on a phone by someone deciding whether to open the
# console. Internal codes are unreadable there -- and worse, they look like
# something is broken. These pin the display layer, and that it is *only* the
# display layer: the codes, the gate and the logs are untouched.
from answer.hold_reasons import (  # noqa: E402
    STAFF_HIDDEN_REASONS,
    STAFF_REASON_LABELS,
    STAFF_UNKNOWN_LABEL,
    staff_reason_labels,
    staff_reason_summary,
)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("PRODUCT_FACT_NOT_VERIFIED", "상품 정보 근거 부족"),
        ("INTENT_UNCLASSIFIED_VALIDATOR_CLEAR", "문의 유형 불명확"),
        ("INTENT_CONFIDENCE_LOW", "분류 신뢰도 낮음"),
        ("ANSWER_REQUIRES_MANUAL_REVIEW", "직원 확인 필요"),
        ("PROCESSING_PLAN_REQUIRES_REVIEW", "직원 검토 필요"),
        ("INTERNAL_PLACEHOLDER_EXPOSURE", "내부 문구 포함"),
        ("EVIDENCE_CONFLICT", "근거 충돌"),
        ("REQUIRED_ORDER_ID_MISSING_OR_INVALID", "주문번호 필요"),
        ("DPS_RESULT_NOT_TRUSTED", "배송·설치 조회 불가"),
        ("POLICY_OR_HIGH_RISK_REVIEW", "위험·분쟁 가능성"),
    ],
)
def test_a_code_becomes_a_phrase_a_staff_member_can_act_on(code, expected):
    assert staff_reason_summary([code]) == expected


def test_the_reported_example_becomes_one_readable_line():
    """The exact list from the report, rendered as staff should see it."""

    summary = staff_reason_summary([
        "PRODUCT_FACT_NOT_VERIFIED",
        "PRELIMINARY_REVIEW_RESOLVED",
        "INTENT_UNCLASSIFIED_VALIDATOR_CLEAR",
        "INTENT_CONFIDENCE_LOW",
    ])
    assert summary == "상품 정보 근거 부족 · 문의 유형 불명확 · 분류 신뢰도 낮음"
    for code in ("PRODUCT_FACT_NOT_VERIFIED", "INTENT_CONFIDENCE_LOW"):
        assert code not in summary


def test_no_internal_code_survives_into_the_message():
    codes = tuple(STAFF_REASON_LABELS) + ("ROUTE_BLOCKED_REVIEW_REQUIRED",
                                          "TOTALLY_NEW_CODE")
    message = hold(hold_codes=codes)
    for code in codes:
        assert code not in message, f"{code} leaked into the message"


# ------------------------------------------------------------ hidden states
@pytest.mark.parametrize("code", sorted(STAFF_HIDDEN_REASONS))
def test_a_processing_state_is_not_offered_as_a_blocking_reason(code):
    """These report how it went, not what stopped it."""

    assert staff_reason_labels([code]) == ()


def test_hidden_states_do_not_empty_a_real_reason_list():
    assert staff_reason_summary(
        ["PRELIMINARY_REVIEW_RESOLVED", "VALIDATOR_NOT_PASS"]
    ) == "검증 실패"


def test_hidden_states_are_still_produced_and_recorded():
    """Hidden from the message only -- the dashboard still explains them."""

    from answer.hold_reasons import REASON_LABELS
    from services.auto_processing_eligibility_service import SOFT_REASONS

    for code in STAFF_HIDDEN_REASONS:
        assert code in REASON_LABELS
        assert code in SOFT_REASONS


# ----------------------------------------------------------------- dedupe
def test_codes_meaning_the_same_thing_are_shown_once():
    assert staff_reason_summary(
        ["ROUTE_BLOCKED_REVIEW_REQUIRED", "ROUTE_DPS_LOOKUP_FAILED"]
    ) == "답변 경로 확인 필요"


def test_several_unknown_codes_do_not_repeat_the_same_phrase():
    assert staff_reason_summary(["NEW_A", "NEW_B", "NEW_C"]) == STAFF_UNKNOWN_LABEL


def test_a_repeated_code_is_shown_once():
    assert staff_reason_summary(
        ["VALIDATOR_NOT_PASS", "VALIDATOR_NOT_PASS"]
    ) == "검증 실패"


# ---------------------------------------------------------------- unknown
def test_an_unrecognised_code_reads_as_something_to_check():
    """A reason added later must not reach staff as a raw identifier."""

    summary = staff_reason_summary(["SOME_FUTURE_REASON"])
    assert summary == STAFF_UNKNOWN_LABEL
    assert "SOME_FUTURE_REASON" not in summary


def test_an_unknown_code_does_not_hide_the_known_ones():
    assert staff_reason_summary(
        ["PRODUCT_FACT_NOT_VERIFIED", "SOME_FUTURE_REASON"]
    ) == f"상품 정보 근거 부족 · {STAFF_UNKNOWN_LABEL}"


def test_empty_and_malformed_input_render_nothing():
    for value in ([], (), None, "", ["", "  "]):
        assert staff_reason_summary(value) == ""


# ------------------------------------------------------------- readability
def test_a_long_reason_list_stays_short_enough_to_read():
    summary = staff_reason_summary([
        "PRODUCT_FACT_NOT_VERIFIED", "VALIDATOR_NOT_PASS",
        "INTENT_CONFIDENCE_LOW", "DPS_RESULT_NOT_TRUSTED",
        "ORDER_LOOKUP_NOT_TRUSTED", "DRAFT_REVIEW_REQUIRED",
        "POLICY_OR_HIGH_RISK_REVIEW",
    ])
    assert "외 2건" in summary
    assert len(summary) <= 80, summary


def test_every_label_is_short_enough_for_a_phone():
    for code, label in STAFF_REASON_LABELS.items():
        assert 2 <= len(label) <= 16, f"{code}: {label}"
        assert not label.endswith("."), f"{code}: 문장이 아니라 구여야 합니다"


def test_every_reason_the_gate_can_produce_has_a_staff_label():
    """A blocking reason with no label would read as "추가 확인 필요"."""

    from answer.hold_reasons import REASON_LABELS

    missing = sorted(
        code for code in REASON_LABELS
        if code not in STAFF_REASON_LABELS and code not in STAFF_HIDDEN_REASONS
    )
    assert missing == [], missing


# ------------------------------------------------ nothing else changed
def test_the_internal_codes_themselves_are_unchanged():
    """The display layer must not have renamed anything."""

    from services.auto_processing_eligibility_service import SOFT_REASONS

    assert SOFT_REASONS == frozenset({
        "INTENT_CONFIDENCE_LOW", "INTENT_CONFIDENCE_UNKNOWN",
        "GPT_CONFIDENCE_LOW", "GPT_CONFIDENCE_UNKNOWN",
        "ORDER_ID_REQUESTED_FROM_CUSTOMER",
        "INTENT_UNCLASSIFIED_VALIDATOR_CLEAR",
        "PRELIMINARY_REVIEW_RESOLVED",
    })


def test_the_dashboard_still_shows_the_full_sentences():
    """Two audiences, two vocabularies -- the long one is untouched."""

    from ui.answer_status_presenter import describe_reason as ui_describe

    assert ui_describe("PRODUCT_FACT_NOT_VERIFIED") == "상품 정보 확인이 필요합니다."
    assert ui_describe("INTENT_CONFIDENCE_LOW") == (
        "문의 분류 신뢰도가 낮게 측정되었습니다."
    )


def test_the_primary_reason_sentence_is_unchanged():
    assert primary_reason(["PRODUCT_FACT_NOT_VERIFIED"]) == (
        REASON_LABELS["PRODUCT_FACT_NOT_VERIFIED"]
    )


def test_a_hold_whose_reasons_are_all_hidden_still_reads_sensibly():
    """Hiding the informational states must not produce a blank line.

    The message keeps its "왜 등록되지 않았는가" framing and simply omits the
    detail line, rather than printing "세부 사유:" with nothing after it.
    """

    message = hold(hold_reason="", hold_codes=("PRELIMINARY_REVIEW_RESOLVED",),
                   generation_skipped=False)
    assert "세부 사유:" not in message
    assert "미등록 사유: 자동 등록 조건을 충족하지 않았습니다." in message
    assert "네이버 등록: 안 됨" in message
    assert "PRELIMINARY_REVIEW_RESOLVED" not in message
