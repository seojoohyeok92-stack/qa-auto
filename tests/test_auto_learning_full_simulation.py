from __future__ import annotations

import itertools

import pytest

from answer.facts import AnswerFacts
from answer.hybrid_models import DraftResult, Emotion, IntentResult, SelfReviewResult
from answer.answer_validator import AnswerValidator
from repositories.answer_repository import AnswerRepository
from repositories.inquiry_repository import InquiryRepository
from repositories.workflow_repository import WorkflowRepository
from answer.models import AnswerResult, AnswerStatus
from services.approval_service import ApprovalService
from services.learning_context_service import LearningContextService


STORE_CODE = "OJE_PLUS"
_ids = itertools.count(1)


@pytest.fixture(autouse=True)
def _auto_learning_env(monkeypatch):
    monkeypatch.setenv("AUTO_STRUCTURED_LEARNING_ENABLED", "true")
    monkeypatch.setenv("AUTO_VERIFIED_FACT_PROMOTION_ENABLED", "true")
    monkeypatch.setenv("AUTO_VERIFIED_FACT_MIN_CONFIRMATIONS", "2")
    yield


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


def test_full_pipeline_from_two_staff_edits_to_next_inquiry_answer_generation(
    tmp_path,
) -> None:
    """문의 -> Program Answer -> Staff Edit -> Final Answer -> Approval ->
    Positive Learning -> Structured Signal extraction -> (2회 반복 확인) ->
    승격 -> 다음 문의의 Retrieval/Evidence/Validator까지 실제로 연결되는지
    검증한다 (4th-phase section 22)."""

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

    assert context["feedback_signals"]["verified_facts"] or context["feedback_signals"]["corrections"], (
        "the twice-confirmed, now-promoted fact must be retrieved for a "
        "differently-phrased follow-up inquiry"
    )
    evidence = context["subquestion_evidence"][0]
    assert evidence["status"] == "ANSWERABLE"
    assert evidence["source"] == "VERIFIED_FEEDBACK_SIGNAL"
    assert evidence["feedback_signal_ids"]

    # The evidence must also survive AnswerValidator unharmed (no spurious
    # conflict/BLOCK) when GPT actually answers from it.
    validator = AnswerValidator()
    validation = validator.validate(
        facts, intent,
        DraftResult(answer="제주도 배송 및 설치가 가능합니다.", confidence=0.9),
        SelfReviewResult(
            passed=True, answered_all_questions=True, has_speculation=False,
            facts_consistent=True, requires_review=False, reason="ok", warnings=(),
        ),
        subquestion_evidence=context["subquestion_evidence"],
    )
    assert validation.passed
    assert not any(rule.code == "VERIFIED_FACT_CONFLICT" for rule in validation.rules)
