from __future__ import annotations

from pathlib import Path

from repositories.auto_post_repository import AutoPostRepository
from repositories.database import Database
from services.auto_post_runtime_service import AutoPostRuntimeService
from services.naver_auto_post_scheduler import (
    ensure_auto_post_scheduler,
    reset_auto_post_runtime_on_process_start,
)


def make_database(tmp_path: Path, name: str = "dashboard-off.db") -> Database:
    database = Database(tmp_path / name)
    assert database.initialize()[-1] == 29
    return database


# CASE DASH-A -- brand-new process, operator switch already OFF -> stays OFF
def test_dash_a_fresh_process_stays_off(tmp_path) -> None:
    database = make_database(tmp_path)
    reset_happened = reset_auto_post_runtime_on_process_start(database)
    assert reset_happened is False
    assert AutoPostRepository(database).settings()["enabled"] is False


# CASE DASH-B -- a previous session left the persisted switch ON; a new
# process must force it back OFF before anything else runs.
def test_dash_b_previous_on_state_is_forced_off_on_new_process(tmp_path) -> None:
    database = make_database(tmp_path)
    AutoPostRepository(database).save_settings(
        enabled=True, interval_minutes=10, max_retries=1,
    )
    assert AutoPostRepository(database).settings()["enabled"] is True

    reset_happened = reset_auto_post_runtime_on_process_start(database)

    assert reset_happened is True
    assert AutoPostRepository(database).settings()["enabled"] is False
    assert AutoPostRepository(database).state()["status"] == "STOPPED"


# CASE DASH-C/D -- after the process-start check has already run once, the
# operator turning ON (a normal in-process action) must never be reverted
# by a later Streamlit rerun (simulated here by calling the startup guard
# again against the same database/process).
def test_dash_c_d_on_survives_a_simulated_rerun(tmp_path, monkeypatch) -> None:
    database = make_database(tmp_path)
    monkeypatch.setenv("NAVER_POST_ENABLED", "true")
    monkeypatch.setenv("NAVER_AUTO_POST_ENABLED", "true")

    # Simulate normal app startup: guard runs once, nothing to reset yet.
    reset_auto_post_runtime_on_process_start(database)

    # Operator explicitly turns ON in this same process.
    runtime = AutoPostRuntimeService(database, authentication_ready=lambda: True)
    result = runtime.enable()
    assert result["status"] in {"RUNNING", "WAITING_FOR_SYNC"}
    assert AutoPostRepository(database).settings()["enabled"] is True

    # A plain Streamlit rerun re-executes app.py top to bottom, which would
    # call the startup guard again -- it must be a no-op this time.
    reset_happened_again = reset_auto_post_runtime_on_process_start(database)
    assert reset_happened_again is False
    assert AutoPostRepository(database).settings()["enabled"] is True

    # ensure_auto_post_scheduler (also called on every rerun) must likewise
    # continue to reflect ON, not be reset.
    scheduler = ensure_auto_post_scheduler(database)
    assert AutoPostRepository(database).settings()["enabled"] is True
    scheduler.stop()


# CASE DASH-I -- a second browser/session in the *same* process must not
# reset the first session's already-ON state.
def test_dash_i_second_session_does_not_reset_existing_on_state(
    tmp_path, monkeypatch,
) -> None:
    database = make_database(tmp_path)
    monkeypatch.setenv("NAVER_POST_ENABLED", "true")
    monkeypatch.setenv("NAVER_AUTO_POST_ENABLED", "true")

    reset_auto_post_runtime_on_process_start(database)
    AutoPostRepository(database).save_settings(
        enabled=True, interval_minutes=10, max_retries=1,
    )

    # A second Database() instance (as a second Streamlit session in the
    # same process would construct) pointed at the identical file.
    second_session_database = Database(database.path)
    reset_happened = reset_auto_post_runtime_on_process_start(
        second_session_database
    )
    assert reset_happened is False
    assert AutoPostRepository(database).settings()["enabled"] is True


# CASE DASH-J -- a genuinely new process (new database path stands in for
# "never touched by this process before") always starts OFF regardless of
# what any previous UI/session state was.
def test_dash_j_new_process_ignores_prior_ui_state(tmp_path) -> None:
    first_process_db = make_database(tmp_path, "first.db")
    AutoPostRepository(first_process_db).save_settings(
        enabled=True, interval_minutes=10, max_retries=1,
    )
    reset_auto_post_runtime_on_process_start(first_process_db)
    assert AutoPostRepository(first_process_db).settings()["enabled"] is False

    # A different database file stands in for a completely independent
    # deployment/process; persisted ON there must also be forced OFF the
    # first time *that* process touches it, regardless of the first
    # database's final state.
    second_process_db = make_database(tmp_path, "second.db")
    AutoPostRepository(second_process_db).save_settings(
        enabled=True, interval_minutes=10, max_retries=1,
    )
    reset_happened = reset_auto_post_runtime_on_process_start(second_process_db)
    assert reset_happened is True
    assert AutoPostRepository(second_process_db).settings()["enabled"] is False
