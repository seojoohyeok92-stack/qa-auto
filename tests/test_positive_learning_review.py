from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

from answer.answer_format import format_final_answer
from answer.answer_provenance import AnswerProvenance
from answer.learning_conflict import LearningConflictError
from answer.models import AnswerResult, AnswerStatus
from repositories.answer_repository import AnswerRepository
from repositories.approval_repository import ApprovalRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_feedback_repository import LearningFeedbackRepository
from repositories.learning_repository import LearningRepository
from repositories.workflow_repository import WorkflowRepository
from services.approval_service import ApprovalService
from services.learning_feedback_service import LearningFeedbackService
from ui.learning_manager import _filter_rows
from ui.review_workspace import approval_learning_trace


def _context(tmp_path, name: str = "positive"):
    database = Database(tmp_path / f"{name}.db")
    database.initialize()
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": f"POSITIVE-{name}",
            "inquiry_type": "PRODUCT_GENERAL",
            "title": "거래명세서 문의",
            "content": "거래명세서는 어떻게 발급하나요?",
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
            answer="구매 내역에서 거래명세서를 확인해 주세요.",
            provider="rules",
            auto_answerable=True,
            needs_review=False,
        ),
    )
    return database, inquiry_id, draft


@pytest.mark.parametrize(
    ("reason", "note"),
    [
        ("", ""),
        ("CONTENT_ACCURATE", ""),
        ("", "유사 거래명세서 문의에서 안내 흐름을 참고"),
        ("REUSE_RECOMMENDED", "좋은 답변 사례로 재사용"),
    ],
)
def test_optional_positive_review_metadata_is_persisted(
    tmp_path, reason: str, note: str
) -> None:
    database, inquiry_id, draft = _context(
        tmp_path, f"optional-{reason or 'none'}-{bool(note)}"
    )
    ApprovalService(database).approve(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        actor="staff-positive",
        positive_reason=reason,
        positive_note=note,
    )

    rows = LearningRepository(database).for_inquiry(inquiry_id)
    assert len(rows) == 1
    metadata = rows[0]["metadata_json"]
    assert metadata["human_verified"] is True
    assert metadata["answer_provenance"] == "PROGRAM_GENERATED"
    assert metadata["answer_reference_id"] == draft["id"]
    assert metadata["positive_reason"] == (reason or None)
    assert metadata["positive_note"] == (note or None)
    assert metadata["verified_at"]


def test_positive_trace_and_learning_manager_use_persisted_metadata(tmp_path) -> None:
    database, inquiry_id, draft = _context(tmp_path, "trace")
    ApprovalService(database).approve(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        actor="staff-positive",
        positive_reason="CONTENT_ACCURATE",
        positive_note="거래명세서 문의에서 설명 흐름을 참고",
    )
    persisted_draft = AnswerRepository(database).get(draft["id"])
    trace = approval_learning_trace(
        database,
        inquiry_id=inquiry_id,
        draft=persisted_draft,
        approval_state=ApprovalRepository(database).get_inquiry_approval(inquiry_id),
        source_answered=False,
    )
    assert trace["positive_learning"] is True
    assert trace["positive_reason"] == "CONTENT_ACCURATE"
    assert trace["positive_note"] == "거래명세서 문의에서 설명 흐름을 참고"
    assert trace["final_reference_id"] == draft["id"]
    assert trace["verified_at"]

    rows = LearningRepository(database).manager_rows()
    learning_id = rows[0]["id"]
    assert _filter_rows(rows, query=str(inquiry_id)) == rows
    assert _filter_rows(rows, query=str(learning_id)) == rows
    assert _filter_rows(rows, query=str(draft["id"])) == rows
    assert _filter_rows(rows, query="설명 흐름") == rows


def test_dashboard_fresh_session_shows_repository_positive_result(tmp_path) -> None:
    database, inquiry_id, draft = _context(tmp_path, "apptest")
    ApprovalService(database).approve(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        positive_reason="CONTENT_ACCURATE",
        positive_note="승인 후 다시 확인할 메모",
    )
    learning_id = LearningRepository(database).for_inquiry(inquiry_id)[0]["id"]
    app = AppTest.from_string(
        f'''
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from ui.review_workspace import _render_answer_panel
db=Database(r"{database.path}")
db.initialize()
_render_answer_panel(db, InquiryRepository(db).get({inquiry_id}))
'''
    ).run(timeout=40)
    assert not app.exception
    rendered = "\n".join(item.value for item in app.markdown)
    assert f"Learning ID: {learning_id}" in rendered
    assert "좋은 이유: 내용 정확" in rendered
    assert "승인 메모: 승인 후 다시 확인할 메모" in rendered
    assert f"Reference: {draft['id']}" in rendered
    assert "Verified At:" in rendered


def test_dashboard_positive_settings_flow_persists_optional_values(tmp_path) -> None:
    database, inquiry_id, _ = _context(tmp_path, "positive-ui")
    app = AppTest.from_string(
        f'''
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from ui.review_workspace import _render_answer_panel
db=Database(r"{database.path}")
db.initialize()
_render_answer_panel(db, InquiryRepository(db).get({inquiry_id}))
'''
    ).run(timeout=40)
    assert not app.exception
    reason = next(item for item in app.selectbox if item.label == "좋은 이유 (선택)")
    reason.set_value("내용 정확")
    note = next(
        item
        for item in app.text_input
        if item.label == "승인 메모 / 다음 유사 문의 참고사항 (선택)"
    )
    note.set_value("다음 거래명세서 문의에서 참고")
    app.run(timeout=40)
    next(button for button in app.button if button.label == "승인").click()
    app.run(timeout=40)
    assert not app.exception
    metadata = LearningRepository(database).for_inquiry(inquiry_id)[0][
        "metadata_json"
    ]
    assert metadata["positive_reason"] == "CONTENT_ACCURATE"
    assert metadata["positive_note"] == "다음 거래명세서 문의에서 참고"


def test_negative_program_answer_blocks_same_answer_approval(tmp_path) -> None:
    database, inquiry_id, draft = _context(tmp_path, "negative-block")
    feedback = LearningFeedbackService(database).capture_dashboard_negative(
        inquiry_id=inquiry_id,
        original_answer_source=AnswerProvenance.PROGRAM_GENERATED,
        original_answer_reference_id=draft["id"],
        correction_reason="FACT_ERROR",
        correction_note="사실 확인이 필요함",
    )

    with pytest.raises(LearningConflictError, match="Negative Learning") as error:
        ApprovalService(database).approve(
            inquiry_id=inquiry_id, draft_id=draft["id"]
        )
    assert error.value.conflict["id"] == feedback[0]["id"]
    assert LearningRepository(database).for_inquiry(inquiry_id) == []
    assert ApprovalRepository(database).get_inquiry_approval(inquiry_id)[
        "approval_status"
    ] == "PENDING"


def test_negative_original_allows_changed_staff_answer_positive(tmp_path) -> None:
    database, inquiry_id, draft = _context(tmp_path, "staff-fixed")
    feedback = LearningFeedbackService(database).capture_dashboard_negative(
        inquiry_id=inquiry_id,
        original_answer_source="PROGRAM_GENERATED",
        original_answer_reference_id=draft["id"],
        correction_reason="FACT_ERROR",
    )
    service = ApprovalService(database)
    service.save_edited_answer(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        edited_answer="직원이 사실관계를 확인해 수정한 답변입니다.",
    )
    service.approve(inquiry_id=inquiry_id, draft_id=draft["id"])

    negative = LearningFeedbackRepository(database).for_inquiry(inquiry_id)
    positive = LearningRepository(database).for_inquiry(inquiry_id)
    assert negative[0]["id"] == feedback[0]["id"]
    assert negative[0]["active"] is True
    assert positive[0]["metadata_json"]["answer_provenance"] == "STAFF_EDITED"
    assert positive[0]["final_answer"] == format_final_answer(
        "직원이 사실관계를 확인해 수정한 답변입니다."
    )


def test_human_verified_positive_blocks_same_answer_negative(tmp_path) -> None:
    database, inquiry_id, draft = _context(tmp_path, "positive-block")
    ApprovalService(database).approve(
        inquiry_id=inquiry_id, draft_id=draft["id"]
    )
    with pytest.raises(LearningConflictError, match="Human Verified Positive"):
        LearningFeedbackService(database).capture_dashboard_negative(
            inquiry_id=inquiry_id,
            original_answer_source="FINAL_ANSWER",
            original_answer_reference_id=draft["id"],
            correction_reason="FACT_ERROR",
        )
    assert LearningFeedbackRepository(database).for_inquiry(inquiry_id) == []


def test_same_inquiry_different_answer_reference_is_not_blocked(tmp_path) -> None:
    database, inquiry_id, first = _context(tmp_path, "different-reference")
    LearningFeedbackService(database).capture_dashboard_negative(
        inquiry_id=inquiry_id,
        original_answer_source="PROGRAM_GENERATED",
        original_answer_reference_id=first["id"],
        correction_reason="FACT_ERROR",
    )
    second = AnswerRepository(database).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.GENERATED,
            category="GENERAL",
            reason="retry",
            answer="새로 생성한 정확한 거래명세서 답변입니다.",
            provider="rules",
            auto_answerable=True,
            needs_review=False,
        ),
    )
    ApprovalService(database).approve(
        inquiry_id=inquiry_id, draft_id=second["id"]
    )
    positive = LearningRepository(database).for_inquiry(inquiry_id)
    assert positive[0]["answer_draft_id"] == second["id"]
