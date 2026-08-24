"""What the dashboard reports about the most recent Naver sync.

Production report, 2026-08-24. Naver had a new product inquiry at 11:48; the
dashboard list stopped at 11:37, and the sync panel showed the query window

    2026-08-17 11:43:27 ~ 2026-08-24 11:43:27

long after the server clock had passed 11:43. That reads as "automatic sync is
stuck on an old window", which is what an operator concluded.

It was not. ``_render_sync_result`` read ``st.session_state`` first, and that
key is written only by a *manual* sync and never cleared. Once an operator ran
one manual sync, the panel was pinned to it for the rest of the browser
session: every automatic sync afterwards -- each of which computes its window
as ``datetime.now(UTC)`` at run time -- stayed invisible. The stale window was
the operator's own 11:43:27 manual run.

The panel now reports the run that actually happened last, manual or
automatic, and stamps it with when it ran and when the next one is due, so a
stopped scheduler is distinguishable from a recent window.

No network, no real sync: the repository is written directly.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import app as appmod
from repositories.database import Database
from repositories.naver_sync_repository import NaverSyncRepository


@pytest.fixture
def database(tmp_path: Path) -> Database:
    value = Database(tmp_path / "sync-display.db")
    value.initialize()
    return value


def _record_run(
    database: Database,
    *,
    sync_id: str,
    to_datetime: datetime,
    days: int = 7,
    status: str = "SUCCESS",
) -> None:
    """Store one finished sync run the way the sync service does."""

    repository = NaverSyncRepository(database)
    repository.start(
        sync_id=sync_id,
        store_id="STORE",
        inquiry_type="PRODUCT_INQUIRY",
        requested_from=(
            to_datetime - timedelta(days=days)
        ).isoformat(timespec="seconds"),
        requested_to=to_datetime.isoformat(timespec="seconds"),
    )
    repository.finish(
        sync_id,
        status=status,
        fetched_count=10,
        inserted_count=1,
        updated_count=0,
        unchanged_count=9,
        skipped_count=0,
        failed_count=0,
        duration_ms=1200,
    )


class _Recorder:
    """Collects the captions the panel renders."""

    def __init__(self) -> None:
        self.captions: list[str] = []

    def caption(self, text: str) -> None:
        self.captions.append(str(text))

    def __getattr__(self, name):  # every other st.* call is a no-op
        def _noop(*args, **kwargs):
            return self

        return _noop

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _render(database: Database, monkeypatch, session: dict) -> list[str]:
    recorder = _Recorder()
    monkeypatch.setattr(appmod.st, "caption", recorder.caption)
    monkeypatch.setattr(appmod.st, "success", lambda *a, **k: None)
    monkeypatch.setattr(appmod.st, "info", lambda *a, **k: None)
    monkeypatch.setattr(appmod.st, "warning", lambda *a, **k: None)
    monkeypatch.setattr(appmod.st, "error", lambda *a, **k: None)
    monkeypatch.setattr(
        appmod.st, "columns", lambda *a, **k: [recorder] * 7
    )
    monkeypatch.setattr(appmod.st, "expander", lambda *a, **k: recorder)
    monkeypatch.setattr(appmod.st, "session_state", session)
    appmod._render_sync_result(database)
    return recorder.captions


STALE_MANUAL = {
    "dashboard_sync_result": {
        "status": "SUCCESS",
        "fetched_count": 1,
        "created_count": 0,
        "requested_from": "2026-08-17T02:43:27+00:00",
        "requested_to": "2026-08-24T02:43:27+00:00",
    }
}


def test_a_later_automatic_sync_replaces_the_pinned_manual_result(
    database: Database, monkeypatch
) -> None:
    """The reported bug: the panel must not stay on the manual run."""

    later = datetime(2026, 8, 24, 3, 53, 0, tzinfo=UTC)  # 12:53 KST
    _record_run(database, sync_id="auto-1", to_datetime=later)

    captions = _render(database, monkeypatch, dict(STALE_MANUAL))
    window = next(text for text in captions if "조회 기간" in text)

    assert "12:53" in window, window
    # The operator's own 11:43:27 manual window must be gone.
    assert "11:43" not in window, window


def test_the_window_is_stamped_with_when_the_run_happened(
    database: Database, monkeypatch
) -> None:
    """A window ending minutes ago must be distinguishable from a dead one."""

    ran = datetime(2026, 8, 24, 3, 53, 0, tzinfo=UTC)
    _record_run(database, sync_id="auto-2", to_datetime=ran)

    window = next(
        text
        for text in _render(database, monkeypatch, {})
        if "조회 기간" in text
    )
    assert "최근 실행" in window


def test_the_session_result_is_still_used_when_nothing_was_stored(
    database: Database, monkeypatch
) -> None:
    """A run that never reached the repository must still be reported."""

    captions = _render(database, monkeypatch, dict(STALE_MANUAL))
    window = next(text for text in captions if "조회 기간" in text)
    assert "11:43" in window, window


def test_no_run_at_all_says_so(database: Database, monkeypatch) -> None:
    captions = _render(database, monkeypatch, {})
    assert any("아직 실행되지 않음" in text for text in captions)


def test_utc_storage_is_displayed_in_kst(
    database: Database, monkeypatch
) -> None:
    """The stored window is UTC; the panel must render KST (+9)."""

    _record_run(
        database,
        sync_id="auto-3",
        to_datetime=datetime(2026, 8, 24, 0, 0, 0, tzinfo=UTC),
    )
    window = next(
        text
        for text in _render(database, monkeypatch, {})
        if "조회 기간" in text
    )
    # 2026-08-24T00:00Z is 09:00 KST, and the window opens 7 days earlier.
    assert "2026-08-24 09:00" in window, window
    assert "2026-08-17 09:00" in window, window


# ------------------------------------------------ the window itself is fresh


def test_each_sync_computes_its_window_from_the_clock_at_run_time(
    database: Database, monkeypatch
) -> None:
    """The reported fear: a window frozen at an earlier sync would skip
    anything that arrived since. It is not frozen -- the orchestrator reads
    the clock on every run, so an inquiry registered between two runs falls
    inside the next window.
    """

    from services import inquiry_sync_orchestrator as module

    captured: list[tuple[datetime, datetime]] = []

    class _FakeResult:
        requested_store_count = 1
        successful_store_count = 1
        fetched_count = 0
        inserted_count = 0
        updated_count = 0
        unchanged_count = 0
        failed_count = 0
        errors: tuple = ()
        skipped_count = 0
        sync_id = "fake"
        api_latest_registered_at = None
        database_latest_registered_at = None
        status = "SUCCESS"
        error_code = None
        error_message = None
        completed_at = None
        requested_from = None
        requested_to = None

    class _FakeSyncService:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def sync_inquiries(self, *, from_datetime, to_datetime, **kwargs):
            captured.append((from_datetime, to_datetime))
            return _FakeResult()

    monkeypatch.setattr(module, "NaverInquirySyncService", _FakeSyncService)
    monkeypatch.setattr(
        module, "get_configured_stores", lambda: [object()]
    )

    orchestrator = module.InquirySyncOrchestrator(database)
    first = datetime(2026, 8, 24, 2, 43, 27, tzinfo=UTC)   # 11:43:27 KST
    second = datetime(2026, 8, 24, 2, 53, 27, tzinfo=UTC)  # 11:53:27 KST

    for moment in (first, second):
        class _Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return moment

        monkeypatch.setattr(module, "datetime", _Clock)
        orchestrator.run(stores=[object()], sync_type="AUTO")

    assert [window[1] for window in captured] == [first, second]
    # An inquiry registered at 11:48 KST sits inside the second window.
    arrived = datetime(2026, 8, 24, 2, 48, 0, tzinfo=UTC)
    second_from, second_to = captured[1]
    assert second_from < arrived < second_to
    # ...and outside the first, which is why it had not appeared yet.
    first_from, first_to = captured[0]
    assert not (first_from < arrived < first_to)
