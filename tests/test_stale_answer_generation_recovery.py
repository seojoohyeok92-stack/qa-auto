from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from repositories.workflow_repository import WorkflowRepository
from ui.review_workspace import _generation_failure_context
from workflow.models import StepCode


def _database(tmp_path) -> tuple[Database, int]:
    database = Database(tmp_path / "stale-answer.db")
    database.initialize()
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "TEST",
            "source_type": "CUSTOMER_INQUIRY",
            "source_question_id": "STALE-ANSWER",
            "content": "test question",
            "raw_json": {},
        }
    ).inquiry_id
    WorkflowRepository(database).initialize_steps(inquiry_id)
    return database, inquiry_id


def _stale_running(database: Database, inquiry_id: int) -> None:
    workflows = WorkflowRepository(database)
    workflows.start_step(inquiry_id, StepCode.ANSWER_GENERATED)
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE workflow_steps SET started_at = '2000-01-01T00:00:00+00:00'
            WHERE inquiry_id = ? AND step_code = ?
            """,
            (inquiry_id, StepCode.ANSWER_GENERATED.value),
        )


def test_stale_acquire_increments_attempt_and_preserves_recovery_history(tmp_path) -> None:
    database, inquiry_id = _database(tmp_path)
    _stale_running(database, inquiry_id)

    recovery = WorkflowRepository(database).recover_stale_answer_generation(
        inquiry_id
    )
    step = WorkflowRepository(database).get_step(
        inquiry_id, StepCode.ANSWER_GENERATED
    )

    assert recovery is not None
    assert recovery["previous_attempt_count"] == 1
    assert step["step_status"] == "RUNNING"
    assert step["attempt_count"] == 2
    assert step["metadata_json"]["stale_recovery"]["previous_started_at"] == (
        "2000-01-01T00:00:00+00:00"
    )


def test_concurrent_stale_acquire_has_exactly_one_owner(tmp_path) -> None:
    database, inquiry_id = _database(tmp_path)
    _stale_running(database, inquiry_id)

    def acquire() -> bool:
        return (
            WorkflowRepository(database).recover_stale_answer_generation(inquiry_id)
            is not None
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        acquired = list(executor.map(lambda _: acquire(), range(2)))

    assert acquired.count(True) == 1
    assert acquired.count(False) == 1


def test_dashboard_shows_retry_only_when_persisted_failure_evidence_exists(tmp_path) -> None:
    database, inquiry_id = _database(tmp_path)
    assert _generation_failure_context(database, inquiry_id, None) is None

    _stale_running(database, inquiry_id)
    LogRepository(database).record_inquiry(
        inquiry_id,
        "AUTOMATIC_DRAFT_FAILED",
        "safe failure",
        level="ERROR",
        details={"safe_error_code": "ANSWERGENERATIONINPROGRESSERROR"},
    )

    context = _generation_failure_context(database, inquiry_id, None)
    assert context is not None
    assert context["label"] == "자동 답변 생성 재시도 중"
    assert context["error_code"] == "ANSWERGENERATIONINPROGRESSERROR"


def test_dashboard_shows_terminal_generation_failure(tmp_path) -> None:
    database, inquiry_id = _database(tmp_path)
    workflows = WorkflowRepository(database)
    workflows.fail_step(
        inquiry_id,
        StepCode.ANSWER_GENERATED,
        "ANSWER_GENERATION_FAILED",
        "safe failure",
    )

    context = _generation_failure_context(database, inquiry_id, None)
    assert context is not None
    assert context["label"] == "자동 답변 생성 실패"
    assert context["error_code"] == "ANSWER_GENERATION_FAILED"
