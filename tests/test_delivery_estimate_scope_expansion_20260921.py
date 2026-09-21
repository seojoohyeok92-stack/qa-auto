"""일반 배송기간과 현재 주문 일정의 분리 (2026-09-21).

두 사실은 다르다.

  GENERAL_DELIVERY_ESTIMATE   이 상품이 보통 얼마나 걸리는가. 같은 상품의
                              유효한 Learning이 근거가 된다.
  CURRENT_ORDER_SCHEDULE      이 고객 주문이 언제 오는가. Naver DPS만 근거다.

근거 우선순위는 DPS 예정일 > 같은 상품의 일반 배송기간 Learning > Review이며,
Learning은 DPS 날짜를 덮지 않고 고객의 날짜로 바뀌지도 않는다.

Learning 문장은 서버 스냅숏(data/서버pc_data_26.9.21)의 실제 행에서 가져왔다.
숫자는 테스트 입력일 뿐 production 코드 어디에도 없다.
"""
from __future__ import annotations

import itertools
import json
import pathlib
import tempfile
from types import SimpleNamespace
from typing import Any

import pytest

from answer.facts import build_answer_facts
from answer.hybrid_models import Emotion, IntentResult
from answer.models import AnswerRequest, AnswerResult, AnswerStatus
from answer.prompt_builder import PromptBuilder
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.answer_service import AnswerService
from services.auto_processing_eligibility_service import (
    AutoProcessingEligibilityService,
    GENERAL_ESTIMATE_CURRENT_STATE,
    GENERAL_ESTIMATE_EXACT_SCHEDULE,
    GENERAL_ESTIMATE_NOT_HEDGED,
    general_estimate_answer_violation,
)
from services.dps_lookup_policy import DpsLookupPolicy
from services.hybrid_answer_service import HybridAnswerService
from services.learning_context_service import LearningContextService
from services.learning_evidence_policy import general_delivery_estimate_claims
from services.market_policy import DPS_MARKETS
from services.semantic_analysis import ENABLED_ENV as SEMANTIC_ENABLED_ENV, parse

_key = itertools.count()

TV43 = "삼성 107.9cm(43인치) 비즈니스TV 4K UHD 1등급 LH43BEFHLGFXKR 스탠드형"
TV43_MODEL = "LH43BEFHLGFXKR"
TV50 = "삼성 125.7cm(50인치) UHD 4K 1등급 비즈니스TV LH50BEFHLGFXKR 스탠드형"
M5 = "삼성 삼탠바이미 스마트 M5 80cm(32인치)IPTV 모니터 화이트+스탠드 2in1거치대"
ORDER = "2026091912345678"

# Server rows (snapshot 26.9.21), answer text verbatim.
L207990 = (
    "안녕하세요 오제앤에스 입니다. 상품페이지에서 안내드리고 있듯이 43인치 제품의 경우 "
    "구매일로부터 설치까지 1~2주 정도 소요되실 예정 입니다. 설치 전날 저녁 시간대에 "
    "수취인 번호로 설치 기사님이 연락하시어 유선 상으로 시간 조율 하에 설치일 설치 방문 하십니다."
)
L154435 = (
    "택배배송 상품은 주문 후 오후 3시 이전 결제건에 한해 당일 발송 처리되며, "
    "배송 완료까지는 보통 1~2영업일 정도 소요됩니다. 도서산간 지역은 추가 1일 정도 더 "
    "소요될 수 있습니다."
)
# L5483 -- the M5 32" parcel lead time that survives the existing quality
# gate on the server. L154435 above states the same policy but is removed there
# as ORDER_SPECIFIC ("발송 처리되며, 배송 완료"), so it is not M5's evidence.
L5483 = (
    "택배배송 상품은 오후 3시 이전 결제 주문에 한해 당일 발송되며, 배송은 보통 "
    "1영업일에서 2영업일 정도 소요됩니다."
)
L157510 = (
    "안녕하세요, 고객님. 블랙 색상의 경우 구매일로부터 2주 소요될 수 있으며 화이트 색상의 "
    "경우 평일 오후 3시까지 주문하실 경우 당일 출고 됩니다.(주말/공휴일 휴무)"
)
# Colour-scoped lead time without the 휴무 note that makes L157510
# TEMPORARY_OR_EXPIRED at the quality gate.
COLOUR_SCOPED = (
    "블랙 색상은 구매일로부터 약 2주 소요될 수 있으며, 화이트 색상은 보통 1~2영업일 "
    "내 배송됩니다."
)
L169310 = "안녕하세요 고객님 말씀하신대로 블랙 제품은 스탠드 품절로 출고까지 2주정도 소요됩니다."
L31343 = (
    "삼성 감사 페스티벌 여파로 인해 서울, 경기, 인천 등의 수도권 지역의 경우 설치까지 "
    "약 1~2주 정도 소요되시는 상태입니다."
)
L19466_50 = "구매하신 제품은 구매일로부터 설치까지 2~3주 정도 소요되실 예정 입니다."
L318949_COUPANG = "별도의 설치비용은 없으나 설치까지는 구매일로부터 약 5주정도 생각해주셔야합니다."
OTHER_CUSTOMER_DATE = "고객님 주문은 9월 25일 설치 예정입니다."
DOCUMENT_LEAD_TIME = (
    "https://m.site.naver.com/1R7Sy 해당 링크를 통해 접수해주시면 거래명세서 전달드리겠습니다. "
    "현재 관련 요청이 많아 작성 후 발송까지 1~2일 정도 소요됩니다."
)

GENERAL_ANSWER = (
    "해당 상품은 일반적으로 구매일로부터 배송/설치까지 약 1~2주 정도 소요될 수 있습니다. "
    "실제 배송·설치 일정은 물류 및 기사님 배차 상황에 따라 달라질 수 있습니다."
)


@pytest.fixture
def database(tmp_path) -> Database:
    value = Database(tmp_path / "delivery-estimate.db")
    value.initialize()
    return value


def _learning(
    database, *, question, answer, product=TV43, model_code=TV43_MODEL,
    store="OJE_PLUS", validity=("PERMANENT", None, None, 1),
    created="2026-08-25T00:00:00Z", market_applicability=None,
):
    metadata = {"learning_signal_type": "POSITIVE", "human_verified": True,
                "answer_provenance": "NAVER_POSTED"}
    if market_applicability:
        # Server Coupang Learning (e.g. L318949): store_code NULL, shared by
        # both Coupang accounts through this label.
        metadata["market_applicability"] = market_applicability
    validity_type, valid_from, valid_until, validity_active = validity
    with database.connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO learning_examples (
                source_key, learning_source, question_original_masked,
                question_normalized, store_code, inquiry_type, product_name,
                model_code, final_answer, seller_answer, posted, rating,
                edit_ratio, quality_score, style_only, version, metadata_json,
                active, usage_count, created_at, updated_at, validity_type,
                valid_from, valid_until, validity_active
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (f"de-{next(_key)}", "SELLER_ANSWER", question, question, store,
             "PRODUCT_INQUIRY", product, model_code, answer, answer, 1, 5, 0.0,
             1.0, 0, 1, json.dumps(metadata, ensure_ascii=False), 1, 0,
             created, created, validity_type, valid_from, valid_until,
             validity_active),
        )
        return int(cursor.lastrowid)


def _semantic(question, *, action="DELIVERY_POLICY", purchase="PRE_PURCHASE",
              atoms=None, attribute="TIMING"):
    # ``attribute`` is GPT①'s requested_attribute. Every delivery question in
    # this file asks "언제 / 얼마나 걸리는지", which GPT① reports as TIMING
    # (server understandings of 3197/3214/3238/3245 carry exactly that).
    current = purchase == "CURRENT_ORDER"
    return parse({
        "primary_action": action, "secondary_actions": [],
        "request_type": "QUESTION", "objects": [],
        "atomic_questions": atoms or [
            {"text": question, "action": action, "requested_attribute": attribute},
        ],
        "deadline": None, "constraints": [], "negation": False,
        "conditional": False,
        "requires_order_context": current,
        "requires_delivery_schedule": current,
        "purchase_state": purchase,
        "asks_delivery_schedule": True, "asks_delivery_outcome": True,
        "confidence": 0.95,
    })


def _context(database, question, semantic, *, product=TV43, option=None,
             store="OJE_PLUS", source_type="NAVER", order_id=None,
             model_code=None):
    inquiry_id = InquiryRepository(database).upsert_work_item({
        "store_code": store, "source_type": source_type,
        "source_question_id": f"de-i-{next(_key)}",
        "inquiry_type": "PRODUCT_INQUIRY", "title": "문의", "content": question,
        "product_name": product, "option_name": option, "product_id": "p1",
        "order_id": order_id, "raw_json": {},
    }).inquiry_id
    metadata: dict[str, Any] = {}
    if model_code:
        metadata = {"model_code": model_code,
                    "model_identity_source": "COUPANG_CONFIRMED_MAPPING"}
    facts = build_answer_facts(
        AnswerRequest(inquiry_type="PRODUCT_INQUIRY", question=question,
                      product_name=product, option_name=option,
                      store_code=store, order_id=order_id or "",
                      metadata=metadata),
        AnswerResult(status=AnswerStatus.NOT_SUPPORTED, category="기타",
                     reason="t", answer="", provider="test",
                     auto_answerable=False, needs_review=True),
    )
    facts.inquiry["inquiry_id"] = inquiry_id
    return LearningContextService(database, hard_conflicts_only=True).build(
        facts,
        IntentResult("GENERAL", (question,), Emotion.NORMAL, "NORMAL", 0.9,
                     False, "t"),
        semantic_analysis=semantic,
    )


def _row(context, question):
    return next(
        item for item in context["subquestion_evidence"]
        if item["subquestion"] == question
    )


# ======================================================================
# 1. 어떤 문장이 일반 배송기간인가 (claim 단위)
# ======================================================================


def test_claims_general_lead_time_sentences_only():
    assert general_delivery_estimate_claims(L207990) == (
        "상품페이지에서 안내드리고 있듯이 43인치 제품의 경우 구매일로부터 설치까지 "
        "1~2주 정도 소요되실 예정 입니다.",
    )
    assert general_delivery_estimate_claims(L154435)


@pytest.mark.parametrize("answer", [
    OTHER_CUSTOMER_DATE,                                 # J 다른 고객 날짜
    "확인 결과 설치 예정일은 2026년 9월 1일입니다.",
    "오늘 발송되었습니다.",
    "8월 5일 출고된 것으로 확인됩니다.",
    L169310,                                             # 품절 = 재고 사실
    L31343,                                              # 행사 여파 = 기간성
    "화이트 재고 보유 중이며 약 1~2일 소요됩니다.",
    "블랙 스탠드는 다음 주 입고 예정으로 약 1주 소요됩니다.",
    DOCUMENT_LEAD_TIME,                                  # 배송이 아닌 기간
    "리뷰 이벤트는 작성일 기준 다음달 초, 5영업일 내에 발송됩니다.",
])
def test_claims_exclude_order_date_stock_event_and_other_subjects(answer):
    assert general_delivery_estimate_claims(answer) == ()


def test_L_colour_scoped_claim_needs_the_customers_colour():
    assert general_delivery_estimate_claims(L157510) == ()
    assert general_delivery_estimate_claims(L157510, option_name="블랙")
    assert general_delivery_estimate_claims(L157510, option_name="화이트")
    assert general_delivery_estimate_claims(L157510, option_name="실버") == ()


# ======================================================================
# 2. Evidence ladder -- LearningContextService
# ======================================================================

PRE_Q = "43인치 TV 주문하면 보통 며칠 걸리나요?"
CUR_Q = "43인치 TV 주문했는데 언제쯤 와요?"


def test_A_naver_pre_purchase_43_general_estimate(database):
    learning_id = _learning(database, question="배송 얼마나 걸리나요?", answer=L207990)
    row = _row(_context(database, PRE_Q, _semantic(PRE_Q)), PRE_Q)

    assert row["source"] == "GENERAL_DELIVERY_ESTIMATE"
    assert row["status"] == "CANDIDATE"
    assert row["learning_ids"] == [learning_id]
    assert row["current_order_schedule"] == "NO_CONFIRMED_ORDER"
    assert row["general_delivery_estimate"][0]["claims"]


def test_B_naver_pre_purchase_m5_uses_its_own_learning(database):
    question = "32인치 M5 주문하면 언제 받아요?"
    learning_id = _learning(
        database, question="오늘 주문하면 언제 받나요?", answer=L5483,
        product=M5, model_code=None,
    )
    _learning(database, question="배송 얼마나 걸리나요?", answer=L207990)  # 43인치
    row = _row(_context(database, question, _semantic(question), product=M5), question)

    assert row["source"] == "GENERAL_DELIVERY_ESTIMATE"
    assert row["learning_ids"] == [learning_id]


def test_no_learning_keeps_the_pre_purchase_hold(database):
    row = _row(_context(database, PRE_Q, _semantic(PRE_Q)), PRE_Q)
    assert row["status"] == "DELIVERY_SCHEDULE_REVIEW"
    assert row["learning_ids"] == []


def test_G_current_order_without_order_id_gets_general_estimate(database):
    _learning(database, question="배송 얼마나 걸리나요?", answer=L207990)
    row = _row(_context(database, CUR_Q, _semantic(
        CUR_Q, action="DELIVERY_STATUS", purchase="CURRENT_ORDER",
    )), CUR_Q)

    assert row["source"] == "GENERAL_DELIVERY_ESTIMATE"
    assert row["current_order_schedule"] == "NOT_CONFIRMED"


def test_current_order_without_learning_still_needs_dps(database):
    row = _row(_context(database, CUR_Q, _semantic(
        CUR_Q, action="DELIVERY_STATUS", purchase="CURRENT_ORDER",
    )), CUR_Q)
    assert row["status"] == "NEEDS_DPS"


def test_I_expired_temporary_lead_time_is_not_used(database):
    _learning(
        database, question="배송 얼마나 걸리나요?", answer=L207990,
        validity=("TEMPORARY", "2026-08-10", "2026-08-16", 1),
    )
    _learning(database, question="배송 얼마나 걸리나요?", answer=L31343)
    row = _row(_context(database, PRE_Q, _semantic(PRE_Q)), PRE_Q)
    assert row["status"] == "DELIVERY_SCHEDULE_REVIEW"


def test_J_other_customers_date_is_never_the_estimate(database):
    _learning(database, question="제 주문 언제 설치되나요?", answer=OTHER_CUSTOMER_DATE)
    row = _row(_context(database, CUR_Q, _semantic(
        CUR_Q, action="DELIVERY_STATUS", purchase="CURRENT_ORDER",
    )), CUR_Q)
    assert row["status"] == "NEEDS_DPS"
    assert row["learning_ids"] == []


def test_K_another_models_lead_time_is_not_used(database):
    _learning(database, question="배송 얼마나 걸리나요?", answer=L19466_50,
              product=TV50, model_code="LH50BEFHLGFXKR")
    row = _row(_context(database, PRE_Q, _semantic(PRE_Q)), PRE_Q)
    assert row["source"] != "GENERAL_DELIVERY_ESTIMATE"
    assert row["learning_ids"] == []


def test_K_same_size_other_model_is_not_the_same_product(database):
    """43인치 LH43BEHH 의 기간은 43인치 LH43BEFH 의 근거가 아니다."""

    _learning(database, question="배송 얼마나 걸리나요?", answer=L207990,
              product="삼성 107.9cm(43인치) UHD 4K 1등급 비즈니스 TV LH43BEHHLGFXKR",
              model_code="LH43BEHHLGFXKR")
    row = _row(_context(database, PRE_Q, _semantic(PRE_Q)), PRE_Q)
    assert row["source"] != "GENERAL_DELIVERY_ESTIMATE"


def test_L_option_mismatch_never_narrows_the_lead_time(database):
    question = "M5 주문하면 언제 받아요?"
    _learning(database, question="M5 주문하면 언제 받아요?", answer=COLOUR_SCOPED,
              product=M5, model_code=None)
    unknown = _row(_context(database, question, _semantic(question), product=M5), question)
    black = _row(
        _context(database, question, _semantic(question), product=M5, option="블랙"),
        question,
    )
    assert unknown["source"] != "GENERAL_DELIVERY_ESTIMATE"
    assert black["source"] == "GENERAL_DELIVERY_ESTIMATE"


def test_M_coupang_general_estimate_from_coupang_learning(database):
    learning_id = _learning(
        # The server row's own question was about the installation fee; the
        # lexical scorer alone (no semantic index here) needs a delivery one.
        database, question="Coupang 상품문의 주문하면 설치까지 며칠 걸리나요?",
        answer=L318949_COUPANG, product=None, model_code="LH43BEHHLGFXKR",
        store=None, market_applicability="COUPANG_ONLY",
    )
    row = _row(_context(
        database, PRE_Q, _semantic(PRE_Q), product="삼성 43인치 LH43BEHHLGFXKR",
        store="COUPANG_OJE_PLUS", source_type="COUPANG_ONLINE",
        model_code="LH43BEHHLGFXKR",
    ), PRE_Q)
    assert row["source"] == "GENERAL_DELIVERY_ESTIMATE"
    assert row["learning_ids"] == [learning_id]


def test_O_coupang_never_reads_naver_only_lead_time(database):
    _learning(database, question="배송 얼마나 걸리나요?", answer=L207990,
              model_code="LH43BEHHLGFXKR")
    row = _row(_context(
        database, PRE_Q, _semantic(PRE_Q), product="삼성 43인치 LH43BEHHLGFXKR",
        store="COUPANG_OJE_NS", source_type="COUPANG_ONLINE",
        model_code="LH43BEHHLGFXKR",
    ), PRE_Q)
    assert row["source"] != "GENERAL_DELIVERY_ESTIMATE"


def test_P_compound_delivery_and_spec_are_independent(database):
    delivery = "43인치 TV 배송 얼마나 걸려요?"
    spec = "동축케이블 사용되나요?"
    _learning(database, question="배송 얼마나 걸리나요?", answer=L207990)
    context = _context(database, f"{delivery} {spec}", _semantic(
        delivery, atoms=[
            {"text": delivery, "action": "DELIVERY_POLICY",
             "requested_attribute": "TIMING"},
            {"text": spec, "action": "PRODUCT_SPEC",
             "requested_attribute": "COMPATIBILITY"},
        ],
    ))
    assert _row(context, delivery)["source"] == "GENERAL_DELIVERY_ESTIMATE"
    spec_row = _row(context, spec)
    assert spec_row["source"] != "GENERAL_DELIVERY_ESTIMATE"
    assert not spec_row.get("general_delivery_estimate")


# ======================================================================
# 3. Eligibility -- 답변이 일반 예상을 넘어서지 않는가
# ======================================================================


@pytest.mark.parametrize("answer,reason", [
    ("1~2주 후에 반드시 도착합니다.", GENERAL_ESTIMATE_NOT_HEDGED),
    ("다음 주 수요일 도착합니다.", GENERAL_ESTIMATE_EXACT_SCHEDULE),
    ("9월 28일 배송됩니다.", GENERAL_ESTIMATE_EXACT_SCHEDULE),
    ("보통 1~2주 소요되며 오후 2시에 방문합니다.", GENERAL_ESTIMATE_EXACT_SCHEDULE),
    ("화이트 재고 보유 중이라 약 1~2일 소요됩니다.", GENERAL_ESTIMATE_CURRENT_STATE),
    ("이미 출고되었으며 보통 1~2일 소요됩니다.", GENERAL_ESTIMATE_CURRENT_STATE),
])
def test_general_estimate_answer_that_says_more_is_held(answer, reason):
    assert general_estimate_answer_violation(answer) == reason


def test_hedged_general_estimate_passes_the_guard():
    assert general_estimate_answer_violation(GENERAL_ANSWER) is None


def _draft(answer, evidence, plan):
    return {
        "original_answer": answer, "validation_status": "PASS",
        "validator_result_json": {"passed": True, "status": "PASS"},
        "metadata_json": {
            "processing_plan": plan,
            "semantic_routing": {"understanding": {"usable": True}},
            "hybrid": {
                "answer_pipeline": "GPT_UNDERSTAND_RETRIEVE_ANSWER",
                "subquestion_evidence": evidence,
                "draft": {"unresolved": [], "can_auto_post": True},
            },
        },
    }


GENERAL_ROW = {"subquestion": PRE_Q, "status": "CANDIDATE",
               "source": "GENERAL_DELIVERY_ESTIMATE", "learning_ids": [1]}


def _evaluate(answer, *, evidence, plan, store="OJE_PLUS"):
    return AutoProcessingEligibilityService().evaluate(
        inquiry={"store_code": store, "content": PRE_Q},
        draft=_draft(answer, evidence, plan), route="GPT_FALLBACK",
    )


PRE_PLAN = {"workflow_block_reasons": ["PRE_PURCHASE_DELIVERY_UNRESOLVED"],
            "requires_order_lookup": False, "requires_dps_lookup": False}
NO_ID_PLAN = {"requires_order_lookup": True, "requires_dps_lookup": True,
              "order_id_status": "MISSING",
              "order_lookup_status": "CUSTOMER_INFORMATION_REQUIRED",
              "dps_lookup_status": "NOT_REQUIRED"}


def test_eligibility_lifts_pre_purchase_block_for_grounded_estimate():
    verdict = _evaluate(GENERAL_ANSWER, evidence=[GENERAL_ROW], plan=PRE_PLAN)
    assert "PRE_PURCHASE_DELIVERY_UNRESOLVED" not in verdict.reasons


def test_eligibility_keeps_pre_purchase_block_without_estimate():
    held = {**GENERAL_ROW, "status": "DELIVERY_SCHEDULE_REVIEW",
            "source": "DELIVERY_SCHEDULE_UNCONFIRMED_PURCHASE"}
    verdict = _evaluate(GENERAL_ANSWER, evidence=[held], plan=PRE_PLAN)
    assert "PRE_PURCHASE_DELIVERY_UNRESOLVED" in verdict.reasons


def test_eligibility_one_grounded_atom_does_not_speak_for_a_held_one():
    held = {"subquestion": "설치예정일은요?", "status": "NEEDS_DPS",
            "source": "CURRENT_DPS_REQUIRED"}
    verdict = _evaluate(GENERAL_ANSWER, evidence=[GENERAL_ROW, held], plan=PRE_PLAN)
    assert "PRE_PURCHASE_DELIVERY_UNRESOLVED" in verdict.reasons


def test_G_naver_no_order_id_estimate_is_not_held_for_the_missing_number():
    verdict = _evaluate(GENERAL_ANSWER, evidence=[GENERAL_ROW], plan=NO_ID_PLAN)
    assert not {
        "REQUIRED_ORDER_ID_MISSING_OR_INVALID", "DPS_RESULT_NOT_TRUSTED",
        "DPS_SNAPSHOT_NOT_VALIDATED",
    } & set(verdict.reasons)


def test_F_dps_failure_keeps_its_hold_even_with_an_estimate():
    plan = {**NO_ID_PLAN, "order_id_status": "VALID",
            "order_lookup_status": "SUCCESS", "dps_lookup_status": "AUTOMATION_ERROR"}
    verdict = _evaluate(GENERAL_ANSWER, evidence=[GENERAL_ROW], plan=plan)
    assert verdict.decision != "SAFE"
    assert "DPS_RESULT_NOT_TRUSTED" in verdict.reasons


def test_N_coupang_current_order_keeps_every_order_and_dps_gate():
    assert "COUPANG" not in DPS_MARKETS
    verdict = _evaluate(
        GENERAL_ANSWER, evidence=[GENERAL_ROW], plan=NO_ID_PLAN,
        store="COUPANG_OJE_NS",
    )
    assert verdict.decision != "SAFE"
    assert "REQUIRED_ORDER_ID_MISSING_OR_INVALID" in verdict.reasons


def test_eligibility_holds_an_exact_date_from_general_evidence():
    verdict = _evaluate("9월 28일 배송됩니다.", evidence=[GENERAL_ROW], plan=PRE_PLAN)
    assert GENERAL_ESTIMATE_EXACT_SCHEDULE in verdict.reasons


def test_prompt_separates_general_lead_time_from_current_schedule():
    rules = " ".join(PromptBuilder.EVIDENCE_JUDGEMENT_RULES)
    for phrase in ("GENERAL_DELIVERY_ESTIMATE", "DPS 설치예정일이 있으면",
                   "방문 시간은 만들지", "재고·입고·품절"):
        assert phrase in rules


# ======================================================================
# 4. AnswerService end-to-end: Phase9 shortcut vs general fallback
# ======================================================================


class _EstimateProvider:
    """GPT② double. Records every call; drafts a hedged general estimate."""

    name = "openai"

    def __init__(self, answer: str = GENERAL_ANSWER) -> None:
        self.answer = answer
        self.tasks: list[str] = []

    def generate_json(self, *, task, prompt, context):
        self.tasks.append(task)
        if task == "UNDERSTANDING":
            return {"category": "GENERAL", "questions": ["q"], "urgency": "NORMAL",
                    "emotion": "NORMAL", "confidence": 0.95,
                    "requires_review": False, "reason": "stub"}
        if task == "DRAFT":
            return {
                "answer": self.answer, "confidence": 0.9, "used_facts": [],
                "missing_information": [], "requires_review": False,
                "warnings": [], "learning_usage": [], "historical_usage": [],
                "feedback_signal_usage": [],
                "subquestion_results": [
                    {"subquestion": "q", "answered": True, "status": "ANSWERABLE"},
                ],
            }
        if task == "SELF_REVIEW":
            return {"passed": True, "answered_all_questions": True,
                    "has_speculation": False, "facts_consistent": True,
                    "requires_review": False, "reason": "stub", "warnings": []}
        raise AssertionError(task)


class _Dps:
    """External DPS double: returns a scripted result, counts real lookups."""

    def __init__(self, result: dict[str, Any]) -> None:
        self.result = dict(result)
        self.calls = 0
        self.policy = DpsLookupPolicy()

    def enrich(self, request, **kwargs):
        self.calls += 1
        request.metadata["dps"] = dict(self.result)
        return SimpleNamespace(decision=SimpleNamespace(lookup_required=True),
                               metadata=request.metadata["dps"], lookup_row=None)

    def skip_for_phase9(self, request, **kwargs):
        request.metadata["dps"] = {"lookup_required": False,
                                   "lookup_status": "NOT_REQUIRED"}
        return SimpleNamespace(decision=SimpleNamespace(lookup_required=False),
                               metadata=request.metadata["dps"], lookup_row=None)


DPS_DATE = {"lookup_required": True, "lookup_status": "SUCCESS",
            "installation_date": "2026-09-24",
            "installation_date_display": "2026-09-24",
            "delivery_status": "구매요청", "installation_status": "구매요청",
            "change_request": False, "general_segments": [], "dps_segments": [],
            "warnings": [], "cache_used": True}
DPS_NO_DATE = {**DPS_DATE, "installation_date": None,
               "installation_date_display": None}
DPS_FAILED = {"lookup_required": True, "lookup_status": "AUTOMATION_ERROR",
              "error_code": "DPS_TAB_NOT_FOUND", "change_request": False}


class _Analyzer:
    def __init__(self, semantic) -> None:
        self.semantic = semantic
        self.last_trace = {"cache_hit": False, "latency_ms": 0.0}

    def analyze(self, question):
        return self.semantic


def _run(monkeypatch, question, *, dps, order_id=None, learning=True,
         store="OJE_PLUS", source_type="NAVER", answer=GENERAL_ANSWER):
    monkeypatch.setenv(SEMANTIC_ENABLED_ENV, "1")
    database = Database(pathlib.Path(tempfile.mkdtemp()) / "e2e.db")
    database.initialize()
    if learning:
        _learning(database, question="배송 얼마나 걸리나요?", answer=L207990)
    inquiry_id = InquiryRepository(database).upsert_work_item({
        "store_code": store, "source_type": source_type,
        "source_question_id": f"e2e-{next(_key)}", "inquiry_type": "PRODUCT_INQUIRY",
        "title": "문의", "content": question, "product_name": TV43,
        "product_id": "p1", "order_id": order_id, "product_order_id": None,
        "raw_json": {},
    }).inquiry_id
    provider = _EstimateProvider(answer)
    fake_dps = _Dps(dps)
    service = AnswerService(
        database, dps_enrichment=fake_dps,
        # Wired as production wires it (GovernedHybridAnswerService).
        hybrid_service=HybridAnswerService(
            provider,
            learning_context_provider=LearningContextService(
                database, hard_conflicts_only=True,
            ).build,
        ),
        semantic_analyzer=_Analyzer(_semantic(
            question, action="DELIVERY_STATUS", purchase="CURRENT_ORDER",
        )),
    )
    service.generate_for_inquiry(inquiry_id)
    record = dict(AnswerRepository(database).latest_for_inquiry(inquiry_id))
    metadata = record.get("metadata_json") or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    return SimpleNamespace(
        answer=str(record.get("original_answer") or ""),
        route=str(metadata.get("selected_answer_route") or ""),
        metadata=metadata, gpt_drafts=provider.tasks.count("DRAFT"),
        dps_calls=fake_dps.calls,
        decision=(metadata.get("production_decision_trace") or {}),
    )


def test_C_D_dps_installation_date_wins_over_learning(monkeypatch):
    run = _run(monkeypatch, CUR_Q, dps=DPS_DATE, order_id=ORDER)
    assert run.route == "DELIVERY_WITH_INSTALLATION_DATE"
    assert run.gpt_drafts == 0
    assert "2026년 9월 24일" in run.answer or "9월 24일" in run.answer
    assert "1~2주" not in run.answer
    assert "변경될 수 있" in run.answer
    assert "방문 예정 시간" not in run.answer


def test_E_dps_success_without_date_falls_back_to_the_general_estimate(monkeypatch):
    run = _run(monkeypatch, CUR_Q, dps=DPS_NO_DATE, order_id=ORDER)
    assert run.gpt_drafts >= 1
    assert run.route not in {"DELIVERY_DATE_UNCONFIRMED", "REVIEW_REQUIRED_SAFE_DRAFT"}
    assert "1~2주" in run.answer
    assert "GENERAL_ESTIMATE_EXACT_SCHEDULE" not in run.decision.get(
        "blocking_reason_codes", [])


def test_E_without_learning_keeps_the_phase9_pending_reply(monkeypatch):
    run = _run(monkeypatch, CUR_Q, dps=DPS_NO_DATE, order_id=ORDER, learning=False)
    assert run.gpt_drafts == 0
    assert run.route == "DELIVERY_DATE_UNCONFIRMED"


def test_F_dps_failure_falls_back_but_stays_with_staff(monkeypatch):
    run = _run(monkeypatch, CUR_Q, dps=DPS_FAILED, order_id=ORDER)
    assert run.gpt_drafts >= 1
    assert run.decision.get("eligibility") != "SAFE"


G_ANSWER = (
    GENERAL_ANSWER + " 정확한 배송·설치 예정일은 네이버 구매내역에서 상품주문번호가 아닌 "
    "일반 주문번호를 확인해 비밀글로 남겨주시면 확인 후 조회해 안내드리겠습니다."
)


def test_G_no_order_id_general_estimate(monkeypatch):
    run = _run(monkeypatch, CUR_Q, dps=DPS_DATE, answer=G_ANSWER)
    assert run.dps_calls == 0
    assert run.gpt_drafts >= 1
    assert "1~2주" in run.answer


def test_H_no_order_id_and_no_learning_keeps_order_number_request(monkeypatch):
    run = _run(monkeypatch, CUR_Q, dps=DPS_DATE, learning=False)
    assert run.dps_calls == 0
    assert run.gpt_drafts == 0
    assert run.route == "ORDER_ID_REQUEST"


def test_N_coupang_exact_schedule_never_calls_dps(monkeypatch):
    """The existing market gate answers it: staff, and no DPS call at all."""

    from answer.exceptions import AutoAnswerProhibitedError

    calls: list[int] = []
    original = _Dps.enrich

    def counted(self, request, **kwargs):
        calls.append(1)
        return original(self, request, **kwargs)

    monkeypatch.setattr(_Dps, "enrich", counted)
    with pytest.raises(AutoAnswerProhibitedError) as raised:
        _run(monkeypatch, CUR_Q, dps=DPS_DATE, order_id=ORDER,
             store="COUPANG_OJE_NS", source_type="COUPANG_ONLINE")
    assert raised.value.policy_reason == "MARKET_ORDER_DPS_NOT_SUPPORTED"
    assert calls == []


# ======================================================================
# 5. 주문번호만 보낸 follow-up (ORDER_NUMBER_ONLY_FOLLOWUP)
# ======================================================================
#
# Server inquiry 2673 -> 2685: "23일 주문했는데요.." got the ORDER_ID_REQUEST
# reply; the next inquiry "주문번호 다시 보내드립니다. 주문번호 ... 다시
# 부탁드립니다" carried a VALID number, but requires_order_lookup was False, no
# lookup ran, and GPT② answered that the request could not be identified.

FOLLOWUP_ORDER = "2026082341151601"
FOLLOWUP_TEXT = f"주문번호 다시 보내드립니다.\n주문번호 {FOLLOWUP_ORDER}\n다시 부탁드립니다"
WRITER = "bada***"
LISTING = "12139453925"


def _bare_number_semantic(text):
    # What GPT① can see in a bare number: an order identifier, nothing else.
    return parse({
        "primary_action": "ORDER_IDENTIFICATION", "secondary_actions": [],
        "request_type": "QUESTION", "objects": [],
        "atomic_questions": [{"text": text, "action": "ORDER_IDENTIFICATION"}],
        "deadline": None, "constraints": [], "negation": False,
        "conditional": False, "requires_order_context": True,
        "requires_delivery_schedule": False, "purchase_state": "CURRENT_ORDER",
        "asks_delivery_schedule": False, "asks_delivery_outcome": False,
        "confidence": 0.9,
    })


class _SequencedAnalyzer:
    def __init__(self, *semantics) -> None:
        self.semantics = list(semantics)
        self.last_trace = {"cache_hit": False, "latency_ms": 0.0}

    def analyze(self, question):
        return self.semantics.pop(0)


def _followup_run(monkeypatch, *, prior_text, prior_semantic, store="OJE_PLUS",
                  source_type="NAVER", writer=WRITER, listing=LISTING,
                  followup_writer=None, followup_listing=None,
                  prior_registered="2026-08-25T12:52:00+09:00",
                  followup_registered="2026-08-25T16:22:00+09:00"):
    monkeypatch.setenv(SEMANTIC_ENABLED_ENV, "1")
    database = Database(pathlib.Path(tempfile.mkdtemp()) / "followup.db")
    database.initialize()
    inquiries = InquiryRepository(database)

    def add(text, registered, who, where):
        inquiry_id = inquiries.upsert_work_item({
            "store_code": store, "source_type": source_type,
            "source_question_id": f"fu-{next(_key)}",
            "inquiry_type": "PRODUCT_INQUIRY", "title": "문의", "content": text,
            "product_name": TV43, "product_id": where, "order_id": None,
            "product_order_id": None, "registered_at": registered,
            "masked_writer_id": who, "raw_json": {},
        }).inquiry_id
        with database.connection() as conn:
            conn.execute(
                "UPDATE inquiries SET masked_writer_id=?, product_id=?, "
                "registered_at=? WHERE id=?",
                (who, where, registered, inquiry_id),
            )
        return inquiry_id

    fake_dps = _Dps(DPS_DATE)
    service = AnswerService(
        database, dps_enrichment=fake_dps,
        hybrid_service=HybridAnswerService(
            _EstimateProvider(),
            learning_context_provider=LearningContextService(
                database, hard_conflicts_only=True,
            ).build,
        ),
        semantic_analyzer=_SequencedAnalyzer(
            prior_semantic, _bare_number_semantic(FOLLOWUP_TEXT),
        ),
    )
    prior_id = add(prior_text, prior_registered, writer, listing)
    prior_route = None
    try:
        service.generate_for_inquiry(prior_id)
    except Exception as error:  # a prior held for staff is still a prior
        prior_route = type(error).__name__
    record = AnswerRepository(database).latest_for_inquiry(prior_id) or {}
    metadata = record.get("metadata_json") or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    prior_route = prior_route or metadata.get("selected_answer_route")
    followup_id = add(
        FOLLOWUP_TEXT, followup_registered,
        writer if followup_writer is None else followup_writer,
        followup_listing or listing,
    )
    calls_before = fake_dps.calls
    raised = None
    try:
        service.generate_for_inquiry(followup_id)
    except Exception as error:
        raised = error
    record = AnswerRepository(database).latest_for_inquiry(followup_id) or {}
    metadata = record.get("metadata_json") or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    plan = metadata.get("processing_plan") or {}
    return SimpleNamespace(
        prior_route=prior_route, route=metadata.get("selected_answer_route"),
        answer=str(record.get("original_answer") or ""),
        followup=plan.get("order_number_followup"),
        requires_dps=plan.get("requires_dps_lookup"),
        dps_calls=fake_dps.calls - calls_before, raised=raised,
    )


PRIOR_DELIVERY = "23일 주문했는데요.. 언제 배송되나요?"


def _prior_delivery_semantic():
    return _semantic(PRIOR_DELIVERY, action="DELIVERY_STATUS", purchase="CURRENT_ORDER")


def test_followup_A_order_number_answers_our_order_number_request(monkeypatch):
    run = _followup_run(
        monkeypatch, prior_text=PRIOR_DELIVERY,
        prior_semantic=_prior_delivery_semantic(),
    )
    assert run.prior_route == "ORDER_ID_REQUEST"
    assert run.followup["previous_route"] == "ORDER_ID_REQUEST"
    assert run.requires_dps is True
    assert run.dps_calls == 1
    assert run.route == "DELIVERY_WITH_INSTALLATION_DATE"
    assert "9월 24일" in run.answer


@pytest.mark.parametrize("text", [
    f"{FOLLOWUP_ORDER} 주문번호입니다.",
    f"주문번호 {FOLLOWUP_ORDER}입니다.",
    f"{FOLLOWUP_ORDER} 이게 제 주문번호입니다!",
    FOLLOWUP_TEXT,
])
def test_followup_bare_number_shapes(text):
    from services.inquiry_processing_plan_service import order_number_only

    assert order_number_only(text, FOLLOWUP_ORDER)


@pytest.mark.parametrize("text", [
    f"주문번호 {FOLLOWUP_ORDER}\n반품 부탁드려요",
    f"{FOLLOWUP_ORDER} 현금영수증 발행해주세요",
    f"{FOLLOWUP_ORDER} 교환 원합니다",
])
def test_followup_number_with_its_own_request_is_not_bare(text):
    from services.inquiry_processing_plan_service import order_number_only

    assert not order_number_only(text, FOLLOWUP_ORDER)


def test_followup_B_after_a_product_question_no_dps(monkeypatch):
    text = "이 제품 스피커 내장인가요?"
    run = _followup_run(
        monkeypatch, prior_text=text,
        prior_semantic=_semantic(text, action="PRODUCT_SPEC", purchase="UNKNOWN"),
    )
    assert run.followup is None
    assert run.dps_calls == 0


def test_followup_C_after_a_return_request_no_delivery_intent(monkeypatch):
    text = "반품하고 싶어요. 어떻게 하나요?"
    run = _followup_run(
        monkeypatch, prior_text=text,
        prior_semantic=parse({
            "primary_action": "CANCEL_RETURN", "secondary_actions": [],
            "request_type": "QUESTION", "objects": [],
            "atomic_questions": [{"text": text, "action": "CANCEL_RETURN"}],
            "deadline": None, "constraints": [], "negation": False,
            "conditional": False, "requires_order_context": True,
            "requires_delivery_schedule": False,
            "purchase_state": "CURRENT_ORDER", "asks_delivery_schedule": False,
            "asks_delivery_outcome": False, "confidence": 0.9,
        }),
    )
    assert run.prior_route != "ORDER_ID_REQUEST"
    assert run.followup is None
    assert run.dps_calls == 0


def test_followup_D_prior_delivery_without_our_request_is_not_linked(monkeypatch):
    """The prior asked about delivery, but no order number was requested."""

    text = "43인치 TV 주문하면 보통 며칠 걸리나요?"
    run = _followup_run(monkeypatch, prior_text=text, prior_semantic=_semantic(text))
    assert run.prior_route != "ORDER_ID_REQUEST"
    assert run.followup is None
    assert run.dps_calls == 0


@pytest.mark.parametrize("change", [
    {"followup_writer": "other***"},                       # different customer
    {"followup_listing": "99999999999"},                   # different listing
    {"writer": "", "followup_writer": ""},                 # no customer key
    {"followup_registered": "2026-09-10T16:22:00+09:00"},  # window passed
])
def test_followup_E_unreliable_link_never_runs_dps(monkeypatch, change):
    run = _followup_run(
        monkeypatch, prior_text=PRIOR_DELIVERY,
        prior_semantic=_prior_delivery_semantic(), **change,
    )
    assert run.followup is None
    assert run.dps_calls == 0


def test_followup_E_a_later_unrelated_inquiry_breaks_the_link(tmp_path):
    """Only the customer's most recent earlier inquiry can be continued."""

    from services.inquiry_processing_plan_service import InquiryProcessingPlanService

    database = Database(tmp_path / "fu-later.db")
    database.initialize()
    inquiries = InquiryRepository(database)

    def add(text, registered):
        inquiry_id = inquiries.upsert_work_item({
            "store_code": "OJE_PLUS", "source_type": "NAVER",
            "source_question_id": f"fl-{next(_key)}",
            "inquiry_type": "PRODUCT_INQUIRY", "title": "문의", "content": text,
            "product_name": TV43, "product_id": LISTING, "raw_json": {},
        }).inquiry_id
        with database.connection() as conn:
            conn.execute(
                "UPDATE inquiries SET masked_writer_id=?, registered_at=? WHERE id=?",
                (WRITER, registered, inquiry_id),
            )
        return inquiry_id

    delivery = add(PRIOR_DELIVERY, "2026-08-25T12:00:00+09:00")
    with database.connection() as conn:
        conn.execute(
            "INSERT INTO answer_drafts (inquiry_id, original_answer, metadata_json, "
            "is_active, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (delivery, "주문번호를 알려주세요", json.dumps({
                "selected_answer_route": "ORDER_ID_REQUEST",
                "processing_plan": {"is_delivery": True,
                                    "detected_intent": "DELIVERY_DATE"},
            }), 1, "2026-08-25T03:00:01Z", "2026-08-25T03:00:01Z"),
        )
    service = InquiryProcessingPlanService(database)
    direct = add(FOLLOWUP_TEXT, "2026-08-25T12:30:00+09:00")
    assert service._order_number_followup(
        inquiries.get(direct), FOLLOWUP_ORDER,
    ) is not None  # control: the link holds when nothing intervenes
    add("스피커 내장인가요?", "2026-08-25T13:00:00+09:00")
    later = add(FOLLOWUP_TEXT, "2026-08-25T14:00:00+09:00")
    assert service._order_number_followup(
        inquiries.get(later), FOLLOWUP_ORDER,
    ) is None


def test_followup_F_coupang_same_shape_keeps_dps_off(monkeypatch):
    run = _followup_run(
        monkeypatch, prior_text=PRIOR_DELIVERY,
        prior_semantic=_prior_delivery_semantic(),
        store="COUPANG_OJE_NS", source_type="COUPANG_ONLINE",
    )
    assert run.followup is None
    assert run.dps_calls == 0


# ======================================================================
# 6. 일반 배송기간은 "언제 / 얼마나 걸리는지"를 묻는 atom 에만 붙는다
# ======================================================================
#
# Server inquiry 3205 ("선결제 후 ... 며칠 후 배송지를 변경할 수 있는지") is
# DELIVERY_POLICY like "배송 얼마나 걸리나요", and got M5's lead time attached.
# What separates them is GPT①'s requested_attribute: TIMING for the second,
# PERMISSION_OR_OPTION for the first. That boundary already exists; the
# general estimate now reads it instead of the action alone.

M5_Q_LEARNING = "오늘 주문하면 언제 받나요?"


def _m5_context(database, question, *, action, attribute, purchase="PRE_PURCHASE"):
    _learning(database, question=M5_Q_LEARNING, answer=L5483, product=M5,
              model_code=None)
    return _row(_context(
        database, question,
        _semantic(question, action=action, purchase=purchase, attribute=attribute),
        product=M5,
    ), question)


@pytest.mark.parametrize("question,action", [
    ("배송 얼마나 걸리나요?", "DELIVERY_POLICY"),                      # A
    ("언제 받을 수 있나요?", "DELIVERY_DEADLINE_CONFIRMATION"),        # B
    ("주문하면 며칠 걸리나요?", "DELIVERY_POLICY"),
    ("설치까지 보통 얼마나 걸리나요?", "SCHEDULE_REQUEST"),
    ("배송 예정 기간이 어떻게 되나요?", "DELIVERY_POLICY"),
])
def test_duration_questions_get_the_general_estimate(database, question, action):
    row = _m5_context(database, question, action=action, attribute="TIMING")
    assert row["source"] == "GENERAL_DELIVERY_ESTIMATE"


@pytest.mark.parametrize("question,action,attribute", [
    # C -- server 3205, GPT①'s own reading
    ("구입 후 주소 변경·발송 가능?", "DELIVERY_POLICY", "PERMISSION_OR_OPTION"),
    ("배송지 변경 가능한가요?", "DELIVERY_POLICY", "PERMISSION_OR_OPTION"),  # D
    ("주소 변경해주세요", "DELIVERY_POLICY", "ACTION_EXECUTION"),
    ("발송 가능한가요?", "DELIVERY_POLICY", "EXISTENCE_OR_CAPABILITY"),
    ("묶음배송 되나요?", "DELIVERY_POLICY", "EXISTENCE_OR_CAPABILITY"),
    ("배송비 얼마인가요?", "DELIVERY_POLICY", "AMOUNT_OR_COST"),            # E
    ("무료배송인가요?", "DELIVERY_POLICY", "AMOUNT_OR_COST"),
    ("배송 언제 되나요?", "DELIVERY_POLICY", "UNKNOWN"),  # no reading = no licence
])
def test_non_duration_delivery_questions_get_no_general_estimate(
    database, question, action, attribute,
):
    row = _m5_context(database, question, action=action, attribute=attribute)
    assert row["source"] != "GENERAL_DELIVERY_ESTIMATE"
    assert not row.get("general_delivery_estimate")


@pytest.mark.parametrize("question,attribute", [
    ("지금 출고됐나요?", "EXISTENCE_OR_CAPABILITY"),                         # F
    ("배송 완료됐나요?", "EXISTENCE_OR_CAPABILITY"),
    ("송장번호 알려주세요", "SPEC_VALUE"),
])
def test_F_current_state_questions_never_use_the_general_estimate(
    database, question, attribute,
):
    row = _m5_context(
        database, question, action="DELIVERY_STATUS", attribute=attribute,
        purchase="CURRENT_ORDER",
    )
    assert row["source"] != "GENERAL_DELIVERY_ESTIMATE"
    assert row["status"] == "NEEDS_DPS"      # the current-order path is unchanged


def test_G_compound_attaches_the_estimate_to_the_duration_atom_only(database):
    delivery = "배송 얼마나 걸리나요?"
    address = "배송지 변경도 가능한가요?"
    spec = "동축케이블 사용되나요?"
    _learning(database, question=M5_Q_LEARNING, answer=L5483, product=M5,
              model_code=None)
    context = _context(database, f"{delivery} {address} {spec}", _semantic(
        delivery, atoms=[
            {"text": delivery, "action": "DELIVERY_POLICY",
             "requested_attribute": "TIMING"},
            {"text": address, "action": "DELIVERY_POLICY",
             "requested_attribute": "PERMISSION_OR_OPTION"},
            {"text": spec, "action": "PRODUCT_SPEC",
             "requested_attribute": "COMPATIBILITY"},
        ],
    ), product=M5)
    assert _row(context, delivery)["source"] == "GENERAL_DELIVERY_ESTIMATE"
    for other in (address, spec):
        row = _row(context, other)
        assert row["source"] != "GENERAL_DELIVERY_ESTIMATE"
        assert not row.get("general_delivery_estimate")
