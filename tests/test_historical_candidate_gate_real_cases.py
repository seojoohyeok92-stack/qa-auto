"""Historical candidate gate regressions taken from real OJE_NS cases.

Every exchange below is a reconstruction of a row that a READ-ONLY replay of
the 45 stored ``COUPANG_ONLINE_HISTORY`` cases actually produced, so each test
records a decision that was observed rather than one imagined from policy text.

The gate answers one question only: *can this stored pair be reused as
knowledge for a later customer?*  It is not the production auto-answer gate,
and a KEEP here is a candidate for review, never an activation -- Coupang
historical rows are written with ``candidate_only`` and stay inactive.  So the
subject of a question never decides the outcome on its own: a 파손/고장/나사
question whose answer gives the official check or the standard AS route is
reusable knowledge, while an answer that reads one customer's photograph is
not, whatever it is about.
"""

from __future__ import annotations

import pytest

from services.historical_learning_quality_service import classify_historical_candidate


# --- always excluded: one order, one transaction, one window ---------------

@pytest.mark.parametrize(
    "question,answer,reason",
    [
        pytest.param(
            "제 주문 언제 배송되나요?", "고객님 건은 9/12 배송 예정입니다.",
            "ORDER_SPECIFIC", id="one-order-delivery-date",
        ),
        pytest.param(
            "지금 어디까지 왔나요?", "현재 출고되어 배송 중입니다.",
            "DELIVERY", id="one-order-delivery-status",
        ),
        pytest.param(
            "주문 취소했는데 처리됐나요?", "이미 주문 취소하신 것으로 확인됩니다.",
            "CS_TRANSACTION", id="one-order-cancellation-state",
        ),
        pytest.param(
            "반품 접수했는데 어떻게 되나요?", "반품 회수 후 환불 처리될 예정입니다.",
            "CS_TRANSACTION", id="one-order-return-state",
        ),
        pytest.param(
            "상품권 언제까지 신청하나요?", "09/30일까지 신청기간입니다.",
            "TEMPORARY", id="voucher-application-window",
        ),
        pytest.param(
            "할인 적용되나요?", "현재 진행 중인 쿠폰 할인으로 구매 가능합니다.",
            "TEMPORARY", id="current-discount",
        ),
        pytest.param(
            "확인 부탁드립니다", "네",
            "LOW_VALUE", id="no-standalone-knowledge",
        ),
    ],
)
def test_order_transaction_and_temporary_rows_are_excluded(
    question: str, answer: str, reason: str
) -> None:
    gate = classify_historical_candidate(question, answer)
    assert gate.decision == "EXCLUDE"
    assert gate.primary_reason == reason


def test_dispatch_promise_is_delivery_even_without_the_word_배송() -> None:
    gate = classify_historical_candidate("빨리 보내주세요", "최대한 빠르게 발송하겠습니다.")
    assert gate.decision == "EXCLUDE"
    assert gate.primary_reason == "DELIVERY"


# --- reusable troubleshooting and AS knowledge must survive ----------------
#
# These are the cases an earlier revision excluded on topic alone: it read
# 나사/전원/고장 in the question and stopped there. The answers below are
# official, general and repeatable, which is what actually decides it.

@pytest.mark.parametrize(
    "question,answer",
    [
        pytest.param(
            "전원이 안 켜지는데 어떻게 해야 하나요?",
            "전원 케이블 연결 상태를 먼저 확인해 주세요. 동일 증상이 지속되면 "
            "삼성전자 서비스센터를 통해 점검을 신청해 주세요.",
            id="power-fault-with-official-check-and-as-route",
        ),
        pytest.param(
            "나사가 빠졌는데 다시 조립하려면 어떻게 해야 하나요?",
            "해당 부위는 사용자가 분해하는 부분이 아닙니다. 추가 조립을 시도하지 "
            "마시고 삼성전자 서비스센터 점검을 받아 주세요.",
            id="loose-screw-with-general-safe-instruction",
        ),
        pytest.param(
            "AS 접수는 어디서 하나요?",
            "삼성전자 서비스센터를 통해 접수할 수 있습니다.",
            id="as-intake-route",
        ),
    ],
)
def test_troubleshooting_answers_that_generalise_stay_keep(
    question: str, answer: str
) -> None:
    gate = classify_historical_candidate(question, answer)
    assert gate.decision == "KEEP"
    assert gate.primary_reason == "STABLE_PRODUCT_FAQ"


# --- the question is fine; only some answers are not ----------------------

def test_touch_question_answered_about_touch_is_a_good_candidate() -> None:
    """"터치되나요?" is a model FAQ.  Nothing about the question is a problem."""

    gate = classify_historical_candidate(
        "터치되나요?", "해당 모델은 터치 기능을 지원하지 않습니다."
    )
    assert gate.decision == "KEEP"
    assert gate.primary_reason == "STABLE_PRODUCT_FAQ"


def test_touch_question_answered_with_mounting_specs_goes_to_review() -> None:
    """Case 285: the same question, answered about VESA holes and weight.

    Two layers hid this.  ``assess`` sees a mismatch only when both sides carry
    a known concept and 터치 is in none of the ten patterns; and the stored
    question carries the marketplace's own "Coupang 상품문의" title, whose two
    tokens pushed a one-word question over the terse-question cap.  The
    footer's "언제든지" even lent the answer a DELIVERY_DATE concept.
    """

    gate = classify_historical_candidate(
        "Coupang 상품문의\n터치되나요?",
        "안녕하세요. ⚙ 오제 챗봇입니다.\n"
        "삼성 S32DM501 모델은 VESA 100x100 / 6.6kg 제품으로 본 무빙 스탠드와 "
        "호환 가능합니다.\n\n"
        "더 궁금하신 점은 저희 고객센터(1588-0000)로 언제든지 문의 주세요\n"
        "(상담가능시간 평일 09:00~18:00). 감사합니다.",
    )
    assert gate.decision == "MANUAL_REVIEW"
    assert gate.primary_reason == "UNKNOWN_INFORMATION"
    assert "QUESTION_ANSWER_MISMATCH" in gate.tags


# --- answers whose reasoning belongs to one asker -------------------------

def test_case_269_verdict_read_off_the_customers_photo_goes_to_review() -> None:
    """Not because it is a 나사/분해 case -- because the answer's finding is
    about one photograph and offers no reassembly method anyone can reuse."""

    gate = classify_historical_candidate(
        "모니터가 고정이 안 되고 기존 나사가 느슨해서 다시 조이다가 "
        "나사가 전부 빠졌습니다. 다시 해주세요.",
        "사진 확인 시 풀면 안 되는 곳을 강제로 푸신 것으로 확인됩니다. "
        "사용설명서에도 해당 부분을 분해하라는 곳이 없었습니다.",
    )
    assert gate.decision == "MANUAL_REVIEW"
    assert gate.primary_reason == "UNKNOWN_INFORMATION"
    assert "UNSUPPORTED_CAUSE_ASSERTION" in gate.tags


def test_case_282_reusable_assembly_service_policy_stays_a_candidate() -> None:
    """The question is one customer's stuck product, but the answer states a
    standing policy -- no assembly service, use the guide video -- which is
    exactly the kind of thing later customers ask."""

    gate = classify_historical_candidate(
        "모니터를 고정하다가 기존에 조립된 나사가 풀렸고 무거워서 "
        "다시 조립할 수 없습니다. 조립해주세요.",
        "별도의 조립서비스는 없습니다. 조립영상 참고 바랍니다.",
    )
    assert gate.decision == "KEEP"
    assert gate.primary_reason == "STABLE_PRODUCT_FAQ"


def test_case_284_is_judged_on_how_little_the_answer_says() -> None:
    """A real fault, but "네 맞습니다." carries no knowledge on its own.  The
    fault is not the reason for review; the empty answer is.

    Stored text, greeting included: "안녕하세요 고객님" is nine characters and
    on its own carried this past the twelve-character low-information floor.
    """

    gate = classify_historical_candidate(
        "Coupang 상품문의\n"
        "갑자기 전원이켜지지않는데요 1588 3366 걸어서 출장서비스신청햇는데 "
        "여기다가 하는게맞나요?",
        "안녕하세요 고객님\n네 맞습니다.",
    )
    assert gate.decision == "MANUAL_REVIEW"
    assert gate.primary_reason == "UNKNOWN_INFORMATION"
    assert "REVIEW_REQUIRED" in gate.tags


def test_case_301_unclear_symptom_answered_by_blaming_the_customer_pc() -> None:
    """Stored wording is 아닌, not 아니라.

    Korean composes the ending into the syllable -- 아닌 is U+B2CC, not 니 with
    a trailing consonant -- so a pattern written as ``아니(?:라|고|며|)`` matched
    the textbook forms and missed the sentence actually on file.
    """

    gate = classify_historical_candidate(
        "Coupang 상품문의\n"
        "에제 설치 허였습니다. 하지만 로그인하면 모니터에 나타납니다 왜그럴까요",
        "안녕하세요 고객님\n"
        "모니터는 송출기기이기때문에 고객님 PC신호를 받고 해당 화면을 "
        "띄워주는 기기입니다.\n"
        "해당 화면에서 안넘어가시는 경우라면 해당부분은 모니터 문제가 아닌 "
        "고객님 PC문제십니다.",
    )
    assert gate.decision == "MANUAL_REVIEW"
    assert gate.primary_reason == "UNKNOWN_INFORMATION"
    assert "UNSUPPORTED_CAUSE_ASSERTION" in gate.tags


# --- protected: stable product FAQ ----------------------------------------

@pytest.mark.parametrize(
    "question,answer",
    [
        pytest.param(
            "스탠드로 주문했는데 이동식 거치대를 사용하려면 벽걸이로 주문해야 하나요?",
            "별도 이동식 스탠드라면 스탠드형으로 주문하셔도 브라켓 체결이 "
            "가능한 VESA 홀이 있어 사용에 문제없습니다.",
            id="case-262-vesa-compatibility",
        ),
        pytest.param(
            "화면을 세로로 돌리면 화면도 자동으로 세로로 전환되나요?",
            "해당 모델은 오토피벗 기능을 지원하여 화면을 세로로 돌리시면 "
            "자동으로 전환됩니다.",
            id="case-268-auto-pivot",
        ),
        pytest.param(
            "와이파이 연결방법 알려주세요.",
            "초기 설정 화면 또는 설정창에서 Wi-Fi 설정을 선택하여 연결하실 수 있습니다.",
            id="case-274-wifi",
        ),
        pytest.param(
            "IPTV가 무엇인가요? 인터넷이랑 넷플릭스, 유튜브 정도만 사용할 예정입니다.",
            "IPTV는 셋톱박스를 연결하여 시청하는 방식이며, 제품 단독으로 Wi-Fi "
            "연결 시 인터넷과 넷플릭스, 유튜브 사용이 가능합니다.",
            id="case-283-iptv-ott",
        ),
        pytest.param(
            "리모컨 하나를 추가로 구매할 수 있나요?",
            "소모품 추가구매는 판매처에서는 어려우며 별도 구매는 "
            "삼성전자서비스센터로 문의해주시기 바랍니다.",
            id="case-292-spare-remote-policy",
        ),
        pytest.param(
            "유선 잭을 꽂으면 TV 시청이 가능한가요?",
            "해당 제품은 RF 안테나 단자가 없어 직접 연결은 불가하며 "
            "셋톱박스를 HDMI로 연결하여 사용하셔야 합니다.",
            id="case-296-rf-input",
        ),
        pytest.param(
            "유선방송 단자가 있나요? 넷플릭스도 사용 가능한가요?",
            "RF 단자는 없어 셋톱박스를 HDMI로 연결해주셔야 하며, 내장 스마트 "
            "기능으로 넷플릭스와 유튜브는 사용 가능합니다.",
            id="case-298-rf-and-ott",
        ),
        pytest.param(
            "기존 거실 TV를 방으로 옮기고 새 TV를 거실에 설치할 수 있나요?",
            "새 제품 설치는 가능하지만 기존 제품 이동은 지원해드리기 어렵습니다.",
            id="case-300-installation-scope-policy",
        ),
        pytest.param(
            "조립 서비스가 제공되나요?",
            "해당 제품은 고객님께서 직접 조립하시는 제품이며 별도 조립 "
            "서비스는 제공되지 않습니다.",
            id="general-assembly-service-policy",
        ),
        pytest.param(
            "VESA 벽걸이 설치 가능한가요?",
            "VESA 100x100 규격을 지원하여 벽걸이 브라켓 설치가 가능합니다.",
            id="general-vesa-installation",
        ),
        pytest.param(
            "일반적인 보증기간은 어떻게 되나요?",
            "제품 보증기간은 구입일로부터 1년이며 부품에 따라 기간이 다를 수 있습니다.",
            id="general-warranty-policy",
        ),
        pytest.param(
            "외부 모니터로 영상 송출이 되나요?",
            "HDMI 출력 단자를 통해 외부 기기로 영상 송출이 가능합니다.",
            id="송출-is-a-product-function-not-a-dispatch",
        ),
        pytest.param(
            "구성품은 어떻게 되나요?",
            "본체와 전용 케이블이 하나의 박스로 발송됩니다.",
            id="bare-발송-in-listing-copy-is-not-a-delivery-enquiry",
        ),
    ],
)
def test_stable_product_faq_stays_keep(question: str, answer: str) -> None:
    gate = classify_historical_candidate(question, answer)
    assert gate.decision == "KEEP"
    assert gate.primary_reason == "STABLE_PRODUCT_FAQ"


# Every stored Coupang row arrives inside this template, so stripping it is not
# a special case for three rows -- it changes how all 45 are read.  These pin
# that a good answer keeps its verdict once the template is on it.
TITLE = "Coupang 상품문의\n"
GREETING = "안녕하세요 고객님\n"
BOT = "안녕하세요. ⚙ 오제 챗봇입니다.\n"
FOOTER = (
    "\n\n더 궁금하신 점은 저희 고객센터(1588-0000)로 언제든지 문의 주세요\n"
    "(상담가능시간 평일 09:00~18:00). 감사합니다."
)


@pytest.mark.parametrize(
    "question,answer",
    [
        pytest.param(
            "유선 잭을 꽂으면 TV 시청이 가능한가요?",
            "해당 제품은 RF 안테나 단자가 없어 직접 연결은 불가하며 "
            "셋톱박스를 HDMI로 연결하여 사용하셔야 합니다.",
            id="case-296-rf-input-templated",
        ),
        pytest.param(
            "와이파이 연결방법 알려주세요.",
            "초기 설정 화면 또는 설정창에서 Wi-Fi 설정을 선택하여 연결하실 수 있습니다.",
            # The footer's 언제든지 reads as DELIVERY_DATE; without stripping,
            # this exact pair turns into a mismatch.
            id="case-274-wifi-templated",
        ),
        pytest.param(
            "터치되나요?",
            "해당 모델은 터치 기능을 지원하지 않습니다.",
            id="touch-answered-about-touch-templated",
        ),
        pytest.param(
            "AS 접수는 어디서 하나요?",
            "삼성전자 서비스센터를 통해 접수할 수 있습니다.",
            id="as-intake-route-templated",
        ),
        pytest.param(
            "기존 거실 TV를 방으로 옮기고 새 TV를 거실에 설치할 수 있나요?",
            "새 제품 설치는 가능하지만 기존 제품 이동은 지원해드리기 어렵습니다.",
            id="case-300-installation-scope-templated",
        ),
    ],
)
def test_template_around_a_good_answer_does_not_change_its_verdict(
    question: str, answer: str
) -> None:
    for wrapper in (GREETING, BOT):
        gate = classify_historical_candidate(
            TITLE + question, wrapper + answer + FOOTER
        )
        assert gate.decision == "KEEP", (wrapper, gate)
        assert gate.primary_reason == "STABLE_PRODUCT_FAQ"


def test_template_does_not_rescue_a_bundle_row_from_review() -> None:
    gate = classify_historical_candidate(
        TITLE + "스탠드와 같이 오나요?",
        GREETING + "모니터와 스탠드 패키지 제품이며 각각 박스로 발송됩니다." + FOOTER,
    )
    assert gate.decision == "MANUAL_REVIEW"
    assert gate.primary_reason == "LISTING_OR_BUNDLE_SPECIFIC"


# --- protected: listing/bundle rows stay a human decision ------------------

@pytest.mark.parametrize(
    "question,answer",
    [
        pytest.param(
            "스탠드와 같이 오나요?",
            "모니터와 스탠드 패키지 제품이며 각각 박스로 발송됩니다.",
            id="case-271-monitor-stand-package",
        ),
        pytest.param(
            "화이트로 주문했는데 화이트가 맞나요?",
            "주문하신 화이트 패키지 제품이 맞습니다.",
            id="case-275-white-package",
        ),
    ],
)
def test_listing_and_bundle_rows_stay_manual_review(question: str, answer: str) -> None:
    gate = classify_historical_candidate(question, answer)
    assert gate.decision == "MANUAL_REVIEW"
    assert gate.primary_reason == "LISTING_OR_BUNDLE_SPECIFIC"
