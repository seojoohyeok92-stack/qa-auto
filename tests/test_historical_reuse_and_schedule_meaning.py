"""과거 고객의 사실이 다른 고객의 근거가 되지 않도록, 그리고 같은 의미가
단계마다 다르게 읽히지 않도록.

COHORT_1 실데이터 40건에서 세 가지가 함께 드러났다.

1. 재사용 불가한 historical 이 SAFE_REUSABLE 로 승격
   inquiry 948 은 "확인되시는 고객님의 주문번호는 2026****1251 입니다" 와
   "주문하신 상품은 8/7(금) 설치 예정이며" 같은 과거 답변을 근거로 받았다.
   둘 다 한 고객에게만 참인 사실이다. 탐지기의 *개념*은 맞았고 표기가 좁았다 --
   ORDER_SPECIFIC 은 "고객님 주문"처럼 두 단어가 붙은 형태만 보고 있어 소유격
   '의' 가 끼면 놓쳤고, 날짜 탐지기는 범위(8/7~8/9)와 '8월 7일' 표기만 알아
   맨 '8/7(금)' 을 놓쳤다.

2. 확정 주문의 일정 문의가 DPS 로 가지 않고 historical 로 답해짐
   "배송이 이번주 수요일로 잡혀있어 구매했는데 ... 도착 날짜좀 알려주세요" 는
   키워드 정규식이 '도착일' 을 찾다가 '도착 날짜' 를 놓쳐 현재 사실 유예가
   걸리지 않았다. 이제 이해가 그 판단을 한다.

3. 같은 의미가 단계마다 다른 집합으로 적혀 있었음
   "언제설치가능한가요?" 를 분석기가 SCHEDULE_REQUEST 로 읽었는데,
   DELIVERY_WITH_INSTALLATION_DATE 경로는 DELIVERY_STATUS/INSTALLATION_SCHEDULE
   둘만 나열하고 있어 DPS 가 정확한 날짜를 찾아낸 답변이 SEMANTIC_ACTION_MISMATCH
   로 막혔다. '언제냐고 묻는 것' 을 한 곳에서 정의하고 공유한다.
"""
from __future__ import annotations

import pytest

from services.historical_learning_quality_service import (
    HistoricalLearningQualityService,
)
from services.semantic_action_support import COMPATIBLE, MISMATCH, evaluate
from services.semantic_analysis import (
    SCHEDULE_QUESTION_ACTIONS,
    parse,
)


POLICY = HistoricalLearningQualityService()


def assess(question: str, answer: str):
    return POLICY.assess(
        question=question, answer=answer, stored_quality=0.8,
        policy_risk="NONE", active=True, structured_temporary_valid=False,
    )


# 실제 개발 DB 의 historical 답변에서 가져온 문장. case id 는 조건에 쓰지 않는다.
NOT_REUSABLE = (
    ("배송설치 문의 최대한 빠른일자에 배송설치 부탁드립니다.",
     "안녕하세요 고객님. 주문하신 상품은 8/7(금) 설치 예정이며, 이는 확정은 "
     "아니라 일정은 변경될 수 있습니다."),
    ("삼성전자 온누리 행사에서 보완 요청이 와서 문의드립니다.",
     "안녕하세요 오제앤에스 입니다. 확인되시는 고객님의 주문번호는 "
     "2026****1251 입니다."),
    ("도착 예정일 문의",
     "구매하신 제품의 설치예정일은 8월 17일 입니다."),
    ("언제배송되나요?",
     "안녕하세요 오제앤에스 입니다. 확인되는 설치예정일은 8월27일 입니다."),
    ("배송 언제 되나요?",
     "고객님의 배송은 8/28 예정으로 확인됩니다."),
)

# 같은 주제이면서 누구에게나 참인 과거 답변. 계속 쓸 수 있어야 한다.
STILL_REUSABLE = (
    ("제품 설치 없이 문 앞 배송만 받고 싶은데 가능할까요?",
     "삼성 오디세이 G8 LS32HG806 32인치는 택배배송 상품으로 설치 없이 문 앞 "
     "배송이 가능합니다."),
    ("포인트 언제 들어오나요?",
     "당첨자 분들께는 네이버 알림을 통해 메세지가 전달되시며 해당 알림을 통해 "
     "지급을 안내드리고 있습니다."),
    ("삼성센터에서 수리 되나요?",
     "삼성서비스센터 통해 A/S 받아보실 수 있습니다."),
    ("설치기사님이 따로 연락 주시나요?",
     "설치 전날 저녁 시간대에 수취인 번호로 설치 기사님이 연락하시어 유선 상으로 "
     "시간 조율 하에 방문하십니다."),
    ("무상 AS 기간이 얼마인가요?",
     "as기간 1년 / 패널보증기간 2년입니다."),
    ("벽걸이 브라켓 별도 구매해야 하나요?",
     "추가옵션에 벽걸이형 추가 시 벽걸이 브라켓까지 같이 갑니다."),
)


# ==========================================================================
# 1. 고객 특정 historical 은 근거가 되지 않는다
# ==========================================================================


@pytest.mark.parametrize("question,answer", NOT_REUSABLE)
def test_a_fact_about_one_customers_order_is_not_reusable(
    question: str, answer: str,
) -> None:
    verdict = assess(question, answer)

    assert verdict.context_eligible is False, verdict.status
    assert verdict.status in {"ORDER_SPECIFIC", "TEMPORARY_OR_EXPIRED"}


def test_a_possessive_particle_does_not_hide_the_askers_order() -> None:
    """'고객님의 주문번호' 와 '고객님 주문' 은 같은 말이다."""

    joined = assess("주문번호 확인 부탁드려요",
                    "고객님 주문번호는 2026****1251 입니다.")
    possessive = assess("주문번호 확인 부탁드려요",
                        "고객님의 주문번호는 2026****1251 입니다.")

    assert joined.context_eligible is False
    assert possessive.context_eligible is False


def test_a_bare_slash_date_is_recognised_as_one_orders_schedule() -> None:
    """'8월 7일' 만 알고 '8/7' 을 몰랐던 자리."""

    spelled = assess("언제 설치되나요?", "설치 예정일은 8월 7일 입니다.")
    slashed = assess("언제 설치되나요?", "주문하신 상품은 8/7(금) 설치 예정입니다.")

    assert spelled.context_eligible is False
    assert slashed.context_eligible is False


# ==========================================================================
# 2. 대조군 -- 일반적이고 재사용 가능한 same-topic historical 은 그대로
# ==========================================================================


@pytest.mark.parametrize("question,answer", STILL_REUSABLE)
def test_general_same_topic_historical_is_still_usable(
    question: str, answer: str,
) -> None:
    verdict = assess(question, answer)

    assert verdict.context_eligible is True, verdict.status
    assert verdict.status == "SAFE_REUSABLE"


def test_a_campaign_deadline_is_not_read_as_one_orders_schedule() -> None:
    """'9월 5일까지 신청' 은 행사 마감이지 한 주문의 일정이 아니다.

    날짜가 있다는 이유만으로 전부 막으면 안 된다는 쪽의 대조군이다. 기간성
    판정(TEMPORARY)은 그대로 받되, 주문 특정(ORDER_SPECIFIC)으로 분류되지는
    않아야 한다.
    """

    verdict = assess("온누리 신청 언제까지인가요?",
                     "온누리 환급 신청은 9월 5일까지 가능합니다.")

    assert verdict.order_specific is False


# ==========================================================================
# 3. '언제냐고 묻는 것' 은 한 곳에서 정의된다
# ==========================================================================


def _schedule_semantic(action: str):
    return parse({
        "primary_action": action, "secondary_actions": [],
        "request_type": "QUESTION", "objects": [],
        "atomic_questions": [{"text": "질문", "action": action}],
        "deadline": None, "constraints": [], "negation": False,
        "conditional": False, "requires_order_context": True,
        "requires_delivery_schedule": True, "purchase_state": "CURRENT_ORDER",
        "asks_delivery_schedule": True, "confidence": 0.95,
    })


@pytest.mark.parametrize("action", sorted(SCHEDULE_QUESTION_ACTIONS))
def test_a_confirmed_date_answers_every_way_of_asking_when(action: str) -> None:
    decision = evaluate(
        _schedule_semantic(action),
        route="DELIVERY_WITH_INSTALLATION_DATE", template_id="", match_kind=None,
    )

    assert decision.status == COMPATIBLE, action


def test_a_request_to_move_the_date_is_still_a_mismatch() -> None:
    """현재 날짜를 알려주는 것은 날짜를 옮겨달라는 요청을 들어준 것이 아니다."""

    decision = evaluate(
        _schedule_semantic("SCHEDULE_CHANGE"),
        route="DELIVERY_WITH_INSTALLATION_DATE", template_id="", match_kind=None,
    )

    assert decision.status == MISMATCH


def test_an_unrelated_action_is_still_a_mismatch() -> None:
    decision = evaluate(
        _schedule_semantic("COLLECTION"),
        route="DELIVERY_WITH_INSTALLATION_DATE", template_id="", match_kind=None,
    )

    assert decision.status == MISMATCH


def test_schedule_change_is_deliberately_outside_the_shared_set() -> None:
    assert "SCHEDULE_CHANGE" not in SCHEDULE_QUESTION_ACTIONS
    assert "DELIVERY_STATUS" in SCHEDULE_QUESTION_ACTIONS
    assert "SCHEDULE_REQUEST" in SCHEDULE_QUESTION_ACTIONS


# ==========================================================================
# 4. 조회할 주문이 없으면 조회가 필요하다고 말하지 않는다
# ==========================================================================


def test_no_order_to_look_up_does_not_claim_a_lookup_is_pending() -> None:
    """구매 전 고객에게 '주문 조회가 필요합니다' 는 하지 않을 일을 예고하는 말이다."""

    from answer.inquiry_analysis import (
        AnswerStrategy, InquiryAnalysis, InquiryType, OrderIdStatus,
    )
    from answer.models import AnswerRequest, AnswerResult, AnswerStatus
    from services.phase9_answer_policy import (
        DELIVERY_LOOKUP_REQUIRED_ANSWER, DELIVERY_SCHEDULE_REVIEW_ANSWER,
        apply_phase9_rule_policy,
    )

    def analysis(requires_order: bool) -> InquiryAnalysis:
        return InquiryAnalysis(
            inquiry_type=InquiryType.DELIVERY_INSTALLATION_STATUS,
            inquiry_subtype="DELIVERY_OR_INSTALLATION_SCHEDULE",
            requires_order_lookup=requires_order,
            requires_dps_lookup=requires_order,
            requires_order_id=requires_order,
            order_id_present=False,
            order_id_validated=False,
            order_id_status=(
                OrderIdStatus.MISSING if requires_order
                else OrderIdStatus.NOT_REQUIRED
            ),
            answer_strategy=AnswerStrategy.GENERAL_GUIDANCE,
            selected_fact_keys=(),
            confidence=0.9,
            reasons=(),
            manual_review_required=not requires_order,
            auto_answerable=requires_order,
            detected_intent="DELIVERY_STATUS",
        )

    request = AnswerRequest(
        inquiry_type="PRODUCT_INQUIRY", question="주문하면 바로 배송되나요",
    )

    def base() -> AnswerResult:
        return AnswerResult(
            status=AnswerStatus.NOT_SUPPORTED, category="기타",
            reason="템플릿 없음", answer="", provider="test",
            auto_answerable=False, needs_review=True,
        )

    no_order = apply_phase9_rule_policy(request, base(), analysis(False))
    has_order = apply_phase9_rule_policy(request, base(), analysis(True))

    assert no_order is not None and has_order is not None
    assert no_order.answer == DELIVERY_SCHEDULE_REVIEW_ANSWER
    assert "주문 조회가 필요합니다" not in no_order.answer
    # 조회가 실제로 필요한 경우의 기존 문구는 그대로다.
    assert has_order.answer == DELIVERY_LOOKUP_REQUIRED_ANSWER


# ==========================================================================
# 5. 프로그램 이름을 부르는 것과 그 기간에 매인 것은 다르다
# ==========================================================================


# COHORT_1 inquiry 2582 "포토리뷰 블로그리뷰 작성했는데언제받을수있나요?" 의
# 정답 Learning 9건이 전부 검색에서 빠졌다. 9건은 서로 같은 말을 하고 셋은
# 사람이 검증했는데, 빠진 이유는 하나였다 -- 답변에 "리뷰 이벤트" 라는 말이
# 들어 있다는 것. 그 답변이 실제로 말하는 것은 "네이버폼 작성일 기준 다음달 초
# 5영업일 내 지급" 이라는 상시 규칙이고, 오늘 읽어도 그대로 참이다.
REVIEW_EVENT_STANDING_RULE = (
    "리뷰 이벤트는 상품페이지의 스페셜기프트 이벤트 배너에서 확인 가능합니다. "
    "QR코드를 통해 네이버폼을 작성해주시면 작성일 기준 다음달 초, 5영업일 "
    "내에 발송됩니다."
)

STILL_TIME_BOUND = (
    "온누리 환급 신청은 9월 5일까지 가능합니다.",
    "이벤트는 8/1~8/15 진행됩니다.",
    "리뷰 이벤트는 선착순 100명까지 진행됩니다.",
    "프로모션 가격은 오늘 종료됩니다.",
    "하계 휴가 기간에는 배송이 지연됩니다.",
    "추석 연휴에는 배송이 어렵습니다.",
    "현재 재고 보유중입니다.",
)

NOT_TIME_BOUND = (
    REVIEW_EVENT_STANDING_RULE,
    "포토리뷰이벤트의 경우 상품페이지 내의 이벤트안내배너에서 신청방법을 "
    "안내드리고 있습니다.",
    "설치 전날 저녁 시간대에 기사님이 연락하시어 시간 조율 후 방문하십니다.",
    "as기간 1년 / 패널보증기간 2년입니다.",
)


@pytest.mark.parametrize("answer", STILL_TIME_BOUND)
def test_an_answer_with_a_boundary_is_still_time_bound(answer: str) -> None:
    from services.historical_learning_quality_service import is_time_bound

    assert is_time_bound(answer) is True


@pytest.mark.parametrize("answer", NOT_TIME_BOUND)
def test_naming_a_programme_is_not_being_bound_to_it(answer: str) -> None:
    from services.historical_learning_quality_service import is_time_bound

    assert is_time_bound(answer) is False


def test_the_review_payout_rule_is_reusable_knowledge() -> None:
    verdict = assess("포토리뷰 작성했는데 언제 받을 수 있나요?",
                     REVIEW_EVENT_STANDING_RULE)

    assert verdict.status != "TEMPORARY_OR_EXPIRED"


def test_a_demonstrative_on_the_product_is_not_the_askers_order() -> None:
    """'해당 제품' 은 논의 중인 상품을 가리키는 말이지 이 고객의 주문이 아니다."""

    generic = assess("이 TV 패널은 QLED인가요?",
                     "해당 제품은 QLED 패널을 사용하는 상품입니다.")
    theirs = assess("제 주문 어떻게 되나요?",
                    "해당 주문은 8/28 배송 예정으로 확인됩니다.")

    assert generic.order_specific is False
    assert theirs.order_specific is True


def test_a_claim_about_how_things_are_right_now_is_time_bound() -> None:
    """경계는 날짜만이 아니다 -- '평소보다' 는 지금이 평소가 아니라는 말이다.

    프로그램 이름과 기간을 나눌 때 처음 놓쳤던 자리이고, 기존 재감사 테스트가
    잡았다. "삼성 감사제 주문량 증가로 배송이 평소보다 지연되고 있습니다" 는
    날짜도 마감도 부르지 않지만 그 상황이 계속되는 동안만 참이다.
    """
    from services.historical_learning_quality_service import is_time_bound

    assert is_time_bound(
        "삼성 감사제 주문량 증가로 배송이 평소보다 지연되고 있습니다."
    ) is True
    # 대조군: 같은 프로그램 이름이라도 상시 규칙이면 그대로 재사용 가능하다.
    assert is_time_bound(REVIEW_EVENT_STANDING_RULE) is False
