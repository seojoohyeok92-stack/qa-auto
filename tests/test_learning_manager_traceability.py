from __future__ import annotations

from streamlit.testing.v1 import AppTest

from answer.learning_feedback import CorrectionReason
from answer.models import AnswerResult, AnswerStatus
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_feedback_repository import LearningFeedbackRepository
from repositories.learning_repository import LearningRepository
from services.approval_service import ApprovalService
from services.learning_feedback_service import LearningFeedbackService
from ui.learning_manager import _filter_rows


def _approved_staff_learning(tmp_path):
    database = Database(tmp_path / "learning-manager.db")
    database.initialize()
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "STORE",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": "TRACE-APPROVAL-1",
            "inquiry_type": "PRODUCT_GENERAL",
            "title": "승인 건 추적 문의",
            "content": "학습 매니저에서 이 문의를 찾을 수 있나요?",
            "post_status": "NOT_POSTED",
            "raw_json": {},
        }
    ).inquiry_id
    draft = AnswerRepository(database).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.GENERATED,
            category="GENERAL",
            reason="test",
            answer="Program Answer 원본",
            provider="rules",
            auto_answerable=True,
            needs_review=False,
        ),
    )
    service = ApprovalService(database)
    service.save_edited_answer(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        edited_answer="직원이 검증하고 수정한 답변",
        actor="staff-trace",
    )
    service.approve(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        actor="staff-trace",
    )
    return database, inquiry_id, draft


def test_learning_manager_rows_expose_approval_trace_fields(tmp_path) -> None:
    database, inquiry_id, draft = _approved_staff_learning(tmp_path)
    rows = LearningRepository(database).manager_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["inquiry_id"] == inquiry_id
    assert row["answer_draft_id"] == draft["id"]
    assert row["learning_source"] == "APPROVED_EDITED"
    assert row["provenance"] == "STAFF_EDITED"
    assert row["signal_type"] == "POSITIVE"
    assert row["human_verified"] is True
    assert row["created_at"]


def test_learning_manager_searches_inquiry_reference_and_provenance(
    tmp_path,
) -> None:
    database, inquiry_id, _ = _approved_staff_learning(tmp_path)
    rows = LearningRepository(database).manager_rows()
    assert _filter_rows(rows, query=str(inquiry_id)) == rows
    assert _filter_rows(rows, query="승인 건 추적 문의") == rows
    assert _filter_rows(rows, provenance="STAFF_EDITED") == rows
    assert _filter_rows(rows, human_verified="YES") == rows
    assert _filter_rows(rows, signal_type="NEGATIVE") == []


def test_learning_manager_separates_negative_from_positive_grid(tmp_path) -> None:
    database, inquiry_id, draft = _approved_staff_learning(tmp_path)
    LearningFeedbackService(database).capture_dashboard_negative(
        inquiry_id=inquiry_id,
        original_answer_source="PROGRAM_GENERATED",
        original_answer_reference_id=draft["id"],
        correction_reason=CorrectionReason.FACT_ERROR,
        correction_note="잘못된 사실",
    )
    positive = LearningRepository(database).manager_rows()
    feedback = LearningFeedbackRepository(database).manager_rows()
    assert len(positive) == 1
    assert positive[0]["signal_type"] == "POSITIVE"
    assert len(feedback) == 1
    assert feedback[0]["learning_signal_type"] == "NEGATIVE"
    assert _filter_rows(positive, signal_type="NEGATIVE") == []


def test_learning_manager_apptest_explains_role_and_renders_trace(tmp_path) -> None:
    database, inquiry_id, _ = _approved_staff_learning(tmp_path)
    app = AppTest.from_string(
        f'''
from repositories.database import Database
from ui.learning_manager import render_learning_manager
db=Database(r"{database.path}")
db.initialize()
render_learning_manager(db)
'''
    ).run(timeout=40)
    assert not app.exception
    rendered = "\n".join(
        item.value for item in [*app.title, *app.subheader, *app.caption]
    )
    assert "Learning Manager" in rendered
    assert "조회하고" in rendered and "추적" in rendered
    labels = {metric.label for metric in app.metric}
    assert {
        "저장된 Positive",
        "활성 Positive",
        "Human Verified",
        "Negative",
        "Intent Correction",
    } <= labels
    assert app.text_input[0].label == "문의/참조 검색"
    assert app.dataframe
    table = app.dataframe[0].value.to_string()
    assert str(inquiry_id) in table
    assert "STAFF_EDITED" in table
    assert "POSITIVE" in table
    assert "YES" in table
