"""쿠팡 일반 배송 정책 질문의 생성 허용과 색상 표기 등가성.

배송. DPS 가 없는 마켓에서는 ``plan.is_delivery`` 하나가 생성을 막았다. 그
플래그는 메시지 전체에 대한 불리언 한 개여서, 주문 조회가 전혀 필요 없는 절차
질문도 초안 없이 끝났다 -- 문의 11199 "원하는 배송날짜 어떻게 지정해야하나요",
11231 "배송날짜 선택지가 안나왔는데 제가 지정할 수 없는건가요". 운영 스냅숏
(2026-10-22)에서 이 사유로 차단된 쿠팡 문의 20건 중 18건은 특정 주문의 상태
확인·일정 변경·취소·반품 실행 요청이고, 일반 절차 질문은 이 2건뿐이었다.

허용은 적극 확인으로만 한다. ``DELIVERY_POLICY`` 가 실제로 물어진 액션 중에
있어야 하며, 주문 범위 액션이 함께 있으면 차단을 유지한다. 액션 집합과
교집합이 없다는 사실만으로 일반 정책 질문이라고 보지 않는다.

색상. PK 는 카탈로그 표기를 그대로 보관해 LS32DM501EKXKR 의 ``stand_color``
가 "White" 로 저장돼 있고, 답변은 "화이트"라고 쓴다. 문의 11238 은 그 사실이
프롬프트에 전달되고 USED 로 기록되고 Validator 도 통과한 뒤 이 문자열 비교에서
만 탈락해 ``PRODUCT_FACT_NOT_VERIFIED`` 로 자동등록이 막혔다. 닫힌 색상 목록이고
색상 필드에만 적용되며, 답변이 그 색을 말하지 않으면 지금처럼 미검증이다.
"""
from __future__ import annotations

import itertools
import pathlib
import tempfile
from types import SimpleNamespace

import pytest

from answer.exceptions import AutoAnswerProhibitedError
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.answer_service import AnswerService, _delivery_policy_only
from services.dps_lookup_policy import DpsLookupPolicy
from services.product_fact_grounding import FactClaim, _text_findings
from services.semantic_analysis import (
    ENABLED_ENV,
    AtomicQuestion,
    CANCEL_RETURN,
    DELIVERY_DEADLINE_CONFIRMATION,
    DELIVERY_POLICY,
    DELIVERY_STATUS,
    INSTALLATION_METHOD,
    INSTALLATION_SCHEDULE,
    OTHER,
    PRODUCT_SPEC,
    SCHEDULE_CHANGE,
    SCHEDULE_REQUEST,
    SemanticAnalysis,
    parse,
)


def semantic(*actions: str, source: str = "GPT") -> SemanticAnalysis:
    """GPT ① 가 이 액션들을 하위질문으로 돌려준 상태."""

    return SemanticAnalysis(
        primary_action=actions[0] if actions else OTHER,
        atomic_questions=tuple(
            AtomicQuestion(text="질문 %d" % index, action=action)
            for index, action in enumerate(actions)
        ),
        source=source,
        confidence=0.9,
    )


def claim(field_key: str, value: str) -> FactClaim:
    return FactClaim(
        field_key=field_key, value=value, unit="", kind="TEXT",
        measures=(), members=(), subject_terms=(),
    )


def reasons(answer: str, fact: FactClaim) -> list[str]:
    return [item["reason"] for item in _text_findings(answer, fact, (answer,))]


# ------------------------------------------------- 배송 분류: 허용되는 경우

def test_01_delivery_policy_alone_is_allowed() -> None:
    assert _delivery_policy_only(semantic(DELIVERY_POLICY)) is True


def test_02_policy_beside_a_general_question_is_allowed() -> None:
    """함께 물은 것이 일반 질문이면 주문 조회가 생기지 않는다."""

    assert _delivery_policy_only(
        semantic(DELIVERY_POLICY, PRODUCT_SPEC)) is True
    assert _delivery_policy_only(
        semantic(DELIVERY_POLICY, INSTALLATION_METHOD)) is True


def test_03_the_inquiry_level_reading_also_counts(
) -> None:
    """하위질문 분해가 없으면 문의 자체의 액션을 읽는다."""

    value = SemanticAnalysis(
        primary_action=DELIVERY_POLICY, source="GPT", confidence=0.9)
    assert _delivery_policy_only(value) is True


# ------------------------------------------------- 배송 분류: 차단되는 경우

@pytest.mark.parametrize("action", [
    DELIVERY_STATUS,
    DELIVERY_DEADLINE_CONFIRMATION,
    SCHEDULE_REQUEST,
    SCHEDULE_CHANGE,
    INSTALLATION_SCHEDULE,
    CANCEL_RETURN,
])
def test_04_an_order_scoped_action_blocks(action: str) -> None:
    assert _delivery_policy_only(semantic(action)) is False


@pytest.mark.parametrize("action", [
    SCHEDULE_CHANGE,
    SCHEDULE_REQUEST,
    DELIVERY_STATUS,
    INSTALLATION_SCHEDULE,
    DELIVERY_DEADLINE_CONFIRMATION,
    CANCEL_RETURN,
])
def test_05_policy_plus_an_order_scoped_action_blocks(action: str) -> None:
    """정책 질문에 주문 처리 요청이 섞이면 차단을 유지한다."""

    assert _delivery_policy_only(semantic(DELIVERY_POLICY, action)) is False


def test_06_no_action_blocks() -> None:
    assert _delivery_policy_only(semantic()) is False


def test_07_unclassified_blocks() -> None:
    assert _delivery_policy_only(semantic(OTHER)) is False
    assert _delivery_policy_only(semantic(DELIVERY_POLICY, OTHER)) is False


def test_08_an_unusable_understanding_blocks() -> None:
    assert _delivery_policy_only(
        semantic(DELIVERY_POLICY, source="UNAVAILABLE")) is False
    assert _delivery_policy_only(None) is False


def test_09_a_non_delivery_question_is_not_a_policy_question() -> None:
    """주문 범위 액션과 겹치지 않는다는 사실만으로 허용하지 않는다."""

    assert _delivery_policy_only(semantic(PRODUCT_SPEC)) is False
    assert _delivery_policy_only(semantic(INSTALLATION_METHOD)) is False


def test_10_an_empty_action_string_is_not_an_action() -> None:
    value = SemanticAnalysis(
        primary_action=DELIVERY_POLICY,
        atomic_questions=(
            AtomicQuestion(text="질문", action=""),
        ),
        source="GPT", confidence=0.9,
    )
    assert _delivery_policy_only(value) is False


# --------------------------------------------------------- 색상 표기 등가성

def test_11_white_and_the_korean_spelling_are_one_value() -> None:
    """문의 11238 재현."""

    answer = "문의하신 상품은 모니터와 스탠드 모두 화이트 구성입니다."
    assert reasons(answer, claim("stand_color", "White")) == []
    assert reasons(answer, claim("front_color", "WHITE")) == []


def test_12_black_and_the_korean_spelling_are_one_value() -> None:
    answer = "스탠드 색상은 블랙입니다."
    assert reasons(answer, claim("stand_color", "Black")) == []


def test_13_other_korean_spellings_of_the_same_colour_count() -> None:
    assert reasons("스탠드 색상은 흰색입니다.",
                   claim("stand_color", "White")) == []
    assert reasons("후면은 검정색입니다.",
                   claim("rear_color", "Black")) == []


def test_14_two_different_colours_never_match() -> None:
    answer = "문의하신 상품의 스탠드는 블랙입니다."
    assert reasons(answer, claim("stand_color", "White")) == [
        "VALUE_NOT_STATED_IN_ANSWER"]
    assert reasons("스탠드는 화이트입니다.", claim("stand_color", "Black")) == [
        "VALUE_NOT_STATED_IN_ANSWER"]


def test_15_an_answer_that_never_names_the_colour_stays_unverified() -> None:
    answer = "설치는 기사님이 방문해 진행합니다."
    assert reasons(answer, claim("stand_color", "White")) == [
        "VALUE_NOT_STATED_IN_ANSWER"]


def test_16_another_attribute_cannot_borrow_a_colour_group() -> None:
    """색상 필드가 아니면 등가 처리를 쓰지 않는다."""

    answer = "문의하신 상품은 모니터와 스탠드 모두 화이트 구성입니다."
    assert reasons(answer, claim("panel_type", "White")) == [
        "VALUE_NOT_STATED_IN_ANSWER"]
    assert reasons(answer, claim("stand_type", "White")) == [
        "VALUE_NOT_STATED_IN_ANSWER"]


def test_17_a_colour_outside_the_list_is_compared_literally() -> None:
    assert reasons("스탠드는 미스틱 브론즈입니다.",
                   claim("stand_color", "Mystic Bronze")) == [
        "VALUE_NOT_STATED_IN_ANSWER"]
    assert reasons("스탠드는 Mystic Bronze 입니다.",
                   claim("stand_color", "Mystic Bronze")) == []


def test_18_a_negated_colour_is_still_caught() -> None:
    """다른 표기로 부정해도 부정으로 읽는다."""

    assert reasons("스탠드는 화이트가 아닙니다.",
                   claim("stand_color", "White")) == [
        "ANSWER_NEGATES_THE_VALUE"]


def test_19_the_literal_spelling_keeps_working() -> None:
    assert reasons("스탠드 색상은 White 입니다.",
                   claim("stand_color", "White")) == []


# ----------------------------------------- 게이트 자체: 생성 경로 진입 여부

_key = itertools.count()
COUPANG_STORE = "COUPANG_OJE_NS"


class _Dps:
    """외부 DPS 더블. 실제 호출 횟수를 센다."""

    def __init__(self) -> None:
        self.calls = 0
        self.skips = 0
        self.policy = DpsLookupPolicy()

    def enrich(self, request, **kwargs):
        self.calls += 1
        request.metadata["dps"] = {"lookup_required": False}
        return SimpleNamespace(
            decision=SimpleNamespace(lookup_required=False),
            metadata=request.metadata["dps"], lookup_row=None)

    def skip_for_phase9(self, request, **kwargs):
        # 이 호출은 게이트를 지난 뒤에만 일어난다. 테스트가 "막히지 않았다"를
        # 공허하게 통과하지 않도록 통과 증거로 쓴다.
        self.skips += 1
        request.metadata["dps"] = {
            "lookup_required": False, "lookup_status": "NOT_REQUIRED"}
        return SimpleNamespace(
            decision=SimpleNamespace(lookup_required=False),
            metadata=request.metadata["dps"], lookup_row=None)


class _Analyzer:
    def __init__(self, semantic) -> None:
        self.semantic = semantic
        self.last_trace = {"cache_hit": False, "latency_ms": 0.0}

    def analyze(self, question):
        return self.semantic


def _understanding(question: str, action: str, *, purchase: str):
    current = purchase == "CURRENT_ORDER"
    return parse({
        "primary_action": action, "secondary_actions": [],
        "request_type": "QUESTION", "objects": [],
        "atomic_questions": [
            {"text": question, "action": action,
             "requested_attribute": "METHOD_OR_PROCEDURE"},
        ],
        "deadline": None, "constraints": [], "negation": False,
        "conditional": False,
        "requires_order_context": current,
        "requires_delivery_schedule": current,
        "purchase_state": purchase,
        "asks_delivery_schedule": True, "asks_delivery_outcome": True,
        "confidence": 0.95,
    })


def _gate(monkeypatch, question: str, action: str, *, store: str,
          order_id: str | None = None, purchase: str = "PRE_PURCHASE"):
    """``MARKET_ORDER_DPS_NOT_SUPPORTED`` 가 발동하는지와 DPS 호출 횟수."""

    monkeypatch.setenv(ENABLED_ENV, "1")
    database = Database(pathlib.Path(tempfile.mkdtemp()) / "gate.db")
    database.initialize()
    inquiry_id = InquiryRepository(database).upsert_work_item({
        "store_code": store,
        "source_type": (
            "COUPANG_ONLINE_INQUIRY" if "COUPANG" in store
            else "PRODUCT_INQUIRY"),
        "source_question_id": "gate-%d" % next(_key),
        "inquiry_type": "PRODUCT_INQUIRY", "title": "문의",
        "content": question, "product_name": "삼성 TV",
        "product_id": "p1", "order_id": order_id,
        "product_order_id": None, "raw_json": {},
    }).inquiry_id
    dps = _Dps()
    service = AnswerService(
        database, dps_enrichment=dps,
        semantic_analyzer=_Analyzer(
            _understanding(question, action, purchase=purchase)),
    )
    blocked = None
    try:
        service.generate_for_inquiry(inquiry_id)
    except AutoAnswerProhibitedError as error:
        blocked = error.policy_reason
    except Exception:
        # 이 게이트 뒤에서 다른 이유로 실패하는 것은 이 테스트의 관심사가
        # 아니다. 관심사는 게이트가 막았는지 하나뿐이다.
        blocked = None
    return SimpleNamespace(
        blocked=blocked, dps_calls=dps.calls, passed_gate=dps.skips)


def test_20_a_coupang_policy_question_now_reaches_generation(
    monkeypatch,
) -> None:
    """문의 11199·11231 유형. 게이트가 더 이상 막지 않는다."""

    run = _gate(monkeypatch, "원하는 배송날짜 어떻게 지정해야하나요?",
                DELIVERY_POLICY, store=COUPANG_STORE)

    assert run.blocked is None
    assert run.passed_gate >= 1
    assert run.dps_calls == 0


def test_21_an_order_number_alone_does_not_block_a_policy_question(
    monkeypatch,
) -> None:
    """문의 11231 은 주문번호가 있는데도 일반 가능 여부 질문이었다."""

    run = _gate(monkeypatch, "배송날짜 선택지가 안나왔는데 지정할 수 없는건가요?",
                DELIVERY_POLICY, store=COUPANG_STORE,
                order_id="2026102112345678")

    assert run.blocked is None
    assert run.passed_gate >= 1
    assert run.dps_calls == 0


@pytest.mark.parametrize("action", [
    DELIVERY_STATUS, SCHEDULE_CHANGE, SCHEDULE_REQUEST,
    INSTALLATION_SCHEDULE, DELIVERY_DEADLINE_CONFIRMATION,
])
def test_22_a_coupang_order_scoped_question_is_still_blocked(
    monkeypatch, action: str,
) -> None:
    run = _gate(monkeypatch, "오늘 배송 되는 거 맞나요?", action,
                store=COUPANG_STORE, order_id="2026102112345678",
                purchase="CURRENT_ORDER")

    assert run.blocked == "MARKET_ORDER_DPS_NOT_SUPPORTED"
    assert run.passed_gate == 0
    assert run.dps_calls == 0


def test_23_the_naver_path_is_untouched(monkeypatch) -> None:
    """네이버는 이 분기에 진입조차 하지 않는다."""

    for action in (DELIVERY_POLICY, DELIVERY_STATUS, SCHEDULE_CHANGE):
        run = _gate(monkeypatch, "배송 언제 되나요?", action,
                    store="OJE_PLUS", order_id="2026102112345678",
                    purchase="CURRENT_ORDER")
        assert run.blocked is None
