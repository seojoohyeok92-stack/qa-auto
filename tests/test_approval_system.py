from __future__ import annotations

import pytest

from answer.exceptions import AnswerAlreadyPostedError
from answer.models import AnswerResult, AnswerStatus
from answer.answer_format import format_final_answer
from repositories.answer_repository import AnswerRepository
from repositories.approval_repository import ApprovalRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from repositories.workflow_repository import WorkflowRepository
from services.approval_service import ApprovalLockedError, ApprovalService
from workflow.models import StepCode


@pytest.fixture
def database(tmp_path) -> Database:
    database = Database(tmp_path / "approval.db")
    database.initialize()
    return database


@pytest.fixture
def inquiry_id(database: Database) -> int:
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": "APPROVAL-1",
            "title": "상품 문의",
            "content": "넷플릭스가 되나요?",
            "product_name": "삼성 TV",
            "post_status": "NOT_POSTED",
            "raw_json": {},
        }
    ).inquiry_id
    WorkflowRepository(database).initialize_steps(inquiry_id)
    return inquiry_id


@pytest.fixture
def draft(database: Database, inquiry_id: int) -> dict:
    return AnswerRepository(database).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.GENERATED,
            category="제품 기능",
            reason="OTT 규칙",
            answer="프로그램 원본 답변",
            provider="rules",
            auto_answerable=True,
            needs_review=False,
        ),
    )


def test_migration_v3_creates_approval_schema(database: Database) -> None:
    assert database.migration_versions() == list(range(1, 30))
    with database.connection() as connection:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(inquiries)")
        }
        history = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            ("approval_history",),
        ).fetchone()
    assert {"approval_status", "approved_at", "approved_by"} <= columns
    assert history is not None
    assert database.initialize() == []


def test_staff_edit_save_and_autosave_are_recorded(
    database: Database, inquiry_id: int, draft: dict
) -> None:
    service = ApprovalService(database)
    updated = service.save_edited_answer(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        edited_answer="직원 수정 답변",
        actor="검토자",
        autosave=True,
    )
    assert updated["edited_answer"] == format_final_answer("직원 수정 답변")
    assert updated["review_status"] == "IN_REVIEW"
    history = ApprovalRepository(database).history_for_inquiry(inquiry_id)
    assert history[0]["action"] == "EDIT_SAVED"
    assert history[0]["actor"] == "검토자"
    assert LogRepository(database).recent_for_inquiry(inquiry_id)[0][
        "event_code"
    ] == "STAFF_EDIT_AUTOSAVED"


def test_approval_creates_locked_final_snapshot(
    database: Database, inquiry_id: int, draft: dict
) -> None:
    service = ApprovalService(database)
    service.save_edited_answer(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        edited_answer="승인할 직원 답변",
    )
    outcome = service.approve(
        inquiry_id=inquiry_id, draft_id=draft["id"], actor="승인자"
    )
    assert outcome.draft["final_answer"] == format_final_answer("승인할 직원 답변")
    assert outcome.draft["review_status"] == "APPROVED"
    state = ApprovalRepository(database).get_inquiry_approval(inquiry_id)
    assert state["approval_status"] == "APPROVED"
    assert state["workflow_status"] == "READY_TO_POST"
    assert state["approved_by"] == "승인자"
    step = WorkflowRepository(database).get_step(
        inquiry_id, StepCode.STAFF_REVIEW
    )
    assert step["step_status"] == "COMPLETED"
    with pytest.raises(ApprovalLockedError):
        service.save_edited_answer(
            inquiry_id=inquiry_id,
            draft_id=draft["id"],
            edited_answer="승인 뒤 변경",
        )


def test_direct_approval_uses_program_answer(
    database: Database, inquiry_id: int, draft: dict
) -> None:
    outcome = ApprovalService(database).approve(
        inquiry_id=inquiry_id, draft_id=draft["id"]
    )
    assert outcome.draft["final_answer"] == format_final_answer("프로그램 원본 답변")


def test_cancel_approval_requires_reason_and_unlocks_edit(
    database: Database, inquiry_id: int, draft: dict
) -> None:
    service = ApprovalService(database)
    service.approve(inquiry_id=inquiry_id, draft_id=draft["id"])
    with pytest.raises(ValueError, match="사유"):
        service.cancel_approval(
            inquiry_id=inquiry_id, draft_id=draft["id"], reason=""
        )
    cancelled = service.cancel_approval(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        reason="문구 재검토",
        actor="승인자",
    )
    assert cancelled.draft["final_answer"] is None
    assert cancelled.draft["review_status"] == "IN_REVIEW"
    assert ApprovalRepository(database).get_inquiry_approval(inquiry_id)[
        "workflow_status"
    ] == "REVIEW_PENDING"
    assert WorkflowRepository(database).get_step(
        inquiry_id, StepCode.STAFF_REVIEW
    )["step_status"] == "RUNNING"
    assert service.save_edited_answer(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        edited_answer="취소 후 수정",
    )["edited_answer"] == format_final_answer("취소 후 수정")


def test_reset_restores_program_answer_state(
    database: Database, inquiry_id: int, draft: dict
) -> None:
    service = ApprovalService(database)
    service.save_edited_answer(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        edited_answer="임시 수정",
    )
    reset = service.reset_edited_answer(
        inquiry_id=inquiry_id, draft_id=draft["id"]
    )
    assert reset["edited_answer"] is None
    assert reset["review_status"] == "PENDING"


def test_posted_inquiry_allows_internal_cancel_but_still_blocks_edit_and_delete(
    database: Database, inquiry_id: int, draft: dict
) -> None:
    service = ApprovalService(database)
    service.approve(inquiry_id=inquiry_id, draft_id=draft["id"])
    with database.transaction() as connection:
        connection.execute(
            "UPDATE inquiries SET post_status='POSTED' WHERE id=?",
            (inquiry_id,),
        )
        connection.execute(
            "UPDATE answer_drafts SET posted=1 WHERE id=?",
            (draft["id"],),
        )
    cancelled = service.cancel_approval(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        reason="내부 검증 승인 취소",
    )
    assert cancelled.draft["final_answer"] is None
    assert ApprovalRepository(database).get_inquiry_approval(inquiry_id)[
        "post_status"
    ] == "POSTED"
    with pytest.raises(AnswerAlreadyPostedError):
        service.save_edited_answer(
            inquiry_id=inquiry_id,
            draft_id=draft["id"],
            edited_answer="변경 불가",
        )
    with pytest.raises(ValueError, match="삭제"):
        InquiryRepository(database).delete(inquiry_id)


def test_history_is_preserved_in_order(
    database: Database, inquiry_id: int, draft: dict
) -> None:
    service = ApprovalService(database)
    service.save_edited_answer(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        edited_answer="수정",
    )
    service.approve(inquiry_id=inquiry_id, draft_id=draft["id"])
    service.cancel_approval(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        reason="다시 검토",
    )
    actions = [
        row["action"]
        for row in ApprovalRepository(database).history_for_inquiry(inquiry_id)
    ]
    assert actions == ["APPROVAL_CANCELLED", "APPROVED", "EDIT_SAVED"]
