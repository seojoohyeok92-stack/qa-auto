from __future__ import annotations

from typing import Any

from answer.facts import AnswerFacts
from answer.hybrid_models import DraftResult, Emotion, IntentResult, SelfReviewResult
from answer.learning_signal import OriginKind, SignalKind
from answer.answer_validator import AnswerValidator
from repositories.database import Database
from repositories.feedback_signal_provenance_repository import (
    FeedbackSignalProvenanceRepository,
)
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_repository import LearningRepository
from repositories.learning_signal_repository import LearningSignalRepository
from services.learning_context_service import LearningContextService
from services.learning_signal_service import LearningSignalService


STORE_CODE = "OJE_PLUS"


def make_inquiry(database: Database, *, product_id: str, product_name: str, question: str) -> dict[str, Any]:
    inquiries = InquiryRepository(database)
    inquiry_id = inquiries.upsert_work_item(
        {
            "store_code": STORE_CODE,
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": f"SIGNAL-{product_id}-{abs(hash(question)) % 100000}",
            "inquiry_type": "PRODUCT_INQUIRY",
            "title": "상품 문의",
            "content": question,
            "product_id": product_id,
            "product_name": product_name,
            "raw_json": {},
        }
    ).inquiry_id
    return inquiries.get(inquiry_id) or {}


def facts_for(inquiry_id: int, *, question: str, product_id: str, product_name: str) -> AnswerFacts:
    return AnswerFacts(
        inquiry={"inquiry_id": inquiry_id, "question": question, "type": "PRODUCT_INQUIRY"},
        product={"product_id": product_id, "name": product_name},
        order={},
    )


def intent_for(question: str) -> IntentResult:
    return IntentResult(
        "PRODUCT_GENERAL", (question,), Emotion.NORMAL, "NORMAL", 0.9, False, "test",
    )


def capture_signal(
    database: Database,
    *,
    signal_kind: SignalKind,
    content_text: str,
    question: str,
    product_id: str,
    product_name: str,
    origin_kind: OriginKind = OriginKind.NEGATIVE_REVIEW,
    fact_scope: str | None = None,
) -> dict[str, Any] | None:
    service = LearningSignalService(database)
    return service.capture(
        origin_kind=origin_kind,
        signal_kind=signal_kind,
        content_text=content_text,
        inquiry={
            "id": None,
            "store_code": STORE_CODE,
            "product_name": product_name,
            "product_id": product_id,
        },
        question=question,
        product_name=product_name,
        product_id=product_id,
        fact_scope=fact_scope,
    )


# ---------------------------------------------------------------------------
# Structured signal model basics
# ---------------------------------------------------------------------------


def test_reason_only_memo_creates_no_retrievable_signal(tmp_path) -> None:
    database = Database(tmp_path / "signal.db")
    database.initialize()
    result = capture_signal(
        database,
        signal_kind=SignalKind.REASON,
        content_text="답변이 다소 길다",
        question="상품 문의",
        product_id="P1",
        product_name="삼성 TV",
    )
    assert result is None
    assert LearningSignalRepository(database).candidates(store_code=STORE_CODE) == []


def test_empty_content_creates_no_signal_even_if_kind_selected(tmp_path) -> None:
    database = Database(tmp_path / "signal.db")
    database.initialize()
    result = capture_signal(
        database,
        signal_kind=SignalKind.VERIFIED_FACT,
        content_text="   ",
        question="상품 문의",
        product_id="P1",
        product_name="삼성 TV",
    )
    assert result is None


# ---------------------------------------------------------------------------
# Case A -- Verified Fact used when Learning answer itself is insufficient
# ---------------------------------------------------------------------------


def test_case_a_verified_fact_is_retrieved_and_answerable(tmp_path) -> None:
    database = Database(tmp_path / "signal.db")
    database.initialize()
    capture_signal(
        database,
        signal_kind=SignalKind.VERIFIED_FACT,
        content_text="운영 확인 결과 제주도 배송 및 설치가 가능합니다.",
        question="제주도도 배송 설치가 가능한가요",
        product_id="TV-1",
        product_name="삼성 TV",
        origin_kind=OriginKind.POSITIVE_REVIEW,
    )
    inquiry = make_inquiry(
        database, product_id="TV-1", product_name="삼성 TV",
        question="제주도도 배송설치 가능한가요?",
    )
    context = LearningContextService(database).build(
        facts_for(inquiry["id"], question="제주도도 배송설치 가능한가요?",
                  product_id="TV-1", product_name="삼성 TV"),
        intent_for("제주도도 배송설치 가능한가요?"),
    )
    assert context["feedback_signals"]["verified_facts"], context["feedback_signals"]
    evidence = context["subquestion_evidence"][0]
    assert evidence["status"] == "ANSWERABLE"
    assert evidence["source"] == "VERIFIED_FEEDBACK_SIGNAL"
    assert evidence["feedback_signal_ids"]


# ---------------------------------------------------------------------------
# Case B -- Correction outranks a plain Positive Learning answer
# ---------------------------------------------------------------------------


def test_case_b_correction_outranks_stale_positive_learning(tmp_path) -> None:
    database = Database(tmp_path / "signal.db")
    database.initialize()
    source = make_inquiry(
        database, product_id="TV-2", product_name="삼성 TV 55",
        question="제주도 배송 가능 여부",
    )
    LearningRepository(database).upsert(
        {
            "source_key": "stale-jeju-answer",
            "learning_source": "APPROVED_EDITED",
            "inquiry_id": source["id"],
            "answer_draft_id": None,
            "approval_history_id": None,
            "question_original_masked": "제주도 배송 가능한가요?",
            "question_normalized": "제주도 배송 가능한가요",
            "store_code": STORE_CODE,
            "inquiry_type": "PRODUCT_INQUIRY",
            "intent": "GENERAL",
            "product_name": "삼성 TV 55",
            "model_code": None,
            "final_answer": "제주도 배송 관련 사항은 상품별로 상이하니 상세페이지를 참고해 주세요.",
            "rating": 5,
            "quality_score": 1.0,
            "generation_mode": "TEST",
            "template_id": None,
            "processing_route": "TEST",
            "validator_result": "HUMAN_VERIFIED_NAVER_POSTED",
            "posted": True,
            "auto_posted": False,
            "edit_ratio": 0.0,
            "style_only": False,
            "version": 1,
            "style_features_json": {},
            "metadata_json": {"human_verified": True, "learning_signal_type": "POSITIVE"},
            "active": True,
        }
    )
    capture_signal(
        database,
        signal_kind=SignalKind.CORRECTION,
        content_text="운영 확인 결과 제주도 배송 및 설치가 가능합니다. 기존 안내는 정정합니다.",
        question="제주도 배송 가능한가요?",
        product_id="TV-2",
        product_name="삼성 TV 55",
    )
    inquiry = make_inquiry(
        database, product_id="TV-2", product_name="삼성 TV 55",
        question="제주도 배송 가능한가요?",
    )
    context = LearningContextService(database).build(
        facts_for(inquiry["id"], question="제주도 배송 가능한가요?",
                  product_id="TV-2", product_name="삼성 TV 55"),
        intent_for("제주도 배송 가능한가요?"),
    )
    evidence = context["subquestion_evidence"][0]
    assert evidence["source"] == "VERIFIED_FEEDBACK_SIGNAL"
    assert evidence["status"] == "ANSWERABLE"


# ---------------------------------------------------------------------------
# Case C -- Negative BAD_PATTERN guidance, never factual evidence
# ---------------------------------------------------------------------------


def test_case_c_bad_pattern_is_guidance_not_evidence(tmp_path) -> None:
    database = Database(tmp_path / "signal.db")
    database.initialize()
    capture_signal(
        database,
        signal_kind=SignalKind.BAD_PATTERN,
        content_text="질문과 무관한 OTT 앱 설명을 하지 말 것.",
        question="넷플릭스 앱 지원되나요?",
        product_id="TV-3",
        product_name="삼성 TV OTT",
    )
    inquiry = make_inquiry(
        database, product_id="TV-3", product_name="삼성 TV OTT",
        question="넷플릭스 앱 지원되나요?",
    )
    context = LearningContextService(database).build(
        facts_for(inquiry["id"], question="넷플릭스 앱 지원되나요?",
                  product_id="TV-3", product_name="삼성 TV OTT"),
        intent_for("넷플릭스 앱 지원되나요?"),
    )
    assert context["feedback_signals"]["bad_patterns"]
    assert context["feedback_signals"]["verified_facts"] == []
    assert context["feedback_signals"]["corrections"] == []
    evidence = context["subquestion_evidence"][0]
    assert evidence["source"] != "VERIFIED_FEEDBACK_SIGNAL"
    assert evidence["status"] == "NO_RELIABLE_SOURCE"


# ---------------------------------------------------------------------------
# Case D -- Scope mismatch: product-specific fact must not leak cross-product
# ---------------------------------------------------------------------------


def test_case_d_scope_mismatch_blocks_cross_product_fact(tmp_path) -> None:
    database = Database(tmp_path / "signal.db")
    database.initialize()
    service = LearningSignalService(database)
    service.capture(
        origin_kind=OriginKind.POSITIVE_REVIEW,
        signal_kind=SignalKind.VERIFIED_FACT,
        content_text="이 모델은 HDMI 포트가 2개입니다.",
        inquiry={"id": None, "store_code": STORE_CODE},
        question="HDMI 포트가 몇 개인가요?",
        product_id="MODEL-A",
        product_name="삼성 TV A",
        model_code="LH50AAA",
    )
    result = service.retrieve(
        "HDMI 포트가 몇 개인가요?",
        store_code=STORE_CODE,
        product_id="MODEL-B",
        product_name="삼성 TV B",
        model_code="LH55BBB",
    )
    assert result["verified_facts"] == []
    assert result["trace"]["rejection_counts"].get("MODEL_MISMATCH", 0) >= 1


# ---------------------------------------------------------------------------
# Case E -- Topic mismatch: same product, unrelated topic fact must not apply
# ---------------------------------------------------------------------------


def test_case_e_topic_mismatch_blocks_unrelated_fact(tmp_path) -> None:
    database = Database(tmp_path / "signal.db")
    database.initialize()
    service = LearningSignalService(database)
    service.capture(
        origin_kind=OriginKind.POSITIVE_REVIEW,
        signal_kind=SignalKind.VERIFIED_FACT,
        content_text="이 모델은 HDMI 포트가 2개입니다.",
        inquiry={"id": None, "store_code": STORE_CODE},
        question="HDMI 포트가 몇 개인가요?",
        product_id="MODEL-A",
        product_name="삼성 TV A",
        model_code="LH50AAA",
    )
    result = service.retrieve(
        "리모컨은 별도로 구매해야 하나요?",
        store_code=STORE_CODE,
        product_id="MODEL-A",
        product_name="삼성 TV A",
        model_code="LH50AAA",
    )
    assert result["verified_facts"] == []


# ---------------------------------------------------------------------------
# Case F -- Conflicting ACTIVE VERIFIED_FACTs must not be auto-resolved by GPT
# ---------------------------------------------------------------------------


def test_case_f_conflicting_facts_are_withheld_and_flagged(tmp_path) -> None:
    database = Database(tmp_path / "signal.db")
    database.initialize()
    capture_signal(
        database,
        signal_kind=SignalKind.VERIFIED_FACT,
        content_text="운영 확인 결과 제주도 배송이 가능합니다.",
        question="제주도 배송 가능한가요?",
        product_id="TV-4",
        product_name="삼성 TV 65",
        origin_kind=OriginKind.POSITIVE_REVIEW,
    )
    capture_signal(
        database,
        signal_kind=SignalKind.VERIFIED_FACT,
        content_text="운영 확인 결과 제주도 배송이 불가능합니다.",
        question="제주도 배송 가능한가요?",
        product_id="TV-4",
        product_name="삼성 TV 65",
        origin_kind=OriginKind.POSITIVE_REVIEW,
    )
    inquiry = make_inquiry(
        database, product_id="TV-4", product_name="삼성 TV 65",
        question="제주도 배송 가능한가요?",
    )
    context = LearningContextService(database).build(
        facts_for(inquiry["id"], question="제주도 배송 가능한가요?",
                  product_id="TV-4", product_name="삼성 TV 65"),
        intent_for("제주도 배송 가능한가요?"),
    )
    assert context["feedback_signals"]["verified_facts"] == []
    evidence = context["subquestion_evidence"][0]
    assert evidence["status"] == "CONFLICT"

    validator = AnswerValidator()
    validation = validator.validate(
        facts_for(inquiry["id"], question="제주도 배송 가능한가요?",
                  product_id="TV-4", product_name="삼성 TV 65"),
        intent_for("제주도 배송 가능한가요?"),
        DraftResult(answer="제주도 배송이 가능합니다.", confidence=0.9),
        SelfReviewResult(
            passed=True, answered_all_questions=True, has_speculation=False,
            facts_consistent=True, requires_review=False, reason="ok",
            warnings=(),
        ),
        subquestion_evidence=context["subquestion_evidence"],
    )
    assert any(
        rule.code == "VERIFIED_FACT_CONFLICT" and rule.status == "REVIEW_REQUIRED"
        for rule in validation.rules
    )


# ---------------------------------------------------------------------------
# Case G -- No verified fact in DB: system must not fabricate one
# ---------------------------------------------------------------------------


def test_case_g_no_verified_fact_means_no_fabrication(tmp_path) -> None:
    database = Database(tmp_path / "signal.db")
    database.initialize()
    inquiry = make_inquiry(
        database, product_id="TV-5", product_name="삼성 125.7cm TV",
        question="제주도도 배송설치 가능한가요?",
    )
    context = LearningContextService(database).build(
        facts_for(inquiry["id"], question="제주도도 배송설치 가능한가요?",
                  product_id="TV-5", product_name="삼성 125.7cm TV"),
        intent_for("제주도도 배송설치 가능한가요?"),
    )
    assert context["feedback_signals"]["verified_facts"] == []
    assert context["feedback_signals"]["corrections"] == []
    evidence = context["subquestion_evidence"][0]
    assert evidence["source"] != "VERIFIED_FEEDBACK_SIGNAL"


# ---------------------------------------------------------------------------
# Case H -- 685875593-style structural check: only fires once an operator
# has actually registered the fact, and works across phrasing variants.
# ---------------------------------------------------------------------------


def test_case_h_jeju_fact_is_retrieved_across_phrasing_variants(tmp_path) -> None:
    database = Database(tmp_path / "signal.db")
    database.initialize()
    capture_signal(
        database,
        signal_kind=SignalKind.VERIFIED_FACT,
        content_text="제주도 배송 및 설치 가능. 제주도 배송 가능 여부 문의에는 가능하다고 안내.",
        question="제주도 배송 설치 가능한가요?",
        product_id="TV-6",
        product_name="삼성 125.7cm(50인치) 스마트 비즈니스 TV",
        origin_kind=OriginKind.POSITIVE_REVIEW,
    )
    service = LearningSignalService(database)
    for variant in (
        "제주도도 배송설치 가능한가요?",
        "제주 지역도 배송 설치 가능한가요?",
    ):
        result = service.retrieve(
            variant, store_code=STORE_CODE, product_id="TV-6",
            product_name="삼성 125.7cm(50인치) 스마트 비즈니스 TV",
        )
        assert result["verified_facts"], (variant, result["trace"])


# ---------------------------------------------------------------------------
# Case I -- Current/DPS authority is never weakened by a Verified Fact
# ---------------------------------------------------------------------------


def test_case_i_current_dps_authority_preserved_over_verified_fact(tmp_path) -> None:
    database = Database(tmp_path / "signal.db")
    database.initialize()
    capture_signal(
        database,
        signal_kind=SignalKind.VERIFIED_FACT,
        content_text="설치 예정일은 통상 결제 후 7일 이내입니다.",
        question="설치 예정일이 언제인가요?",
        product_id="TV-7",
        product_name="삼성 TV 7",
        origin_kind=OriginKind.POSITIVE_REVIEW,
    )
    inquiry = make_inquiry(
        database, product_id="TV-7", product_name="삼성 TV 7",
        question="설치 예정일이 언제인가요?",
    )
    facts = AnswerFacts(
        inquiry={
            "inquiry_id": inquiry["id"], "question": "설치 예정일이 언제인가요?",
            "type": "PRODUCT_INQUIRY",
        },
        product={"product_id": "TV-7", "name": "삼성 TV 7"},
        order={"order_id": "2026070500000001"},
        installation={
            "installation_date_confirmed": True, "date": "2026-08-30",
            "required_delivery_date": "2026-08-30",
        },
    )
    context = LearningContextService(database).build(
        facts, intent_for("설치 예정일이 언제인가요?"),
    )
    evidence = context["subquestion_evidence"][0]
    assert evidence["status"] == "ANSWERABLE"
    assert evidence["source"] == "CURRENT_DPS"


# ---------------------------------------------------------------------------
# Case J -- No structured feedback at all: behavior must be unchanged
# ---------------------------------------------------------------------------


def test_case_j_no_feedback_signals_is_a_no_op(tmp_path) -> None:
    database = Database(tmp_path / "signal.db")
    database.initialize()
    inquiry = make_inquiry(
        database, product_id="TV-8", product_name="삼성 TV 8",
        question="배송은 언제 되나요?",
    )
    context = LearningContextService(database).build(
        facts_for(inquiry["id"], question="배송은 언제 되나요?",
                  product_id="TV-8", product_name="삼성 TV 8"),
        intent_for("배송은 언제 되나요?"),
    )
    signals = context["feedback_signals"]
    assert signals == {
        "verified_facts": [], "corrections": [], "good_patterns": [],
        "bad_patterns": [], "unresolved_conflicts": False,
    }
    for evidence in context["subquestion_evidence"]:
        assert evidence["source"] != "VERIFIED_FEEDBACK_SIGNAL"
        assert evidence["feedback_signal_ids"] == []


# ---------------------------------------------------------------------------
# Provenance: Feedback Signal retrieval/attachment/usage is tracked
# ---------------------------------------------------------------------------


def test_feedback_signal_provenance_is_recorded_on_context_build(tmp_path) -> None:
    database = Database(tmp_path / "signal.db")
    database.initialize()
    capture_signal(
        database,
        signal_kind=SignalKind.VERIFIED_FACT,
        content_text="운영 확인 결과 제주도 배송 및 설치가 가능합니다.",
        question="제주도도 배송 설치가 가능한가요",
        product_id="TV-9",
        product_name="삼성 TV 9",
        origin_kind=OriginKind.POSITIVE_REVIEW,
    )
    inquiry = make_inquiry(
        database, product_id="TV-9", product_name="삼성 TV 9",
        question="제주도도 배송설치 가능한가요?",
    )
    LearningContextService(database).build(
        facts_for(inquiry["id"], question="제주도도 배송설치 가능한가요?",
                  product_id="TV-9", product_name="삼성 TV 9"),
        intent_for("제주도도 배송설치 가능한가요?"),
    )
    with database.connection() as connection:
        rows = connection.execute(
            "SELECT * FROM answer_feedback_signal_provenance WHERE inquiry_id=?",
            (inquiry["id"],),
        ).fetchall()
    assert rows
    assert rows[0]["signal_kind"] == "VERIFIED_FACT"
    assert rows[0]["usage_status"] == "PENDING"
