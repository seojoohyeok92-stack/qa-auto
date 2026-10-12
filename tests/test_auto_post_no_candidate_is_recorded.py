"""An auto-post event may not end without an account of itself.

Measured on the server backup of 2026-10-12: auto-post event 1433 (inquiry
10850) was claimed and completed in 31ms by run 5809, which reported
processed 0 / succeeded 0 / failed 0 / unknown 0 / skipped 0. The inquiry was
left REVIEW_PENDING / NOT_POSTED with no post decision and no attempt against
it. The reason -- it had been answered at the source -- was knowable only
because the sync had recorded it eight hours earlier; the scheduler recorded
nothing. An all-zero result means the candidate query excluded the row, so no
stage of the pipeline ran and no stage logged why.

The event still completes. The queue, its leases and its retry rules are
unchanged: a row the candidate query excludes is not pending work. Only the
silence is removed.
"""
from __future__ import annotations

import json
from pathlib import Path

from repositories.auto_post_event_repository import AutoPostEventRepository
from repositories.auto_post_repository import AutoPostRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.naver_sync_repository import NaverSyncRepository
from services.auto_post_pipeline_service import (
    AutoPostPipelineService,
    AutoPostRunResult,
)
from services.naver_auto_post_scheduler import NaverAutoPostScheduler


class FakeTimer:
    def __init__(self, delay, callback) -> None:
        self.delay = delay
        self.callback = callback
        self.daemon = False

    def start(self) -> None:
        pass

    def cancel(self) -> None:
        pass


def arrange(tmp_path: Path, monkeypatch, *, name: str = "no-candidate"):
    database = Database(tmp_path / f"{name}.db")
    database.initialize()
    monkeypatch.setenv("NAVER_POST_ENABLED", "true")
    monkeypatch.setenv("NAVER_AUTO_POST_ENABLED", "true")
    NaverSyncRepository(database).save_auto_settings(
        enabled=True, interval_minutes=10
    )
    AutoPostRepository(database).save_settings(
        enabled=True, interval_minutes=10, max_retries=1
    )
    return database


def make_inquiry(database: Database, external_id: str = "Q-NC") -> int:
    return InquiryRepository(database).upsert_work_item({
        "store_code": "STORE",
        "source_type": "PRODUCT_INQUIRY",
        "source_question_id": external_id,
        "external_inquiry_id": external_id,
        "inquiry_type": "PRODUCT_INQUIRY",
        "content": "문의",
        "raw_json": {},
    }).inquiry_id


def events_for(database: Database, inquiry_id: int, code: str) -> list[dict]:
    with database.connection() as connection:
        rows = connection.execute(
            "SELECT level, message, details_json FROM activity_logs"
            " WHERE inquiry_id=? AND event_code=? ORDER BY id",
            (inquiry_id, code),
        ).fetchall()
    return [
        {
            "level": row["level"],
            "message": row["message"],
            "details": json.loads(row["details_json"] or "{}"),
        }
        for row in rows
    ]


def test_an_all_zero_result_records_no_candidate_before_completing(
    tmp_path: Path, monkeypatch,
) -> None:
    database = arrange(tmp_path, monkeypatch)
    inquiry_id = make_inquiry(database)
    event = AutoPostEventRepository(database).create(
        inquiry_id=inquiry_id, store_code="STORE", external_id="Q-NC",
        source_sync_id="SYNC-NC", runtime_enabled=True,
    )

    class SelectsNothing:
        def run_pending(self, **kwargs):
            assert kwargs["inquiry_ids"] == [inquiry_id]
            return AutoPostRunResult()

    result = NaverAutoPostScheduler(
        database, pipeline_factory=lambda _: SelectsNothing(),
        timer_factory=FakeTimer, owner_id="NC-LEADER",
    ).run_once(trigger="POLLING_FALLBACK")

    assert result["processed_count"] == 0
    # The event is still terminal -- this is not a new retry path.
    assert AutoPostEventRepository(database).get(event["id"])["status"] == (
        "COMPLETED"
    )
    recorded = events_for(database, inquiry_id, "AUTO_POST_NO_CANDIDATE")
    assert len(recorded) == 1, "the silent completion must leave a record"
    entry = recorded[0]
    assert entry["level"] == "WARNING"
    assert entry["details"]["reason_code"] == "NO_CANDIDATE"
    assert entry["details"]["event_id"] == event["id"]
    assert entry["details"]["network_call_count"] == 0
    assert entry["details"]["auto_post_run_id"] == result["auto_post_run_id"]


def test_the_record_carries_the_state_the_candidate_query_filters_on(
    tmp_path: Path, monkeypatch,
) -> None:
    """Reported so an operator can read the exclusion, never re-derived here."""

    database = arrange(tmp_path, monkeypatch, name="state")
    inquiry_id = make_inquiry(database, "Q-ANSWERED")
    # The production shape: answered at the source, so the candidate query
    # excludes it. This is inquiry 10850's situation.
    with database.transaction() as connection:
        connection.execute(
            "UPDATE inquiries SET source_answered=1 WHERE id=?", (inquiry_id,)
        )
    event = AutoPostEventRepository(database).create(
        inquiry_id=inquiry_id, store_code="STORE", external_id="Q-ANSWERED",
        source_sync_id="SYNC-ANSWERED", runtime_enabled=True,
    )

    class SelectsNothing:
        def run_pending(self, **kwargs):
            return AutoPostRunResult()

    NaverAutoPostScheduler(
        database, pipeline_factory=lambda _: SelectsNothing(),
        timer_factory=FakeTimer, owner_id="NC-STATE",
    ).run_once(trigger="POLLING_FALLBACK")

    details = events_for(
        database, inquiry_id, "AUTO_POST_NO_CANDIDATE"
    )[0]["details"]
    assert details["source_answered"] == 1
    assert details["store_code"] == "STORE"
    assert details["post_status"] == "NOT_POSTED"
    assert "workflow_status" in details
    assert "source_deleted" in details
    assert AutoPostEventRepository(database).get(event["id"])["status"] == (
        "COMPLETED"
    )


def test_the_real_pipeline_excluding_a_row_is_recorded_too(
    tmp_path: Path, monkeypatch,
) -> None:
    """The production path, with the real candidate query doing the excluding.

    No double stands in for the pipeline here: ``AutoPostPipelineService`` runs
    its own SQL, finds nothing for an inquiry answered at the source, and
    returns all zeros without reaching any post service or network call.
    """

    database = arrange(tmp_path, monkeypatch, name="real-pipeline")
    inquiry_id = make_inquiry(database, "Q-REAL")
    with database.transaction() as connection:
        connection.execute(
            "UPDATE inquiries SET source_answered=1 WHERE id=?", (inquiry_id,)
        )
    event = AutoPostEventRepository(database).create(
        inquiry_id=inquiry_id, store_code="STORE", external_id="Q-REAL",
        source_sync_id="SYNC-REAL", runtime_enabled=True,
    )

    result = NaverAutoPostScheduler(
        database, pipeline_factory=AutoPostPipelineService,
        timer_factory=FakeTimer, owner_id="NC-REAL",
    ).run_once(trigger="POLLING_FALLBACK")

    assert result["processed_count"] == 0
    assert result["skipped_count"] == 0
    assert AutoPostEventRepository(database).get(event["id"])["status"] == (
        "COMPLETED"
    )
    recorded = events_for(database, inquiry_id, "AUTO_POST_NO_CANDIDATE")
    assert len(recorded) == 1
    assert recorded[0]["details"]["source_answered"] == 1


def test_a_processed_event_records_no_such_warning(
    tmp_path: Path, monkeypatch,
) -> None:
    """The record is for silence only: a run that did work stays as it was."""

    database = arrange(tmp_path, monkeypatch, name="processed")
    inquiry_id = make_inquiry(database, "Q-OK")
    AutoPostEventRepository(database).create(
        inquiry_id=inquiry_id, store_code="STORE", external_id="Q-OK",
        source_sync_id="SYNC-OK", runtime_enabled=True,
    )

    class Posts:
        def run_pending(self, **kwargs):
            return AutoPostRunResult(processed_count=1, succeeded_count=1)

    result = NaverAutoPostScheduler(
        database, pipeline_factory=lambda _: Posts(),
        timer_factory=FakeTimer, owner_id="NC-OK",
    ).run_once(trigger="POLLING_FALLBACK")

    assert result["succeeded_count"] == 1
    assert events_for(database, inquiry_id, "AUTO_POST_NO_CANDIDATE") == []


def test_a_skip_the_pipeline_already_explains_records_no_such_warning(
    tmp_path: Path, monkeypatch,
) -> None:
    """``_hold_for_review`` and the pipeline's own skips keep their reasons.

    A non-zero ``skipped_count`` means a stage ran and recorded why, which is
    the existing SKIP accounting this change must not duplicate.
    """

    database = arrange(tmp_path, monkeypatch, name="skipped")
    inquiry_id = make_inquiry(database, "Q-SKIP")
    AutoPostEventRepository(database).create(
        inquiry_id=inquiry_id, store_code="STORE", external_id="Q-SKIP",
        source_sync_id="SYNC-SKIP", runtime_enabled=True,
    )

    class Holds:
        def run_pending(self, **kwargs):
            return AutoPostRunResult(processed_count=1, skipped_count=1)

    NaverAutoPostScheduler(
        database, pipeline_factory=lambda _: Holds(),
        timer_factory=FakeTimer, owner_id="NC-SKIP",
    ).run_once(trigger="POLLING_FALLBACK")

    assert events_for(database, inquiry_id, "AUTO_POST_NO_CANDIDATE") == []
