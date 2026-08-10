from __future__ import annotations

from contextlib import contextmanager
import sqlite3

import pytest

from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from repositories.workflow_repository import WorkflowRepository
from services.inquiry_sync_service import (
    InquirySyncService,
    normalize_work_item,
)
from workflow.models import DEFAULT_STEP_ORDER, StepCode


@pytest.fixture
def database(tmp_path) -> Database:
    database = Database(tmp_path / "sync.db")
    database.initialize()
    return database


@pytest.fixture
def service(database: Database) -> InquirySyncService:
    return InquirySyncService(
        InquiryRepository(database),
        WorkflowRepository(database),
        LogRepository(database),
    )


def make_work_item(
    inquiry_id: str = "I-100",
    *,
    title: str = "배송 문의",
    customer_name: str = "홍길동",
) -> dict:
    return {
        "store_code": "OJE_PLUS",
        "store_name": "오제플러스",
        "source": "CUSTOMER_INQUIRY",
        "inquiry_id": inquiry_id,
        "title": title,
        "content": "주문번호 2026072912345678 설치일 문의",
        "product_name": "TV",
        "product_option": "65인치",
        "customer_name": customer_name,
        "order_id": "2026072912345678",
        "product_order_ids": ["P-100"],
        "registered_at": "2026-07-29T09:00:00+09:00",
        "answered": False,
        "original_data": {"inquiryNo": inquiry_id},
    }


def test_existing_work_item_normalization() -> None:
    normalized = normalize_work_item(make_work_item())
    assert normalized["source_type"] == "CUSTOMER_INQUIRY"
    assert normalized["source_question_id"] == "I-100"
    assert normalized["option_name"] == "65인치"
    assert normalized["product_order_id"] == "P-100"
    assert normalized["answer_status"] == "UNANSWERED"


def test_work_item_with_missing_optional_fields_is_supported() -> None:
    normalized = normalize_work_item(
        {
            "store_code": "OJE_PLUS",
            "source": "PRODUCT_INQUIRY",
            "inquiry_id": "P-1",
        }
    )
    assert normalized["source_question_id"] == "P-1"
    assert normalized["content"] is None
    assert normalized["order_id"] is None


@pytest.mark.parametrize(
    "missing_field",
    ["store_code", "source", "inquiry_id"],
)
def test_missing_required_identity_field_is_rejected(
    missing_field: str,
) -> None:
    item = make_work_item()
    item.pop(missing_field)
    if missing_field == "inquiry_id":
        item["original_data"] = {}
    with pytest.raises(ValueError):
        normalize_work_item(item)


def test_new_sync_creates_all_steps_and_completes_collection(
    database: Database,
    service: InquirySyncService,
) -> None:
    result = service.sync([make_work_item()])
    inquiry = InquiryRepository(database).get_by_source(
        "OJE_PLUS",
        "CUSTOMER_INQUIRY",
        "I-100",
    )
    assert result == {"new": 1, "updated": 0, "unchanged": 0, "failed": 0}
    assert inquiry is not None
    steps = WorkflowRepository(database).list_steps(inquiry["id"])
    assert len(steps) == len(DEFAULT_STEP_ORDER)
    assert steps[0]["step_code"] == StepCode.INQUIRY_COLLECTED.value
    assert steps[0]["step_status"] == "COMPLETED"
    assert all(step["step_status"] == "PENDING" for step in steps[1:])


def test_sync_accepts_original_load_work_queue_tuple(
    service: InquirySyncService,
) -> None:
    result = service.sync(([make_work_item()], [{"message": "ignored"}]))
    assert result["new"] == 1


def test_resync_repairs_missing_workflow_steps(
    database: Database,
    service: InquirySyncService,
) -> None:
    service.sync([make_work_item()])
    inquiry = InquiryRepository(database).list()[0]
    with database.transaction() as connection:
        connection.execute(
            "DELETE FROM workflow_steps WHERE inquiry_id = ?",
            (inquiry["id"],),
        )
    result = service.sync([make_work_item()])
    steps = WorkflowRepository(database).list_steps(inquiry["id"])
    assert result["unchanged"] == 1
    assert len(steps) == len(DEFAULT_STEP_ORDER)
    assert steps[0]["step_status"] == "COMPLETED"


def test_unchanged_resync_does_not_open_redundant_workflow_write(
    database: Database,
    service: InquirySyncService,
) -> None:
    service.sync([make_work_item()])

    class ReadOnlyWorkflowDatabase:
        def connection(self):
            return database.connection()

        @contextmanager
        def transaction(self):
            raise sqlite3.OperationalError(
                "attempt to write a readonly database"
            )
            yield  # pragma: no cover

    workflow = WorkflowRepository(ReadOnlyWorkflowDatabase())  # type: ignore[arg-type]
    resync = InquirySyncService(
        InquiryRepository(database),
        workflow,
        LogRepository(database),
    )

    assert resync.sync([make_work_item()]) == {
        "new": 0,
        "updated": 0,
        "unchanged": 1,
        "failed": 0,
    }


def test_sync_failure_records_exact_processing_stage(
    database: Database,
    service: InquirySyncService,
) -> None:
    service.sync([make_work_item()])

    class FailingWorkflow(WorkflowRepository):
        def initialize_steps(self, inquiry_id: int) -> int:
            raise sqlite3.OperationalError(
                "attempt to write a readonly database"
            )

    events: list[tuple[str, dict]] = []
    resync = InquirySyncService(
        InquiryRepository(database),
        FailingWorkflow(database),
        LogRepository(database),
    )
    result = resync.sync(
        [make_work_item()],
        event_callback=lambda code, details, **_: events.append((code, details)),
    )

    assert result == {"new": 0, "updated": 0, "unchanged": 1, "failed": 1}
    failure = next(
        details for code, details in events
        if code == "NAVER_SYNC_DB_UPSERT_FAILED"
    )
    assert failure["failed_stage"] == "WORKFLOW_INITIALIZE"
    assert failure["error"] == "attempt to write a readonly database"
    assert "initialize_steps" in failure["stack_trace"]


def test_one_sync_failure_does_not_stop_remaining_items(
    database: Database,
    service: InquirySyncService,
) -> None:
    bad_item = {"store_code": "OJE_PLUS", "source": "CUSTOMER_INQUIRY"}
    result = service.sync(
        [make_work_item("I-1"), bad_item, make_work_item("I-2")]
    )
    assert result == {"new": 2, "updated": 0, "unchanged": 0, "failed": 1}
    assert InquiryRepository(database).count() == 2
    failure_log = LogRepository(database).recent_system(limit=10)
    assert any(
        row["event_code"] == "INQUIRY_SYNC_ITEM_FAILED"
        for row in failure_log
    )


def test_repeat_sync_preserves_staff_status_steps_answers_and_learning(
    database: Database,
    service: InquirySyncService,
) -> None:
    service.sync([make_work_item()])
    inquiries = InquiryRepository(database)
    inquiry = inquiries.get_by_source(
        "OJE_PLUS",
        "CUSTOMER_INQUIRY",
        "I-100",
    )
    assert inquiry is not None
    inquiry_id = inquiry["id"]
    inquiries.update_status(inquiry_id, "REVIEW_PENDING")
    workflows = WorkflowRepository(database)
    workflows.start_step(inquiry_id, StepCode.STAFF_REVIEW)

    with database.transaction() as connection:
        answer_cursor = connection.execute(
            """
            INSERT INTO answer_drafts (
                inquiry_id, original_answer, edited_answer, review_status
            ) VALUES (?, ?, ?, ?)
            """,
            (inquiry_id, "원본", "직원 수정", "IN_REVIEW"),
        )
        connection.execute(
            """
            INSERT INTO learning_candidates (
                inquiry_id, answer_draft_id, candidate_type,
                original_answer, edited_answer, candidate_status
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                inquiry_id,
                answer_cursor.lastrowid,
                "STAFF_EDIT",
                "원본",
                "직원 수정",
                "PENDING",
            ),
        )

    changed = make_work_item(title="변경된 배송 문의")
    changed["answered"] = True
    result = service.sync([changed])
    stored = inquiries.get(inquiry_id)
    assert result["updated"] == 1
    assert stored["title"] == "변경된 배송 문의"
    assert stored["workflow_status"] == "REVIEW_PENDING"
    assert stored["answer_status"] == "UNANSWERED"
    assert workflows.get_step(
        inquiry_id,
        StepCode.STAFF_REVIEW,
    )["step_status"] == "RUNNING"
    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM answer_drafts WHERE inquiry_id = ?",
            (inquiry_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM learning_candidates WHERE inquiry_id = ?",
            (inquiry_id,),
        ).fetchone()[0] == 1


def test_raw_json_drops_secret_fields(
    database: Database,
    service: InquirySyncService,
) -> None:
    item = make_work_item()
    item["access_token"] = "secret-token"
    item["nested"] = {"api_key": "secret-key", "safe": "value"}
    service.sync([item])
    stored = InquiryRepository(database).list()[0]["raw_json"]
    assert "access_token" not in stored
    assert "api_key" not in stored["nested"]
    assert stored["nested"]["safe"] == "value"


def test_sync_failure_log_masks_customer_data(
    database: Database,
    service: InquirySyncService,
) -> None:
    invalid = make_work_item(customer_name="홍길동")
    invalid.pop("inquiry_id")
    invalid["original_data"] = {}
    invalid["content"] = "010-1234-5678 user@example.com"
    service.sync([invalid])
    logs = LogRepository(database).recent_system(limit=10)
    failure = next(
        row for row in logs
        if row["event_code"] == "INQUIRY_SYNC_ITEM_FAILED"
    )
    serialized = str(failure)
    assert "홍길동" not in serialized
    assert "010-1234-5678" not in serialized
    assert "user@example.com" not in serialized
