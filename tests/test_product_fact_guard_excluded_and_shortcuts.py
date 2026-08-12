from __future__ import annotations

from pathlib import Path

import pytest

from answer.answer_provenance import AnswerProvenance
from answer.learning_conflict import LearningConflictError
from answer.models import AnswerResult, AnswerStatus
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_feedback_repository import LearningFeedbackRepository
from repositories.learning_repository import LearningRepository
from repositories.workflow_repository import WorkflowRepository
from services.approval_service import ApprovalService
from services.auto_processing_eligibility_service import (
    AutoProcessingEligibilityService,
)
from services.learning_feedback_service import LearningFeedbackService
from services.product_fact_guard import classify_product_fact
from services.similar_answer_service import SimilarAnswerService
from ui.learning_manager import _filter_rows


def _context(tmp_path: Path, name: str = "excluded"):
    database = Database(tmp_path / f"{name}.db")
    database.initialize()
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": f"EXCLUDED-{name}",
            "inquiry_type": "PRODUCT_GENERAL",
            "title": "상품 문의",
            "content": "HDMI 단자가 몇 개인가요?",
            "product_id": f"PRODUCT-{name}",
            "product_name": "삼성 TV 50인치 MODEL50A",
            "post_status": "NOT_POSTED",
            "raw_json": {},
        }
    ).inquiry_id
    WorkflowRepository(database).initialize_steps(inquiry_id)
    draft = AnswerRepository(database).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.GENERATED,
            category="GENERAL",
            reason="test",
            answer="HDMI 단자는 3개입니다.",
            provider="rules",
            auto_answerable=True,
            needs_review=False,
        ),
    )
    return database, inquiry_id, draft


def _learning(
    repository: LearningRepository,
    *,
    inquiry_id: int,
    source_key: str,
    question: str,
    answer: str,
) -> dict:
    return repository.upsert(
        {
            "source_key": source_key,
            "inquiry_id": inquiry_id,
            "answer_draft_id": None,
            "learning_source": "APPROVED_UNEDITED",
            "question_original_masked": question,
            "question_normalized": question.lower(),
            "store_code": "OJE_PLUS",
            "inquiry_type": "PRODUCT_GENERAL",
            "intent": "PRODUCT_GENERAL",
            "product_name": "삼성 TV 50인치 MODEL50A",
            "model_code": "MODEL50A",
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


def test_common_policy_is_not_product_fact_sensitive() -> None:
    decision = classify_product_fact(
        "43인치 TV 거래명세서는 어디서 발급하나요?",
        inquiry_type="PRODUCT_GENERAL",
        inquiry_subtype="PRODUCT_SPEC_OR_FEATURE",
        product_id="TV-43",
    )
    assert decision.sensitive is False
    assert decision.reason == "COMMON_POLICY_OR_PROCEDURE"


@pytest.mark.parametrize(
    "question",
    [
        "HDMI 단자가 몇 개인가요?",
        "VESA 규격이 무엇인가요?",
        "스탠드가 포함되나요?",
        "구성품은 무엇인가요?",
        "벽걸이 기능을 지원하나요?",
    ],
)
def test_model_fact_questions_are_sensitive(question: str) -> None:
    assert classify_product_fact(
        question,
        inquiry_type="PRODUCT_GENERAL",
        inquiry_subtype="PRODUCT_SPEC_OR_FEATURE",
    ).sensitive


def test_cross_product_learning_body_is_filtered_only_for_product_facts(
    tmp_path: Path,
) -> None:
    database, inquiry_id, _ = _context(tmp_path, "source-50")
    repository = LearningRepository(database)
    _learning(
        repository,
        inquiry_id=inquiry_id,
        source_key="tv50-hdmi",
        question="HDMI 단자가 몇 개인가요",
        answer="HDMI 단자는 3개입니다.",
    )
    search = SimilarAnswerService(repository)
    assert search.search(
        "HDMI 단자가 몇 개인가요",
        store_code="OJE_PLUS",
        product_id="TV-43",
        model_code="MODEL43B",
        product_fact_sensitive=True,
    ) == []
    assert search.search(
        "HDMI 단자가 몇 개인가요",
        store_code="OJE_PLUS",
        product_id="PRODUCT-source-50",
        model_code="MODEL50A",
        product_fact_sensitive=True,
    )[0]["final_answer"] == "HDMI 단자는 3개입니다."
    # Common policy retrieval keeps the existing cross-product behavior.
    assert search.search(
        "HDMI 단자가 몇 개인가요",
        store_code="OJE_PLUS",
        product_id="TV-43",
        product_fact_sensitive=False,
    )


def test_unverified_product_fact_is_hard_blocked_from_auto_post() -> None:
    result = AutoProcessingEligibilityService().evaluate(
        inquiry={"source_answered": False, "post_status": "NOT_POSTED"},
        draft={
            "original_answer": "HDMI 단자는 3개입니다.",
            "review_status": "PENDING",
            "validation_status": "PASSED",
            "validator_result_json": {"passed": True},
            "posted": False,
            "metadata_json": {
                "product_fact_guard": {
                    "sensitive": True,
                    "current_fact_verified": False,
                }
            },
        },
        route="GPT_FALLBACK",
    )
    assert result.decision == "REVIEW_REQUIRED"
    assert "PRODUCT_FACT_NOT_VERIFIED" in result.reasons


def test_dashboard_excluded_is_idempotent_and_blocks_same_answer_evaluations(
    tmp_path: Path,
) -> None:
    database, inquiry_id, draft = _context(tmp_path)
    service = LearningFeedbackService(database)
    first = service.capture_dashboard_excluded(
        inquiry_id=inquiry_id,
        original_answer_source=AnswerProvenance.PROGRAM_GENERATED,
        original_answer_reference_id=draft["id"],
        exclusion_reason="NOT_REUSABLE",
        exclusion_note="특정 고객 예외",
    )
    second = service.capture_dashboard_excluded(
        inquiry_id=inquiry_id,
        original_answer_source=AnswerProvenance.PROGRAM_GENERATED,
        original_answer_reference_id=draft["id"],
        exclusion_reason="NOT_REUSABLE",
        exclusion_note="특정 고객 예외",
    )
    assert first["id"] == second["id"]
    assert first["learning_signal_type"] == "EXCLUDED"
    with pytest.raises(LearningConflictError, match="학습 제외"):
        ApprovalService(database).approve(
            inquiry_id=inquiry_id, draft_id=draft["id"]
        )
    with pytest.raises(LearningConflictError, match="학습 제외"):
        service.capture_dashboard_negative(
            inquiry_id=inquiry_id,
            original_answer_source="PROGRAM_GENERATED",
            original_answer_reference_id=draft["id"],
            correction_reason="FACT_ERROR",
        )
    assert LearningRepository(database).for_inquiry(inquiry_id) == []


def test_excluded_revoke_allows_new_evaluation_and_keeps_audit(tmp_path: Path) -> None:
    database, inquiry_id, draft = _context(tmp_path, "revoke")
    service = LearningFeedbackService(database)
    excluded = service.capture_dashboard_excluded(
        inquiry_id=inquiry_id,
        original_answer_source="PROGRAM_GENERATED",
        original_answer_reference_id=draft["id"],
        exclusion_reason="ONE_TIME_EXCEPTION",
    )
    revoked = service.revoke_dashboard_excluded(
        feedback_id=excluded["id"], reason="재평가 필요", actor="staff"
    )
    assert revoked["active"] is False
    assert revoked["metadata_json"]["status"] == "REVOKED"
    assert revoked["metadata_json"]["revoke_reason"] == "재평가 필요"
    negative = service.capture_dashboard_negative(
        inquiry_id=inquiry_id,
        original_answer_source="PROGRAM_GENERATED",
        original_answer_reference_id=draft["id"],
        correction_reason="FACT_ERROR",
    )
    assert negative[0]["learning_signal_type"] == "NEGATIVE"
    assert len(LearningFeedbackRepository(database).for_inquiry(inquiry_id)) == 2


def test_excluded_program_answer_allows_distinct_staff_edited_positive(
    tmp_path: Path,
) -> None:
    database, inquiry_id, draft = _context(tmp_path, "staff")
    LearningFeedbackService(database).capture_dashboard_excluded(
        inquiry_id=inquiry_id,
        original_answer_source="PROGRAM_GENERATED",
        original_answer_reference_id=draft["id"],
        exclusion_reason="NOT_GENERALIZABLE",
    )
    approval = ApprovalService(database)
    approval.save_edited_answer(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        edited_answer="현재 모델 자료를 확인한 직원 수정 답변입니다.",
    )
    approval.approve(inquiry_id=inquiry_id, draft_id=draft["id"])
    positive = LearningRepository(database).for_inquiry(inquiry_id)[0]
    assert positive["metadata_json"]["answer_provenance"] == "STAFF_EDITED"
    assert LearningFeedbackRepository(database).for_inquiry(inquiry_id)[0][
        "active"
    ] is True


def test_excluded_repository_state_survives_fresh_dashboard_session_and_manager_search(
    tmp_path: Path,
) -> None:
    from streamlit.testing.v1 import AppTest

    database, inquiry_id, draft = _context(tmp_path, "ui")
    saved = LearningFeedbackService(database).capture_dashboard_excluded(
        inquiry_id=inquiry_id,
        original_answer_source="PROGRAM_GENERATED",
        original_answer_reference_id=draft["id"],
        exclusion_reason="CUSTOMER_SPECIFIC",
        exclusion_note="특정 고객 개별 보상",
    )
    script = f'''
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from ui.review_workspace import _render_answer_panel
db=Database(r"{database.path}")
db.initialize()
_render_answer_panel(db, InquiryRepository(db).get({inquiry_id}))
'''
    first = AppTest.from_string(script).run(timeout=40)
    fresh = AppTest.from_string(script).run(timeout=40)
    assert not first.exception and not fresh.exception
    for app in (first, fresh):
        rendered = "\n".join(item.value for item in app.markdown)
        assert "학습 제외 저장 완료" in rendered
        assert f"Feedback ID <b>{saved['id']}</b>" in rendered
        assert "CUSTOMER_SPECIFIC" in rendered
        assert "특정 고객 개별 보상" in rendered
    rows = LearningFeedbackRepository(database).manager_rows()
    assert _filter_rows(
        rows, query=str(saved["id"]), signal_type="EXCLUDED"
    )[0]["id"] == saved["id"]
    assert _filter_rows(
        rows, query="특정 고객 개별 보상", signal_type="EXCLUDED"
    )[0]["id"] == saved["id"]


def test_streamlit_viewer_mode_preserves_cache_and_browser_copy_contract() -> None:
    config = Path(".streamlit/config.toml").read_text(encoding="utf-8")
    assert 'toolbarMode = "viewer"' in config
    assert "cache_data" in config and "cache_resource" in config
    assert "javascript" not in config.lower()
