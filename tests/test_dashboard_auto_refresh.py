from __future__ import annotations

from pathlib import Path

import pytest

from app import dashboard_work_items_from_database
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from ui.production_dashboard import (
    dashboard_database_changed,
    dashboard_database_revision,
    render_realtime_operations,
)


def _database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "dashboard-refresh.db")
    database.initialize()
    return database


def _inquiry(question_id: str) -> dict[str, object]:
    return {
        "store_code": "STORE",
        "source_type": "PRODUCT_INQUIRY",
        "source_question_id": question_id,
        "title": "new inquiry",
        "content": "new inquiry body",
        "registered_at": "2026-08-24T14:04:49+09:00",
        "raw_json": {},
    }


def test_new_inquiry_changes_revision_and_is_visible_on_next_db_read(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    baseline = dashboard_database_revision(database)

    InquiryRepository(database).upsert_work_item(_inquiry("NEW-1"))

    assert dashboard_database_changed(database, baseline) is True
    assert [
        item["inquiry_id"]
        for item in dashboard_work_items_from_database(database)
    ] == ["NEW-1"]


def test_periodic_fragment_requests_rerun_without_starting_workers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database = _database(tmp_path)
    baseline = dashboard_database_revision(database)

    InquiryRepository(database).upsert_work_item(_inquiry("NEW-2"))

    class ExpectedRerun(Exception):
        pass

    def forbidden(*args, **kwargs):
        raise AssertionError("UI refresh must not start background work")

    monkeypatch.setattr(
        "services.inquiry_sync_orchestrator.InquirySyncOrchestrator.run",
        forbidden,
    )
    monkeypatch.setattr(
        "services.naver_auto_sync_scheduler.NaverAutoSyncScheduler.run_once",
        forbidden,
    )
    monkeypatch.setattr(
        "services.naver_auto_post_scheduler.NaverAutoPostScheduler.run_once",
        forbidden,
    )
    monkeypatch.setattr(
        "services.auto_post_pipeline_service.AutoPostPipelineService.run_pending",
        forbidden,
    )
    monkeypatch.setattr(
        "ui.production_dashboard.DashboardOperationsService.snapshot",
        forbidden,
    )
    monkeypatch.setattr(
        "ui.production_dashboard.st.rerun",
        lambda: (_ for _ in ()).throw(ExpectedRerun()),
    )

    with pytest.raises(ExpectedRerun):
        render_realtime_operations.__wrapped__(database, baseline)
