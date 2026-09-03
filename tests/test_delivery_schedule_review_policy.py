"""구매 완료가 확인되지 않은 배송 일정 문의는 자동답변하지 않는다.

확정된 두 운영정책이 사실 한 상황이다 -- 알려줄 일정이 존재하지 않는다.

  구매 전   주문이 아직 없으니 배송일도 없다. 결제 시점·기사 일정·재고에 따라
            달라지는 값이라 과거 평균이나 다른 주문의 기록으로 대신할 수 없다.
  애매      주문했는지 문의에 적혀 있지 않다. 여기서 "이미 주문하셨다면
            주문번호를 보내주세요"로 답하는 것은 구매 전 고객에게 배송기간을
            지어내는 것과 같은 종류의 실수다 -- 둘 다 시스템이 모르는 것을
            안다고 전제한다.

기존 구조로는 "애매"를 표현할 수 없었다. 실제 과거 문의 40건 측정에서 구매 전
9건과 주문 완료 9건은 action 만으로 100% 갈렸지만, 본문에 구매 여부가 없는 6건
("배송 언제 되나요??", "언제 받을수 있나요?")도 GPT 가 confidence 0.9 이상으로
DELIVERY_STATUS 를 단정했다. 모델이 불확실했던 게 아니라, 본문에 답이 없는
질문에 답하고 있었다. confidence threshold 를 올려도 잡히지 않는다.

그래서 다른 것을 묻는다 -- "이 고객이 주문했는가"가 아니라 "이 문의가 그렇게
말하는가". ``purchase_state`` 는 추론이 아니라 관찰이다. 실측 24/24 정확.

두 번째로 필요했던 구분은 "언제"를 묻는지 여부다. action 으로는 갈리지 않는다:
"배송비는 얼마인가요?"와 "지금 주문하면 배송 얼마나 걸리나요?"는 둘 다
DELIVERY_POLICY 에 deadline 도 없다. ``asks_delivery_schedule`` 이 그 관찰이고,
실측 10/10 정확.
"""
from __future__ import annotations

import itertools

import pytest

from answer.models import AnswerRequest
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.inquiry_analysis_service import (
    DELIVERY_SCHEDULE_REVIEW_SOURCE,
    InquiryAnalysisService,
)
from services.inquiry_processing_plan_service import InquiryProcessingPlanService
from services.semantic_analysis import (
    delivery_schedule_needs_review,
    delivery_schedule_question,
    parse,
    purchase_confirmed,
    unavailable,
)


_key = itertools.count()
ORDER_ID = "2026070448206811"


@pytest.fixture
def database(tmp_path) -> Database:
    value = Database(tmp_path / "delivery-schedule.db")
    value.initialize()
    return value


def semantic(
    primary: str,
    *,
    purchase_state: str = "UNKNOWN",
    asks_schedule: bool = False,
    atomic: list[dict[str, str]] | None = None,
    order_context: bool = False,
    delivery_schedule: bool = False,
    secondary: list[str] | None = None,
    asks_outcome: bool | None = None,
):
    return parse({
        "primary_action": primary,
        "secondary_actions": secondary or [],
        "request_type": "QUESTION",
        "objects": [],
        "atomic_questions": atomic or [{"text": "질문", "action": primary}],
        "deadline": None,
        "constraints": [],
        "negation": False,
        "conditional": False,
        "requires_order_context": order_context,
        "requires_delivery_schedule": delivery_schedule,
        "purchase_state": purchase_state,
        "asks_delivery_schedule": asks_schedule,
        **(
            {} if asks_outcome is None
            else {"asks_delivery_outcome": asks_outcome}
        ),
        "confidence": 0.95,
    })


def inquiry(database: Database, content: str, *, order_id: str | None = None) -> dict:
    inquiries = InquiryRepository(database)
    inquiry_id = inquiries.upsert_work_item({
        "store_code": "OJE_PLUS", "source_type": "NAVER",
        "source_question_id": f"ds-{next(_key)}", "inquiry_type": "PRODUCT_INQUIRY",
        "title": "문의", "content": content, "product_name": "삼성 TV",
        "product_id": "p1", "order_id": order_id, "product_order_id": None,
        "raw_json": {},
    }).inquiry_id
    return inquiries.get(inquiry_id)


def plan_for(database: Database, content: str, understanding, *, order_id=None):
    return InquiryProcessingPlanService(database).create(
        inquiry(database, content, order_id=order_id),
        semantic_analysis=understanding,
    )


# 실제 개발 DB 문의와 그 문의에 대해 실제 GPT 가 돌려준 값. 문의 ID 는 조건에
# 쓰지 않는다 -- 문장과 semantic 값만 가져왔다.
PRE_PURCHASE_CASES = (
    ("오늘 구매하면 배송이 언제일까요?", "DELIVERY_POLICY"),
    ("오늘 주문 하면 배송 몇일 인가요?", "DELIVERY_POLICY"),
    ("지금 주문하면 언제쯤 받을수 있나요?", "DELIVERY_DEADLINE_CONFIRMATION"),
    ("오늘주문하면 언제 받을수있을까요. 급해서요.", "SCHEDULE_REQUEST"),
    ("배송기간 알려주세요 오늘 구매시 급해요", "DELIVERY_POLICY"),
    ("지금 구매하면 정상적으로 받을 수 있나요", "DELIVERY_POLICY"),
    ("오늘 12시전 주문하면 출고예정일 좀 알려주세요", "SCHEDULE_REQUEST"),
    ("지금 주문하면 이번주 안에 받을 수 있나요?", "DELIVERY_DEADLINE_CONFIRMATION"),
    # 경계 사례로 확정된 것: 실제 일정 가능성을 묻고 있다.
    ("배송, 설치를 같은 날 받을 수 있나요", "DELIVERY_POLICY"),
    ("혹시 토요일에도 배달 가능하나요", "DELIVERY_POLICY"),
)

AMBIGUOUS_CASES = (
    ("배송 언제 되나요??", "DELIVERY_STATUS", True),
    ("언제 받을수 있나요?", "DELIVERY_STATUS", True),
    ("언제배송되나요 대략적인일자라도알려주세요.", "DELIVERY_STATUS", True),
    ("경기도 안성 빠른 배송 언제더ㅣ나여!", "DELIVERY_POLICY", False),
)

CURRENT_ORDER_CASES = (
    ("6월 27일 구매했는데 배송 언제 시작될까요?", "DELIVERY_STATUS"),
    ("어제 주문했는데 언제 오나요?", "DELIVERY_STATUS"),
    ("7월9일 결제했는데 언제쯤 배송되나요?", "DELIVERY_STATUS"),
    ("구매했는데 배송일자 언제쯤 될까요?", "DELIVERY_STATUS"),
    ("주문했는데 설치예정일 언제인가요?", "INSTALLATION_SCHEDULE"),
    ("주문한 상품 배송일 변경하고 싶습니다.", "SCHEDULE_CHANGE"),
)

# 일정이 아니라 절차·비용·방식을 묻는 문의. 구매 전이어도 막지 않는다.
PROCEDURE_CASES = (
    ("배송과 설치는 어떤 방식으로 진행되나요?", "DELIVERY_POLICY"),
    ("배송비는 얼마인가요?", "DELIVERY_POLICY"),
    ("설치는 기사님이 직접 해주시나요?", "INSTALLATION_METHOD"),
    ("무타공 설치인가요?", "INSTALLATION_METHOD"),
    ("브라켓 별도 구매해야하나요?", "PACKAGE_CONTENTS"),
)


# ==========================================================================
# 1. 관찰 두 가지가 세 상태를 만든다
# ==========================================================================


@pytest.mark.parametrize("text,action", PRE_PURCHASE_CASES)
def test_pre_purchase_schedule_question_needs_review(text: str, action: str) -> None:
    assert delivery_schedule_needs_review(
        semantic(action, purchase_state="PRE_PURCHASE", asks_schedule=True)
    ) is True


@pytest.mark.parametrize("text,action,delivery_schedule", AMBIGUOUS_CASES)
def test_ambiguous_schedule_question_needs_review(
    text: str, action: str, delivery_schedule: bool,
) -> None:
    """구매 여부가 문의에 없으면 어느 쪽으로도 단정하지 않는다."""

    assert delivery_schedule_needs_review(
        semantic(action, purchase_state="UNKNOWN", asks_schedule=True,
                 delivery_schedule=delivery_schedule)
    ) is True


@pytest.mark.parametrize("text,action", CURRENT_ORDER_CASES)
def test_current_order_schedule_question_is_never_held_by_this_policy(
    text: str, action: str,
) -> None:
    assert delivery_schedule_needs_review(
        semantic(action, purchase_state="CURRENT_ORDER", asks_schedule=True,
                 delivery_schedule=True, order_context=True)
    ) is False


@pytest.mark.parametrize("text,action", PROCEDURE_CASES)
def test_a_procedure_or_price_question_is_not_a_schedule_question(
    text: str, action: str,
) -> None:
    """"배송과 설치는 어떤 방식으로" 까지 막으면 안 된다."""

    understanding = semantic(action, purchase_state="UNKNOWN", asks_schedule=False)
    assert delivery_schedule_question(understanding) is False
    assert delivery_schedule_needs_review(understanding) is False


def test_a_validated_order_id_confirms_the_purchase_on_its_own() -> None:
    """본문이 말하지 않아도 플랫폼이 붙여둔 주문은 실재한다."""

    understanding = semantic("DELIVERY_STATUS", purchase_state="UNKNOWN",
                             asks_schedule=True, delivery_schedule=True)
    assert purchase_confirmed(understanding) is False
    assert purchase_confirmed(understanding, order_id_validated=True) is True
    assert delivery_schedule_needs_review(
        understanding, order_id_validated=True
    ) is False


def test_no_understanding_never_confirms_a_purchase() -> None:
    assert purchase_confirmed(None) is False
    assert purchase_confirmed(unavailable("PROVIDER_ERROR")) is False
    assert delivery_schedule_needs_review(None) is False


def test_a_missing_purchase_state_reads_as_unknown() -> None:
    """구 provider 가 필드를 안 보내도 주문이 있다고 읽히지 않는다."""

    raw = {
        "primary_action": "DELIVERY_STATUS", "secondary_actions": [],
        "request_type": "QUESTION", "objects": [],
        "atomic_questions": [{"text": "q", "action": "DELIVERY_STATUS"}],
        "deadline": None, "constraints": [], "negation": False,
        "conditional": False, "requires_order_context": True,
        "requires_delivery_schedule": True, "confidence": 0.95,
    }
    understanding = parse(raw)
    assert understanding.purchase_state == "UNKNOWN"
    assert understanding.asks_delivery_schedule is False
    assert purchase_confirmed(understanding) is False


# ==========================================================================
# 2. 라우팅 -- 조회 없음, 주문번호 요구 없음, 자동답변 없음
# ==========================================================================


@pytest.mark.parametrize("text,action", PRE_PURCHASE_CASES)
def test_pre_purchase_routing(database, text: str, action: str) -> None:
    plan = plan_for(database, text, semantic(
        action, purchase_state="PRE_PURCHASE", asks_schedule=True,
        atomic=[{"text": text, "action": action}],
    ))

    assert plan.requires_order_lookup is False
    assert plan.requires_dps_lookup is False
    assert plan.analysis.requires_order_id is False
    assert plan.analysis.answer_strategy.value != "REQUEST_ORDER_ID"
    assert plan.analysis.manual_review_required is True
    assert plan.analysis.auto_answerable is False


@pytest.mark.parametrize("text,action,delivery_schedule", AMBIGUOUS_CASES)
def test_ambiguous_routing_never_demands_an_order_number(
    database, text: str, action: str, delivery_schedule: bool,
) -> None:
    plan = plan_for(database, text, semantic(
        action, purchase_state="UNKNOWN", asks_schedule=True,
        delivery_schedule=delivery_schedule, order_context=delivery_schedule,
        atomic=[{"text": text, "action": action}],
    ))

    assert plan.requires_order_lookup is False
    assert plan.requires_dps_lookup is False
    assert plan.analysis.requires_order_id is False, (
        "구매 여부를 모르는 고객에게 주문번호를 자동 요구하지 않는다"
    )
    assert plan.analysis.answer_strategy.value != "REQUEST_ORDER_ID"
    assert plan.analysis.manual_review_required is True
    assert DELIVERY_SCHEDULE_REVIEW_SOURCE in plan.analysis.manual_review_sources


@pytest.mark.parametrize("text,action", CURRENT_ORDER_CASES)
def test_current_order_routing_is_untouched(
    database, text: str, action: str,
) -> None:
    """기존 Order/DPS pipeline 은 그대로다."""

    plan = plan_for(database, text, semantic(
        action, purchase_state="CURRENT_ORDER", asks_schedule=True,
        delivery_schedule=True, order_context=True,
        atomic=[{"text": text, "action": action}],
    ))

    assert plan.requires_order_lookup is True
    assert plan.requires_dps_lookup is True
    assert DELIVERY_SCHEDULE_REVIEW_SOURCE not in (
        plan.analysis.manual_review_sources
    )


def test_ambiguous_with_a_platform_order_keeps_the_lookup(database) -> None:
    """레코드에 주문이 붙어 있으면 조회는 정상 진행된다."""

    plan = plan_for(
        database, "배송 언제 되나요??",
        semantic("DELIVERY_STATUS", purchase_state="UNKNOWN", asks_schedule=True,
                 delivery_schedule=True, order_context=True),
        order_id=ORDER_ID,
    )

    assert plan.requires_order_lookup is True
    assert plan.requires_dps_lookup is True
    assert DELIVERY_SCHEDULE_REVIEW_SOURCE not in (
        plan.analysis.manual_review_sources
    )


def test_ordered_customer_without_an_order_number_keeps_the_existing_policy(
    database,
) -> None:
    """예 D -- 주문한 것은 분명하고 주문번호만 없는 경우."""

    text = "주문했는데 주문번호를 모르겠어요. 언제 배송되나요?"
    plan = plan_for(database, text, semantic(
        "DELIVERY_STATUS", purchase_state="CURRENT_ORDER", asks_schedule=True,
        delivery_schedule=True, order_context=True,
        atomic=[{"text": text, "action": "DELIVERY_STATUS"}],
    ))

    assert plan.requires_order_lookup is True
    assert DELIVERY_SCHEDULE_REVIEW_SOURCE not in (
        plan.analysis.manual_review_sources
    )


@pytest.mark.parametrize("text,action", PROCEDURE_CASES)
def test_procedure_questions_are_not_held(database, text: str, action: str) -> None:
    plan = plan_for(database, text, semantic(
        action, purchase_state="UNKNOWN", asks_schedule=False,
        atomic=[{"text": text, "action": action}],
    ))

    assert DELIVERY_SCHEDULE_REVIEW_SOURCE not in (
        plan.analysis.manual_review_sources
    )


# ==========================================================================
# 3. Learning 이 이 hold 를 뒤집지 못한다
# ==========================================================================


def _context(database, text, understanding, *, order_id=None):
    from answer.facts import build_answer_facts
    from answer.hybrid_models import Emotion, IntentResult
    from answer.models import AnswerRequest, AnswerResult, AnswerStatus
    from services.learning_context_service import LearningContextService

    value = inquiry(database, text, order_id=order_id)
    facts = build_answer_facts(
        AnswerRequest(inquiry_type="PRODUCT_INQUIRY", question=text,
                      product_name="삼성 TV"),
        AnswerResult(status=AnswerStatus.NOT_SUPPORTED, category="기타",
                     reason="템플릿 없음", answer="", provider="test",
                     auto_answerable=False, needs_review=True),
    )
    facts.inquiry["inquiry_id"] = value["id"]
    return LearningContextService(database).build(
        facts,
        IntentResult("GENERAL", (text,), Emotion.NORMAL, "NORMAL", 0.9, False, "t"),
        semantic_analysis=understanding,
    )


def test_a_correction_signal_cannot_make_a_held_question_answerable(
    database,
) -> None:
    """운영자가 남긴 "직원이 검토하는게 맞다"가 답변의 근거로 쓰이던 경로."""

    from services.learning_signal_service import LearningSignalService

    text = "지금 주문하면 배송 얼마나 걸릴까요?"
    LearningSignalService(database).capture(
        origin_kind="NEGATIVE_REVIEW", signal_kind="CORRECTION",
        content_text=(
            "아직 구매하지 않은 고객의 배송문의이다. 배송기간을 유추할수 "
            "없으므로 답변이 생성 되더라도 직원이 검토하는게 맞다"
        ),
        inquiry={"id": None, "store_code": "OJE_PLUS",
                 "product_name": "삼성 TV", "product_id": "p1"},
        question="구매 전 배송기간 문의", product_name="삼성 TV", product_id="p1",
    )

    context = _context(database, text, semantic(
        "DELIVERY_POLICY", purchase_state="PRE_PURCHASE", asks_schedule=True,
        atomic=[{"text": text, "action": "DELIVERY_POLICY"}],
    ))

    for item in context["subquestion_evidence"]:
        assert item["status"] == "DELIVERY_SCHEDULE_REVIEW"
        assert item["source"] == "DELIVERY_SCHEDULE_UNCONFIRMED_PURCHASE"
        assert item["answer_required"] is False


def test_an_ambiguous_schedule_question_is_held_in_the_evidence_map(
    database,
) -> None:
    context = _context(database, "배송 언제 되나요??", semantic(
        "DELIVERY_STATUS", purchase_state="UNKNOWN", asks_schedule=True,
        delivery_schedule=True, order_context=True,
    ))

    assert {item["status"] for item in context["subquestion_evidence"]} == {
        "DELIVERY_SCHEDULE_REVIEW"
    }


def test_a_confirmed_order_keeps_its_dps_evidence_path(database) -> None:
    context = _context(database, "주문한 상품 설치예정일 언제인가요?", semantic(
        "INSTALLATION_SCHEDULE", purchase_state="CURRENT_ORDER",
        asks_schedule=True, delivery_schedule=True, order_context=True,
    ))

    statuses = {item["status"] for item in context["subquestion_evidence"]}
    assert "DELIVERY_SCHEDULE_REVIEW" not in statuses


def test_a_compound_holds_only_the_schedule_part(database) -> None:
    text = "오늘 주문하면 언제 도착하나요? 그리고 A/S는 몇 년인가요?"
    context = _context(database, text, semantic(
        "DELIVERY_POLICY", purchase_state="PRE_PURCHASE", asks_schedule=True,
        secondary=["REPAIR"],
        atomic=[
            {"text": "오늘 주문하면 언제 도착하나요?", "action": "DELIVERY_POLICY"},
            {"text": "A/S는 몇 년인가요?", "action": "REPAIR"},
        ],
    ))

    rows = {item["subquestion"]: item["status"]
            for item in context["subquestion_evidence"]}
    assert rows["오늘 주문하면 언제 도착하나요?"] == "DELIVERY_SCHEDULE_REVIEW"
    assert rows["A/S는 몇 년인가요?"] != "DELIVERY_SCHEDULE_REVIEW"


# ==========================================================================
# 4. 최종 terminal state
# ==========================================================================


def test_held_schedule_question_ends_in_review_required_with_a_draft(
    tmp_path,
) -> None:
    """조회 없음, 초안 있음, 자동등록 차단.

    초안은 계속 만든다. 정책은 "자동으로 답하지 마라"이지 "직원에게 빈 화면을
    주라"가 아니다.
    """

    from repositories.answer_repository import AnswerRepository
    from services.answer_service import AnswerService
    from services.auto_processing_eligibility_service import (
        AutoProcessingEligibilityService,
    )
    from answer.engine import AnswerEngine
    from tests.test_inquiry_processing_plan import (
        FakeDps, FakeOrderLookup, ForbiddenHybrid,
    )

    value = Database(tmp_path / "e2e.db")
    value.initialize()
    inquiry_id = InquiryRepository(value).upsert_work_item({
        "store_code": "OJE_PLUS", "source_type": "NAVER",
        "source_question_id": "DELIVERY-SCHEDULE-E2E",
        "inquiry_type": "PRODUCT_INQUIRY", "title": "문의",
        "content": "오늘 주문하면 내일 받을 수 있나요?",
        "product_name": "삼성 스마트모니터 M5 32인치", "product_id": "p1",
        "raw_json": {},
    }).inquiry_id
    order = FakeOrderLookup({"success": False, "orders": []})
    dps = FakeDps()

    outcome = AnswerService(
        value, engine=AnswerEngine(), hybrid_service=ForbiddenHybrid(),
        order_lookup_service=order, dps_enrichment=dps,
    ).generate_for_inquiry(inquiry_id)
    draft = AnswerRepository(value).active_for_inquiry(inquiry_id)
    eligibility = AutoProcessingEligibilityService().evaluate(
        inquiry=InquiryRepository(value).get(inquiry_id), draft=draft,
        route=str(outcome.result.metadata.get("selected_answer_route") or ""),
    )

    assert order.calls == 0
    assert dps.calls == 0
    assert draft is not None and str(draft.get("original_answer") or "").strip()
    assert eligibility.decision == "REVIEW_REQUIRED"
    assert "POLICY_OR_HIGH_RISK_REVIEW" in eligibility.reasons


# ==========================================================================
# 5. 답변 내용에 대한 마지막 방어선
# ==========================================================================


def test_a_delivery_period_is_never_published_without_a_confirmed_order() -> None:
    """질문 분류와 무관하게, 답변이 기간을 말하면 막는다.

    실측 사례: "지금 주문하면 정상적으로 받을 수 있나요" 는 PRE_PURCHASE 로는
    맞게 이해됐지만 asks_delivery_schedule=False 로 나왔다 -- 언제가 아니라
    되는지를 묻고 있으니 그 판단 자체는 일리가 있다. 그런데 생성된 답변은
    "배송 및 설치까지 약 3~4주 소요될 예정입니다" 였다.

    질문을 어떻게 분류했든 고객에게 나가는 것은 문장이다. 주문이 없는 고객에게
    맞을 수 있는 기간 숫자는 존재하지 않는다.
    """

    from services.auto_processing_eligibility_service import (
        delivery_period_claim,
    )

    assert delivery_period_claim("배송 및 설치까지 약 3~4주 소요될 예정입니다.")
    assert delivery_period_claim("보통 1영업일에서 2영업일 정도 소요됩니다.")
    assert delivery_period_claim("오후 3시 이전 결제 주문은 당일 발송됩니다.")


def test_a_confirmed_schedule_is_not_a_period_claim() -> None:
    """DPS 가 확인해 준 날짜는 막지 않는다 -- 실재하는 날짜다."""

    from services.auto_processing_eligibility_service import (
        delivery_period_claim,
    )

    assert delivery_period_claim("확인되는 설치예정일은 8월 27일 입니다.") is None
    assert delivery_period_claim(
        "설치 일정은 설치 전날 카카오톡으로 안내됩니다."
    ) is None
    assert delivery_period_claim("배송비는 무료입니다.") is None


def test_the_gate_holds_an_unconfirmed_period_answer(database) -> None:
    from services.auto_processing_eligibility_service import (
        AutoProcessingEligibilityService,
    )

    understanding = semantic(
        "DELIVERY_POLICY", purchase_state="PRE_PURCHASE", asks_schedule=False,
    )
    plan = plan_for(database, "지금 구매하면 정상적으로 받을 수 있나요", understanding)
    assert plan.analysis.purchase_confirmed is False

    verdict = AutoProcessingEligibilityService().evaluate(
        inquiry=inquiry(database, "지금 구매하면 정상적으로 받을 수 있나요"),
        draft={
            "original_answer": "구매하시면 배송 및 설치까지 약 3~4주 소요될 예정입니다.",
            "validation_status": "PASS",
            "metadata_json": {"processing_plan": plan.to_dict()},
        },
        route="GPT_FALLBACK",
    )

    assert verdict.decision == "REVIEW_REQUIRED"
    assert "UNCONFIRMED_PURCHASE_DELIVERY_PERIOD" in verdict.reasons


def test_a_confirmed_order_may_still_state_its_real_schedule(database) -> None:
    """주문이 확인된 고객의 실제 일정 안내까지 막으면 안 된다."""

    from services.auto_processing_eligibility_service import (
        AutoProcessingEligibilityService,
    )

    understanding = semantic(
        "DELIVERY_STATUS", purchase_state="CURRENT_ORDER", asks_schedule=True,
        delivery_schedule=True, order_context=True,
    )
    plan = plan_for(
        database, "주문했는데 배송 언제 되나요?", understanding, order_id=ORDER_ID,
    )
    assert plan.analysis.purchase_confirmed is True

    verdict = AutoProcessingEligibilityService().evaluate(
        inquiry=inquiry(database, "주문했는데 배송 언제 되나요?", order_id=ORDER_ID),
        draft={
            "original_answer": "확인되는 설치예정일은 8월 27일 입니다.",
            "validation_status": "PASS",
            "metadata_json": {"processing_plan": plan.to_dict()},
        },
        route="DELIVERY_WITH_INSTALLATION_DATE",
    )

    assert "UNCONFIRMED_PURCHASE_DELIVERY_PERIOD" not in verdict.reasons


# ==========================================================================
# 구매 전 배송 문의는 "언제"보다 넓다 -- inquiry 2393
# ==========================================================================
#
# "지금 구매하면 정상적으로 받을 수 있나요" 는 날짜도 기간도 부르지 않는다.
# GPT 는 asks_delivery_schedule=false 로 옳게 답했고 -- 그 필드가 명시적으로
# "WHEN" 만 뜻하기 때문이다 -- 그 결과 앞단에서 아무것도 걸리지 않았다. GPT 가
# "약 3~4주"를 지어냈고, 완성된 답변의 문구를 읽는 게시 관문 하나가 그것을
# 세웠다. 고객에게 가지는 않았지만 마지막 한 겹에만 기대는 구조였다.
#
# 정책이 실제로 묻는 것은 더 넓다: 아직 없는 주문을 이 고객이 받게 되는가 --
# 언제, 얼마나 걸려, 어느 날까지, 혹은 받기는 하는가. ``asks_delivery_outcome``
# 이 그 관찰이고 timing 관찰의 상위집합이다.
#
# 아래 값은 실제 GPT 가 각 문장에 대해 돌려준 관찰을 그대로 옮긴 것이다.
# (scratchpad/eval/semantic_controls.json, 13/13)

# 구매 전 -- 시기·기간·도착 가능성·실현 가능성. 전부 REVIEW.
PRE_PURCHASE_OUTCOME_CASES = (
    ("지금 주문하면 배송 얼마나 걸리나요?", "DELIVERY_POLICY", True, True),
    ("구매하면 며칠 안에 받을 수 있나요?", "DELIVERY_POLICY", True, True),
    ("이번 주 금요일까지 받을 수 있을까요? 아직 주문 전입니다.",
     "DELIVERY_DEADLINE_CONFIRMATION", True, True),
    ("주문하면 바로 배송되나요?", "DELIVERY_POLICY", True, True),
    # 아래 둘은 timing 관찰만으로는 잡히지 않는다. 새 관찰이 유일한 근거다.
    ("빨리 받고 싶은데 가능한가요? 구매 예정입니다.", "DELIVERY_POLICY", False, True),
    ("지금 구매하면 정상적으로 받을 수 있나요", "DELIVERY_POLICY", False, True),
)

# 구매 전이지만 절차·비용을 묻는다. 막으면 안 된다.
PROCEDURE_OUTCOME_CASES = (
    ("배송과 설치는 어떤 방식으로 진행되나요?", "DELIVERY_POLICY"),
    ("설치는 기사님이 해주시나요?", "INSTALLATION_METHOD"),
    ("배송 후 설치 과정이 어떻게 되나요?", "INSTALLATION_METHOD"),
    ("배송비는 얼마인가요?", "DELIVERY_POLICY"),
)


@pytest.mark.parametrize(
    "text,action,asks_schedule,asks_outcome", PRE_PURCHASE_OUTCOME_CASES,
)
def test_asking_whether_you_receive_it_is_held_like_asking_when(
    text: str, action: str, asks_schedule: bool, asks_outcome: bool,
) -> None:
    understanding = semantic(
        action, purchase_state="PRE_PURCHASE",
        asks_schedule=asks_schedule, asks_outcome=asks_outcome,
        atomic=[{"text": text, "action": action}],
    )

    assert delivery_schedule_question(understanding) is True, text
    assert delivery_schedule_needs_review(understanding) is True, text


@pytest.mark.parametrize("text,action", PROCEDURE_OUTCOME_CASES)
def test_asking_how_it_is_done_is_not_asking_whether_you_get_it(
    text: str, action: str,
) -> None:
    understanding = semantic(
        action, purchase_state="PRE_PURCHASE",
        asks_schedule=False, asks_outcome=False,
        atomic=[{"text": text, "action": action}],
    )

    assert delivery_schedule_question(understanding) is False, text
    assert delivery_schedule_needs_review(understanding) is False, text


@pytest.mark.parametrize(
    "text,action,asks_schedule,asks_outcome", PRE_PURCHASE_OUTCOME_CASES,
)
def test_the_plan_holds_it_before_anything_is_generated(
    database, text: str, action: str, asks_schedule: bool, asks_outcome: bool,
) -> None:
    """마지막 문구 검사에 닿기 전에 계획이 이미 옳아야 한다."""

    plan = plan_for(database, text, semantic(
        action, purchase_state="PRE_PURCHASE",
        asks_schedule=asks_schedule, asks_outcome=asks_outcome,
        atomic=[{"text": text, "action": action}],
    ))

    assert plan.analysis.manual_review_required is True, text
    assert DELIVERY_SCHEDULE_REVIEW_SOURCE in plan.analysis.manual_review_sources
    assert plan.analysis.requires_order_id is False
    assert plan.requires_order_lookup is False
    assert plan.requires_dps_lookup is False
    # 초안은 계속 쓰인다 -- 직원이 빈 화면을 열면 안 된다.
    assert plan.can_generate_draft is True


@pytest.mark.parametrize("text,action", PROCEDURE_OUTCOME_CASES)
def test_the_plan_does_not_hold_a_procedure_question(
    database, text: str, action: str,
) -> None:
    plan = plan_for(database, text, semantic(
        action, purchase_state="PRE_PURCHASE",
        asks_schedule=False, asks_outcome=False,
        atomic=[{"text": text, "action": action}],
    ))

    assert DELIVERY_SCHEDULE_REVIEW_SOURCE not in plan.analysis.manual_review_sources


def test_a_confirmed_order_is_still_answered_from_its_own_record(
    database,
) -> None:
    """대조군 E -- 주문이 있는 고객의 일정 문의는 이 정책이 건드리지 않는다."""

    understanding = semantic(
        "DELIVERY_STATUS", purchase_state="CURRENT_ORDER",
        asks_schedule=True, asks_outcome=True, delivery_schedule=True,
        atomic=[{"text": "어제 주문했는데 배송 언제 오나요?",
                 "action": "DELIVERY_STATUS"}],
    )

    assert delivery_schedule_needs_review(understanding) is False

    plan = plan_for(database, "어제 주문했는데 배송 언제 오나요?", understanding)
    assert DELIVERY_SCHEDULE_REVIEW_SOURCE not in plan.analysis.manual_review_sources


def test_an_unknown_purchase_state_is_held_either_way(database) -> None:
    """대조군 F -- 주문 여부가 문의에 없으면 그것도 REVIEW 다."""

    understanding = semantic(
        "DELIVERY_STATUS", purchase_state="UNKNOWN",
        asks_schedule=True, asks_outcome=True,
        atomic=[{"text": "배송 언제 되나요??", "action": "DELIVERY_STATUS"}],
    )

    assert delivery_schedule_needs_review(understanding) is True

    plan = plan_for(database, "배송 언제 되나요??", understanding)
    assert plan.analysis.requires_order_id is False
    assert plan.requires_order_lookup is False
    assert plan.requires_dps_lookup is False


# --- 두 관찰이 어긋날 수 없다 ----------------------------------------------


def test_asking_when_is_one_way_of_asking_whether_you_get_it() -> None:
    """timing 이 참인데 outcome 이 거짓인 이해는 만들어질 수 없다."""

    understanding = semantic(
        "DELIVERY_POLICY", purchase_state="PRE_PURCHASE",
        asks_schedule=True, asks_outcome=False,
    )

    assert understanding.asks_delivery_outcome is True


def test_an_older_provider_that_reports_neither_behaves_as_before() -> None:
    """새 필드를 모르는 이해는 예전 그대로 동작한다."""

    silent = semantic(
        "DELIVERY_POLICY", purchase_state="PRE_PURCHASE", asks_schedule=False,
    )
    timing_only = semantic(
        "DELIVERY_POLICY", purchase_state="PRE_PURCHASE", asks_schedule=True,
    )

    assert silent.asks_delivery_outcome is False
    assert delivery_schedule_needs_review(silent) is False
    assert timing_only.asks_delivery_outcome is True
    assert delivery_schedule_needs_review(timing_only) is True


@pytest.mark.parametrize("text", (
    "상품 배송은 언제 받을 수 있나요?",
    "희망 날짜에 배송받을 수 있나요?",
))
def test_deterministic_unconfirmed_delivery_outcome_fails_closed(text: str) -> None:
    """Provider/config fallback must never promote an unknown purchase."""

    analysis = InquiryAnalysisService().analyze(
        AnswerRequest(question=text)
    )

    assert analysis.inquiry_subtype in {
        "UNCONFIRMED_DELIVERY_OUTCOME",
        "PRE_PURCHASE_DELIVERY_GUIDANCE",
    }
    assert analysis.requires_order_id is False
    assert analysis.requires_order_lookup is False
    assert analysis.requires_dps_lookup is False
    assert analysis.manual_review_required is True


def test_deterministic_current_order_and_procedure_keep_their_paths() -> None:
    service = InquiryAnalysisService()
    current = service.analyze(
        AnswerRequest(question="어제 주문했는데 배송 일정이 언제인가요?")
    )
    procedure = service.analyze(
        AnswerRequest(question="배송과 설치는 어떤 절차로 진행되나요?")
    )

    assert current.requires_order_id is True
    assert current.requires_order_lookup is True
    assert current.requires_dps_lookup is True
    assert current.manual_review_required is False
    assert procedure.requires_order_lookup is False
    assert procedure.requires_dps_lookup is False
    assert procedure.manual_review_required is False


def test_provider_unavailable_uses_the_same_fail_closed_delivery_fallback(
    database: Database,
) -> None:
    plan = plan_for(
        database,
        "상품 배송은 언제 받을 수 있나요?",
        unavailable("PROVIDER_ERROR"),
    )

    assert plan.analysis.requires_order_id is False
    assert plan.requires_order_lookup is False
    assert plan.requires_dps_lookup is False
    assert plan.analysis.manual_review_required is True
    assert plan.selected_answer_route != "ORDER_ID_REQUEST"
