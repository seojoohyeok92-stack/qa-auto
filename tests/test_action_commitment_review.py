"""조치를 수행하겠다고 약속한 답변은 자동등록하지 않는다.

문의 3145 -- "고효율 가전환급금 신청하는데 거래내역서가 필요합니다. 발부
부탁드립니다." -- 는 GPT ① 이 ``request_type=ACTION_REQUEST`` /
``requested_attribute=ACTION_EXECUTION`` 으로 읽었는데, "정확한 결제 금액을
알려주시면 확인 후 작성해드리겠습니다" 라는 답변이 그대로 고객에게 등록됐다.
아무도 쓰기로 합의하지 않은 서류를 시스템이 약속한 것이고, Validator PASS,
차단 사유 없음, 그 사이에 아무 관문도 없었다.

기존 ``POLICY_STAFF_ONLY_ACTIONS`` 는 CANCEL_RETURN·DAMAGE_REPORT 를 액션으로
막는데 이 문의의 액션은 OTHER 였다 -- 요청은 attribute 쪽에 있었다.
``_CASE_COMMITMENT`` 도 닿지 않고 넓혀서도 안 된다: 그 동사는
교환/환불/반품/회수/수거(이미 두 액션이 막는 영역)이고, 종결부에
"가능합니다"/"됩니다" 가 있어 "수거를 요청해 주시면 됩니다" 같은 절차 안내까지
잡는다.

두 조건이 모두 필요하다. GPT ① 이 조치 요청이라고 말하고, 완성된 답변이
그 조치를 직접 수행한다고 말해야 한다. 정보·절차 안내와 단순 확인은 그대로
통과한다 -- 문의 11238 의 "모니터와 스탠드 모두 화이트 구성입니다" 는 검증된
사실 진술이고 약속이 아니다.
"""
from __future__ import annotations

import pytest

from services.auto_processing_eligibility_service import (
    ACTION_COMMITMENT_REVIEW,
    POLICY_STAFF_ONLY_ACTIONS,
    _promises_an_action,
)

EXECUTION = ({"action": "OTHER", "requested_attribute": "ACTION_EXECUTION"},)
INFORMATION = (
    {"action": "OTHER", "requested_attribute": "METHOD_OR_PROCEDURE"},
)

# 운영 스냅숏의 실제 본문.
ANSWER_3145 = (
    "♣♧안녕하세요♧♣\n오제 챗봇(Chat Bot)이 답변드립니다.\n\n"
    "고효율 가전 환급 신청용 거래내역서는 결제내역에서 해당 상품을 선택한 뒤, "
    "주문/배송상세정보 페이지를 모델명과 구매자 정보가 보이도록 캡처해 "
    "제출하실 수 있습니다. 별도로 작성된 거래내역서가 필요하시면 정확한 결제 "
    "금액을 알려주시면 확인 후 작성해드리겠습니다."
)
ANSWER_11238 = "문의하신 상품은 모니터와 스탠드 모두 화이트 구성입니다."
ANSWER_11025 = (
    "구매 시 폐가전 수거를 체크하지 못했더라도 설치 기사님의 해피콜 또는 "
    "방문 시 수거를 요청해 주시면 됩니다."
)
ANSWER_10582 = "불량 없는 상품으로 보내 달라는 요청 사항 확인했습니다."
CLOSING = (
    "안내드린 내용이 문의하신 내용과 다른 경우, 네이버 톡톡으로 문의 "
    "남겨주시면 담당자가 확인 후 안내드리겠습니다."
)


# ------------------------------------------------------------- 발동하는 경우

def test_01_inquiry_3145_is_held() -> None:
    """운영에서 자동등록된 실제 본문."""

    assert _promises_an_action(EXECUTION, answer=ANSWER_3145) is True


@pytest.mark.parametrize("answer", [
    "거래내역서를 발급해 드리겠습니다.",
    "거래명세서를 발부해 드리겠습니다.",
    "주문 취소 처리해 드리겠습니다.",
    "환불 진행해 드리겠습니다.",
    "재발송해 드리겠습니다.",
    "주소지 변경하여 다시 반영 도와드리겠습니다.",
    "옵션을 수정해 드리겠습니다.",
    "수거 접수해 드리겠습니다.",
])
def test_02_a_promise_to_perform_the_action_is_held(answer: str) -> None:
    assert _promises_an_action(EXECUTION, answer=answer) is True


# --------------------------------------------------------- 발동하지 않는 경우

def test_03_inquiry_11238_keeps_passing() -> None:
    """검증된 제품 사실 진술은 약속이 아니다."""

    assert _promises_an_action(EXECUTION, answer=ANSWER_11238) is False


def test_04_a_plain_colour_answer_is_unaffected() -> None:
    for answer in (
        "스탠드 색상은 화이트입니다.",
        "전면과 후면 모두 블랙입니다.",
        "문의하신 모델은 화이트 단일 구성입니다.",
    ):
        assert _promises_an_action(EXECUTION, answer=answer) is False


def test_05_procedure_guidance_is_unaffected() -> None:
    """직원 조치 없이 고객이 스스로 할 수 있는 안내."""

    assert _promises_an_action(EXECUTION, answer=ANSWER_11025) is False
    assert _promises_an_action(
        EXECUTION, answer="상품페이지의 신청 링크에서 접수해 주시면 됩니다.",
    ) is False


def test_06_an_acknowledgement_is_unaffected() -> None:
    assert _promises_an_action(EXECUTION, answer=ANSWER_10582) is False


def test_07_promising_information_is_not_promising_an_action() -> None:
    """"안내드리겠습니다" 는 정보 약속이다. 문의 10905·10931 본문."""

    for answer in (
        "변경 가능 여부를 안내드리겠습니다.",
        "확인 후 안내드리겠습니다.",
        "담당자가 확인 후 안내드리겠습니다.",
    ):
        assert _promises_an_action(EXECUTION, answer=answer) is False


def test_08_the_standard_closing_never_fires() -> None:
    """모든 챗봇 답변이 이 맺음말을 달고 있다."""

    assert _promises_an_action(EXECUTION, answer=CLOSING) is False
    assert _promises_an_action(
        EXECUTION, answer=ANSWER_11238 + "\n\n" + CLOSING) is False


def test_09_a_bare_undertaking_is_not_enough() -> None:
    """지시: 단순히 "드리겠습니다" 라는 표현만으로 보류하지 않는다."""

    for answer in (
        "감사합니다. 좋은 하루 보내시기 바랍니다.",
        "말씀해 주셔서 감사드립니다.",
        "자세히 설명드리겠습니다.",
        "답변드리겠습니다.",
    ):
        assert _promises_an_action(EXECUTION, answer=answer) is False


# ------------------------------------------------- 요청이 조치가 아닌 경우

def test_10_an_information_request_is_not_checked() -> None:
    """같은 본문이라도 ACTION_EXECUTION 요청이 아니면 검사하지 않는다."""

    assert _promises_an_action(INFORMATION, answer=ANSWER_3145) is False


def test_11_no_understanding_holds_nothing() -> None:
    assert _promises_an_action((), answer=ANSWER_3145) is False
    assert _promises_an_action(
        ({"action": "OTHER"},), answer=ANSWER_3145) is False


def test_12_a_non_mapping_question_is_ignored() -> None:
    assert _promises_an_action(
        ("문자열", None), answer=ANSWER_3145) is False


def test_13_one_execution_atom_among_several_is_enough() -> None:
    questions = (
        {"action": "PRODUCT_SPEC", "requested_attribute": "SPEC_VALUE"},
        {"action": "OTHER", "requested_attribute": "ACTION_EXECUTION"},
    )
    assert _promises_an_action(questions, answer=ANSWER_3145) is True
    assert _promises_an_action(questions, answer=ANSWER_11238) is False


def test_14_an_empty_answer_holds_nothing() -> None:
    assert _promises_an_action(EXECUTION, answer="") is False
    assert _promises_an_action(EXECUTION, answer=None) is False


# ------------------------------------------------------- 기존 정책 보존

def test_15_the_existing_staff_only_actions_are_unchanged() -> None:
    assert POLICY_STAFF_ONLY_ACTIONS == frozenset(
        {"CANCEL_RETURN", "DAMAGE_REPORT"})


def test_16_the_reason_code_is_stable() -> None:
    """로그의 blocking_reason_codes 에 이 문자열로 기록된다."""

    assert ACTION_COMMITMENT_REVIEW == "ACTION_COMMITMENT_REVIEW"


def test_17_the_dashboard_labels_the_reason() -> None:
    from services.dashboard_operations_service import _REASON_BUCKETS

    flat = {code for _label, codes in _REASON_BUCKETS for code in codes}
    assert ACTION_COMMITMENT_REVIEW in flat
