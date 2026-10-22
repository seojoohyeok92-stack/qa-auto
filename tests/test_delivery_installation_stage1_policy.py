"""배송·설치 문의 1단계 정책: 일반 문의는 자동등록, DPS 필요 문의는 차단.

두 가지를 분리해서 본다.

  일반 배송·설치 문의   Learning / Product Knowledge 근거로 답할 수 있다. 주문도
                        DPS 도 필요 없다. 근거가 있으면 기존 validator·근거
                        검증·유보표현 요구를 그대로 통과한 뒤 자동등록된다.
  주문별 조회 문의      이 고객의 주문을 DPS 로 조회해야 답이 나온다. 조회는
                        그대로 돌고 날짜는 초안까지 들어오지만, 이번 단계에서는
                        사람이 보낸다.

앞선 조사에서 확인된 세 결함도 이 정책 안에서 함께 고친다.

  D-1  "설치까지 얼마나 걸리나요?" 가 배송 일정 보류에 걸렸다. 보류를 세우는 두
       계층이 문의 단위 술어를 쓰고 있었고, 원자 단위 제한은 검색 계층에만
       있었다.
  D-2  일반 배송+설치 복합문의가 불필요하게 전체 차단됐다. 같은 원인이다.
  D-3  "제 상품 설치예정일이 언제인가요?" 가 주문번호를 묻지 않았다. 키워드
       계층이 fail-closed 로 GENERAL_GUIDANCE 를 돌려주는데, 그 전제(주문 존재
       여부를 문장이 말해주지 않는다)를 이해 계층이 이미 해결해 두고 있었다.
"""
from __future__ import annotations

import itertools

import pytest

from answer.inquiry_analysis import AnswerStrategy
from answer.models import AnswerRequest
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.auto_processing_eligibility_service import (
    DPS_REQUIREMENT_INCONSISTENT,
    DPS_SCHEDULE_AUTO_POST_NOT_ENABLED,
    EVIDENCE_NOT_SUFFICIENT,
    AutoProcessingEligibilityService,
    _evidence_insufficient,
    _lead_time_subquestions,
)
from services.inquiry_analysis_service import InquiryAnalysisService
from services.inquiry_processing_plan_service import InquiryProcessingPlanService
from services.semantic_analysis import parse

_key = itertools.count()
ORDER_ID = "2026070448206811"

# 근거가 충분한 배송기간 답변. 유보표현을 유지한다 -- 일반 리드타임은 이
# 고객의 일정이 아니므로 단정하면 안 되고, 그 요구는 이번 단계에서 그대로
# 남는다(아래 ``test_02b`` 가 고정한다).
DELIVERY_ANSWER = (
    "안녕하세요. 해당 상품은 보통 주문 후 3~4주 정도 소요되는 편입니다.\n"
    "주문이 확인되면 정확한 일정을 다시 안내드리겠습니다.\n감사합니다."
)
INSTALL_ANSWER = (
    "안녕하세요. 설치는 전문 기사님이 방문하여 진행해 드립니다.\n"
    "벽걸이 설치도 가능하며 현장에서 상태를 확인한 뒤 진행합니다.\n감사합니다."
)
BOTH_ANSWER = (
    "안녕하세요. 배송은 보통 주문 후 3~4주 정도 소요되는 편입니다.\n"
    "설치는 전문 기사님이 방문하여 진행해 드립니다.\n감사합니다."
)
ORDER_REQUEST_ANSWER = (
    "안녕하세요. 주문 일정을 확인해 드리기 위해 주문번호가 필요합니다.\n"
    "주문번호를 알려주시면 바로 확인해 안내드리겠습니다.\n감사합니다."
)

# 검색 계층이 한 sub-question 에 대해 돌려주는 판정. 이 테스트가 바꾸는 유일한
# 노브다 -- "근거가 있다 / 없다" 를 여기서만 표현한다.
LEAD_TIME = {"source": "GENERAL_DELIVERY_ESTIMATE", "status": "ANSWERABLE"}
DELIVERY_HELD = {
    "source": "PRE_PURCHASE_DELIVERY_GUIDANCE",
    "status": "DELIVERY_SCHEDULE_REVIEW",
}
INSTALL_FACT = {"source": "PRODUCT_KNOWLEDGE", "status": "ANSWERABLE"}


@pytest.fixture
def database(tmp_path) -> Database:
    value = Database(tmp_path / "stage1.db")
    value.initialize()
    return value


def semantic(
    primary: str,
    *,
    atomic: list[dict[str, str]],
    purchase_state: str = "UNKNOWN",
    asks_schedule: bool = False,
    secondary: list[str] | None = None,
    delivery_schedule: bool = False,
):
    """GPT ① 이 돌려준 이해. atomic 은 항상 명시한다 -- 1단계의 쟁점이다."""

    return parse({
        "primary_action": primary,
        "secondary_actions": secondary or [],
        "request_type": "QUESTION",
        "objects": [],
        "atomic_questions": atomic,
        "deadline": None,
        "constraints": [],
        "negation": False,
        "conditional": False,
        "requires_order_context": False,
        "requires_delivery_schedule": delivery_schedule,
        "purchase_state": purchase_state,
        "asks_delivery_schedule": asks_schedule,
        "confidence": 0.95,
    })


def inquiry(
    database: Database,
    content: str,
    *,
    order_id: str | None = None,
    store: str = "OJE_PLUS",
    source_type: str = "NAVER",
) -> dict:
    inquiries = InquiryRepository(database)
    inquiry_id = inquiries.upsert_work_item({
        "store_code": store, "source_type": source_type,
        "source_question_id": f"s1-{next(_key)}",
        "inquiry_type": "PRODUCT_INQUIRY",
        "title": "문의", "content": content, "product_name": "삼성 TV",
        "product_id": "p1", "order_id": order_id, "product_order_id": None,
        "raw_json": {},
    }).inquiry_id
    return inquiries.get(inquiry_id)


def analysis_for(content: str, understanding, *, order_id: str = ""):
    return InquiryAnalysisService().analyze(
        AnswerRequest(
            inquiry_id=1, store_code="OJE_PLUS", question=content,
            order_id=order_id, product_name="삼성 TV", metadata={},
        ),
        semantic=understanding,
    )


def plan_for(
    database: Database,
    content: str,
    understanding,
    *,
    order_id: str | None = None,
    store: str = "OJE_PLUS",
    source_type: str = "NAVER",
):
    return InquiryProcessingPlanService(database).create(
        inquiry(
            database, content, order_id=order_id,
            store=store, source_type=source_type,
        ),
        semantic_analysis=understanding,
    )


def verdict(
    plan,
    *,
    answer: str,
    evidence: list[dict],
    understanding,
    store: str = "OJE_PLUS",
    route: str | None = None,
):
    """실제 자동등록 게이트의 판정. plan 은 실제 Plan 서비스가 만든 것이다."""

    plan_value = plan.to_dict()
    # 검색은 sub-question 마다 그 질문을 키로 한 행을 남긴다
    # (learning_context_service.py:1904). 이 fixture 의 행들은 질문 순서대로
    # 주어지므로, 같은 순서의 atom 문장을 키로 채워 production 모양을 맞춘다.
    atoms = [item.text for item in understanding.atomic_questions]
    evidence = [
        item if "subquestion" in item else {
            **item,
            "subquestion": atoms[index] if index < len(atoms) else "",
        }
        for index, item in enumerate(evidence)
    ]
    draft = {
        "original_answer": answer,
        "validation_status": "PASS",
        "validator_result_json": {"passed": True, "status": "PASS"},
        "review_status": "NONE",
        "metadata_json": {
            "processing_plan": plan_value,
            "hybrid": {
                "answer_pipeline": "GPT_UNDERSTAND_RETRIEVE_ANSWER",
                "subquestion_evidence": evidence,
                "draft": {"unresolved": [], "can_auto_post": True},
            },
            "semantic_routing": {
                "understanding": {
                    "usable": True,
                    "questions": [
                        {"text": item.text, "action": item.action,
                         "requested_attribute": item.requested_attribute}
                        for item in understanding.atomic_questions
                    ],
                },
            },
        },
    }
    return AutoProcessingEligibilityService().evaluate(
        inquiry={
            "id": 1, "store_code": store, "source_answered": 0,
            "post_status": "PENDING",
        },
        draft=draft,
        route=route or plan_value["selected_answer_route"],
    )


# ---------------------------------------------------------------------------
# A. 일반 배송·설치 문의 (DPS 불필요)
# ---------------------------------------------------------------------------

GENERAL_DELIVERY = "지금 주문하면 배송까지 얼마나 걸리나요?"
GENERAL_DELIVERY_UNDERSTANDING = dict(
    primary="DELIVERY_POLICY", purchase_state="PRE_PURCHASE",
    asks_schedule=True,
    atomic=[{"text": "배송까지 얼마나 걸리나요", "action": "DELIVERY_POLICY",
             "requested_attribute": "TIMING"}],
)


def test_01_general_lead_time_with_evidence_auto_posts(database):
    """1. 일반 배송기간 + 충분한 근거 → 자동등록 가능."""

    understanding = semantic(**GENERAL_DELIVERY_UNDERSTANDING)
    plan = plan_for(database, GENERAL_DELIVERY, understanding)
    assert plan.requires_dps_lookup is False
    assert verdict(
        plan, answer=DELIVERY_ANSWER, evidence=[LEAD_TIME],
        understanding=understanding,
    ).decision == "SAFE"


def test_02_general_lead_time_without_evidence_is_held(database):
    """2. 일반 배송기간 + 근거 없음 → 자동등록 차단."""

    understanding = semantic(**GENERAL_DELIVERY_UNDERSTANDING)
    plan = plan_for(database, GENERAL_DELIVERY, understanding)
    assert plan.workflow_block_reasons == ("PRE_PURCHASE_DELIVERY_UNRESOLVED",)
    result = verdict(
        plan, answer=DELIVERY_ANSWER, evidence=[DELIVERY_HELD],
        understanding=understanding,
    )
    assert result.decision == "REVIEW_REQUIRED"
    assert "PRE_PURCHASE_DELIVERY_UNRESOLVED" in result.reasons


def test_02b_general_lead_time_must_stay_hedged(database):
    """근거가 있어도 단정하면 막힌다 -- 기존 유보표현 요구를 유지한다."""

    understanding = semantic(**GENERAL_DELIVERY_UNDERSTANDING)
    plan = plan_for(database, GENERAL_DELIVERY, understanding)
    result = verdict(
        plan, answer="안녕하세요. 주문 후 3~4주 소요됩니다.\n감사합니다.",
        evidence=[LEAD_TIME], understanding=understanding,
    )
    assert result.decision == "REVIEW_REQUIRED"
    assert "GENERAL_ESTIMATE_NOT_HEDGED" in result.reasons


GENERAL_INSTALL = "설치까지 얼마나 걸리나요?"
GENERAL_INSTALL_UNDERSTANDING = dict(
    primary="INSTALLATION_METHOD", asks_schedule=True,
    atomic=[{"text": "설치까지 얼마나 걸리나요",
             "action": "INSTALLATION_METHOD"}],
)


def test_03_general_installation_with_evidence_auto_posts(database):
    """3. 일반 설치 안내 + 근거 있음 → 자동등록 가능. (D-1)

    ``asks_delivery_schedule`` 이 켜져 있어도 물어본 것은 설치 방법이다. 이
    문의에는 배송 질문이 없으므로 배송 일정 보류가 붙을 자리가 없다.
    """

    understanding = semantic(**GENERAL_INSTALL_UNDERSTANDING)
    analysis = analysis_for(GENERAL_INSTALL, understanding)
    assert analysis.manual_review_required is False
    assert "PRE_PURCHASE_DELIVERY_GUIDANCE" not in analysis.manual_review_sources
    plan = plan_for(database, GENERAL_INSTALL, understanding)
    assert plan.workflow_block_reasons == ()
    assert plan.requires_dps_lookup is False
    assert verdict(
        plan, answer=INSTALL_ANSWER, evidence=[INSTALL_FACT],
        understanding=understanding,
    ).decision == "SAFE"


# ---------------------------------------------------------------------------
# C. 복합문의
# ---------------------------------------------------------------------------

COMPOUND = "지금 주문하면 배송까지 얼마나 걸리고 설치도 해주시나요?"
COMPOUND_UNDERSTANDING = dict(
    primary="DELIVERY_POLICY", secondary=["INSTALLATION_METHOD"],
    purchase_state="PRE_PURCHASE", asks_schedule=True,
    atomic=[
        {"text": "배송까지 얼마나 걸리나요", "action": "DELIVERY_POLICY",
         "requested_attribute": "TIMING"},
        {"text": "설치도 해주시나요", "action": "INSTALLATION_METHOD"},
    ],
)


def test_04_compound_general_with_full_evidence_auto_posts(database):
    """4. 일반 배송+설치 복합문의 + 전체 근거 있음 → 자동등록 가능."""

    understanding = semantic(**COMPOUND_UNDERSTANDING)
    plan = plan_for(database, COMPOUND, understanding)
    assert plan.requires_dps_lookup is False
    assert verdict(
        plan, answer=BOTH_ANSWER, evidence=[LEAD_TIME, INSTALL_FACT],
        understanding=understanding,
    ).decision == "SAFE"


def test_05_compound_with_one_gap_keeps_review(database):
    """5. 복합문의 일부 근거 부족 → 기존 검토 정책.

    설치 쪽 근거가 멀쩡해도 배송 atom 이 보류면 문의 전체가 멈춘다. 근거 하나가
    다른 atom 을 대신하지 못한다는 기존 규칙이고, 1단계는 그것을 건드리지 않는다.
    """

    understanding = semantic(**COMPOUND_UNDERSTANDING)
    plan = plan_for(database, COMPOUND, understanding)
    result = verdict(
        plan, answer=BOTH_ANSWER, evidence=[DELIVERY_HELD, INSTALL_FACT],
        understanding=understanding,
    )
    assert result.decision == "REVIEW_REQUIRED"
    assert "PRE_PURCHASE_DELIVERY_UNRESOLVED" in result.reasons


def test_05b_compound_without_any_delivery_atom_is_not_held(database):
    """배송 atom 이 없는 복합문의는 배송 보류에 걸리지 않는다. (D-2)"""

    understanding = semantic(
        primary="INSTALLATION_METHOD", secondary=["PRODUCT_SPEC"],
        asks_schedule=True,
        atomic=[
            {"text": "벽걸이 설치 가능한가요", "action": "INSTALLATION_METHOD"},
            {"text": "스피커 내장인가요", "action": "PRODUCT_SPEC"},
        ],
    )
    content = "벽걸이 설치 가능한가요? 스피커는 내장인가요?"
    analysis = analysis_for(content, understanding)
    # 이 문의에는 키워드 계층이 규칙을 못 찾은 별개의 보류(UNCLASSIFIED)가
    # 그대로 남아 있다 -- 자동등록 게이트가 이미 soft 로 처리하는 분류 공백이고
    # 1단계에서 건드리지 않는다. 사라져야 하는 것은 배송 보류 쪽이다.
    assert "PRE_PURCHASE_DELIVERY_GUIDANCE" not in analysis.manual_review_sources
    assert (
        "DELIVERY_SCHEDULE_UNCONFIRMED_PURCHASE"
        not in analysis.manual_review_sources
    )
    plan = plan_for(database, content, understanding)
    assert plan.workflow_block_reasons == ()
    assert verdict(
        plan, answer=INSTALL_ANSWER,
        evidence=[INSTALL_FACT, INSTALL_FACT], understanding=understanding,
    ).decision == "SAFE"


# ---------------------------------------------------------------------------
# B. 주문별 배송·설치 조회 문의 (DPS 필요)
# ---------------------------------------------------------------------------

ORDER_INSTALL = "제 상품 설치예정일이 언제인가요?"
ORDER_INSTALL_UNDERSTANDING = dict(
    primary="INSTALLATION_SCHEDULE", purchase_state="CURRENT_ORDER",
    asks_schedule=True, delivery_schedule=True,
    atomic=[{"text": "설치예정일이 언제인가요",
             "action": "INSTALLATION_SCHEDULE",
             "requested_attribute": "TIMING"}],
)


def test_06_order_specific_without_order_id_requests_it_and_is_held(database):
    """6. 주문별 문의 + 주문번호 없음 → 주문번호 요청 초안, 자동등록 차단. (D-3)"""

    understanding = semantic(**ORDER_INSTALL_UNDERSTANDING)
    analysis = analysis_for(ORDER_INSTALL, understanding)
    assert analysis.answer_strategy is AnswerStrategy.REQUEST_ORDER_ID
    assert analysis.manual_review_required is False
    plan = plan_for(database, ORDER_INSTALL, understanding)
    assert plan.requires_dps_lookup is True
    assert plan.selected_answer_route == "ORDER_ID_REQUEST"
    assert plan.can_generate_draft is True
    result = verdict(
        plan, answer=ORDER_REQUEST_ANSWER, evidence=[],
        understanding=understanding,
    )
    assert result.decision == "REVIEW_REQUIRED"
    assert DPS_SCHEDULE_AUTO_POST_NOT_ENABLED in result.reasons


def test_07_order_specific_with_valid_order_id_is_held(database):
    """7. 주문별 문의 + 유효한 주문번호 → DPS 대상 유지, 자동등록 차단."""

    understanding = semantic(**ORDER_INSTALL_UNDERSTANDING)
    plan = plan_for(database, ORDER_INSTALL, understanding, order_id=ORDER_ID)
    assert plan.requires_dps_lookup is True
    assert plan.order_id_status == "VALID"
    result = verdict(
        plan, answer="안녕하세요. 설치 예정일을 확인해 안내드리겠습니다.",
        evidence=[], understanding=understanding,
    )
    assert result.decision == "REVIEW_REQUIRED"
    assert DPS_SCHEDULE_AUTO_POST_NOT_ENABLED in result.reasons


def test_07b_dps_block_survives_a_successful_lookup(database):
    """조회가 성공해도 막힌다 -- DPS 기반 자동등록은 1단계에서 열지 않는다."""

    understanding = semantic(**ORDER_INSTALL_UNDERSTANDING)
    plan = plan_for(database, ORDER_INSTALL, understanding, order_id=ORDER_ID)
    value = plan.to_dict()
    value["dps_lookup_status"] = "SUCCESS"
    value["valid_dps_snapshot_available"] = True
    value["order_lookup_status"] = "SUCCESS"

    class _Plan:
        def to_dict(self):
            return value

    result = verdict(
        _Plan(), answer="안녕하세요. 설치 예정일은 확인 후 안내드리겠습니다.",
        evidence=[], understanding=understanding, route="GPT_DIRECT",
    )
    assert result.decision == "REVIEW_REQUIRED"
    assert DPS_SCHEDULE_AUTO_POST_NOT_ENABLED in result.reasons


def test_07c_plan_and_analysis_disagreement_is_reported(database):
    """Plan 과 분석의 DPS 필요 판정이 어긋나면 안전하게 검토로 보낸다."""

    understanding = semantic(**GENERAL_DELIVERY_UNDERSTANDING)
    plan = plan_for(database, GENERAL_DELIVERY, understanding)
    value = plan.to_dict()
    value["analysis"]["requires_dps_lookup"] = True

    class _Plan:
        def to_dict(self):
            return value

    result = verdict(
        _Plan(), answer=DELIVERY_ANSWER, evidence=[LEAD_TIME],
        understanding=understanding, route="GPT_DIRECT",
    )
    assert result.decision == "REVIEW_REQUIRED"
    assert DPS_REQUIREMENT_INCONSISTENT in result.reasons
    assert DPS_SCHEDULE_AUTO_POST_NOT_ENABLED in result.reasons


# ---------------------------------------------------------------------------
# 8~10. 분류가 번지지 않는지, 기존 경로가 그대로인지
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("content, primary, asks_schedule", [
    ("지금 주문하면 배송까지 얼마나 걸리나요?", "DELIVERY_POLICY", True),
    ("보통 배송기간이 얼마나 걸리나요?", "DELIVERY_POLICY", True),
    ("배송비는 얼마인가요?", "DELIVERY_POLICY", False),
    ("기사님이 직접 설치하시나요?", "INSTALLATION_METHOD", False),
    ("설치까지 보통 며칠 걸리나요?", "INSTALLATION_METHOD", True),
    ("벽걸이 설치가 가능한가요?", "INSTALLATION_METHOD", False),
])
def test_08_general_questions_are_not_dps_work(
    database, content, primary, asks_schedule,
):
    """8. 일반 문의가 DPS 대상으로 분류되지 않는다."""

    understanding = semantic(
        primary=primary, asks_schedule=asks_schedule,
        atomic=[{"text": content, "action": primary}],
    )
    plan = plan_for(database, content, understanding)
    assert plan.requires_dps_lookup is False
    assert plan.analysis.requires_dps_lookup is False
    assert plan.requires_order_lookup is False


def test_09_unrelated_naver_inquiry_still_auto_posts(database):
    """9. 배송·설치와 무관한 네이버 문의의 자동등록은 그대로다."""

    understanding = semantic(
        primary="PRODUCT_SPEC",
        atomic=[{"text": "스피커 내장인가요", "action": "PRODUCT_SPEC"}],
    )
    content = "스피커 내장인가요?"
    plan = plan_for(database, content, understanding)
    assert plan.requires_dps_lookup is False
    assert verdict(
        plan, answer="안녕하세요. 해당 모델은 스피커가 내장되어 있습니다.",
        evidence=[INSTALL_FACT], understanding=understanding,
    ).decision == "SAFE"


def test_10_coupang_delivery_inquiry_gains_no_new_reason(database):
    """10. 쿠팡 운영 경로에는 새 사유가 붙지 않는다.

    쿠팡에는 DPS 가 없다. ``requires_dps_lookup`` 은 실행할 수 없는 요구를
    가리키고, 그런 문의는 기존 disabled/untrusted 사유가 이미 잡고 있다. 1단계의
    새 사유를 거기에 더하면 잘못된 단계를 가리키는 이름이 하나 늘어날 뿐이다.
    """

    understanding = semantic(**ORDER_INSTALL_UNDERSTANDING)
    plan = plan_for(
        database, ORDER_INSTALL, understanding, order_id=ORDER_ID,
        store="COUPANG_OJE_NS", source_type="COUPANG_ONLINE",
    )
    result = verdict(
        plan, answer="안녕하세요. 설치 예정일을 확인해 안내드리겠습니다.",
        evidence=[], understanding=understanding, store="COUPANG_OJE_NS",
    )
    assert DPS_SCHEDULE_AUTO_POST_NOT_ENABLED not in result.reasons
    assert DPS_REQUIREMENT_INCONSISTENT not in result.reasons


def test_10b_coupang_general_inquiry_still_auto_posts(database):
    """쿠팡 일반 문의의 자동등록도 그대로다."""

    understanding = semantic(
        primary="INSTALLATION_METHOD",
        atomic=[{"text": "벽걸이 설치 가능한가요",
                 "action": "INSTALLATION_METHOD"}],
    )
    content = "벽걸이 설치 가능한가요?"
    plan = plan_for(
        database, content, understanding,
        store="COUPANG_OJE_NS", source_type="COUPANG_ONLINE",
    )
    assert verdict(
        plan, answer=INSTALL_ANSWER, evidence=[INSTALL_FACT],
        understanding=understanding, store="COUPANG_OJE_NS",
    ).decision == "SAFE"


# ---------------------------------------------------------------------------
# 11~14. 소요기간 질문의 근거 없음 (배포 전 검증에서 나온 1-C)
# ---------------------------------------------------------------------------

NO_SOURCE = {"source": None, "status": "NO_RELIABLE_SOURCE"}
LEAD_TIME_Q = "설치까지 보통 며칠 걸리나요?"


def lead_time_understanding(attribute="TIMING",
                            action="INSTALLATION_METHOD", text=LEAD_TIME_Q):
    return semantic(
        primary=action, asks_schedule=True,
        atomic=[{"text": text, "action": action,
                 "requested_attribute": attribute}],
    )


def test_11_lead_time_without_a_source_is_held(database):
    """근거가 없으면 GPT ② 가 답했다고 보고해도 자동등록하지 않는다.

    측정된 결함: 검색은 NO_RELIABLE_SOURCE 를 기록했고, GPT ② 는 그 atom 을
    answered 로 보고했고, 답변은 기간을 말하지 않았고, validator 는 통과했고,
    자동등록이 허용됐다. GPT ② 의 자기보고는 판정 대상인 답변을 쓴 당사자의
    말이고, 검색이 기록한 출처 판정은 그와 독립적이다.
    """

    understanding = lead_time_understanding()
    plan = plan_for(database, LEAD_TIME_Q, understanding)
    result = verdict(
        plan, answer="안녕하세요. 설치 소요기간은 확인 후 안내드리겠습니다.",
        evidence=[{**NO_SOURCE, "subquestion": LEAD_TIME_Q}],
        understanding=understanding,
    )
    assert result.decision == "REVIEW_REQUIRED"
    assert EVIDENCE_NOT_SUFFICIENT in result.reasons


def test_12_lead_time_with_a_source_still_auto_posts(database):
    """근거가 있으면 기존 SAFE 판정이 그대로다."""

    understanding = lead_time_understanding()
    plan = plan_for(database, LEAD_TIME_Q, understanding)
    assert verdict(
        plan, answer=INSTALL_ANSWER,
        evidence=[{**INSTALL_FACT, "subquestion": LEAD_TIME_Q}],
        understanding=understanding,
    ).decision == "SAFE"


@pytest.mark.parametrize("attribute, action, question", [
    ("AMOUNT_OR_COST", "DELIVERY_POLICY", "배송비는 얼마인가요?"),
    ("EXISTENCE_OR_CAPABILITY", "INSTALLATION_METHOD", "벽걸이 설치가 가능한가요?"),
    ("METHOD_OR_PROCEDURE", "INSTALLATION_METHOD", "설치는 어떻게 진행되나요?"),
])
def test_13_other_attributes_keep_their_policy(
    database, attribute, action, question,
):
    """다른 종류의 질문에는 이 근거 차단을 적용하지 않는다.

    같은 action 을 공유하지만 묻는 것이 기간이 아니다. 배송비는 고정 정책에서,
    설치 가능 여부는 카탈로그에서 답한다 -- 애초에 필요하지 않았던 Learning
    행이 없다는 이유로 붙잡으면 안 된다.
    """

    understanding = lead_time_understanding(
        attribute=attribute, action=action, text=question)
    plan = plan_for(database, question, understanding)
    result = verdict(
        plan, answer="안녕하세요. 확인한 내용으로 안내드립니다.",
        evidence=[{**NO_SOURCE, "subquestion": question}],
        understanding=understanding,
    )
    assert EVIDENCE_NOT_SUFFICIENT not in result.reasons


@pytest.mark.parametrize("route", [
    "ORDER_ID_REQUEST", "DELIVERY_WITH_INSTALLATION_DATE",
])
def test_14_settled_routes_keep_their_policy(database, route):
    """주문번호 요청과 DPS 날짜 route 는 검색 이전에 정해진 답변이다."""

    understanding = lead_time_understanding()
    plan = plan_for(database, LEAD_TIME_Q, understanding)
    result = verdict(
        plan, answer="안녕하세요. 주문번호를 알려주시면 확인해 안내드리겠습니다.",
        evidence=[{**NO_SOURCE, "subquestion": LEAD_TIME_Q}],
        understanding=understanding, route=route,
    )
    assert EVIDENCE_NOT_SUFFICIENT not in result.reasons


# ---------------------------------------------------------------------------
# 15~18. 소요기간 근거 게이트의 두 경계
# ---------------------------------------------------------------------------

def _draft_metadata(questions, entries, *, pipeline=True):
    hybrid = {}
    if pipeline:
        hybrid["answer_pipeline"] = "GPT_UNDERSTAND_RETRIEVE_ANSWER"
    if entries is not None:
        hybrid["subquestion_evidence"] = list(entries)
    return {
        "hybrid": hybrid,
        "semantic_routing": {
            "understanding": {"usable": True, "questions": list(questions)},
        },
    }


LEAD_TIME_ATOM = {
    "text": LEAD_TIME_Q, "action": "INSTALLATION_METHOD",
    "requested_attribute": "TIMING",
}


@pytest.mark.parametrize("attribute", [None, "", "UNKNOWN"])
def test_15_an_unreported_attribute_falls_back_to_the_wording(
    database, attribute,
):
    """GPT ① 가 속성을 보고하지 않아도 소요기간 질문은 범위에 들어온다.

    ``requested_attribute`` 는 추가된 필드이고 ``parse()`` 는 모르는 값을
    UNKNOWN 으로 저장한다. 측정된 경계: 같은 질문에서 속성만 빠지면 범위가
    비고, NO_RELIABLE_SOURCE 를 깔고 자동등록됐다.
    """

    atom = {"text": LEAD_TIME_Q, "action": "INSTALLATION_METHOD"}
    if attribute is not None:
        atom["requested_attribute"] = attribute
    metadata = _draft_metadata(
        [atom], [{**NO_SOURCE, "subquestion": LEAD_TIME_Q}])
    assert _lead_time_subquestions(metadata) == frozenset({LEAD_TIME_Q})
    assert _evidence_insufficient(metadata, route="GPT_DIRECT") is True


@pytest.mark.parametrize("attribute, question", [
    ("AMOUNT_OR_COST", "배송비는 얼마나 되나요?"),
    ("EXISTENCE_OR_CAPABILITY", "설치 서비스가 언제든 가능한가요?"),
])
def test_16_a_reported_attribute_is_believed(database, attribute, question):
    """보고된 속성은 믿는다 -- 문장에 '얼마나'·'언제' 가 있어도 범위 밖이다.

    보조 판정은 UNKNOWN 일 때만 쓴다. 그렇지 않으면 같은 어휘를 공유하는
    비용·가능여부 질문까지 근거 검증이 번진다.
    """

    metadata = _draft_metadata(
        [{"text": question, "action": "DELIVERY_POLICY",
          "requested_attribute": attribute}],
        [{**NO_SOURCE, "subquestion": question}])
    assert _lead_time_subquestions(metadata) == frozenset()
    assert _evidence_insufficient(metadata, route="GPT_DIRECT") is False


@pytest.mark.parametrize("entries, expected", [
    ([{"subquestion": LEAD_TIME_Q, "status": "NO_RELIABLE_SOURCE"}], True),
    ([], True),
    ([{"subquestion": "스피커 내장인가요", "status": "ANSWERABLE"}], True),
    (None, True),
    ([{"subquestion": LEAD_TIME_Q, "status": "ANSWERABLE"}], False),
    ([{"subquestion": LEAD_TIME_Q, "status": "CANDIDATE"}], False),
])
def test_17_a_lead_time_atom_needs_its_own_verdict(database, entries, expected):
    """평가 행이 없는 것과 근거가 없는 것은 같은 결론이다."""

    metadata = _draft_metadata([LEAD_TIME_ATOM], entries)
    assert _evidence_insufficient(
        metadata, route="GPT_DIRECT") is expected


def test_17b_a_draft_without_the_pipeline_marker_is_untouched(database):
    """이 단계가 없던 때의 초안은 건드리지 않는다."""

    metadata = _draft_metadata([LEAD_TIME_ATOM], None, pipeline=False)
    assert _evidence_insufficient(metadata, route="GPT_DIRECT") is False


def test_18_a_dps_inquiry_keeps_only_its_own_reason(database):
    """DPS 필요 문의에는 이 사유를 붙이지 않는다 -- 전용 정책이 이미 있다."""

    understanding = lead_time_understanding(action="INSTALLATION_SCHEDULE")
    plan = plan_for(database, LEAD_TIME_Q, understanding, order_id=ORDER_ID)
    assert plan.requires_dps_lookup is True
    result = verdict(
        plan, answer="안녕하세요. 설치 예정일을 확인해 안내드리겠습니다.",
        evidence=[], understanding=understanding, route="GPT_DIRECT",
    )
    assert DPS_SCHEDULE_AUTO_POST_NOT_ENABLED in result.reasons
    assert EVIDENCE_NOT_SUFFICIENT not in result.reasons
