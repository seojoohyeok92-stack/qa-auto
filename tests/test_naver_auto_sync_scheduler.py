from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

from answer.models import AnswerResult, AnswerStatus
from config import NaverAutoSyncSettings, validate_auto_sync_interval
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.dps_repository import DpsRepository
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from repositories.naver_sync_repository import NaverSyncRepository
from repositories.workflow_repository import WorkflowRepository
from services.inquiry_sync_service import InquirySyncService
from services.naver_auto_sync_scheduler import (
    NaverAutoSyncScheduler,
    ensure_auto_sync_scheduler,
)


class FakeTimer:
    created: list["FakeTimer"] = []

    def __init__(self, delay, callback) -> None:
        self.delay = delay
        self.callback = callback
        self.daemon = False
        self.started = False
        self.cancelled = False
        self.__class__.created.append(self)

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True


class Result:
    def __init__(
        self,
        *,
        status: str = "SUCCESS",
        fetched: int = 3,
        inserted: int = 1,
        updated: int = 1,
        unchanged: int = 1,
        failed: int = 0,
    ) -> None:
        self.value = {
            "status": status,
            "sync_id": "auto-sync",
            "fetched_count": fetched,
            "inserted_count": inserted,
            "updated_count": updated,
            "unchanged_count": unchanged,
            "failed_count": failed,
        }

    def to_dict(self):
        return dict(self.value)


class RecordingService:
    def __init__(self, database, calls, *, result=None, error=None) -> None:
        self.calls = calls
        self.result = result or Result()
        self.error = error

    def run(self, **kwargs):
        self.calls.append(dict(kwargs))
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
def database(tmp_path: Path) -> Database:
    value = Database(tmp_path / "auto-sync.db")
    value.initialize()
    return value


def enable(database: Database, interval: int = 10) -> NaverSyncRepository:
    runs = NaverSyncRepository(database)
    runs.save_auto_settings(
        enabled=True,
        interval_minutes=interval,
    )
    return runs


def scheduler(
    database: Database,
    calls: list[dict],
    *,
    owner: str = "owner-a",
    result: Result | None = None,
    error: Exception | None = None,
    now=None,
) -> NaverAutoSyncScheduler:
    enable(database)
    return NaverAutoSyncScheduler(
        database,
        service_factory=lambda db: RecordingService(
            db, calls, result=result, error=error
        ),
        timer_factory=FakeTimer,
        owner_id=owner,
        now=now or (lambda: datetime(2026, 7, 31, 6, 0, tzinfo=UTC)),
    )


def test_default_interval_is_ten_minutes() -> None:
    settings = NaverAutoSyncSettings()
    assert settings.enabled is False
    assert settings.interval_minutes == 10


def test_auto_sync_migration_is_idempotent_and_creates_state(
    database: Database,
) -> None:
    assert 12 in database.migration_versions()
    assert database.initialize() == []
    assert NaverSyncRepository(database).auto_settings()[
        "interval_minutes"
    ] == 10
    assert NaverSyncRepository(database).auto_state()["status"] == "STOPPED"


@pytest.mark.parametrize("value", [0, 1, 6, 20, 120])
def test_disallowed_interval_is_rejected(value: int) -> None:
    with pytest.raises(ValueError):
        validate_auto_sync_interval(value)


def test_settings_are_persistent_after_restart(database: Database) -> None:
    NaverSyncRepository(database).save_auto_settings(
        enabled=True, interval_minutes=15
    )
    restored = NaverSyncRepository(Database(database.path)).auto_settings()
    assert restored["enabled"] is True
    assert restored["interval_minutes"] == 15


def test_ensure_scheduler_returns_one_instance_after_rerun(
    database: Database, monkeypatch
) -> None:
    monkeypatch.setenv("NAVER_SYNC_ENABLED", "true")
    monkeypatch.setenv("NAVER_AUTO_SYNC_ENABLED", "true")
    monkeypatch.setenv("NAVER_AUTO_SYNC_INTERVAL_MINUTES", "10")
    first = ensure_auto_sync_scheduler(
        database, timer_factory=FakeTimer
    )
    second = ensure_auto_sync_scheduler(
        database, timer_factory=FakeTimer
    )
    assert first is second
    first.stop()


def test_second_process_cannot_take_non_stale_leader_lease(
    database: Database,
) -> None:
    first = scheduler(database, [], owner="process-a")
    second = scheduler(database, [], owner="process-b")
    assert first.start() is True
    assert second.start() is False
    assert NaverSyncRepository(database).auto_state()["owner_id"] == (
        "process-a"
    )
    first.stop()
    second.stop()


def test_auto_run_skips_while_manual_sync_lock_exists(
    database: Database,
) -> None:
    calls: list[dict] = []
    runs = enable(database)
    assert runs.acquire_lock(
        store_id="STORE",
        sync_id="manual",
        ttl_seconds=300,
        sync_type="MANUAL",
        owner_id="browser",
    )
    value = scheduler(database, calls).run_once()
    assert value == {"status": "SKIPPED", "reason": "SYNC_IN_PROGRESS"}
    assert calls == []
    runs.release_locks("manual")


def test_manual_run_cannot_lock_store_while_auto_sync_runs(
    database: Database,
) -> None:
    runs = NaverSyncRepository(database)
    assert runs.acquire_lock(
        store_id="STORE",
        sync_id="auto",
        ttl_seconds=300,
        sync_type="AUTO",
        owner_id="scheduler",
    )
    assert not runs.acquire_lock(
        store_id="STORE",
        sync_id="manual",
        ttl_seconds=300,
        sync_type="MANUAL",
        owner_id="browser",
    )


def test_stale_store_lock_is_recovered(database: Database) -> None:
    runs = NaverSyncRepository(database)
    assert runs.acquire_lock(
        store_id="STORE", sync_id="old", ttl_seconds=30
    )
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE naver_sync_locks
            SET expires_at='2000-01-01T00:00:00+00:00'
            WHERE store_id='STORE'
            """
        )
    assert runs.acquire_lock(
        store_id="STORE", sync_id="new", ttl_seconds=30
    )
    assert runs.active_locks()[0]["sync_id"] == "new"


def test_successful_auto_sync_persists_state_and_log(
    database: Database,
) -> None:
    calls: list[dict] = []
    value = scheduler(database, calls).run_once()
    state = NaverSyncRepository(database).auto_state()
    assert value["status"] == "SUCCESS"
    assert state["status"] == "SUCCESS"
    assert state["fetched_count"] == 3
    assert state["inserted_count"] == 1
    assert state["last_success_at"]
    assert state["next_run_at"]
    assert calls == [{"sync_type": "AUTO", "owner_id": "owner-a"}]
    assert any(
        row["event_code"] == "NAVER_AUTO_SYNC_COMPLETED"
        for row in LogRepository(database).recent_system(limit=20)
    )


def test_failed_auto_sync_keeps_next_schedule(database: Database) -> None:
    value = scheduler(
        database, [], error=TimeoutError("secret details")
    ).run_once()
    state = NaverSyncRepository(database).auto_state()
    assert value["status"] == "FAILED"
    assert state["status"] == "FAILED"
    assert state["next_run_at"]
    assert state["consecutive_failures"] == 1
    assert "secret details" not in str(state)


def test_successful_auto_sync_triggers_event_auto_post(
    database: Database, monkeypatch
) -> None:
    triggers: list[str] = []

    class PostScheduler:
        def run_once(self, *, trigger: str):
            triggers.append(trigger)
            return {"status": "DISABLED"}

    monkeypatch.setattr(
        "services.naver_auto_post_scheduler.ensure_auto_post_scheduler",
        lambda _: PostScheduler(),
    )
    scheduler(database, []).run_once()
    assert triggers == ["AUTO_SYNC_COMPLETED"]


def test_partial_auto_sync_is_not_reported_as_success(
    database: Database,
) -> None:
    value = scheduler(
        database,
        [],
        result=Result(status="PARTIAL_SYNC", failed=1),
    ).run_once()
    state = NaverSyncRepository(database).auto_state()
    assert value["status"] == "PARTIAL_SYNC"
    assert state["last_success_at"] is None
    assert state["consecutive_failures"] == 1


def test_disabled_auto_sync_never_calls_service(database: Database) -> None:
    calls: list[dict] = []
    value = NaverAutoSyncScheduler(
        database,
        service_factory=lambda db: RecordingService(db, calls),
        timer_factory=FakeTimer,
    ).run_once()
    assert value["status"] == "DISABLED"
    assert calls == []


def test_next_run_uses_configured_interval(database: Database) -> None:
    runs = enable(database, interval=30)
    current = datetime(2026, 7, 31, 6, 0, tzinfo=UTC)
    value = NaverAutoSyncScheduler(
        database,
        service_factory=lambda db: RecordingService(db, []),
        timer_factory=FakeTimer,
        owner_id="clock-owner",
        now=lambda: current,
    )
    value.run_once()
    assert _dt(runs.auto_state()["next_run_at"]) == current + timedelta(
        minutes=30
    )


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_scheduler_restores_existing_next_run(database: Database) -> None:
    runs = enable(database)
    assert runs.acquire_scheduler_lease(owner_id="first")
    expected = "2026-07-31T06:07:00+00:00"
    runs.set_auto_next_run(owner_id="first", next_run_at=expected)
    runs.release_scheduler_lease("first")
    current = datetime(2026, 7, 31, 6, 0, tzinfo=UTC)
    value = NaverAutoSyncScheduler(
        database,
        timer_factory=FakeTimer,
        owner_id="restart",
        now=lambda: current,
    )
    assert value.start() is True
    assert NaverSyncRepository(database).auto_state()["next_run_at"] == expected
    assert FakeTimer.created[-1].delay == 30
    value.stop()


def test_auto_sync_preserves_derived_and_local_metadata(
    database: Database,
) -> None:
    inquiries = InquiryRepository(database)
    inquiry_id = inquiries.upsert_work_item(
        {
            "store_code": "STORE",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": "Q-1",
            "title": "old",
            "content": "same",
            "raw_json": {
                "queue": "AUTO_PROCESSABLE",
                "priority": "HIGH",
                "analysis": {"intent": "PRODUCT"},
                "order_snapshot": {"protected": True},
            },
        }
    ).inquiry_id

    class SyncService:
        def __init__(self, db):
            self.service = InquirySyncService(
                InquiryRepository(db),
                WorkflowRepository(db),
                LogRepository(db),
            )

        def run(self, **kwargs):
            result = self.service.sync(
                [
                    {
                        "store_code": "STORE",
                        "source": "PRODUCT_INQUIRY",
                        "inquiry_id": "Q-1",
                        "title": "new",
                        "content": "same",
                        "raw_payload": {"source_status": "OPEN"},
                    }
                ]
            )
            return Result(
                fetched=1,
                inserted=result["new"],
                updated=result["updated"],
                unchanged=result["unchanged"],
            )

    enable(database)
    value = NaverAutoSyncScheduler(
        database,
        service_factory=SyncService,
        timer_factory=FakeTimer,
        owner_id="metadata-owner",
    )
    value.run_once()
    raw = inquiries.get(inquiry_id)["raw_json"]
    assert raw["queue"] == "AUTO_PROCESSABLE"
    assert raw["priority"] == "HIGH"
    assert raw["analysis"] == {"intent": "PRODUCT"}
    assert raw["order_snapshot"] == {"protected": True}


def test_auto_sync_protects_draft_final_approval_and_dps(
    database: Database,
) -> None:
    inquiries = InquiryRepository(database)
    inquiry_id = inquiries.upsert_work_item(
        {
            "store_code": "STORE",
            "source_type": "CUSTOMER_INQUIRY",
            "source_question_id": "Q-PROTECTED",
            "content": "배송 문의",
            "order_id": "2026073112345678",
            "raw_json": {"order_snapshot": {"keep": True}},
        }
    ).inquiry_id
    draft = AnswerRepository(database).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.GENERATED,
            category="배송",
            reason="protected",
            answer="보호 답변",
            provider="rules",
            auto_answerable=True,
            needs_review=False,
        ),
    )
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE answer_drafts
            SET edited_answer='직원 수정', final_answer='승인 답변',
                review_status='APPROVED'
            WHERE id=?
            """,
            (draft["id"],),
        )
        connection.execute(
            """
            UPDATE inquiries
            SET approval_status='APPROVED', approved_by='tester'
            WHERE id=?
            """,
            (inquiry_id,),
        )
    dps = DpsRepository(database).create_lookup_result(
        inquiry_id=inquiry_id,
        order_id="2026073112345678",
        lookup_status="SUCCESS",
        raw_result={},
        normalized_result={"lookup_status": "SUCCESS"},
    )
    before = _protected(database, inquiry_id)

    class NoopSync:
        def __init__(self, db):
            self.db = db

        def run(self, **kwargs):
            return Result(fetched=0, inserted=0, updated=0, unchanged=0)

    enable(database)
    NaverAutoSyncScheduler(
        database,
        service_factory=NoopSync,
        timer_factory=FakeTimer,
        owner_id="protect-owner",
    ).run_once()
    assert _protected(database, inquiry_id) == before
    assert DpsRepository(database).get(dps["id"])["id"] == dps["id"]


def _protected(database: Database, inquiry_id: int) -> tuple:
    inquiry = InquiryRepository(database).get(inquiry_id)
    draft = AnswerRepository(database).active_for_inquiry(inquiry_id)
    return (
        draft["id"],
        draft["edited_answer"],
        draft["final_answer"],
        draft["review_status"],
        inquiry["approval_status"],
        inquiry["approved_by"],
        inquiry["post_status"],
        inquiry["raw_json"]["order_snapshot"],
    )


def test_auto_sync_calls_read_pipeline_only(database: Database) -> None:
    calls: list[dict] = []
    enable(database)
    NaverAutoSyncScheduler(
        database,
        service_factory=lambda db: RecordingService(db, calls),
        timer_factory=FakeTimer,
        owner_id="read-only",
    ).run_once()
    assert calls == [{"sync_type": "AUTO", "owner_id": "read-only"}]
    assert all("post" not in str(call).lower() for call in calls)
    assert all("put" not in str(call).lower() for call in calls)


def legacy_apptest_displays_auto_sync_status(tmp_path: Path) -> None:
    path = tmp_path / "auto-ui.db"
    db = Database(path)
    db.initialize()
    runs = enable(db)
    assert runs.acquire_scheduler_lease(owner_id="ui")
    runs.finish_auto_run(
        owner_id="ui",
        status="SUCCESS",
        next_run_at="2026-07-31T06:10:00+00:00",
        result={
            "fetched_count": 9,
            "inserted_count": 2,
            "updated_count": 1,
            "unchanged_count": 6,
            "failed_count": 0,
            "sync_id": "ui-auto",
        },
    )
    app = AppTest.from_string(
        f'''
import os
os.environ["NAVER_SYNC_ENABLED"]="true"
from app import render_dashboard_actions
from repositories.database import Database
render_dashboard_actions(Database(r"{path}"), [], [])
'''
    ).run(timeout=30)
    assert not app.exception
    assert any(
        item.label == "네이버 문의 자동 동기화"
        for item in app.expander
    )
    metrics = {item.label: item.value for item in app.metric}
    assert metrics["최근 조회"] == "9"
    assert metrics["신규"] == "2"
    assert any("다음 예정" in item.value for item in app.caption)


def test_apptest_disables_manual_button_while_sync_running(
    tmp_path: Path,
) -> None:
    path = tmp_path / "auto-running-ui.db"
    db = Database(path)
    db.initialize()
    NaverSyncRepository(db).acquire_lock(
        store_id="STORE",
        sync_id="running",
        ttl_seconds=300,
        sync_type="AUTO",
        owner_id="scheduler",
    )
    app = AppTest.from_string(
        f'''
import os
os.environ["NAVER_SYNC_ENABLED"]="true"
from app import render_dashboard_actions
from config import StoreConfig
from repositories.database import Database
render_dashboard_actions(
    Database(r"{path}"),
    [StoreConfig("STORE","스토어","id","secret",True)],
    [],
)
'''
    ).run(timeout=30)
    assert not app.exception
    button = next(
        item for item in app.button
        if item.label == "네이버 문의 동기화"
    )
    assert button.disabled
    assert any("이미 동기화가 진행 중" in item.value for item in app.info)
