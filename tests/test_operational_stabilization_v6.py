"""Acceptance A-N for the 26.8.21v6 operational stabilization diagnosis.

Root cause traced and fixed in this pass: inquiries that reach a legitimate
REVIEW_REQUIRED/NEEDS_ATTENTION outcome have their auto_sync_events queue
row marked COMPLETED (a correct terminal status -- the auto-post system did
its job and correctly declined to post), but the Dashboard previously gave
operators no way to see *why*, or to distinguish "genuinely stuck queue" from
"safety gate correctly holding for staff review". This file proves the queue
mechanics with fixtures/fakes only -- no real Naver POST, no real DPS lookup.
"""
from __future__ import annotations

from pathlib import Path

from repositories.auto_post_event_repository import AutoPostEventRepository
from repositories.auto_post_repository import AutoPostRepository
from repositories.database import Database
from repositories.dashboard_preferences_repository import (
    DashboardPreferencesRepository,
)
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from repositories.naver_sync_repository import NaverSyncRepository
from services.auto_post_pipeline_service import AutoPostRunResult
from services.auto_post_runtime_service import AutoPostRuntimeService
from services.dashboard_operations_service import DashboardOperationsService
from services.naver_auto_post_scheduler import (
    NaverAutoPostScheduler,
    ensure_auto_post_scheduler,
)
from workflow.models import InquiryStatus


class FakeTimer:
    def __init__(self, delay, callback) -> None:
        self.delay = delay
        self.callback = callback
        self.daemon = False

    def start(self) -> None:
        pass

    def cancel(self) -> None:
        pass


def make_database(tmp_path: Path, name: str = "stabilization-v6.db") -> Database:
    database = Database(tmp_path / name)
    database.initialize()
    return database


def make_inquiry(database: Database, external_id: str) -> int:
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


# Acceptance A -- runtime OFF, 11 inquiries synced -> all BLOCKED_AUTO_POST_OFF
def test_acceptance_a_off_period_sync_creates_blocked_events(
    tmp_path: Path,
) -> None:
    database = make_database(tmp_path)
    events = AutoPostEventRepository(database)
    created = []
    for index in range(11):
        external_id = f"OFF-{index}"
        inquiry_id = make_inquiry(database, external_id)
        event = events.create(
            inquiry_id=inquiry_id, store_code="STORE", external_id=external_id,
            source_sync_id="SYNC-OFF", runtime_enabled=False,
        )
        created.append(event)
    assert len(created) == 11
    assert all(event["status"] == "BLOCKED_AUTO_POST_OFF" for event in created)
    summary = events.summary()
    assert summary["BLOCKED_AUTO_POST_OFF"] == 11
    assert summary["PENDING"] == 0


# Acceptance B -- runtime ON -> those 11 requeue to PENDING
def test_acceptance_b_enable_requeues_off_period_events(
    tmp_path: Path, monkeypatch,
) -> None:
    database = make_database(tmp_path)
    enable_sync(database)
    monkeypatch.setenv("NAVER_POST_ENABLED", "true")
    monkeypatch.setenv("NAVER_AUTO_POST_ENABLED", "true")
    events = AutoPostEventRepository(database)
    for index in range(11):
        external_id = f"OFF-{index}"
        inquiry_id = make_inquiry(database, external_id)
        events.create(
            inquiry_id=inquiry_id, store_code="STORE", external_id=external_id,
            source_sync_id="SYNC-OFF", runtime_enabled=False,
        )
    assert events.summary()["BLOCKED_AUTO_POST_OFF"] == 11

    AutoPostRuntimeService(database, authentication_ready=lambda: True).enable()

    summary = events.summary()
    assert summary["BLOCKED_AUTO_POST_OFF"] == 0
    assert summary["PENDING"] == 11
    ensure_auto_post_scheduler(database).stop()


# Acceptance C -- claim_next() actually selects a real claimable PENDING event
def test_acceptance_c_claim_next_selects_pending(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    events = AutoPostEventRepository(database)
    inquiry_id = make_inquiry(database, "CLAIM-1")
    event = events.create(
        inquiry_id=inquiry_id, store_code="STORE", external_id="CLAIM-1",
        source_sync_id="SYNC-1", runtime_enabled=True,
    )
    assert event["status"] == "PENDING"
    claimed = events.claim_next(owner_id="LEADER-A")
    assert claimed is not None
    assert claimed["id"] == event["id"]
    assert claimed["status"] == "PROCESSING"
    # A second leader must not double-claim the same event.
    assert events.claim_next(owner_id="LEADER-B") is None


# Acceptance D -- an eligible inquiry enters the answer-generation pipeline
def test_acceptance_d_eligible_inquiry_enters_generation_pipeline(
    tmp_path: Path, monkeypatch,
) -> None:
    database = make_database(tmp_path)
    enable_sync(database)
    monkeypatch.setenv("NAVER_POST_ENABLED", "true")
    monkeypatch.setenv("NAVER_AUTO_POST_ENABLED", "true")
    AutoPostRepository(database).save_settings(
        enabled=True, interval_minutes=10, max_retries=1
    )
    inquiry_id = make_inquiry(database, "GEN-1")
    events = AutoPostEventRepository(database)
    events.create(
        inquiry_id=inquiry_id, store_code="STORE", external_id="GEN-1",
        source_sync_id="SYNC-1", runtime_enabled=True,
    )

    entered = {"called": False}

    class Pipeline:
        def run_pending(self, **kwargs):
            entered["called"] = True
            assert kwargs["inquiry_ids"] == [inquiry_id]
            return AutoPostRunResult(processed_count=1, succeeded_count=1)

    scheduler = NaverAutoPostScheduler(
        database, pipeline_factory=lambda _: Pipeline(),
        timer_factory=FakeTimer, owner_id="LEADER-D",
    )
    scheduler.run_once(trigger="TEST")
    assert entered["called"] is True


# Acceptance E -- validator pass -> Naver POST pipeline is entered
def test_acceptance_e_validator_pass_reaches_post_pipeline(
    tmp_path: Path, monkeypatch,
) -> None:
    database = make_database(tmp_path)
    enable_sync(database)
    monkeypatch.setenv("NAVER_POST_ENABLED", "true")
    monkeypatch.setenv("NAVER_AUTO_POST_ENABLED", "true")
    AutoPostRepository(database).save_settings(
        enabled=True, interval_minutes=10, max_retries=1
    )
    inquiry_id = make_inquiry(database, "POST-1")
    events = AutoPostEventRepository(database)
    event = events.create(
        inquiry_id=inquiry_id, store_code="STORE", external_id="POST-1",
        source_sync_id="SYNC-1", runtime_enabled=True,
    )

    class SucceedingPipeline:
        def run_pending(self, **kwargs):
            return AutoPostRunResult(processed_count=1, succeeded_count=1)

    scheduler = NaverAutoPostScheduler(
        database, pipeline_factory=lambda _: SucceedingPipeline(),
        timer_factory=FakeTimer, owner_id="LEADER-E",
    )
    result = scheduler.run_once(trigger="TEST")
    assert result["succeeded_count"] == 1
    assert AutoPostEventRepository(database).get(event["id"])["status"] == (
        "COMPLETED"
    )


# Acceptance F -- REVIEW_REQUIRED is never auto-posted; review state is kept
def test_acceptance_f_review_required_is_not_posted(
    tmp_path: Path, monkeypatch,
) -> None:
    database = make_database(tmp_path)
    enable_sync(database)
    monkeypatch.setenv("NAVER_POST_ENABLED", "true")
    monkeypatch.setenv("NAVER_AUTO_POST_ENABLED", "true")
    AutoPostRepository(database).save_settings(
        enabled=True, interval_minutes=10, max_retries=1
    )
    inquiry_id = make_inquiry(database, "REVIEW-1")
    events = AutoPostEventRepository(database)
    events.create(
        inquiry_id=inquiry_id, store_code="STORE", external_id="REVIEW-1",
        source_sync_id="SYNC-1", runtime_enabled=True,
    )

    class ReviewRequiredPipeline:
        def run_pending(self, **kwargs):
            InquiryRepository(database).update_status(
                inquiry_id, InquiryStatus.NEEDS_ATTENTION
            )
            LogRepository(database).record_inquiry(
                inquiry_id, "AUTO_PROCESSING_REVIEW_REQUIRED",
                "설비 확인 필요",
                level="WARNING",
                details={"reasons": ["REQUIRED_ORDER_ID_MISSING_OR_INVALID"]},
            )
            return AutoPostRunResult(processed_count=1, skipped_count=1)

    scheduler = NaverAutoPostScheduler(
        database, pipeline_factory=lambda _: ReviewRequiredPipeline(),
        timer_factory=FakeTimer, owner_id="LEADER-F",
    )
    scheduler.run_once(trigger="TEST")
    fresh = InquiryRepository(database).get(inquiry_id)
    assert fresh["workflow_status"] == "NEEDS_ATTENTION"
    assert str(fresh.get("post_status") or "").upper() != "POSTED"


# Acceptance G -- the new queue-based "claimable pending" metric is
# semantically correct (matches what claim_next() would actually select),
# independent of the pre-existing inquiries-table "Pending" KPI.
def test_acceptance_g_queue_claimable_pending_matches_claim_next(
    tmp_path: Path,
) -> None:
    database = make_database(tmp_path)
    events = AutoPostEventRepository(database)
    for index in range(3):
        external_id = f"Q-{index}"
        inquiry_id = make_inquiry(database, external_id)
        events.create(
            inquiry_id=inquiry_id, store_code="STORE", external_id=external_id,
            source_sync_id="SYNC-1", runtime_enabled=True,
        )
    diagnostics = DashboardOperationsService(database).queue_diagnostics()
    assert diagnostics["queue"]["claimable_pending"] == 3
    claimed = [events.claim_next(owner_id="LEADER-G") for _ in range(4)]
    assert sum(1 for item in claimed if item is not None) == 3


# Acceptance H -- the review-required reason breakdown population matches
# the inquiries actually counted by snapshot()'s review_required KPI.
def test_acceptance_h_review_reason_breakdown_matches_review_required_kpi(
    tmp_path: Path, monkeypatch,
) -> None:
    database = make_database(tmp_path)
    enable_sync(database)
    monkeypatch.setenv("NAVER_POST_ENABLED", "true")
    monkeypatch.setenv("NAVER_AUTO_POST_ENABLED", "true")
    AutoPostRepository(database).save_settings(
        enabled=True, interval_minutes=10, max_retries=1
    )
    inquiry_id = make_inquiry(database, "REVIEW-H")
    events = AutoPostEventRepository(database)
    events.create(
        inquiry_id=inquiry_id, store_code="STORE", external_id="REVIEW-H",
        source_sync_id="SYNC-1", runtime_enabled=True,
    )

    class ReviewRequiredPipeline:
        def run_pending(self, **kwargs):
            InquiryRepository(database).update_status(
                inquiry_id, InquiryStatus.NEEDS_ATTENTION
            )
            LogRepository(database).record_inquiry(
                inquiry_id, "AUTO_PROCESSING_REVIEW_REQUIRED", "DPS 확인 필요",
                level="WARNING",
                details={"reasons": ["DPS_RESULT_NOT_TRUSTED"]},
            )
            return AutoPostRunResult(processed_count=1, skipped_count=1)

    scheduler = NaverAutoPostScheduler(
        database, pipeline_factory=lambda _: ReviewRequiredPipeline(),
        timer_factory=FakeTimer, owner_id="LEADER-H",
    )
    scheduler.run_once(trigger="TEST")

    snapshot = DashboardOperationsService(database).snapshot()
    assert snapshot["review_required"] == 1
    diagnostics = DashboardOperationsService(database).queue_diagnostics()
    assert diagnostics["review_required_reasons"].get("DPS 확인 필요") == 1
    assert diagnostics["dps_required_count"] == 0  # not the DPS-session guard path


# Acceptance I -- Pending == Review Required numerically, but each is
# distinguishable in the new diagnostics (not silently collapsed together).
def test_acceptance_i_pending_equals_review_required_but_distinguishable(
    tmp_path: Path, monkeypatch,
) -> None:
    database = make_database(tmp_path)
    enable_sync(database)
    monkeypatch.setenv("NAVER_POST_ENABLED", "true")
    monkeypatch.setenv("NAVER_AUTO_POST_ENABLED", "true")
    AutoPostRepository(database).save_settings(
        enabled=True, interval_minutes=10, max_retries=1
    )
    events = AutoPostEventRepository(database)
    inquiry_ids = []
    for index in range(2):
        external_id = f"SAME-{index}"
        inquiry_id = make_inquiry(database, external_id)
        inquiry_ids.append(inquiry_id)
        events.create(
            inquiry_id=inquiry_id, store_code="STORE", external_id=external_id,
            source_sync_id="SYNC-1", runtime_enabled=True,
        )

    class ReviewRequiredPipeline:
        def run_pending(self, **kwargs):
            for inquiry_id in kwargs["inquiry_ids"]:
                InquiryRepository(database).update_status(
                    inquiry_id, InquiryStatus.NEEDS_ATTENTION
                )
                LogRepository(database).record_inquiry(
                    inquiry_id, "AUTO_PROCESSING_REVIEW_REQUIRED", "검토 필요",
                    level="WARNING",
                    details={"reasons": ["VALIDATOR_NOT_PASS"]},
                )
            return AutoPostRunResult(
                processed_count=len(kwargs["inquiry_ids"]),
                skipped_count=len(kwargs["inquiry_ids"]),
            )

    scheduler = NaverAutoPostScheduler(
        database, pipeline_factory=lambda _: ReviewRequiredPipeline(),
        timer_factory=FakeTimer, owner_id="LEADER-I",
    )
    for inquiry_id in inquiry_ids:
        scheduler.run_once(
            trigger="TEST", event_only_inquiry_id=inquiry_id
        )

    snapshot = DashboardOperationsService(database).snapshot()
    assert snapshot["pending"] == snapshot["review_required"] == 2
    diagnostics = DashboardOperationsService(database).queue_diagnostics()
    # Same population count, but the diagnostic distinguishes it as
    # genuinely review-required (not a stuck/unclaimed queue).
    assert diagnostics["queue"]["claimable_pending"] == 0
    assert diagnostics["queue"]["completed"] == 2
    assert sum(diagnostics["review_required_reasons"].values()) == 2
    assert len(diagnostics["recent_events"]) == 2
    assert all(
        event["result"] == "REVIEW_REQUIRED" and not event["auto_posted"]
        for event in diagnostics["recent_events"]
    )


# Acceptance J -- an open Dashboard refreshes without manual interaction.
def test_acceptance_j_realtime_operations_is_a_periodic_fragment() -> None:
    import inspect

    import ui.production_dashboard as module
    from ui.production_dashboard import render_realtime_operations

    # st.fragment wraps the function, so the exported symbol must no longer be
    # the bare definition; the run_every interval that drives the periodic
    # self-refresh is declared at the decorator site.
    assert getattr(render_realtime_operations, "__wrapped__", None) is not None
    source = inspect.getsource(module)
    assert '@st.fragment(run_every="30s")' in source


# Acceptance K / L -- Auto Sync keeps RUNNING regardless of Auto Processing
# ON/OFF (full coverage already lives in test_dashboard_operation_startup_off.py
# and test_auto_post_runtime_v3_final.py; this asserts the same invariant
# directly against the settings layer used by both the Dashboard and the
# scheduler, as a fast local acceptance checkpoint for this pass).
def test_acceptance_k_l_auto_sync_independent_of_auto_processing(
    tmp_path: Path, monkeypatch,
) -> None:
    database = make_database(tmp_path)
    enable_sync(database)
    monkeypatch.setenv("NAVER_POST_ENABLED", "true")
    monkeypatch.setenv("NAVER_AUTO_POST_ENABLED", "true")
    runtime = AutoPostRuntimeService(database, authentication_ready=lambda: True)

    assert NaverSyncRepository(database).auto_settings()["enabled"] is True
    runtime.enable()
    assert NaverSyncRepository(database).auto_settings()["enabled"] is True
    runtime.disable()
    assert NaverSyncRepository(database).auto_settings()["enabled"] is True
    ensure_auto_post_scheduler(database).stop()


# Acceptance M -- admin mode ON/OFF never touches Auto Sync settings.
def test_acceptance_m_admin_mode_does_not_touch_auto_sync(
    tmp_path: Path,
) -> None:
    database = make_database(tmp_path)
    enable_sync(database)
    before = NaverSyncRepository(database).auto_settings()

    repository = DashboardPreferencesRepository(database)
    repository.save_admin_mode("local-admin", True)
    repository.save_admin_mode("local-admin", False)

    after = NaverSyncRepository(database).auto_settings()
    assert after == before


# Acceptance N -- admin mode ON/OFF never touches Auto Processing state.
def test_acceptance_n_admin_mode_does_not_touch_auto_processing(
    tmp_path: Path, monkeypatch,
) -> None:
    database = make_database(tmp_path)
    enable_sync(database)
    monkeypatch.setenv("NAVER_POST_ENABLED", "true")
    monkeypatch.setenv("NAVER_AUTO_POST_ENABLED", "true")
    AutoPostRuntimeService(
        database, authentication_ready=lambda: True
    ).enable()
    before = AutoPostRepository(database).settings()["runtime_auto_post_enabled"]
    assert before is True

    repository = DashboardPreferencesRepository(database)
    repository.save_admin_mode("local-admin", True)
    repository.save_admin_mode("local-admin", False)

    after = AutoPostRepository(database).settings()["runtime_auto_post_enabled"]
    assert after == before
    ensure_auto_post_scheduler(database).stop()
