from __future__ import annotations

from repositories.approval_repository import ApprovalRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from ui.dashboard import _state_matches_filter


def test_official_kpi_filter_classification() -> None:
    assert _state_matches_filter(
        {"workflow_status": "NEW", "approval_status": "PENDING"}, "NEW"
    )
    assert _state_matches_filter(
        {
            "workflow_status": "REVIEW_PENDING",
            "approval_status": "PENDING",
            "post_status": "NOT_POSTED",
            "has_draft": True,
        },
        "DRAFTED",
    )
    assert _state_matches_filter(
        {
            "workflow_status": "REVIEW_PENDING",
            "approval_status": "PENDING",
            "has_draft": True,
        },
        "REVIEW",
    )
    assert _state_matches_filter(
        {"post_status": "NOT_POSTED", "approval_status": "APPROVED"},
        "APPROVED",
    )
    assert not _state_matches_filter(
        {"post_status": "POSTED", "approval_status": "PENDING"},
        "APPROVED",
    )
    assert _state_matches_filter(
        {"workflow_status": "FAILED"}, "ATTENTION"
    )


def test_dashboard_state_query_includes_approval_columns(tmp_path) -> None:
    database = Database(tmp_path / "dashboard.db")
    database.initialize()
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "STORE",
            "source_type": "CUSTOMER_INQUIRY",
            "source_question_id": "KPI-1",
            "content": "문의",
            "raw_json": {},
        }
    ).inquiry_id
    states = ApprovalRepository(database).dashboard_states()
    assert states == [
        {
            "id": inquiry_id,
            "store_code": "STORE",
            "source_type": "CUSTOMER_INQUIRY",
            "source_question_id": "KPI-1",
            "workflow_status": "NEW",
            "answer_status": "UNANSWERED",
            "post_status": "NOT_POSTED",
            "approval_status": "PENDING",
            "approved_at": None,
            "approved_by": None,
            "has_draft": False,
            "latest_review_status": None,
        }
    ]
