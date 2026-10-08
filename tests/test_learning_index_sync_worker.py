"""The approval must not wait for the index, and N approvals must cost one pass.

Measured on a filled 66.5MB / 1,939-vector index before this change: the
approval call returned in 3.16-3.23s, of which the database commit was 12-19ms
and the post-commit sync 3.15-3.21s (index load 1.16s, embedding 0.45-0.53s,
index save 1.42s). The row was committed before any of that began, so the UI
was waiting for work whose only consumer is the next retrieval.

What the worker guarantees, and what each test here pins:

* the request path never touches the write lock, the embedding endpoint or the
  index file, so it cannot be slow for any of those reasons;
* a pass covers the whole eligible population, so requests that arrive during
  one are folded into a single follow-up rather than queued individually;
* that follow-up happens exactly once however many requests arrived, and it
  does happen -- a row written after the running pass read the population must
  not have to wait for the periodic repair;
* a failure inside the worker is logged and cannot reach the approval, which
  has already returned successfully;
* the pass still serialises against the periodic repair on the same lock.

The worker is driven with an injected ``spawn`` wherever the test needs to
control when a pass runs. ``spawn`` only decides *where* the loop body runs;
the coalescing logic under test is the shipped one in either case.
"""

from __future__ import annotations

import pathlib
import threading
import time

import pytest

from repositories.database import Database
from services.learning_index_sync_hook import (
    _INDEX_WRITE_LOCK,
    SemanticIndexNotifier,
    _CoalescingWorker,
    reset_semantic_index_workers,
    worker_for,
    wait_for_semantic_index_workers,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_semantic_index_workers(timeout=30.0)
    yield
    reset_semantic_index_workers(timeout=30.0)


def make_database(tmp_path) -> Database:
    database = Database(tmp_path / "worker.db")
    database.initialize()
    return database


def example(key: str) -> dict:
    """The column set ``upsert`` writes explicitly, with its NOT NULL columns."""

    return {
        "source_key": key,
        "inquiry_id": None,
        "answer_draft_id": None,
        "approval_history_id": None,
        "learning_source": "APPROVED_UNEDITED",
        "question_original_masked": "리모컨이 포함되나요?",
        "question_normalized": "리모컨이 포함되나요",
        "store_code": "OJE_PLUS",
        "inquiry_type": "PRODUCT_GENERAL",
        "intent": "PACKAGE_CONTENTS",
        "generation_mode": "GPT",
        "template_id": None,
        "processing_route": "GPT",
        "validator_result": "PASS",
        "seller_answer": "",
        "gpt_draft": "리모컨은 기본 포함입니다.",
        "edited_answer": "리모컨은 기본 포함입니다.",
        "final_answer": "리모컨은 기본 포함입니다.",
        "posted": 1,
        "posted_at": None,
        "auto_posted": 0,
        "rating": 5,
        "edit_ratio": 0.0,
        "quality_score": 0.9,
        "style_only": 0,
        "version": 1,
        "style_features_json": {},
        "metadata_json": {"human_verified": True},
        "active": 1,
        "usage_count": 0,
        "last_used_at": None,
        "validity_type": "PERMANENT",
    }


# --- 1. the approval does not wait for the sync ---------------------------

def test_the_notifier_returns_before_the_sync_runs(tmp_path):
    """The call returns while the pass is still blocked inside ``run``."""

    released = threading.Event()
    started = threading.Event()

    def blocking_sync(*_a, **_k):
        started.set()
        released.wait(10)

    worker = _CoalescingWorker()
    notifier = SemanticIndexNotifier(
        make_database(tmp_path), index_path=tmp_path / "index.json",
        worker=worker)
    notifier.worker._spawn = lambda target: threading.Thread(
        target=target, daemon=True).start()

    import services.learning_index_sync_hook as hook
    original = hook.sync_semantic_index
    hook.sync_semantic_index = blocking_sync
    try:
        elapsed = time.perf_counter()
        assert notifier() is None
        elapsed = time.perf_counter() - elapsed
        assert started.wait(5), "the worker never started the pass"
        # The pass is provably still inside ``run`` at this point, and the
        # call already returned -- so the return did not wait for it.
        assert worker.busy is True
        assert elapsed < 1.0, elapsed
    finally:
        released.set()
        worker.wait_idle(10)
        hook.sync_semantic_index = original


def test_the_request_path_does_not_take_the_write_lock(tmp_path):
    """Held from another thread, the lock must not delay the notifier.

    This is the property the UI actually depends on: a periodic repair holding
    the lock used to make the approval wait for it.
    """

    notifier = SemanticIndexNotifier(
        make_database(tmp_path), index_path=tmp_path / "index.json",
        worker=_CoalescingWorker(spawn=lambda target: None))
    holding = threading.Event()
    release = threading.Event()

    def holder():
        with _INDEX_WRITE_LOCK:
            holding.set()
            release.wait(10)

    threading.Thread(target=holder, daemon=True).start()
    assert holding.wait(5)
    try:
        started = time.perf_counter()
        notifier()
        assert (time.perf_counter() - started) < 0.5
    finally:
        release.set()


# --- 2. and 3. one slot, and exactly one follow-up ------------------------

def test_requests_during_a_pass_collapse_into_one_more(tmp_path):
    """Ten approvals during a running pass cost two passes, not ten."""

    release = threading.Event()
    in_pass = threading.Event()
    runs: list[int] = []

    def run():
        runs.append(1)
        in_pass.set()
        release.wait(10)

    worker = _CoalescingWorker()
    assert worker.request(run) is True
    assert in_pass.wait(5)

    quick = threading.Event()
    quick.set()

    def fast_run():
        runs.append(1)

    for _ in range(10):
        assert worker.request(fast_run) is False
    assert worker.coalesced == 10
    assert worker.requests == 11

    release.set()
    assert worker.wait_idle(10)
    # One pass for the first request, exactly one for all ten that followed.
    assert worker.passes == 2
    assert len(runs) == 2


def test_the_follow_up_pass_happens_exactly_once(tmp_path):
    """Not zero -- a row written mid-pass must not wait for the repair."""

    first = threading.Event()
    release = threading.Event()
    passes: list[str] = []

    def slow():
        passes.append("slow")
        first.set()
        release.wait(10)

    def quick():
        passes.append("quick")

    worker = _CoalescingWorker()
    worker.request(slow)
    assert first.wait(5)
    worker.request(quick)
    release.set()
    assert worker.wait_idle(10)
    assert passes == ["slow", "quick"]
    assert worker.passes == 2
    assert worker.coalesced == 1


def test_a_request_after_the_pass_ended_starts_a_new_one():
    """Coalescing applies to overlap only, never to a later save."""

    worker = _CoalescingWorker()
    worker.request(lambda: None)
    assert worker.wait_idle(10)
    assert worker.passes == 1
    assert worker.request(lambda: None) is True
    assert worker.wait_idle(10)
    assert worker.passes == 2
    assert worker.coalesced == 0


def test_the_pass_uses_the_most_recent_request_parameters():
    """A Streamlit rerun rebuilds the notifier; the newest one must be used."""

    release = threading.Event()
    first = threading.Event()
    seen: list[str] = []

    def one():
        seen.append("one")
        first.set()
        release.wait(10)

    worker = _CoalescingWorker()
    worker.request(one)
    assert first.wait(5)
    worker.request(lambda: seen.append("two"))
    worker.request(lambda: seen.append("three"))
    release.set()
    assert worker.wait_idle(10)
    assert seen == ["one", "three"]


# --- 4. a worker failure never reaches the approval -----------------------

def test_a_failing_pass_does_not_fail_the_save(tmp_path, caplog):
    database = make_database(tmp_path)
    notifier = SemanticIndexNotifier(
        database, index_path=tmp_path / "index.json",
        worker=_CoalescingWorker())

    import services.learning_index_sync_hook as hook
    original = hook.sync_semantic_index

    def exploding(*_a, **_k):
        raise RuntimeError("embedding endpoint is down")

    hook.sync_semantic_index = exploding
    try:
        from repositories.learning_repository import LearningRepository

        repository = LearningRepository(database)
        repository.on_committed = notifier
        saved = repository.upsert(example("failing-pass"))
        assert saved["id"], "the save must succeed regardless"
        assert notifier.wait_idle(10)
    finally:
        hook.sync_semantic_index = original

    assert notifier.worker.failures == 1
    assert notifier.worker.passes == 1


def test_a_failing_pass_leaves_the_worker_usable():
    """A dead slot would refuse every later approval in the process."""

    worker = _CoalescingWorker()
    worker.request(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert worker.wait_idle(10)
    assert worker.failures == 1
    assert worker.busy is False
    done = threading.Event()
    assert worker.request(done.set) is True
    assert worker.wait_idle(10)
    assert done.is_set()
    assert worker.passes == 2


def test_a_thread_that_cannot_start_does_not_wedge_the_slot(tmp_path):
    """``can't start new thread`` must not stop every later approval.

    ``request`` claims the slot before spawning. If the spawn fails and the
    slot is not given back, ``_running`` stays set for the life of the process
    and every subsequent approval is coalesced into a pass that never runs --
    silent, and visible only as "the index stopped updating". The periodic
    repair would carry on, because it calls ``sync_semantic_index`` directly.
    """

    attempts = {"n": 0}

    def refuse_once(target):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("can't start new thread")
        target()

    worker = _CoalescingWorker(spawn=refuse_once)
    with pytest.raises(RuntimeError):
        worker.request(lambda: None)
    assert worker.busy is False, "the slot was never given back"

    done = threading.Event()
    assert worker.request(done.set) is True
    assert done.is_set()


def test_a_failed_spawn_does_not_fail_the_save(tmp_path):
    """The approval is already committed; a refused thread cannot undo it."""

    from repositories.learning_repository import LearningRepository

    database = make_database(tmp_path)

    def refuse(target):
        raise RuntimeError("can't start new thread")

    notifier = SemanticIndexNotifier(
        database, index_path=tmp_path / "index.json",
        worker=_CoalescingWorker(spawn=refuse))
    repository = LearningRepository(database)
    repository.on_committed = notifier

    saved = repository.upsert(example("refused-thread"))
    assert saved["id"], "the save must survive a worker that cannot start"
    assert notifier.worker.busy is False


def test_the_slot_is_released_even_by_an_exception_nothing_catches():
    """``except Exception`` is not enough, and this is the test that says so.

    The loop's inner handler catches ``Exception``, so a ``RuntimeError`` never
    reaches the outer recovery and the test above passes with that recovery
    deleted -- mutation testing found exactly that. A ``BaseException`` is what
    the outer block exists for: without it the loop exits with ``_running``
    still true and ``_idle`` still clear, and every later approval in the
    process is coalesced into a pass that will never run.

    The loop is run on this thread, through the ``spawn`` seam, so the
    ``BaseException`` is observed here instead of being reported by pytest as
    an unhandled exception in a thread nobody owns.
    """

    worker = _CoalescingWorker(spawn=lambda target: target())

    def abandon():
        raise KeyboardInterrupt("thread torn down mid-pass")

    with pytest.raises(KeyboardInterrupt):
        worker.request(abandon)

    assert worker.busy is False, "the slot was never released"
    # And the next approval really does get a pass of its own.
    done = threading.Event()
    assert worker.request(done.set) is True
    assert done.is_set()
    assert worker.wait_idle(10)


# --- 5. still serialised against the periodic repair ----------------------

def test_the_pass_still_serialises_on_the_shared_write_lock(tmp_path):
    """The worker moved the wait off the UI thread; it did not remove it."""

    database = make_database(tmp_path)
    notifier = SemanticIndexNotifier(
        database, index_path=tmp_path / "index.json",
        worker=_CoalescingWorker())
    entered = threading.Event()
    release = threading.Event()

    def holder():
        with _INDEX_WRITE_LOCK:
            entered.set()
            release.wait(5)

    threading.Thread(target=holder, daemon=True).start()
    assert entered.wait(5)
    notifier()
    # With the lock held elsewhere the pass cannot finish its critical section.
    assert notifier.wait_idle(1.0) is False
    release.set()
    assert notifier.wait_idle(15) is True


def test_the_repair_scheduler_calls_the_synchronous_sync_unchanged():
    """``sync_semantic_index`` is still the repair's own, still blocking."""

    import inspect

    from services import learning_index_repair_scheduler as scheduler
    from services.learning_index_sync_hook import sync_semantic_index

    default = inspect.signature(
        scheduler.SemanticIndexRepairScheduler.__init__
    ).parameters["sync"].default
    assert default is sync_semantic_index
    source = pathlib.Path(scheduler.__file__).read_text(encoding="utf-8")
    assert "worker" not in source
    assert "_CoalescingWorker" not in source


# --- 6. the row really does reach the index -------------------------------

def test_the_saved_row_reaches_the_index_in_the_background(tmp_path):
    """End to end with a fake embedding client: real sync, real index file."""

    from repositories.learning_repository import LearningRepository
    from services.learning_semantic_index import LearningSemanticIndex

    index_path = tmp_path / "index.json"
    database = make_database(tmp_path)

    class FakeClient:
        model = "text-embedding-3-small"

        def __init__(self):
            self.calls = 0

        def embed(self, texts):
            texts = list(texts)
            self.calls += 1
            return [[0.1] * 1536 for _ in texts]

    client = FakeClient()
    notifier = SemanticIndexNotifier(
        database, index_path=index_path, client_factory=lambda: client,
        worker=_CoalescingWorker())
    repository = LearningRepository(database)
    repository.on_committed = notifier

    saved = repository.upsert(example("reaches-index"))
    assert notifier.wait_idle(20), "the background pass did not finish"

    assert index_path.exists(), "the index was never written"
    index = LearningSemanticIndex.load(index_path)
    assert int(saved["id"]) in index.vectors
    assert int(saved["id"]) in index.fingerprints
    assert client.calls == 1


def test_consecutive_saves_cost_one_embedding_pass_not_three(tmp_path):
    """The point of coalescing, measured where it matters: API requests.

    The three rows are written while the first pass is held, so all three are
    in the population by the time the follow-up pass reads it -- one request
    carrying three texts instead of three passes of one.
    """

    from repositories.learning_repository import LearningRepository
    from services.learning_semantic_index import LearningSemanticIndex

    index_path = tmp_path / "index.json"
    database = make_database(tmp_path)
    gate = threading.Event()
    first = threading.Event()

    class GatedClient:
        model = "text-embedding-3-small"

        def __init__(self):
            self.batches: list[int] = []

        def embed(self, texts):
            texts = list(texts)
            self.batches.append(len(texts))
            if len(self.batches) == 1:
                first.set()
                gate.wait(10)
            return [[0.1] * 1536 for _ in texts]

    client = GatedClient()
    notifier = SemanticIndexNotifier(
        database, index_path=index_path, client_factory=lambda: client,
        worker=_CoalescingWorker())
    repository = LearningRepository(database)
    repository.on_committed = notifier

    ids = [int(repository.upsert(example("row-0"))["id"])]
    assert first.wait(10), "the first pass never reached the embedding call"
    for n in (1, 2, 3):
        ids.append(int(repository.upsert(example("row-%d" % n))["id"]))
    gate.set()
    assert notifier.wait_idle(30)

    index = LearningSemanticIndex.load(index_path)
    assert all(identifier in index.vectors for identifier in ids)
    # Two passes total, and the second one embedded the three rows together.
    assert notifier.worker.passes == 2
    assert client.batches == [1, 3], client.batches


# --- 7. the existing hook contract is unchanged ---------------------------

def test_a_suppressed_notifier_still_requests_nothing(tmp_path):
    worker = _CoalescingWorker(spawn=lambda target: None)
    notifier = SemanticIndexNotifier(
        make_database(tmp_path), index_path=tmp_path / "index.json",
        worker=worker)
    with notifier.suppressed():
        assert notifier() is None
    assert worker.requests == 0
    assert notifier.calls == 0


def test_the_index_path_is_still_derived_from_the_database(tmp_path):
    notifier = SemanticIndexNotifier(make_database(tmp_path))
    assert notifier.index_path.parent.resolve() == tmp_path.resolve()


def test_one_worker_per_index_path(tmp_path):
    """A Streamlit rerun must not get a worker of its own, or nothing coalesces."""

    first = SemanticIndexNotifier(
        make_database(tmp_path), index_path=tmp_path / "shared.json")
    second = SemanticIndexNotifier(
        make_database(tmp_path), index_path=tmp_path / "shared.json")
    other = SemanticIndexNotifier(
        make_database(tmp_path), index_path=tmp_path / "other.json")
    assert first.worker is second.worker
    assert first.worker is not other.worker
    assert worker_for(tmp_path / "shared.json") is first.worker


def test_the_service_shares_the_registry_worker(tmp_path):
    from services.learning_service import LearningService

    database = make_database(tmp_path)
    service = LearningService(database)
    assert service.index_notifier.worker is worker_for(
        service.index_notifier.index_path)


def test_draining_reports_quiescence():
    worker = worker_for("registry-drain.json")
    release = threading.Event()
    worker.request(lambda: release.wait(10))
    assert wait_for_semantic_index_workers(timeout=0.3) is False
    release.set()
    assert wait_for_semantic_index_workers(timeout=15) is True
