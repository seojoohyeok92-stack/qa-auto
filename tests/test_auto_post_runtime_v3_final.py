from __future__ import annotations

from pathlib import Path

from repositories.auto_post_event_repository import AutoPostEventRepository
from repositories.auto_post_repository import AutoPostRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from repositories.naver_sync_repository import NaverSyncRepository
from repositories.workflow_repository import WorkflowRepository
from services.auto_post_pipeline_service import AutoPostRunResult
from services.auto_post_runtime_service import AutoPostRuntimeService
from services.inquiry_sync_service import InquirySyncService
from services.naver_auto_post_scheduler import NaverAutoPostScheduler


class FakeTimer:
    def __init__(self, delay, callback) -> None:
        self.delay = delay
        self.callback = callback
        self.daemon = False
        self.started = False
        self.cancelled = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True


def make_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "runtime-v3.db")
    assert database.initialize()[-1] == 28
    return database


def make_inquiry(database: Database, external_id: str = "Q-NEW") -> int:
    return InquiryRepository(database).upsert_work_item(
        {
            "store_code": "STORE",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": external_id,
            "external_inquiry_id": external_id,
            "inquiry_type": "PRODUCT_INQUIRY",
            "content": "문의",
            "raw_json": {},
        }
    ).inquiry_id


def enable_sync(database: Database) -> None:
    NaverSyncRepository(database).save_auto_settings(
        enabled=True, interval_minutes=10
    )


def test_environment_false_rejects_runtime_request_as_disabled_by_env(
    tmp_path: Path, monkeypatch,
) -> None:
    database = make_database(tmp_path)
    monkeypatch.setenv("NAVER_POST_ENABLED", "false")
    monkeypatch.setenv("NAVER_AUTO_POST_ENABLED", "false")
    result = AutoPostRuntimeService(
        database, authentication_ready=lambda: True
    ).enable()
    assert result["status"] == "DISABLED_BY_ENV"
    assert AutoPostRepository(database).settings()[
        "runtime_auto_post_enabled"
    ] is False
    assert AutoPostRepository(database).state()["status"] == "DISABLED_BY_ENV"


def test_runtime_off_never_starts_scheduler(
    tmp_path: Path, monkeypatch,
) -> None:
    database = make_database(tmp_path)
    enable_sync(database)
    monkeypatch.setenv("NAVER_POST_ENABLED", "true")
    monkeypatch.setenv("NAVER_AUTO_POST_ENABLED", "true")
    scheduler = NaverAutoPostScheduler(
        database, timer_factory=FakeTimer, owner_id="OFF"
    )
    assert scheduler.start() is False
    assert scheduler.started is False


def test_environment_true_runtime_on_runs_and_off_preserves_auto_sync(
    tmp_path: Path, monkeypatch,
) -> None:
    database = make_database(tmp_path)
    enable_sync(database)
    monkeypatch.setenv("NAVER_POST_ENABLED", "true")
    monkeypatch.setenv("NAVER_AUTO_POST_ENABLED", "true")
    runtime = AutoPostRuntimeService(
        database, authentication_ready=lambda: True
    )
    assert runtime.enable()["status"] == "RUNNING"
    assert AutoPostRepository(database).state()["status"] == "RUNNING"
    assert runtime.disable()["status"] == "STOPPED"
    assert NaverSyncRepository(database).auto_settings()["enabled"] is True


def test_runtime_survives_repository_recreation_and_one_leader(
    tmp_path: Path,
) -> None:
    database = make_database(tmp_path)
    AutoPostRepository(database).save_settings(
        enabled=True, interval_minutes=10, max_retries=1
    )
    assert AutoPostRepository(Database(database.path)).settings()[
        "runtime_auto_post_enabled"
    ] is True
    first = NaverAutoPostScheduler(
        database, timer_factory=FakeTimer, owner_id="LEADER-A"
    )
    second = NaverAutoPostScheduler(
        database, timer_factory=FakeTimer, owner_id="LEADER-B"
    )
    assert first.start() is True
    assert second.start() is False
    assert AutoPostRepository(database).state()["owner_id"] == "LEADER-A"
    first.stop()


def test_event_queue_is_unique_by_both_identities_and_off_is_blocked(
    tmp_path: Path,
) -> None:
    database = make_database(tmp_path)
    inquiry_id = make_inquiry(database)
    events = AutoPostEventRepository(database)
    first = events.create(
        inquiry_id=inquiry_id, store_code="STORE", external_id="Q-NEW",
        source_sync_id="SYNC-1", runtime_enabled=False,
    )
    assert first and first["status"] == "BLOCKED_AUTO_POST_OFF"
    assert events.create(
        inquiry_id=inquiry_id, store_code="STORE", external_id="Q-NEW",
        source_sync_id="SYNC-2", runtime_enabled=True,
    ) is None


def test_sync_commit_creates_event_without_failing_when_runtime_off(
    tmp_path: Path,
) -> None:
    database = make_database(tmp_path)
    result = InquirySyncService(
        InquiryRepository(database),
        WorkflowRepository(database),
        LogRepository(database),
    ).sync(
        [{
            "store_code": "STORE",
            "source": "PRODUCT_INQUIRY",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": "SYNC-Q-1",
            "external_inquiry_id": "SYNC-Q-1",
            "inquiry_type": "PRODUCT_INQUIRY",
            "content": "배송 문의",
            "raw_json": {},
        }],
        correlation_id="AUTO-SYNC-1",
    )
    assert result == {"new": 1, "updated": 0, "unchanged": 0, "failed": 0}
    inquiry_id = InquiryRepository(database).list()[0]["id"]
    event = AutoPostEventRepository(database).get_for_inquiry(inquiry_id)
    assert event["status"] == "BLOCKED_AUTO_POST_OFF"
    assert event["source_sync_id"] == "AUTO-SYNC-1"


def test_event_failure_moves_to_scheduler_retry_then_completes(
    tmp_path: Path, monkeypatch,
) -> None:
    database = make_database(tmp_path)
    enable_sync(database)
    monkeypatch.setenv("NAVER_POST_ENABLED", "true")
    monkeypatch.setenv("NAVER_AUTO_POST_ENABLED", "true")
    AutoPostRepository(database).save_settings(
        enabled=True, interval_minutes=10, max_retries=1
    )
    inquiry_id = make_inquiry(database)
    event = AutoPostEventRepository(database).create(
        inquiry_id=inquiry_id, store_code="STORE", external_id="Q-NEW",
        source_sync_id="SYNC-1", runtime_enabled=True,
    )

    class Pipeline:
        failed = True

        def run_pending(self, **kwargs):
            assert kwargs["inquiry_ids"] == [inquiry_id]
            if self.failed:
                self.failed = False
                return AutoPostRunResult(processed_count=1, failed_count=1)
            return AutoPostRunResult(processed_count=1, succeeded_count=1)

    pipeline = Pipeline()
    scheduler = NaverAutoPostScheduler(
        database, pipeline_factory=lambda _: pipeline,
        timer_factory=FakeTimer, owner_id="EVENT-LEADER",
    )
    first = scheduler.run_once(trigger="AUTO_SYNC_EVENT")
    assert first["failed_count"] == 1
    assert AutoPostEventRepository(database).get(event["id"])["status"] == (
        "RETRY_BY_SCHEDULER"
    )
    second = scheduler.run_once(trigger="POLLING_FALLBACK")
    assert second["succeeded_count"] == 1
    assert AutoPostEventRepository(database).get(event["id"])["status"] == (
        "COMPLETED"
    )
    codes = {
        row["event_code"]
        for row in LogRepository(database).recent_for_inquiry(inquiry_id)
    }
    assert {
        "AUTO_SYNC_EVENT_PROCESSING", "AUTO_SYNC_EVENT_FAILED",
        "AUTO_SYNC_EVENT_RETRY_BY_SCHEDULER", "AUTO_SYNC_EVENT_COMPLETED",
    } <= codes


def test_stale_processing_recovers_without_selecting_post_unknown(
    tmp_path: Path,
) -> None:
    database = make_database(tmp_path)
    inquiry_id = make_inquiry(database)
    events = AutoPostEventRepository(database)
    event = events.create(
        inquiry_id=inquiry_id, store_code="STORE", external_id="Q-NEW",
        source_sync_id="SYNC-1", runtime_enabled=True,
    )
    assert events.claim_next(owner_id="CRASHED") is not None
    with database.transaction() as connection:
        connection.execute(
            "UPDATE auto_sync_events SET lease_expires_at='2000-01-01' WHERE id=?",
            (event["id"],),
        )
    assert events.recover_stale_processing() == 1
    assert events.get(event["id"])["status"] == "RETRY_BY_SCHEDULER"
    with database.transaction() as connection:
        connection.execute(
            "UPDATE inquiries SET post_status='POST_UNKNOWN' WHERE id=?",
            (inquiry_id,),
        )
    assert AutoPostRepository(database).candidates(
        max_retries=10, inquiry_ids=[inquiry_id]
    ) == []


def test_post_unknown_turns_runtime_off_and_is_never_retried(
    tmp_path: Path, monkeypatch,
) -> None:
    database = make_database(tmp_path)
    enable_sync(database)
    monkeypatch.setenv("NAVER_POST_ENABLED", "true")
    monkeypatch.setenv("NAVER_AUTO_POST_ENABLED", "true")
    AutoPostRepository(database).save_settings(
        enabled=True, interval_minutes=10, max_retries=3
    )
    inquiry_id = make_inquiry(database)
    event = AutoPostEventRepository(database).create(
        inquiry_id=inquiry_id, store_code="STORE", external_id="Q-NEW",
        source_sync_id="SYNC-UNKNOWN", runtime_enabled=True,
    )

    class UnknownPipeline:
        def run_pending(self, **kwargs):
            return AutoPostRunResult(processed_count=1, unknown_count=1)

    result = NaverAutoPostScheduler(
        database, pipeline_factory=lambda _: UnknownPipeline(),
        timer_factory=FakeTimer, owner_id="UNKNOWN-LEADER",
    ).run_once(trigger="AUTO_SYNC_EVENT")
    assert result["unknown_count"] == 1
    assert AutoPostRepository(database).settings()[
        "runtime_auto_post_enabled"
    ] is False
    assert AutoPostEventRepository(database).get(event["id"])["status"] == "FAILED"
