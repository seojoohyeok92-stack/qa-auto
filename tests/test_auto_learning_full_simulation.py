from __future__ import annotations

import itertools

from answer.facts import AnswerFacts
from answer.hybrid_models import Emotion, IntentResult
from repositories.answer_repository import AnswerRepository
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_repository import LearningRepository
from repositories.workflow_repository import WorkflowRepository
from answer.models import AnswerResult, AnswerStatus
from services.approval_service import ApprovalService
from services.learning_context_service import LearningContextService


STORE_CODE = "OJE_PLUS"
_ids = itertools.count(1)


def _approve_inquiry(database, *, program_answer: str, final_answer: str):
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": STORE_CODE,
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": f"SIM-{next(_ids)}",
            "inquiry_type": "PRODUCT_INQUIRY",
            "title": "상품 문의",
            "content": "제주도 배송 설치 가능한가요?",
            "product_name": "삼성 TV",
            "raw_json": {},
        }
    ).inquiry_id
    WorkflowRepository(database).initialize_steps(inquiry_id)
    draft = AnswerRepository(database).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.GENERATED, category="GENERAL", reason="test",
            answer=program_answer, provider="rules", auto_answerable=True,
            needs_review=False,
        ),
    )
    ApprovalService(database).save_edited_answer(
        inquiry_id=inquiry_id, draft_id=draft["id"], edited_answer=final_answer,
    )
    ApprovalService(database).approve(
        inquiry_id=inquiry_id, draft_id=draft["id"], actor="직원",
    )
    return inquiry_id


def test_two_explicit_approvals_create_positive_learning_without_auto_signal_promotion(
    tmp_path, monkeypatch,
) -> None:
    """명시적 승인은 Positive Learning을 만들지만 반복 확인만으로
    structured signal을 runtime evidence로 자동 승격하지 않는다."""
    monkeypatch.setenv("AUTO_STRUCTURED_LEARNING_ENABLED", "true")
    monkeypatch.setenv("AUTO_VERIFIED_FACT_PROMOTION_ENABLED", "true")
    monkeypatch.setenv("AUTO_VERIFIED_FACT_MIN_CONFIRMATIONS", "2")

    from repositories.database import Database

    database = Database(tmp_path / "sim.db")
    database.initialize()

    # Two independent inquiries each get a real Program->Final correction.
    _approve_inquiry(
        database,
        program_answer="제주도 배송 여부는 확인이 필요합니다.",
        final_answer="제주도 배송 및 설치 가능합니다.",
    )
    _approve_inquiry(
        database,
        program_answer="확인이 필요합니다.",
        final_answer="제주도 배송 및 설치 가능합니다.",
    )

    # A brand new, differently-phrased inquiry arrives next.
    target_inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": STORE_CODE,
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": "SIM-TARGET",
            "inquiry_type": "PRODUCT_INQUIRY",
            "title": "상품 문의",
            "content": "제주도도 배송설치 가능한가요?",
            "product_name": "삼성 TV",
            "raw_json": {},
        }
    ).inquiry_id

    facts = AnswerFacts(
        inquiry={
            "inquiry_id": target_inquiry_id,
            "question": "제주도도 배송설치 가능한가요?",
            "type": "PRODUCT_INQUIRY",
        },
        product={"name": "삼성 TV"},
        order={},
    )
    intent = IntentResult(
        "PRODUCT_GENERAL", ("제주도도 배송설치 가능한가요?",), Emotion.NORMAL,
        "NORMAL", 0.9, False, "test",
    )
    context = LearningContextService(database).build(facts, intent)

    positives = LearningRepository(database).candidates(store_code=STORE_CODE)
    assert len(positives) == 2
    assert all(row["metadata_json"].get("human_verified") for row in positives)
    assert context["feedback_signals"]["verified_facts"] == []
    assert context["feedback_signals"]["corrections"] == []
    assert context["subquestion_evidence"][0]["source"] != "VERIFIED_FEEDBACK_SIGNAL"
