from __future__ import annotations

from streamlit.testing.v1 import AppTest

from answer.models import AnswerResult, AnswerStatus
from repositories.answer_repository import AnswerRepository
from repositories.approval_repository import ApprovalRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_feedback_repository import LearningFeedbackRepository
from repositories.learning_repository import LearningRepository
from repositories.naver_posted_answer_repository import NaverPostedAnswerRepository
from repositories.workflow_repository import WorkflowRepository
from services.approval_service import ApprovalService
from services.learning_feedback_service import LearningFeedbackService
from ui.learning_manager import _filter_rows
from ui.review_workspace import approval_learning_trace


def _context(tmp_path, name: str = "revoke"):
    database = Database(tmp_path / f"{name}.db")
    database.initialize()
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "STORE",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": f"REVOKE-{name}",
            "inquiry_type": "PRODUCT_GENERAL",
            "title": "승인 취소 테스트",
            "content": "정확한 답변을 확인해 주세요.",
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
            answer="승인할 Program Answer입니다.",
            provider="rules",
            auto_answerable=True,
            needs_review=False,
        ),
    )
    return database, inquiry_id, draft


def test_cancel_exact_human_verified_positive_is_soft_revoked(tmp_path) -> None:
    database, inquiry_id, draft = _context(tmp_path, "exact")
    service = ApprovalService(database)
    service.approve(inquiry_id=inquiry_id, draft_id=draft["id"])
    target = LearningRepository(database).for_inquiry(inquiry_id)[0]

    outcome = service.cancel_approval_with_learning(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        reason="직원이 잘못 승인함",
        learning_id=target["id"],
        actor="staff-revoke",
    )

    state = ApprovalRepository(database).get_inquiry_approval(inquiry_id)
    revoked = LearningRepository(database).get(target["id"])
    assert state["approval_status"] == "PENDING"
    assert state["approved_at"] is None and state["approved_by"] is None
    assert outcome.draft["final_answer"] is None
    assert outcome.draft["review_status"] == "IN_REVIEW"
    assert outcome.learning["id"] == target["id"]
    assert revoked["active"] is False
    assert revoked["metadata_json"]["human_verified"] == 0
    assert revoked["metadata_json"]["learning_status"] == "REVOKED"
    assert revoked["metadata_json"]["revoke_reason"] == "직원이 잘못 승인함"
    assert revoked["metadata_json"]["revoked_at"]
    assert revoked["metadata_json"]["revoke_approval_history_id"] == outcome.history["id"]
    assert LearningRepository(database).candidates(store_code="STORE") == []
    assert outcome.history["action"] == "APPROVAL_CANCELLED"
    assert outcome.history["reason"] == "직원이 잘못 승인함"


def test_cancel_only_target_learning_and_preserves_negative_intent(tmp_path) -> None:
    database, inquiry_id, draft = _context(tmp_path, "signals")
    feedback = LearningFeedbackService(database).capture_dashboard_negative(
        inquiry_id=inquiry_id,
        original_answer_source="PROGRAM_GENERATED",
        original_answer_reference_id=draft["id"],
        correction_reason="ROUTING_ERROR",
        corrected_intent="PRODUCT_GENERAL",
    )
    service = ApprovalService(database)
    service.save_edited_answer(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        edited_answer="직원이 수정한 다른 답변입니다.",
    )
    service.approve(inquiry_id=inquiry_id, draft_id=draft["id"])
    target = LearningRepository(database).for_inquiry(inquiry_id)[0]

    other_inquiry = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "STORE",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": "REVOKE-OTHER-SAME-DB",
            "inquiry_type": "PRODUCT_GENERAL",
            "title": "다른 문의",
            "content": "다른 Learning은 유지되어야 합니다.",
            "post_status": "NOT_POSTED",
            "raw_json": {},
        }
    ).inquiry_id
    WorkflowRepository(database).initialize_steps(other_inquiry)
    other_draft = AnswerRepository(database).create_program_draft(
        other_inquiry,
        AnswerResult(
            status=AnswerStatus.GENERATED,
            category="GENERAL",
            reason="other",
            answer="다른 문의의 답변입니다.",
            provider="rules",
            auto_answerable=True,
            needs_review=False,
        ),
    )
    other_service = ApprovalService(database)
    other_service.approve(inquiry_id=other_inquiry, draft_id=other_draft["id"])
    other = LearningRepository(database).for_inquiry(other_inquiry)[0]

    service.cancel_approval_with_learning(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        reason="라우팅 재검토",
        learning_id=target["id"],
    )
    persisted_feedback = LearningFeedbackRepository(database).for_inquiry(inquiry_id)
    assert {row["id"] for row in persisted_feedback} == {row["id"] for row in feedback}
    assert all(row["active"] for row in persisted_feedback)
    assert LearningRepository(database).get(other["id"])["active"] is True


def test_cancel_then_staff_edit_can_be_reapproved(tmp_path) -> None:
    database, inquiry_id, draft = _context(tmp_path, "reapprove")
    service = ApprovalService(database)
    service.approve(inquiry_id=inquiry_id, draft_id=draft["id"])
    first = LearningRepository(database).for_inquiry(inquiry_id)[0]
    service.cancel_approval_with_learning(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        reason="다른 답변을 Final로 선택",
        learning_id=first["id"],
    )
    service.save_edited_answer(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        edited_answer="취소 후 직원이 수정한 새 답변입니다.",
    )
    service.approve(inquiry_id=inquiry_id, draft_id=draft["id"])
    rows = LearningRepository(database).for_inquiry(inquiry_id)
    active = [row for row in rows if row["active"]]
    assert len(active) == 1
    assert active[0]["metadata_json"]["answer_provenance"] == "STAFF_EDITED"
    assert LearningRepository(database).get(first["id"])["active"] is False


def test_naver_posted_approval_cancel_does_not_change_remote_truth(tmp_path) -> None:
    database, inquiry_id, draft = _context(tmp_path, "naver")
    with database.transaction() as connection:
        connection.execute(
            "UPDATE inquiries SET source_answered=1, post_status='POSTED' WHERE id=?",
            (inquiry_id,),
        )
    posted = NaverPostedAnswerRepository(database).observe(
        inquiry_id=inquiry_id,
        answer_body="네이버에 실제 게시된 답변입니다.",
        answer_id="NAVER-ANSWER-1",
        source_api="TEST_FIXTURE",
    )
    service = ApprovalService(database)
    learned = service.approve_posted_answer(inquiry_id=inquiry_id, actor="staff")
    outcome = service.cancel_approval_with_learning(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        reason="네이버 답변 내부 검증 취소",
        learning_id=learned["id"],
    )
    current = NaverPostedAnswerRepository(database).current(inquiry_id)
    assert current["id"] == posted["id"]
    assert current["answer_body"] == "네이버에 실제 게시된 답변입니다."
    assert current["answer_id"] == "NAVER-ANSWER-1"
    assert InquiryRepository(database).get(inquiry_id)["post_status"] == "POSTED"
    assert outcome.learning["active"] is False


def test_revoked_trace_and_learning_manager_survive_fresh_repository_read(tmp_path) -> None:
    database, inquiry_id, draft = _context(tmp_path, "trace")
    service = ApprovalService(database)
    service.approve(inquiry_id=inquiry_id, draft_id=draft["id"])
    target = LearningRepository(database).for_inquiry(inquiry_id)[0]
    service.cancel_approval_with_learning(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        reason="사실 오류 발견",
        learning_id=target["id"],
    )
    trace = approval_learning_trace(
        database,
        inquiry_id=inquiry_id,
        draft=AnswerRepository(database).get(draft["id"]),
        approval_state=ApprovalRepository(database).get_inquiry_approval(inquiry_id),
        source_answered=False,
    )
    assert trace["approval_complete"] is False
    assert trace["revoked_learning_id"] == target["id"]
    assert trace["revoked_learning_reason"] == "사실 오류 발견"
    rows = LearningRepository(database).manager_rows()
    assert _filter_rows(rows, query="사실 오류 발견") == rows
    assert rows[0]["metadata_json"]["learning_status"] == "REVOKED"


def test_dashboard_cancel_requires_reason_confirmation_and_persists_result(tmp_path) -> None:
    database, inquiry_id, draft = _context(tmp_path, "apptest")
    ApprovalService(database).approve(inquiry_id=inquiry_id, draft_id=draft["id"])
    app_code = f'''
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from ui.review_workspace import _render_answer_panel
db=Database(r"{database.path}")
db.initialize()
_render_answer_panel(db, InquiryRepository(db).get({inquiry_id}))
'''
    app = AppTest.from_string(app_code).run(timeout=40)
    assert not app.exception
    cancel = next(button for button in app.button if button.label == "승인 취소")
    assert cancel.disabled is True
    reason = next(item for item in app.text_input if item.label == "승인 취소 사유")
    reason.set_value("Dashboard에서 잘못 승인함")
    confirmation = next(
        item
        for item in app.checkbox
        if "Human Verified Positive Learning" in item.label
    )
    confirmation.check()
    app.run(timeout=40)
    cancel = next(button for button in app.button if button.label == "승인 취소")
    assert cancel.disabled is False
    cancel.click()
    app.run(timeout=40)
    assert not app.exception
    rendered = "\n".join(item.value for item in app.markdown)
    assert "승인 취소 완료" in rendered
    assert "현재 상태: 검토 대기" in rendered
    assert "Positive Learning: 비활성화" in rendered
    assert "Dashboard에서 잘못 승인함" in rendered

    fresh = AppTest.from_string(app_code).run(timeout=40)
    assert not fresh.exception
    fresh_rendered = "\n".join(item.value for item in fresh.markdown)
    assert "승인 취소 완료" in fresh_rendered
    assert "Dashboard에서 잘못 승인함" in fresh_rendered
