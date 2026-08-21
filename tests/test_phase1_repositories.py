from __future__ import annotations

import sqlite3

import pytest

from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import (
    LogRepository,
    mask_sensitive_data,
    mask_sensitive_text,
)
from repositories.workflow_repository import WorkflowRepository
from workflow.models import DEFAULT_STEP_ORDER, StepCode, StepStatus


@pytest.fixture
def database(tmp_path) -> Database:
    database = Database(tmp_path / "phase1.db")
    database.initialize()
    return database


@pytest.fixture
def inquiry_data() -> dict:
    return {
        "store_code": "OJE_PLUS",
        "source_type": "CUSTOMER_INQUIRY",
        "source_question_id": "Q-100",
        "inquiry_type": "배송",
        "title": "설치 문의",
        "content": "언제 설치되나요?",
        "product_name": "TV",
        "option_name": "65인치",
        "customer_display": "홍길동",
        "order_id": "2026072912345678",
        "product_order_id": "P-100",
        "registered_at": "2026-07-29T09:00:00+09:00",
        "answer_status": "UNANSWERED",
        "post_status": "NOT_POSTED",
        "raw_json": {"nested": {"value": 1}},
    }


@pytest.fixture
def inquiry_id(database: Database, inquiry_data: dict) -> int:
    return InquiryRepository(database).upsert_work_item(inquiry_data).inquiry_id


def test_database_schema_initialization(database: Database) -> None:
    expected = {
        "schema_migrations",
        "inquiries",
        "workflow_steps",
        "activity_logs",
        "dps_results",
        "answer_drafts",
        "learning_candidates",
    }
    with database.connection() as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = ?",
            ("table",),
        ).fetchall()
    assert expected.issubset({row["name"] for row in rows})
    assert database.health()["journal_mode"] == "WAL"
    assert database.health()["foreign_keys"] is True


def test_database_migration_is_not_applied_twice(tmp_path) -> None:
    database = Database(tmp_path / "migration.db")
    assert database.initialize() == list(range(1, 28))
    assert database.initialize() == []
    assert database.migration_versions() == list(range(1, 28))


def test_same_inquiry_upsert_does_not_duplicate(
    database: Database,
    inquiry_data: dict,
) -> None:
    repository = InquiryRepository(database)
    first = repository.upsert_work_item(inquiry_data)
    second = repository.upsert_work_item(inquiry_data)
    assert first.outcome == "new"
    assert second.outcome == "unchanged"
    assert first.inquiry_id == second.inquiry_id
    assert repository.count() == 1


def test_raw_json_round_trip(
    database: Database,
    inquiry_data: dict,
) -> None:
    repository = InquiryRepository(database)
    inquiry_id = repository.upsert_work_item(inquiry_data).inquiry_id
    stored = repository.get(inquiry_id)
    assert stored is not None
    assert stored["raw_json"] == inquiry_data["raw_json"]


def test_data_survives_database_reconnection(
    tmp_path,
    inquiry_data: dict,
) -> None:
    path = tmp_path / "persistent.db"
    first_database = Database(path)
    first_database.initialize()
    created = InquiryRepository(first_database).upsert_work_item(inquiry_data)

    second_database = Database(path)
    second_database.initialize()
    stored = InquiryRepository(second_database).get(created.inquiry_id)
    assert stored is not None
    assert stored["source_question_id"] == "Q-100"


def test_workflow_initializes_all_steps_in_order(
    database: Database,
    inquiry_id: int,
) -> None:
    repository = WorkflowRepository(database)
    assert repository.initialize_steps(inquiry_id) == len(DEFAULT_STEP_ORDER)
    assert repository.initialize_steps(inquiry_id) == 0
    assert [row["step_code"] for row in repository.list_steps(inquiry_id)] == [
        step.value for step in DEFAULT_STEP_ORDER
    ]


def test_pending_running_completed_transition(
    database: Database,
    inquiry_id: int,
) -> None:
    repository = WorkflowRepository(database)
    repository.initialize_steps(inquiry_id)
    running = repository.start_step(inquiry_id, StepCode.QUESTION_ANALYZED)
    completed = repository.complete_step(inquiry_id, StepCode.QUESTION_ANALYZED)
    assert running["step_status"] == StepStatus.RUNNING.value
    assert running["attempt_count"] == 1
    assert completed["step_status"] == StepStatus.COMPLETED.value
    assert completed["completed_at"]


def test_step_failure_records_error(
    database: Database,
    inquiry_id: int,
) -> None:
    repository = WorkflowRepository(database)
    repository.initialize_steps(inquiry_id)
    repository.start_step(inquiry_id, StepCode.DPS_LOOKUP)
    failed = repository.fail_step(
        inquiry_id,
        StepCode.DPS_LOOKUP,
        "DPS_TIMEOUT",
        "DPS 응답 시간이 초과되었습니다.",
    )
    assert failed["step_status"] == StepStatus.FAILED.value
    assert failed["last_error_code"] == "DPS_TIMEOUT"
    assert "초과" in failed["last_error_message"]


def test_retry_after_failure_increments_attempt_count(
    database: Database,
    inquiry_id: int,
) -> None:
    repository = WorkflowRepository(database)
    repository.initialize_steps(inquiry_id)
    repository.start_step(inquiry_id, StepCode.NAVER_ORDER_LOOKUP)
    repository.fail_step(
        inquiry_id,
        StepCode.NAVER_ORDER_LOOKUP,
        "TEMPORARY",
        "temporary error",
    )
    retried = repository.retry_step(
        inquiry_id,
        StepCode.NAVER_ORDER_LOOKUP,
    )
    assert retried["step_status"] == StepStatus.RUNNING.value
    assert retried["attempt_count"] == 2
    assert retried["last_error_code"] is None


def test_step_can_be_skipped(
    database: Database,
    inquiry_id: int,
) -> None:
    repository = WorkflowRepository(database)
    repository.initialize_steps(inquiry_id)
    skipped = repository.skip_step(inquiry_id, StepCode.DPS_LOOKUP)
    assert skipped["step_status"] == StepStatus.SKIPPED.value
    assert skipped["completed_at"]


def test_invalid_step_status_and_transition_are_rejected(
    database: Database,
    inquiry_id: int,
) -> None:
    repository = WorkflowRepository(database)
    repository.initialize_steps(inquiry_id)
    repository.complete_step(inquiry_id, StepCode.INQUIRY_COLLECTED)
    with pytest.raises(ValueError, match="Invalid workflow step transition"):
        repository.start_step(inquiry_id, StepCode.INQUIRY_COLLECTED)
    with database.connection() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE workflow_steps SET step_status = ?
                WHERE inquiry_id = ? AND step_code = ?
                """,
                ("TYPO_STATUS", inquiry_id, StepCode.DPS_LOOKUP.value),
            )


def test_deleting_inquiry_cascades_workflow_steps(
    database: Database,
    inquiry_id: int,
) -> None:
    workflows = WorkflowRepository(database)
    workflows.initialize_steps(inquiry_id)
    assert InquiryRepository(database).delete(inquiry_id) is True
    with database.connection() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM workflow_steps WHERE inquiry_id = ?",
            (inquiry_id,),
        ).fetchone()[0]
    assert count == 0


def test_activity_log_storage_and_queries(
    database: Database,
    inquiry_id: int,
) -> None:
    repository = LogRepository(database)
    repository.record_inquiry(
        inquiry_id,
        "TEST_INQUIRY",
        "문의 로그",
        details={"status": "ok"},
    )
    repository.record_system("TEST_SYSTEM", "시스템 로그")
    assert repository.recent_for_inquiry(inquiry_id)[0]["event_code"] == (
        "TEST_INQUIRY"
    )
    assert repository.recent_system()[0]["event_code"] == "TEST_SYSTEM"


def test_log_privacy_masking(database: Database) -> None:
    repository = LogRepository(database)
    repository.record_system(
        "MASK_TEST",
        (
            "고객명: 홍길동 phone=010-1234-5678 "
            "email=user@example.com order=2026072912345678 "
            "api_key=secret-value"
        ),
        details={
            "access_token": "very-secret",
            "email": "user@example.com",
        },
        customer_names=("홍길동",),
    )
    stored = repository.recent_system()[0]
    combined = stored["message"] + str(stored["details_json"])
    assert "홍길동" not in combined
    assert "010-1234-5678" not in combined
    assert "user@example.com" not in combined
    assert "secret-value" not in combined
    assert "2026072912345678" not in combined
    assert "2026****5678" in combined


def test_abnormally_long_log_values_are_limited(database: Database) -> None:
    repository = LogRepository(database)
    repository.record_system(
        "LONG_LOG",
        "x" * 10_000,
        details={"payload": "y" * 30_000},
    )
    stored = repository.recent_system()[0]
    assert len(stored["message"]) == 2_000
    assert stored["details_json"]["truncated"] is True


def test_inquiry_search_filter_count_and_source_lookup(
    database: Database,
    inquiry_data: dict,
) -> None:
    repository = InquiryRepository(database)
    created = repository.upsert_work_item(inquiry_data)
    repository.update_status(created.inquiry_id, "DPS_PENDING")
    assert repository.count("DPS_PENDING") == 1
    assert repository.list(search="설치")[0]["id"] == created.inquiry_id
    assert repository.list(workflow_status="DPS_PENDING")[0]["id"] == (
        created.inquiry_id
    )
    assert repository.get_by_source(
        "OJE_PLUS",
        "CUSTOMER_INQUIRY",
        "Q-100",
    )["id"] == created.inquiry_id


def test_mask_sensitive_data_does_not_mutate_input() -> None:
    original = {"password": "abc", "nested": {"phone": "010-9876-5432"}}
    masked = mask_sensitive_data(original)
    assert original["password"] == "abc"
    assert masked["password"] == "<masked-secret>"
    assert "010-9876-5432" not in str(masked)
    assert mask_sensitive_text("Bearer abcdefghijkl") == (
        "Bearer <masked-token>"
    )
