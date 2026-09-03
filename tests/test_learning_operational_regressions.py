"""서버에서 실제로 잘못 등록된 네 가지 문의 유형.

commit a23df76 을 서버에 올린 뒤 운영 화면에서 확인된 것들이고, 넷 다 같은
모양의 결함이다 -- 주제어는 맞는데 고객이 실제로 알고 싶어한 것이 다르다.

    주문 전 배송      order/DPS 는 SKIPPED 였는데 등록된 답변은 주문번호를 요구
    리뷰 보상 지급     EVENT/네이버페이 단어만 맞는 Learning 이 선택
    상품권 신청 확인    이미 신청한 고객에게 신청 *방법* 을 다시 안내
    폐가전            수거 가능 여부 / 신청 방법 / 이미 요청한 건이 한 덩어리

문의 ID 나 문장 예외는 쓰지 않는다. 전부 synthetic fixture 이고, 구분하는
기준은 GPT Semantic 이 이미 내놓은 atomic question / requested information /
action 이다.

각 테스트는 semantic 이 실제로 retrieval 과 evidence 까지 같은 의미로
전달되는지를 본다: primary action, atomic question, order/DPS 요구 여부,
선택된 Positive, 선택된 Negative correction, evidence coverage, 최종 route.
"""
from __future__ import annotations

import itertools
import json
from typing import Any

import pytest

from answer.facts import AnswerFacts
from answer.hybrid_models import Emotion, IntentResult
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.inquiry_processing_plan_service import InquiryProcessingPlanService
from services.learning_context_service import LearningContextService
from services.semantic_analysis import parse


STORE = "OJE_PLUS"
PRODUCT = "삼성 125.7cm(50인치) UHD 4K 1등급 비즈니스TV LH50BEFHLGFXKR 스탠드형"
PRODUCT_ID = "12139453925"

_key = itertools.count()


@pytest.fixture
def database(tmp_path) -> Database:
    value = Database(tmp_path / "operational.db")
    value.initialize()
    return value


def semantic(
    primary: str,
    *,
    atomic: list[dict[str, str]],
    order_context: bool = False,
    delivery_schedule: bool = False,
    purchase_state: str = "UNKNOWN",
    asks_schedule: bool = False,
):
    return parse({
        "primary_action": primary,
        "secondary_actions": [],
        "request_type": "QUESTION",
        "objects": [],
        "atomic_questions": atomic,
        "deadline": None,
        "constraints": [],
        "negation": False,
        "conditional": False,
        "requires_order_context": order_context,
        "requires_delivery_schedule": delivery_schedule,
        "purchase_state": purchase_state,
        "asks_delivery_schedule": asks_schedule,
        "confidence": 0.95,
    })


def make_inquiry(database: Database, question: str) -> dict[str, Any]:
    inquiries = InquiryRepository(database)
    inquiry_id = inquiries.upsert_work_item({
        "store_code": STORE,
        "source_type": "NAVER",
        "source_question_id": f"op-{next(_key)}",
        "inquiry_type": "PRODUCT_INQUIRY",
        "title": "문의",
        "content": question,
        "product_id": PRODUCT_ID,
        "product_name": PRODUCT,
        "raw_json": {},
    }).inquiry_id
    return inquiries.get(inquiry_id) or {}


def facts_for(inquiry: dict[str, Any], question: str) -> AnswerFacts:
    return AnswerFacts(
        inquiry={
            "inquiry_id": inquiry["id"],
            "question": question,
            "type": "PRODUCT_INQUIRY",
        },
        product={"product_id": PRODUCT_ID, "name": PRODUCT},
        order={},
    )


def intent_for(question: str, *questions: str) -> IntentResult:
    return IntentResult(
        "PRODUCT_GENERAL", (question, *questions), Emotion.NORMAL, "NORMAL",
        0.9, False, "test",
    )


def add_learning(
    database: Database,
    *,
    question: str,
    answer: str,
    action: str | None = None,
    learning_source: str = "APPROVED_UNEDITED",
    provenance: str = "PROGRAM_GENERATED",
) -> int:
    metadata: dict[str, Any] = {
        "learning_signal_type": "POSITIVE",
        "human_verified": True,
        "answer_provenance": provenance,
    }
    if action:
        metadata["semantic"] = {"primary_action": action}
    with database.connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO learning_examples (
                source_key, learning_source, question_original_masked,
                question_normalized, store_code, inquiry_type, product_name,
                final_answer, seller_answer, posted, rating, edit_ratio,
                quality_score, style_only, version, metadata_json, active,
                usage_count, created_at, updated_at, validity_type,
                validity_active
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f"op-l-{next(_key)}", learning_source, question, question,
                STORE, "PRODUCT_INQUIRY", PRODUCT, answer, answer, 1, 5, 0.0,
                1.0, 0, 1, json.dumps(metadata, ensure_ascii=False), 1, 0,
                "2026-08-01T00:00:00Z", "2026-08-01T00:00:00Z",
                "PERMANENT", 1,
            ),
        )
        return int(cursor.lastrowid)


def add_negative_memo(
    database: Database, *, question: str, memo: str,
    reason: str = "INTENT_NOT_REFLECTED",
) -> int:
    source = make_inquiry(database, question)
    with database.connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO learning_feedback (
                source_key, feedback_type, correction_reason, correction_note,
                learning_signal_type, source, inquiry_id, question_masked,
                metadata_json, active, created_at, updated_at
            ) VALUES (?, 'STAFF_CORRECTION', ?, ?, 'NEGATIVE',
                      'DASHBOARD_NEGATIVE_REVIEW', ?, ?, '{}', 1,
                      '2026-08-01T00:00:00Z','2026-08-01T00:00:00Z')
            """,
            (
                f"op-f-{next(_key)}", reason, memo, int(source["id"]), question,
            ),
        )
        return int(cursor.lastrowid)


def evidence_for(context: dict[str, Any], subquestion: str) -> dict[str, Any]:
    """The evidence row for one sub-question.

    A single-atomic-question inquiry keys its one row on the customer's whole
    question rather than the decomposed text -- the decomposition of a
    one-question inquiry *is* the inquiry. That is existing behaviour and the
    lookup accommodates it rather than asserting a different shape.
    """

    rows = context["subquestion_evidence"]
    for item in rows:
        if item["subquestion"] == subquestion:
            return item
    if len(rows) == 1:
        return rows[0]
    raise AssertionError(f"no evidence row for {subquestion!r}")


# ==========================================================================
# TEST 1 -- 주문 전 배송
# ==========================================================================


PRE_ORDER_QUESTION = (
    "아직 주문 안 했는데 배송일 지정 가능한가요? "
    "며칠 내 배송 가능한지도 궁금합니다."
)
PRE_ORDER_ATOMIC = [
    {"text": "주문 전에 배송일을 지정할 수 있는지", "action": "DELIVERY_POLICY"},
    {"text": "주문하면 며칠 안에 배송이 가능한지", "action": "DELIVERY_POLICY"},
]


def test_1_pre_order_delivery_requires_no_order_or_dps_lookup(database) -> None:
    inquiry = make_inquiry(database, PRE_ORDER_QUESTION)

    plan = InquiryProcessingPlanService(database).create(
        inquiry,
        semantic_analysis=semantic(
            "DELIVERY_POLICY", atomic=PRE_ORDER_ATOMIC,
            purchase_state="PRE_PURCHASE", asks_schedule=True,
        ),
    )

    assert plan.requires_order_lookup is False
    assert plan.requires_dps_lookup is False
    assert plan.order_id_status == "NOT_REQUIRED"


def test_1_pre_order_delivery_never_reuses_an_order_number_request(
    database,
) -> None:
    order_scoped = add_learning(
        database,
        question="제 주문 배송이 언제 되나요?",
        answer=(
            "확인을 위해 네이버 주문내역의 주문번호를 비밀글로 남겨주시면 "
            "배송예정일을 안내드리겠습니다."
        ),
        action="DELIVERY_STATUS",
    )
    policy = add_learning(
        database,
        question="주문하면 배송까지 며칠 걸리나요?",
        answer="결제 완료 후 평균 2주 정도 배송 기간이 소요됩니다.",
        action="DELIVERY_POLICY",
    )
    inquiry = make_inquiry(database, PRE_ORDER_QUESTION)

    context = LearningContextService(database).build(
        facts_for(inquiry, PRE_ORDER_QUESTION),
        intent_for(*[item["text"] for item in PRE_ORDER_ATOMIC]),
        semantic_analysis=semantic(
            "DELIVERY_POLICY", atomic=PRE_ORDER_ATOMIC,
            purchase_state="PRE_PURCHASE", asks_schedule=True,
        ),
    )

    selected = [
        int(item["learning_example_id"])
        for item in context["similar_approved_answers"]
    ]
    assert order_scoped not in selected, (
        "주문이 없는 고객에게 주문번호를 요구하는 답변이 근거가 되면 안 된다"
    )
    assert policy in selected
    assert (
        context["learning_retrieval"]["rejection_counts"]["ORDER_SCOPE_MISMATCH"]
        >= 1
    )
    assert (
        context["learning_retrieval"]["semantic_goal"]["primary_action"]
        == "DELIVERY_POLICY"
    )


def test_1_a_real_current_schedule_question_still_requires_dps(database) -> None:
    """주문 전 문의의 DPS 억제가 진짜 배송조회의 DPS blocker를 약화시키지 않는다."""

    question = "제가 주문한 상품 배송예정일이 언제인가요?"
    atomic = [{"text": question, "action": "DELIVERY_STATUS"}]
    inquiry = make_inquiry(database, question)

    understanding = semantic(
        "DELIVERY_STATUS", atomic=atomic,
        order_context=True, delivery_schedule=True,
        purchase_state="CURRENT_ORDER", asks_schedule=True,
    )
    plan = InquiryProcessingPlanService(database).create(
        inquiry, semantic_analysis=understanding,
    )
    context = LearningContextService(database).build(
        facts_for(inquiry, question), intent_for(question),
        semantic_analysis=understanding,
    )

    assert plan.requires_order_lookup is True
    assert plan.requires_dps_lookup is True
    evidence = evidence_for(context, question)
    assert evidence["status"] == "NEEDS_DPS"
    assert evidence["source"] == "CURRENT_DPS_REQUIRED"
    assert evidence["evidence_coverage"] == "DEFERRED_TO_CURRENT_FACT"


def test_1_pre_order_delivery_applies_the_pre_order_correction_memo(
    database,
) -> None:
    """운영 스냅샷의 실제 메모 형태를 그대로 쓴 fixture.

    "아직 구매하지 않은 고객의 문의에 대한 답변이 잘못됨. 지금 결제하면
    배송기간이 얼마나 걸리는지를 안내 하는 답변이 돼야함" -- 16-1 이 요구하는
    교정이 이미 운영자 손으로 쓰여 있었고, 지금까지 읽힌 적이 없었다.
    """

    feedback_id = add_negative_memo(
        database,
        question="해외직구인가요? 배송은 얼마나 걸리나요?",
        memo=(
            "해외직구 여부에 대한 설명은 잘했으나 아직 구매하지 않은 고객의 "
            "문의에 대한 답변이 잘못됨. 지금 결제하면 배송기간이 얼마나 "
            "걸리는지를 안내 하는 답변이 돼야함."
        ),
        reason="DELIVERY_INSTALLATION_ERROR",
    )
    inquiry = make_inquiry(database, PRE_ORDER_QUESTION)

    context = LearningContextService(database).build(
        facts_for(inquiry, PRE_ORDER_QUESTION),
        intent_for(*[item["text"] for item in PRE_ORDER_ATOMIC]),
        semantic_analysis=semantic(
            "DELIVERY_POLICY", atomic=PRE_ORDER_ATOMIC,
            purchase_state="PRE_PURCHASE", asks_schedule=True,
        ),
    )

    corrections = context["negative_corrections"]
    assert [item["correction_id"] for item in corrections] == [feedback_id]
    assert any("배송기간" in text for text in corrections[0]["corrections"])
    # 제약일 뿐, 근거로 승격되지 않는다.
    for item in context["subquestion_evidence"]:
        assert item["source"] != "VERIFIED_FEEDBACK_SIGNAL"


def test_1_pre_order_delivery_is_held_by_policy_not_merely_unsupported(
    database,
) -> None:
    """확정 운영정책 이후의 기대값.

    이전 단계에서는 "근거가 없으면 NO_RELIABLE_SOURCE" 로 검증했다. 지금은
    근거의 유무와 무관하게 구매 전 배송 문의 자체를 자동답변 대상에서 뺀다 --
    Learning 이 있든 없든 결과는 직원 검토다.
    """

    inquiry = make_inquiry(database, PRE_ORDER_QUESTION)

    context = LearningContextService(database).build(
        facts_for(inquiry, PRE_ORDER_QUESTION),
        intent_for(*[item["text"] for item in PRE_ORDER_ATOMIC]),
        semantic_analysis=semantic(
            "DELIVERY_POLICY", atomic=PRE_ORDER_ATOMIC,
            purchase_state="PRE_PURCHASE", asks_schedule=True,
        ),
    )

    for item in context["subquestion_evidence"]:
        assert item["status"] == "DELIVERY_SCHEDULE_REVIEW"
        assert item["source"] == "DELIVERY_SCHEDULE_UNCONFIRMED_PURCHASE"
        assert item["evidence_coverage"] == "UNSUPPORTED"
        assert item["answer_required"] is False


# ==========================================================================
# TEST 2 -- 리뷰 보상 지급 시점
# ==========================================================================


REVIEW_QUESTION = "포토리뷰 네이버페이 2만원은 언제 받나요?"
REVIEW_ATOMIC = [
    {"text": "포토리뷰 네이버페이 보상을 언제 받는지", "action": "BENEFIT"},
]


def test_2_review_reward_timing_needs_no_order_or_dps(database) -> None:
    inquiry = make_inquiry(database, REVIEW_QUESTION)

    plan = InquiryProcessingPlanService(database).create(
        inquiry, semantic_analysis=semantic("BENEFIT", atomic=REVIEW_ATOMIC),
    )

    assert plan.requires_order_lookup is False
    assert plan.requires_dps_lookup is False


def test_2_review_reward_timing_prefers_the_payout_timing_learning(
    database,
) -> None:
    timing = add_learning(
        database,
        question="포토리뷰 네이버페이 포인트는 언제 지급되나요?",
        answer=(
            "포토리뷰 네이버페이 포인트는 리뷰 등록 확인 후 익월 중 "
            "지급됩니다."
        ),
        action="BENEFIT",
    )
    how_to_write = add_learning(
        database,
        question="포토리뷰는 어떻게 작성하나요?",
        answer="포토리뷰는 구매한 상품 사진과 함께 리뷰를 등록해 주시면 됩니다.",
        action="FORM_FIELD_GUIDANCE",
    )
    inquiry = make_inquiry(database, REVIEW_QUESTION)

    context = LearningContextService(database).build(
        facts_for(inquiry, REVIEW_QUESTION),
        intent_for(REVIEW_ATOMIC[0]["text"]),
        semantic_analysis=semantic("BENEFIT", atomic=REVIEW_ATOMIC),
    )

    selected = [
        int(item["learning_example_id"])
        for item in context["similar_approved_answers"]
    ]
    assert selected and selected[0] == timing
    assert how_to_write not in selected, (
        "같은 이벤트 주제라도 requested information 이 다르면 근거가 아니다"
    )
    evidence = evidence_for(context, REVIEW_ATOMIC[0]["text"])
    assert evidence["status"] == "ANSWERABLE"
    assert evidence["source"] == "ACTIVE_POSITIVE_LEARNING"
    assert evidence["evidence_coverage"] == "SUPPORTED"
    assert evidence["learning_ids"] == [timing]


# ==========================================================================
# TEST 3/4 -- 신청 후 확인 vs 신청 방법
# ==========================================================================


ALREADY_APPLIED_QUESTION = "상품권 신청 얼마 전에 했는데 확인 부탁드려요."
ALREADY_APPLIED_ATOMIC = [
    {"text": "얼마 전 신청한 상품권 건이 처리되었는지 확인", "action": "OTHER"},
]
HOW_TO_APPLY_QUESTION = "상품권은 어디서 신청하나요?"
HOW_TO_APPLY_ATOMIC = [
    {"text": "상품권을 어디서 신청하는지", "action": "FORM_FIELD_GUIDANCE"},
]


def _application_corpus(database: Database) -> int:
    return add_learning(
        database,
        question="상품권은 어디서 신청하나요?",
        answer="상품권 신청은 상품 상세페이지의 신청 링크에서 진행해 주세요.",
        action="FORM_FIELD_GUIDANCE",
    )


def test_3_already_applied_is_not_answered_with_how_to_apply(database) -> None:
    how_to_apply = _application_corpus(database)
    inquiry = make_inquiry(database, ALREADY_APPLIED_QUESTION)

    context = LearningContextService(database).build(
        facts_for(inquiry, ALREADY_APPLIED_QUESTION),
        intent_for(ALREADY_APPLIED_ATOMIC[0]["text"]),
        semantic_analysis=semantic("OTHER", atomic=ALREADY_APPLIED_ATOMIC),
    )

    selected = [
        int(item["learning_example_id"])
        for item in context["similar_approved_answers"]
    ]
    assert how_to_apply not in selected, (
        "이미 신청한 고객에게 신청 방법을 다시 안내하면 안 된다"
    )
    evidence = evidence_for(context, ALREADY_APPLIED_ATOMIC[0]["text"])
    assert evidence["status"] == "NO_RELIABLE_SOURCE"
    assert evidence["evidence_coverage"] == "UNSUPPORTED"


def test_3_a_negative_memo_about_this_mistake_becomes_a_constraint(
    database,
) -> None:
    _application_corpus(database)
    feedback_id = add_negative_memo(
        database,
        question="상품권 신청했는데 확인해주세요",
        memo=(
            "이미 신청한 고객임. 신청 방법을 다시 안내하면 잘못됨. "
            "신청 접수 여부를 확인 후 안내해야함."
        ),
    )
    inquiry = make_inquiry(database, ALREADY_APPLIED_QUESTION)

    context = LearningContextService(database).build(
        facts_for(inquiry, ALREADY_APPLIED_QUESTION),
        intent_for(ALREADY_APPLIED_ATOMIC[0]["text"]),
        semantic_analysis=semantic("OTHER", atomic=ALREADY_APPLIED_ATOMIC),
    )

    corrections = context["negative_corrections"]
    assert [item["correction_id"] for item in corrections] == [feedback_id]
    assert any("잘못됨" in text for text in corrections[0]["bad_patterns"])
    assert any("확인" in text for text in corrections[0]["corrections"])
    # Constraint, never evidence: the sub-question is still unanswered.
    evidence = evidence_for(context, ALREADY_APPLIED_ATOMIC[0]["text"])
    assert evidence["status"] == "NO_RELIABLE_SOURCE"
    assert context["negative_correction_policy"][
        "correction_scope_is_the_named_claim_only"
    ] is True


def test_4_how_to_apply_is_a_different_atomic_question(database) -> None:
    how_to_apply = _application_corpus(database)
    inquiry = make_inquiry(database, HOW_TO_APPLY_QUESTION)

    context = LearningContextService(database).build(
        facts_for(inquiry, HOW_TO_APPLY_QUESTION),
        intent_for(HOW_TO_APPLY_ATOMIC[0]["text"]),
        semantic_analysis=semantic(
            "FORM_FIELD_GUIDANCE", atomic=HOW_TO_APPLY_ATOMIC,
        ),
    )

    selected = [
        int(item["learning_example_id"])
        for item in context["similar_approved_answers"]
    ]
    assert how_to_apply in selected, (
        "신청 방법 문의에는 신청 방법 Learning 이 근거가 되어야 한다"
    )


# ==========================================================================
# TEST 5 -- 폐가전: Positive 와 Negative correction 이 같이 쓰인다
# ==========================================================================


COLLECTION_QUESTION = "폐가전 수거 가능한가요?"
COLLECTION_ATOMIC = [
    {"text": "폐가전 무료수거가 가능한지", "action": "COLLECTION"},
]


def test_5_collection_uses_the_positive_fact_and_the_correction_together(
    database,
) -> None:
    positive = add_learning(
        database,
        question="폐가전 무료수거 되나요?",
        answer="폐가전은 무료수거가 가능합니다.",
        action="COLLECTION",
    )
    feedback_id = add_negative_memo(
        database,
        question="폐가전 수거도 해주시나요?",
        memo=(
            "폐가전 무료수거 안내는 맞음. 고객센터 별도 신청 안내는 잘못됨. "
            "설치기사 방문 시 수거 요청하도록 안내."
        ),
    )
    inquiry = make_inquiry(database, COLLECTION_QUESTION)

    context = LearningContextService(database).build(
        facts_for(inquiry, COLLECTION_QUESTION),
        intent_for(COLLECTION_ATOMIC[0]["text"]),
        semantic_analysis=semantic("COLLECTION", atomic=COLLECTION_ATOMIC),
    )

    selected = [
        int(item["learning_example_id"])
        for item in context["similar_approved_answers"]
    ]
    corrections = context["negative_corrections"]

    assert positive in selected, "맞는 Positive 사실은 그대로 남아야 한다"
    assert [item["correction_id"] for item in corrections] == [feedback_id]
    assert any("고객센터" in text for text in corrections[0]["bad_patterns"])
    assert any("설치기사" in text for text in corrections[0]["corrections"])
    assert any("맞음" in text for text in corrections[0]["good_patterns"])

    evidence = evidence_for(context, COLLECTION_ATOMIC[0]["text"])
    assert evidence["status"] == "ANSWERABLE"
    assert evidence["source"] == "ACTIVE_POSITIVE_LEARNING"
    assert evidence["learning_ids"] == [positive]


def test_5_the_wrong_application_route_is_not_grounded_by_the_correction(
    database,
) -> None:
    """BAD_PATTERN 은 어떤 주장도 뒷받침하지 못한다."""

    from services.hybrid_answer_service import HybridAnswerService

    add_learning(
        database,
        question="폐가전 무료수거 되나요?",
        answer="폐가전은 무료수거가 가능합니다.",
        action="COLLECTION",
    )
    add_negative_memo(
        database,
        question="폐가전 수거도 해주시나요?",
        memo=(
            "폐가전 무료수거 안내는 맞음. 고객센터 별도 신청 안내는 잘못됨. "
            "설치기사 방문 시 수거 요청하도록 안내."
        ),
    )
    inquiry = make_inquiry(database, COLLECTION_QUESTION)
    context = LearningContextService(database).build(
        facts_for(inquiry, COLLECTION_QUESTION),
        intent_for(COLLECTION_ATOMIC[0]["text"]),
        semantic_analysis=semantic("COLLECTION", atomic=COLLECTION_ATOMIC),
    )

    grounded = HybridAnswerService._evidence_texts(context)

    assert "설치기사" in grounded, "교정 방향은 근거로 인용될 수 있어야 한다"
    assert "고객센터" not in grounded, (
        "잘못된 내용이 답변의 근거가 되면 안 된다"
    )


def test_5_a_collection_negative_does_not_reach_an_as_question(database) -> None:
    add_negative_memo(
        database,
        question="폐가전 수거도 해주시나요?",
        memo=(
            "폐가전 무료수거 안내는 맞음. 고객센터 별도 신청 안내는 잘못됨. "
            "설치기사 방문 시 수거 요청하도록 안내."
        ),
    )
    question = "국내 삼성 서비스센터에서 A/S 받을 수 있나요?"
    inquiry = make_inquiry(database, question)

    context = LearningContextService(database).build(
        facts_for(inquiry, question),
        intent_for("국내 삼성 서비스센터에서 A/S 가능한지"),
        semantic_analysis=semantic("REPAIR", atomic=[
            {"text": "국내 삼성 서비스센터에서 A/S 가능한지", "action": "REPAIR"},
        ]),
    )

    assert context["negative_corrections"] == []
    assert "negative_correction_policy" not in context


# ==========================================================================
# prompt contract -- 교정 지식이 실제로 GPT 프롬프트에 도달하는지
# ==========================================================================


def test_negative_correction_instructions_reach_the_draft_prompt(
    database,
) -> None:
    from answer.prompt_builder import PromptBuilder
    from services.learning_context_service import prompt_context

    add_negative_memo(
        database,
        question="폐가전 수거도 해주시나요?",
        memo=(
            "폐가전 무료수거 안내는 맞음. 고객센터 별도 신청 안내는 잘못됨. "
            "설치기사 방문 시 수거 요청하도록 안내."
        ),
    )
    inquiry = make_inquiry(database, COLLECTION_QUESTION)
    context = LearningContextService(database).build(
        facts_for(inquiry, COLLECTION_QUESTION),
        intent_for(COLLECTION_ATOMIC[0]["text"]),
        semantic_analysis=semantic("COLLECTION", atomic=COLLECTION_ATOMIC),
    )
    evidence = prompt_context(context)
    assert evidence["negative_corrections"]

    prompt = PromptBuilder().build(
        task="DRAFT",
        facts=facts_for(inquiry, COLLECTION_QUESTION),
        extra=evidence,
    )

    assert "negative_correction_instructions" in prompt
    assert "설치기사" in prompt
    # Retrieval traces stay out of the prompt, as they already did.
    assert "learning_retrieval" not in prompt


def test_no_negative_correction_leaves_the_prompt_contract_unchanged(
    database,
) -> None:
    from answer.prompt_builder import PromptBuilder
    from services.learning_context_service import prompt_context

    inquiry = make_inquiry(database, COLLECTION_QUESTION)
    context = LearningContextService(database).build(
        facts_for(inquiry, COLLECTION_QUESTION),
        intent_for(COLLECTION_ATOMIC[0]["text"]),
        semantic_analysis=semantic("COLLECTION", atomic=COLLECTION_ATOMIC),
    )

    prompt = PromptBuilder().build(
        task="DRAFT",
        facts=facts_for(inquiry, COLLECTION_QUESTION),
        extra=prompt_context(context),
    )

    assert context["negative_corrections"] == []
    assert "negative_correction_instructions" not in prompt
