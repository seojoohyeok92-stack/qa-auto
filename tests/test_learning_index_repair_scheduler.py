"""The periodic repair is a backstop, and has to behave like one.

Three properties carry the weight. It must not be how a new Learning reaches
the index -- that is the post-commit hook, and a repair that ran at startup
would try to embed a whole legacy corpus the moment a dashboard came up. It
must recover the cases the hook cannot: a failed embedding, an ``active=0``
removal that notifies nothing, a suppressed batch import. And it must never
be able to take the dashboard down or stack passes behind a slow one.

Every test here drives the clock through an injected timer factory; none
waits thirty minutes, and none reaches the network. The packaged index is
compared byte-for-byte and mtime-for-mtime at module scope, because this
work has already damaged it twice.
"""
from __future__ import annotations

import hashlib
import logging
import pathlib
import sqlite3
import threading

import pytest

from services import learning_index_repair_scheduler as module
from services.learning_index_repair_scheduler import (
    REPAIR_INTERVAL_MINUTES, SemanticIndexRepairScheduler,
    ensure_semantic_index_repair_scheduler,
    stop_all_semantic_index_repair_schedulers,
)
from services.learning_semantic_index import (
    DEFAULT_INDEX_PATH, LearningSemanticIndex, fingerprint_for,
)
from test_learning_index_sync_hook import (
    FakeDatabase, SCHEMA, insert, make_database,
)
from test_semantic_index_incremental_sync import (
    FakeClient, index_with, rows_for,
)

PACKAGED = pathlib.Path(DEFAULT_INDEX_PATH)


def _packaged_state():
    if not PACKAGED.exists():
        return None
    info = PACKAGED.stat()
    return (info.st_size, info.st_mtime_ns,
            hashlib.sha256(PACKAGED.read_bytes()).hexdigest())


@pytest.fixture(autouse=True)
def packaged_index_is_untouched():
    """§20: this suite must not be able to write the real index."""

    before = _packaged_state()
    yield
    assert _packaged_state() == before, (
        "the packaged semantic index changed during this test")


@pytest.fixture(autouse=True)
def no_scheduler_survives(request):
    """§15: a leaked timer would hang the suite."""

    yield
    stop_all_semantic_index_repair_schedulers()
    with module._REGISTRY_LOCK:
        module._SCHEDULERS.clear()


class ManualTimer:
    """A Timer that fires only when a test says so.

    Records the delay it was asked for, so the interval is asserted rather
    than waited out.
    """

    created: list["ManualTimer"] = []

    def __init__(self, delay, function):
        self.delay = delay
        self.function = function
        self.daemon = False
        self.started = False
        self.cancelled = False
        ManualTimer.created.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        assert self.started and not self.cancelled
        self.function()

    @classmethod
    def reset(cls):
        cls.created = []

    @classmethod
    def pending(cls):
        return [t for t in cls.created if t.started and not t.cancelled]


@pytest.fixture(autouse=True)
def fresh_timers():
    ManualTimer.reset()
    yield
    ManualTimer.reset()


def build(tmp_path, rows=(), *, client=None, interval=None):
    """A scheduler over a temp database, with a temp sibling index."""

    database = make_database(tmp_path, rows)
    sibling = tmp_path / "learning_semantic_index.json"
    calls: list[dict] = []
    fake = client

    def sync(db, **kwargs):
        from services.learning_index_sync_hook import sync_semantic_index

        calls.append({"database": db, "kwargs": dict(kwargs)})
        return sync_semantic_index(db, client=fake,
                                   lock=threading.RLock(), **kwargs)

    scheduler = SemanticIndexRepairScheduler(
        database, timer_factory=ManualTimer, sync=sync,
        **({"interval_minutes": interval} if interval else {}))
    return scheduler, database, sibling, calls


# ------------------------------------------------- interval and first tick
def test_the_interval_is_thirty_minutes():
    assert REPAIR_INTERVAL_MINUTES == 30


def test_the_first_tick_is_a_whole_interval_away_not_at_startup(tmp_path):
    """§10: a legacy index must not be rebuilt by the dashboard coming up."""

    scheduler, _db, sibling, calls = build(
        tmp_path, rows_for((1, "하나")), client=FakeClient(salt=1))

    assert scheduler.start() is True
    assert calls == []                      # nothing ran yet
    assert not sibling.exists()
    assert scheduler.ticks == 0

    timers = ManualTimer.pending()
    assert len(timers) == 1
    assert timers[0].delay == pytest.approx(30 * 60.0)
    assert timers[0].daemon is True


def test_each_tick_reschedules_one_interval_later(tmp_path):
    """A tick that did not reschedule would leave the scheduler dead.

    Asserted on a NEWLY CREATED timer rather than on "something is pending":
    the timer that just fired is still started-and-uncancelled, so a count of
    pending timers cannot tell a reschedule from a scheduler that stopped.
    """

    scheduler, _db, _sibling, _calls = build(
        tmp_path, rows_for((1, "하나")), client=FakeClient(salt=1))
    scheduler.start()
    first = ManualTimer.pending()[0]
    created_before = len(ManualTimer.created)

    first.fire()

    assert scheduler.ticks == 1
    assert len(ManualTimer.created) == created_before + 1, (
        "the tick did not schedule the next one")
    following = ManualTimer.created[-1]
    assert following is not first
    assert following.started is True
    assert following.cancelled is False
    assert following.delay == pytest.approx(30 * 60.0)
    assert first.cancelled is True          # replaced, not left running


def test_the_loop_keeps_going_for_several_ticks(tmp_path):
    """Three passes, so the loop is shown to continue rather than just start."""

    scheduler, _db, _sibling, _calls = build(
        tmp_path, rows_for((1, "하나")), client=FakeClient(salt=1))
    scheduler.start()

    for expected in (1, 2, 3):
        created_before = len(ManualTimer.created)
        ManualTimer.created[-1].fire()
        assert scheduler.ticks == expected
        assert len(ManualTimer.created) == created_before + 1
        assert ManualTimer.created[-1].delay == pytest.approx(30 * 60.0)


# ------------------------------------------------------ what a tick repairs
def test_a_missing_learning_is_repaired(tmp_path):
    """§12 A: in the corpus, absent from the index."""

    existing = rows_for((1, "기존 답변입니다"))
    scheduler, database, sibling, _calls = build(
        tmp_path, existing, client=FakeClient(salt=1))
    index_with(existing, fingerprints=True, salt=0).save(sibling)
    insert(database, 2, "질분 2 입니까", "새 답변")

    scheduler.start()
    ManualTimer.pending()[0].fire()

    reloaded = LearningSemanticIndex.load(sibling)
    assert set(reloaded.vectors) == {1, 2}
    assert 2 in reloaded.fingerprints
    assert scheduler.repairs == 1


def test_a_stale_learning_is_re_embedded(tmp_path):
    """§12 B: the text moved, the fingerprint did not."""

    both = rows_for((1, "첫 답변입니다"),
                    (2, "둘째 답변입니다"))
    scheduler, database, sibling, _calls = build(
        tmp_path, both, client=FakeClient(salt=1))
    index_with(both, fingerprints=True, salt=0).save(sibling)
    untouched = LearningSemanticIndex.load(sibling).vectors[2]
    insert(database, 1, both[0]["question"],
           "정책이 바띀어 고친 답변")

    scheduler.start()
    ManualTimer.pending()[0].fire()

    reloaded = LearningSemanticIndex.load(sibling)
    assert reloaded.vectors[2] == untouched       # bit for bit
    assert reloaded.fingerprints[1] == fingerprint_for(
        {"question": both[0]["question"],
         "answer": "정책이 바띀어 고친 답변"})
    assert scheduler.repairs == 1


def test_a_deactivated_learning_loses_its_vector(tmp_path):
    """§12 C: the backstop for the removal paths the hook does not notify.

    revoke_human_verified, deactivate_automatic_positive, deactivate_draft
    and the feedback revocation all set active=0 without notifying anything.
    This is the pass that cleans up after them.
    """

    both = rows_for((1, "남습니다"), (2, "바집니다"))
    client = FakeClient(salt=1)
    scheduler, database, sibling, _calls = build(tmp_path, both, client=client)
    index_with(both, fingerprints=True, salt=0).save(sibling)
    with database.connection() as connection:
        connection.execute("UPDATE learning_examples SET active=0 WHERE id=2")
        connection.commit()

    scheduler.start()
    ManualTimer.pending()[0].fire()

    reloaded = LearningSemanticIndex.load(sibling)
    assert set(reloaded.vectors) == {1}
    assert set(reloaded.fingerprints) == {1}
    assert client.calls == 0                 # a removal needs no request
    assert scheduler.repairs == 1


def test_a_fresh_index_is_left_entirely_alone(tmp_path, caplog):
    """§12 D: no embedding, no save, and no new mtime."""

    rows = rows_for((1, "그대로"), (2, "이것도 그대로"))
    client = FakeClient(salt=1)
    scheduler, _db, sibling, _calls = build(tmp_path, rows, client=client)
    index_with(rows, fingerprints=True, salt=0).save(sibling)
    before = sibling.read_bytes()
    before_mtime = sibling.stat().st_mtime_ns

    scheduler.start()
    with caplog.at_level(logging.DEBUG):
        ManualTimer.pending()[0].fire()

    assert client.calls == 0
    assert scheduler.repairs == 0
    assert sibling.read_bytes() == before
    assert sibling.stat().st_mtime_ns == before_mtime
    assert any("already current" in record.getMessage()
               for record in caplog.records)
    assert not any(record.levelno >= logging.INFO
                   and "semantic index repaired" in record.getMessage()
                   for record in caplog.records)


# ------------------------------------------- failure and self-healing (§13)
def test_a_failed_tick_leaves_the_index_alone_and_keeps_ticking(
        tmp_path, caplog):
    existing = rows_for((1, "기존 답변입니다"))
    database = make_database(tmp_path, existing)
    sibling = tmp_path / "learning_semantic_index.json"
    index_with(existing, fingerprints=True, salt=0).save(sibling)
    before = sibling.read_bytes()
    before_mtime = sibling.stat().st_mtime_ns
    insert(database, 2, "질분 2 입니까", "새 답변")

    clients = [FakeClient(salt=1, fail_on_call=1), FakeClient(salt=1)]

    def sync(db, **kwargs):
        from services.learning_index_sync_hook import sync_semantic_index

        return sync_semantic_index(db, client=clients.pop(0),
                                   lock=threading.RLock(), **kwargs)

    scheduler = SemanticIndexRepairScheduler(
        database, timer_factory=ManualTimer, sync=sync)
    scheduler.start()

    with caplog.at_level(logging.DEBUG):
        ManualTimer.pending()[0].fire()

    # the failing tick changed nothing and did not stop the scheduler
    assert sibling.read_bytes() == before
    assert sibling.stat().st_mtime_ns == before_mtime
    assert scheduler.repairs == 0
    assert scheduler.started is True
    assert len(ManualTimer.pending()) == 1
    assert any("semantic index sync failed" in record.getMessage()
               for record in caplog.records)

    # the next tick heals it
    ManualTimer.pending()[0].fire()

    reloaded = LearningSemanticIndex.load(sibling)
    assert set(reloaded.vectors) == {1, 2}
    assert scheduler.repairs == 1
    assert scheduler.ticks == 2


def test_an_unexpected_exception_in_a_tick_does_not_stop_the_timer(
        tmp_path, caplog):
    """sync_semantic_index does not raise; if something else does, survive."""

    database = make_database(tmp_path, rows_for((1, "하나")))

    def exploding(db, **kwargs):
        raise RuntimeError("something unforeseen")

    scheduler = SemanticIndexRepairScheduler(
        database, timer_factory=ManualTimer, sync=exploding)
    scheduler.start()

    with caplog.at_level(logging.WARNING):
        ManualTimer.pending()[0].fire()

    assert scheduler.started is True
    assert len(ManualTimer.pending()) == 1        # rescheduled anyway
    assert any("repair tick failed" in record.getMessage()
               for record in caplog.records)


# ------------------------------------------------------- overlap (§9)
def test_a_tick_arriving_while_one_runs_is_skipped_not_queued(tmp_path):
    database = make_database(tmp_path, rows_for((1, "하나")))
    entered = threading.Event()
    release = threading.Event()
    concurrent = []
    depth = {"n": 0}

    def slow(db, **kwargs):
        depth["n"] += 1
        if depth["n"] > 1:
            concurrent.append(True)
        entered.set()
        release.wait(timeout=5)
        depth["n"] -= 1
        return None

    scheduler = SemanticIndexRepairScheduler(
        database, timer_factory=ManualTimer, sync=slow)
    scheduler.start()
    timer = ManualTimer.pending()[0]

    worker = threading.Thread(target=timer.fire, daemon=True)
    worker.start()
    assert entered.wait(timeout=5)

    # a second tick, while the first is inside sync
    scheduler._tick()
    assert scheduler.ticks == 1           # the second did not count as a run
    assert concurrent == []

    release.set()
    worker.join(timeout=10)
    assert scheduler.ticks == 1


# ------------------------------------------- duplication and shutdown
def test_ensure_returns_one_scheduler_per_database(tmp_path):
    """§14: Streamlit reruns call the startup block again and again."""

    from repositories.database import Database

    database = Database(tmp_path / "real.db")
    database.initialize()

    first = ensure_semantic_index_repair_scheduler(
        database, timer_factory=ManualTimer)
    second = ensure_semantic_index_repair_scheduler(
        database, timer_factory=ManualTimer)
    third = ensure_semantic_index_repair_scheduler(
        Database(tmp_path / "real.db"), timer_factory=ManualTimer)

    assert first is second is third
    assert first.started is True
    assert len(ManualTimer.pending()) == 1       # not three timers

    other = Database(tmp_path / "another.db")
    other.initialize()
    fourth = ensure_semantic_index_repair_scheduler(
        other, timer_factory=ManualTimer)
    assert fourth is not first
    assert len(ManualTimer.pending()) == 2


def test_the_app_starts_the_scheduler_at_startup():
    """Wired into the existing lifecycle block, not left unreachable.

    Source inspection rather than importing app.py, which would pull in
    Streamlit and the whole dashboard. What matters is that the single call
    sits in the startup block beside the schedulers already started there.
    """

    source = (pathlib.Path(module.__file__).parents[1]
              / "app.py").read_text(encoding="utf-8")

    assert source.count(
        "ensure_semantic_index_repair_scheduler(database)") == 1
    start = source.index("ensure_auto_sync_scheduler(database)")
    end = source.index("ensure_cdp_chrome_on_start()")
    assert start < source.index(
        "ensure_semantic_index_repair_scheduler(database)") < end


def test_stop_cancels_the_timer_and_stops_rescheduling(tmp_path):
    scheduler, _db, _sibling, _calls = build(
        tmp_path, rows_for((1, "하나")), client=FakeClient(salt=1))
    scheduler.start()
    timer = ManualTimer.pending()[0]

    scheduler.stop()

    assert scheduler.started is False
    assert timer.cancelled is True
    assert ManualTimer.pending() == []

    # a tick that fires after stop does nothing and schedules nothing.
    # Counted as "were any NEW timers created", because un-cancelling this
    # one to re-fire it would otherwise make it look pending again.
    created_before = len(ManualTimer.created)
    timer.cancelled = False
    timer.fire()
    assert scheduler.ticks == 0
    assert len(ManualTimer.created) == created_before


def test_stop_all_stops_every_registered_scheduler(tmp_path):
    from repositories.database import Database

    first = Database(tmp_path / "one.db")
    first.initialize()
    second = Database(tmp_path / "two.db")
    second.initialize()
    one = ensure_semantic_index_repair_scheduler(
        first, timer_factory=ManualTimer)
    two = ensure_semantic_index_repair_scheduler(
        second, timer_factory=ManualTimer)

    stop_all_semantic_index_repair_schedulers()

    assert one.started is False
    assert two.started is False
    assert ManualTimer.pending() == []


def test_the_real_timer_is_a_daemon_so_shutdown_is_not_blocked(tmp_path):
    """A non-daemon timer would keep the interpreter alive for 30 minutes."""

    from repositories.database import Database

    database = Database(tmp_path / "real.db")
    database.initialize()
    scheduler = SemanticIndexRepairScheduler(database)
    try:
        scheduler.start()
        with scheduler._guard:
            assert scheduler._timer is not None
            assert scheduler._timer.daemon is True
    finally:
        scheduler.stop()
    assert scheduler._timer is None


# ----------------------------------------- it reuses the existing machinery
def test_the_tick_calls_sync_semantic_index_with_no_index_path(tmp_path):
    """§4/§5: no new engine, and the index path stays derived."""

    scheduler, database, _sibling, calls = build(
        tmp_path, rows_for((1, "하나")), client=FakeClient(salt=1))
    scheduler.start()
    ManualTimer.pending()[0].fire()

    assert len(calls) == 1
    assert calls[0]["database"] is database
    assert "index_path" not in calls[0]["kwargs"]


def test_the_default_sync_is_the_shared_hook_function(tmp_path):
    from services.learning_index_sync_hook import sync_semantic_index

    from repositories.database import Database

    database = Database(tmp_path / "real.db")
    database.initialize()
    scheduler = SemanticIndexRepairScheduler(database)
    assert scheduler.sync is sync_semantic_index


def test_the_scheduler_writes_the_index_beside_its_own_database(tmp_path):
    """§5: a temp database repairs a temp index, never the packaged one."""

    from repositories.database import Database
    from services.learning_index_sync_hook import index_path_for

    database = Database(tmp_path / "nested" / "real.db")
    database.initialize()
    expected = tmp_path / "nested" / "learning_semantic_index.json"
    assert index_path_for(database) == expected
    assert expected.resolve() != PACKAGED.resolve()

    scheduler = SemanticIndexRepairScheduler(
        database, timer_factory=ManualTimer,
        sync=lambda db, **kwargs: __import__(
            "services.learning_index_sync_hook", fromlist=["x"]
        ).sync_semantic_index(db, client=FakeClient(salt=1),
                              lock=threading.RLock(), **kwargs))
    scheduler.start()
    with database.connection() as connection:
        connection.execute(
            "INSERT INTO learning_examples (source_key, learning_source, "
            "question_original_masked, question_normalized, final_answer, "
            "rating, edit_ratio, quality_score, version, active) "
            "VALUES ('k','APPROVED_EDITED','질분','질분',"
            "'답변',5,0,0,1,1)")
        connection.commit()
    ManualTimer.pending()[0].fire()

    assert expected.exists()
    assert len(LearningSemanticIndex.load(expected).vectors) == 1


def test_the_scheduler_uses_the_shared_write_lock(tmp_path):
    """§6: no new lock -- the hook's module lock is what serialises."""

    import services.learning_index_sync_hook as hook_module

    source = pathlib.Path(
        module.__file__).read_text(encoding="utf-8")
    assert "RLock()" in source           # only for its own _guard
    assert "_INDEX_WRITE_LOCK" not in source
    assert hook_module._INDEX_WRITE_LOCK is not None

    # and a tick that goes through the real sync honours it: with the module
    # lock held, the tick cannot complete its critical section
    database = make_database(tmp_path, rows_for((1, "하나")))
    sibling = tmp_path / "learning_semantic_index.json"
    index_with(rows_for((1, "하나")), fingerprints=True,
               salt=0).save(sibling)
    insert(database, 2, "질분 2", "둘")

    scheduler = SemanticIndexRepairScheduler(
        database, timer_factory=ManualTimer,
        sync=lambda db, **kwargs: hook_module.sync_semantic_index(
            db, client=FakeClient(salt=1), **kwargs))
    scheduler.start()

    acquired = hook_module._INDEX_WRITE_LOCK.acquire(blocking=False)
    assert acquired
    finished = threading.Event()

    def run():
        ManualTimer.pending()[0].fire()
        finished.set()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    reached = finished.wait(timeout=1.0)
    hook_module._INDEX_WRITE_LOCK.release()
    worker.join(timeout=10)

    assert reached is False, (
        "the tick completed while the shared index write lock was held")
    assert set(LearningSemanticIndex.load(sibling).vectors) == {1, 2}
