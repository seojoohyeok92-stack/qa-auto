from __future__ import annotations

from pathlib import Path

import pytest

from answer.answer_validator import AnswerValidator
from answer.facts import AnswerFacts
from answer.hybrid_models import (
    DraftResult,
    Emotion,
    IntentResult,
    SelfReviewResult,
)
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_repository import LearningRepository
from services.learning_compatibility_service import (
    LearningCompatibilityService,
    extract_product_identity,
)
from services.auto_processing_eligibility_service import (
    AutoProcessingEligibilityService,
)
from services.similar_answer_service import SimilarAnswerService
from services.draft_generation_service import DraftGenerationService


def _identity(
    name: str | None = None,
    *,
    product_id: str | None = None,
    model_code: str | None = None,
    option: str | None = None,
):
    return extract_product_identity(
        product_id=product_id,
        product_name=name,
        model_code=model_code,
        option=option,
    )


def _decision(
    current_question: str,
    candidate_question: str,
    candidate_answer: str,
    *,
    current_name: str = "삼성 85인치 TV QN85A",
    candidate_name: str = "삼성 85인치 TV QN85A",
    current_id: str | None = "TV-85-A",
    candidate_id: str | None = "TV-85-A",
    current_model: str | None = "QN85A",
    candidate_model: str | None = "QN85A",
    authority: str = "AUTO",
):
    return LearningCompatibilityService().evaluate(
        current_question=current_question,
        current_product=_identity(
            current_name, product_id=current_id, model_code=current_model
        ),
        candidate_question=candidate_question,
        candidate_answer=candidate_answer,
        candidate_product=_identity(
            candidate_name, product_id=candidate_id, model_code=candidate_model
        ),
        authority=authority,
    )


def test_same_model_and_topic_is_eligible() -> None:
    result = _decision(
        "85인치 TV VESA 규격이 어떻게 되나요?",
        "85인치 TV VESA 규격 문의",
        "해당 모델의 VESA 규격을 확인해 주세요.",
    )
    assert result.eligible is True
    assert result.product_match == "EXACT_MODEL"
    assert result.topic_match == "MATCH"


def test_size_variant_mismatch_is_hard_rejected() -> None:
    result = _decision(
        "32인치 TV VESA 규격이 어떻게 되나요?",
        "85인치 TV VESA 규격 문의",
        "85인치 전용 브라켓 규격입니다.",
        current_name="삼성 32인치 TV QN32B",
        current_id=None,
        candidate_id=None,
        current_model=None,
        candidate_model=None,
    )
    assert result.eligible is False
    assert result.reject_reason == "PRODUCT_VARIANT_MISMATCH"


def test_same_product_wrong_topic_is_hard_rejected() -> None:
    result = _decision(
        "43인치 TV에 동축케이블을 연결하면 방송 채널을 볼 수 있나요?",
        "43인치 TV VESA 브라켓 규격 문의",
        "벽걸이 설치에는 VESA 확장 브라켓이 필요할 수 있습니다.",
        current_name="삼성 43인치 UHD TV QN43A",
        candidate_name="삼성 43인치 UHD TV QN43A",
        current_id="TV-43-A",
        candidate_id="TV-43-A",
        current_model="QN43A",
        candidate_model="QN43A",
    )
    assert result.eligible is False
    assert result.reject_reason == "TOPIC_MISMATCH"


def test_same_product_antenna_topic_is_eligible() -> None:
    result = _decision(
        "43인치 TV에 동축 안테나를 연결해 방송을 볼 수 있나요?",
        "동축 안테나 단자로 지상파 방송을 수신할 수 있나요?",
        "모델별 안테나 단자와 방송 수신 사양을 확인해야 합니다.",
        current_name="삼성 43인치 UHD TV QN43A",
        candidate_name="삼성 43인치 UHD TV QN43A",
        current_id="TV-43-A",
        candidate_id="TV-43-A",
        current_model="QN43A",
        candidate_model="QN43A",
    )
    assert result.eligible is True
    assert result.topic_match == "MATCH"


@pytest.mark.parametrize(
    ("question", "learning_question", "learning_answer"),
    [
        ("배송은 언제 오나요?", "HDMI 포트가 몇 개인가요?", "HDMI 포트 안내입니다."),
        ("배송은 언제 오나요?", "설치 방법이 궁금합니다.", "설치 절차 안내입니다."),
        ("설치 방법이 궁금합니다.", "반품 절차가 궁금합니다.", "반품 안내입니다."),
        ("HDMI 연결 방법은?", "리모컨 사용법은?", "리모컨 안내입니다."),
        ("HDMI 연결 방법은?", "VESA 브라켓 규격은?", "브라켓 안내입니다."),
        ("넷플릭스 OTT 지원하나요?", "VESA 브라켓 규격은?", "브라켓 안내입니다."),
        ("안테나 방송 수신이 되나요?", "스탠드 구성품은?", "스탠드 안내입니다."),
        ("제품 크기와 무게는?", "프로모션 사은품은?", "행사 안내입니다."),
        ("반품할 수 있나요?", "리모컨 기능은?", "리모컨 안내입니다."),
        ("A/S 접수 방법은?", "배송 일정은?", "배송 안내입니다."),
    ],
)
def test_cross_topic_learning_is_rejected(
    question: str, learning_question: str, learning_answer: str
) -> None:
    result = _decision(question, learning_question, learning_answer)
    assert result.eligible is False
    assert result.reject_reason == "TOPIC_MISMATCH"


def test_human_verified_does_not_bypass_product_or_topic_scope() -> None:
    wrong_product = _decision(
        "HDMI 포트 수가 궁금합니다.",
        "HDMI 포트 수가 궁금합니다.",
        "HDMI 포트는 세 개입니다.",
        current_name="삼성 TV QN43A",
        candidate_name="삼성 TV QN50B",
        current_id="TV-A",
        candidate_id="TV-B",
        current_model="QN43A",
        candidate_model="QN50B",
        authority="APPROVED",
    )
    wrong_topic = _decision(
        "HDMI 포트 수가 궁금합니다.",
        "리모컨 사용법이 궁금합니다.",
        "리모컨 안내입니다.",
        authority="APPROVED",
    )
    assert wrong_product.reject_reason == "MODEL_MISMATCH"
    assert wrong_topic.reject_reason == "TOPIC_MISMATCH"


def test_auto_exact_product_topic_is_eligible_but_uncertain_identity_is_not() -> None:
    exact = _decision(
        "HDMI 포트 수가 궁금합니다.",
        "HDMI 포트 수가 궁금합니다.",
        "모델별 HDMI 포트 수를 안내합니다.",
    )
    uncertain = _decision(
        "HDMI 포트 수가 궁금합니다.",
        "HDMI 포트 수가 궁금합니다.",
        "모델별 HDMI 포트 수를 안내합니다.",
        current_name="삼성 UHD TV",
        candidate_name="삼성 UHD TV",
        current_id=None,
        candidate_id=None,
        current_model=None,
        candidate_model=None,
    )
    assert exact.eligible is True
    assert uncertain.eligible is False
    assert uncertain.reject_reason == "INSUFFICIENT_PRODUCT_IDENTITY"


def _save_learning(
    database: Database,
    *,
    inquiry_id: int,
    question: str,
    answer: str,
    source_key: str,
) -> dict:
    return LearningRepository(database).upsert(
        {
            "source_key": source_key,
            "inquiry_id": inquiry_id,
            "learning_source": "APPROVED_UNEDITED",
            "question_original_masked": question,
            "question_normalized": question.lower(),
            "store_code": "OJE_PLUS",
            "inquiry_type": "PRODUCT_GENERAL",
            "intent": "PRODUCT_GENERAL",
            "product_name": "삼성 43인치 UHD TV QN43A",
            "model_code": "QN43A",
            "final_answer": answer,
            "rating": 5,
            "edit_ratio": 0.0,
            "quality_score": 1.0,
            "style_only": False,
            "version": 1,
            "metadata_json": {
                "learning_signal_type": "POSITIVE",
                "human_verified": True,
            },
            "active": True,
        }
    )


def test_coax_vs_vesa_runtime_returns_zero_and_diagnostics(tmp_path: Path) -> None:
    database = Database(tmp_path / "compatibility.db")
    database.initialize()
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": "SOURCE-VESA",
            "inquiry_type": "PRODUCT_GENERAL",
            "title": "상품 문의",
            "content": "VESA 브라켓 규격이 궁금합니다.",
            "product_id": "TV-43-A",
            "product_name": "삼성 43인치 UHD TV QN43A",
            "post_status": "NOT_POSTED",
            "raw_json": {},
        }
    ).inquiry_id
    _save_learning(
        database,
        inquiry_id=inquiry_id,
        question="43인치 TV VESA 브라켓 규격이 궁금합니다.",
        answer="VESA 확장 브라켓 필요 여부를 확인해 주세요.",
        source_key="coax-vs-vesa",
    )
    search = SimilarAnswerService(LearningRepository(database))
    selected = search.search(
        "43인치 TV 동축 안테나 연결과 방송 채널 수신이 궁금합니다.",
        store_code="OJE_PLUS",
        product_name="삼성 43인치 UHD TV QN43A",
        product_id="TV-43-A",
        model_code="QN43A",
    )
    assert selected == []
    assert search.last_trace["selected_count"] == 0
    assert search.last_trace["rejection_counts"]["TOPIC_MISMATCH"] == 1
    diagnostic = search.last_trace["candidate_diagnostics"][0]
    assert diagnostic["eligible"] is False
    assert diagnostic["reject_reason"] == "TOPIC_MISMATCH"
    prompt_context = search.context(
        "43인치 TV 동축 안테나 연결과 방송 채널 수신이 궁금합니다.",
        store_code="OJE_PLUS",
        product_name="삼성 43인치 UHD TV QN43A",
        product_id="TV-43-A",
        model_code="QN43A",
    )
    assert prompt_context["similar_approved_answers"] == []

    class Provider:
        def generate_json(self, **_kwargs):
            return {
                "answer": "해당 모델의 방송 수신 사양은 담당자 확인이 필요합니다.",
                "confidence": 0.5,
                "used_facts": [],
                "missing_information": ["verified_product_fact"],
                "requires_review": True,
                "warnings": [],
                "learning_usage": [{
                    "learning_id": 1,
                    "matched_subquestion": "동축 안테나 문의",
                    "answer_supported": True,
                }],
            }

    intent = IntentResult(
        "ANTENNA_BROADCAST",
        ("43인치 TV 동축 안테나 연결과 방송 채널 수신이 궁금합니다.",),
        Emotion.NORMAL,
        "NORMAL",
        0.9,
        True,
        "no compatible evidence",
    )
    generated = DraftGenerationService(
        Provider(), learning_context_provider=lambda *_: prompt_context
    ).generate(
        AnswerFacts(
            inquiry={"question": intent.questions[0]},
            product={"product_id": "TV-43-A", "name": "삼성 43인치 UHD TV QN43A"},
        ),
        intent,
    )
    assert generated.learning_usage == ()


def test_answer_relevance_blocks_wrong_topic_and_reviews_partial_compound() -> None:
    service = LearningCompatibilityService()
    wrong = service.answer_relevance(
        questions=["HDMI 포트가 몇 개인가요?"],
        answer="리모컨 사용 방법을 안내드립니다.",
    )
    partial = service.answer_relevance(
        questions=["HDMI 포트가 몇 개인가요?", "리모컨 사용법은 무엇인가요?"],
        answer="HDMI 포트 연결 방법을 안내드립니다.",
    )
    assert wrong.status == "BLOCK"
    assert wrong.reason == "ANSWER_TOPIC_MISMATCH"
    assert partial.status == "REVIEW_REQUIRED"
    assert partial.reason == "COMPOUND_QUESTION_PARTIAL_COVERAGE"


def test_preloaded_candidate_pool_does_not_trigger_per_candidate_queries() -> None:
    class Repository:
        calls = 0

        def candidates(self, **_kwargs):
            self.calls += 1
            raise AssertionError("preloaded candidate pool must be reused")

        def candidate_diagnostics(self, **_kwargs):
            raise AssertionError("preloaded diagnostics must be reused")

    candidate = {
        "id": 7,
        "learning_source": "APPROVED_UNEDITED",
        "question_original_masked": "HDMI 포트 수가 궁금합니다.",
        "question_normalized": "hdmi 포트 수가 궁금합니다",
        "final_answer": "해당 모델의 HDMI 포트 수 안내입니다.",
        "metadata_json": {
            "learning_signal_type": "POSITIVE",
            "human_verified": True,
        },
        "source_product_id": "TV-43-A",
        "source_product_name": "삼성 43인치 TV QN43A",
        "source_option_name": None,
        "product_name": "삼성 43인치 TV QN43A",
        "model_code": "QN43A",
        "inquiry_type": "PRODUCT_GENERAL",
        "intent": "PRODUCT_SPEC",
        "style_only": False,
        "style_features_json": {},
        "rating": 5,
        "created_at": "2026-08-20T00:00:00Z",
    }
    repository = Repository()
    context = SimilarAnswerService(repository).context(
        "HDMI 포트 수가 궁금합니다.",
        store_code="OJE_PLUS",
        product_name="삼성 43인치 TV QN43A",
        product_id="TV-43-A",
        model_code="QN43A",
        candidate_pool=[candidate],
        candidate_diagnostics={
            "active_candidates": 1,
            "filtered_by_validity": 0,
            "revoked": 0,
            "negative_excluded": 0,
        },
    )
    assert repository.calls == 0
    assert len(context["similar_approved_answers"]) == 1


def test_validator_blocks_final_answer_with_unrelated_topic() -> None:
    facts = AnswerFacts(
        inquiry={"question": "HDMI 포트가 몇 개인가요?"},
        rule={"answer": "리모컨 사용 방법을 안내드립니다."},
    )
    intent = IntentResult(
        "PRODUCT_SPEC",
        ("HDMI 포트가 몇 개인가요?",),
        Emotion.NORMAL,
        "NORMAL",
        0.9,
        False,
        "test",
    )
    draft = DraftResult(
        answer="리모컨 사용 방법을 안내드립니다.",
        confidence=0.9,
        used_facts=("rule.answer",),
    )
    review = SelfReviewResult(
        True, True, False, True, False, "test"
    )
    result = AnswerValidator().validate(facts, intent, draft, review)
    assert result.status == "BLOCK"
    assert any(
        item.code == "ANSWER_TOPIC_RELEVANCE" and item.status == "BLOCK"
        for item in result.rules
    )


def test_validator_requires_review_for_partial_compound_answer() -> None:
    facts = AnswerFacts(
        inquiry={
            "question": "HDMI 포트 수와 리모컨 사용법이 궁금합니다."
        },
        rule={"answer": "HDMI 연결 방법을 안내드립니다."},
    )
    intent = IntentResult(
        "PRODUCT_SPEC",
        ("HDMI 포트 수는?", "리모컨 사용법은?"),
        Emotion.NORMAL,
        "NORMAL",
        0.9,
        False,
        "compound",
    )
    draft = DraftResult(
        answer="HDMI 연결 방법을 안내드립니다.",
        confidence=0.9,
        used_facts=("rule.answer",),
        subquestion_results=(
            {"subquestion": "HDMI 포트 수는?", "answered": True},
            {"subquestion": "리모컨 사용법은?", "answered": False},
        ),
    )
    review = SelfReviewResult(
        True, False, False, True, True, "partial"
    )
    result = AnswerValidator().validate(facts, intent, draft, review)
    assert result.status == "REVIEW_REQUIRED"
    assert any(
        item.code == "SUBQUESTION_EVIDENCE_COVERAGE"
        and item.status == "REVIEW_REQUIRED"
        for item in result.rules
    )


def test_zero_learning_with_verified_rule_fact_can_answer() -> None:
    facts = AnswerFacts(
        inquiry={"question": "HDMI 포트 수가 궁금합니다."},
        rule={"answer": "확인된 제품 자료상 HDMI 포트는 3개입니다."},
    )
    intent = IntentResult(
        "PRODUCT_SPEC", ("HDMI 포트 수가 궁금합니다.",),
        Emotion.NORMAL, "NORMAL", 0.9, False, "verified rule",
    )
    draft = DraftResult(
        answer="확인된 제품 자료상 HDMI 포트는 3개입니다.",
        confidence=0.9,
        used_facts=("rule.answer",),
        learning_usage=(),
    )
    review = SelfReviewResult(True, True, False, True, False, "pass")
    result = AnswerValidator().validate(facts, intent, draft, review)
    assert result.status == "PASS"


def test_zero_learning_without_verified_product_fact_requires_review() -> None:
    result = AutoProcessingEligibilityService().evaluate(
        inquiry={"source_answered": False, "post_status": "NOT_POSTED"},
        draft={
            "original_answer": "이 모델은 특정 방송 규격을 지원합니다.",
            "review_status": "PENDING",
            "validation_status": "PASSED",
            "validator_result_json": {"passed": True},
            "posted": False,
            "metadata_json": {
                "learning_usage": [],
                "product_fact_guard": {
                    "sensitive": True,
                    "current_fact_verified": False,
                },
            },
        },
        route="GPT_FALLBACK",
    )
    assert result.decision == "REVIEW_REQUIRED"
    assert "PRODUCT_FACT_NOT_VERIFIED" in result.reasons
