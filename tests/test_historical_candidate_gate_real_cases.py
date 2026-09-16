"""Historical candidate gate regressions taken from real OJE_NS cases.

Every exchange below is a reconstruction of a row that a READ-ONLY replay of
the 45 stored ``COUPANG_ONLINE_HISTORY`` cases actually produced, so each test
records a decision that was observed to be wrong rather than one imagined from
the policy text.
"""

from __future__ import annotations

import pytest

from services.historical_learning_quality_service import classify_historical_candidate


# --- individual product trouble: one customer's unit, not a standing FAQ ----

def test_loosened_screws_that_fell_out_is_a_cs_case_not_assembly_faq() -> None:
    """Case 269 reached KEEP: no concept pattern covers 나사 or 조립."""

    gate = classify_historical_candidate(
        "모니터가 고정이 안 되고 기존 나사가 느슨해서 다시 조이다가 "
        "나사가 전부 빠졌습니다. 다시 해주세요.",
        "풀면 안 되는 곳을 강제로 푸신 것으로 보입니다.",
    )
    assert gate.decision == "EXCLUDE"
    assert gate.primary_reason == "DAMAGE_DEFECT"
    assert "INDIVIDUAL_PRODUCT_TROUBLE" in gate.tags


def test_failed_reassembly_request_is_excluded_despite_policy_shaped_answer() -> None:
    """Case 282: the answer reads like a standing 조립서비스 policy, but the
    question is one customer's stuck product."""

    gate = classify_historical_candidate(
        "모니터를 고정하다가 기존에 조립된 나사가 풀렸고 무거워서 "
        "다시 조립할 수 없습니다. 조립해주세요.",
        "별도 조립서비스는 제공하지 않으며 조립 영상을 참고해주시기 바랍니다.",
    )
    assert gate.decision == "EXCLUDE"
    assert gate.primary_reason == "DAMAGE_DEFECT"


def test_dead_power_with_engineer_visit_already_booked_is_excluded() -> None:
    """Case 284: a live fault plus a service call the customer already made."""

    gate = classify_historical_candidate(
        "갑자기 전원이 켜지지 않아서 삼성 서비스센터에 출장서비스를 "
        "신청했는데 그렇게 하는 게 맞나요?",
        "네 맞습니다. 서비스센터로 접수하시면 됩니다.",
    )
    assert gate.decision == "EXCLUDE"
    assert gate.primary_reason == "DAMAGE_DEFECT"
    assert "PRODUCT_MALFUNCTION" in gate.tags


# --- question/answer mismatch the concept vocabulary could not see ----------

def test_touch_question_answered_with_mounting_specs_goes_to_review() -> None:
    """Case 285 reached KEEP because 터치 is in no concept pattern, which
    switched the mismatch test off entirely."""

    gate = classify_historical_candidate(
        "터치되나요?",
        "VESA 100x100 규격이며 무게는 6.6kg으로 무빙 스탠드와 호환됩니다.",
    )
    assert gate.decision == "MANUAL_REVIEW"
    assert gate.primary_reason == "UNKNOWN_INFORMATION"
    assert "QUESTION_ANSWER_MISMATCH" in gate.tags


def test_unclear_symptom_answered_by_blaming_the_customer_pc_goes_to_review() -> None:
    """Case 301: the question's meaning is unclear and the answer settles the
    cause on the customer's side with nothing behind it."""

    gate = classify_historical_candidate(
        "설치 후 로그인하면 모니터에 나타납니다. 왜 그런가요?",
        "모니터는 PC 신호를 표시하는 장치이며 화면에서 넘어가지 않는다면 "
        "모니터 문제가 아니라 고객님 PC 문제입니다.",
    )
    assert gate.decision == "MANUAL_REVIEW"
    assert gate.primary_reason == "UNKNOWN_INFORMATION"
    assert "UNSUPPORTED_CAUSE_ASSERTION" in gate.tags


# --- delivery wording that used only 발송 -----------------------------------

def test_dispatch_promise_is_delivery_even_without_the_word_배송() -> None:
    gate = classify_historical_candidate("빨리 보내주세요", "최대한 빠르게 발송하겠습니다.")
    assert gate.decision == "EXCLUDE"
    assert gate.primary_reason == "DELIVERY"


# --- protected: stable product FAQ must stay reusable ----------------------

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
