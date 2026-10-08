"""Keep the semantic index current after a Learning is saved, best effort.

A Learning that has been committed is a fact; its vector is a derived
convenience. So this runs AFTER the database transaction has committed, and
it cannot fail the save: whatever goes wrong -- the embeddings endpoint is
down, the model was changed, the index file could not be replaced -- the row
is already stored and the person who saved it is told it worked. The index
simply stays as it was, and the next sync picks the row up.

That is why nothing here raises. It is also why nothing here is silent: a
swallowed exception with no log would turn "the index is stale" into a
question nobody can answer. Every failure is logged with its type and the
counts, and never with answer text, inquiry text or order numbers.

What it runs is the STEP 2-2 incremental sync over the WHOLE eligible
population -- not the one row that changed. ``learning_semantic_sync.sync``
treats its ``rows`` as the entire population and removes anything the index
holds that the argument omits, so handing it a single row would empty the
index of everything else. Re-reading the population instead costs about
1.09s when nothing has changed -- measured against a filled 61.7MB index,
most of it parsing the file -- against a Learning write rate measured in
events per day, and it means creation, modification and removal are all
handled by one path rather than three.
"""
from __future__ import annotations

import logging
import pathlib
import threading
from typing import Any

from services.learning_index_population import all_row_ids, eligible_rows
from services.learning_semantic_index import DEFAULT_INDEX_PATH
from services import learning_semantic_sync

LOGGER = logging.getLogger(__name__)

# One index writer at a time within this process. The sequence that has to
# stay consistent is load -> classify -> embed -> save: a second sync that
# loaded the file before the first one saved would write back a view that
# predates it, and the first sync's work would be lost. The lock is held
# across the whole sequence for that reason, and it is taken only after the
# database transaction has committed, so it never overlaps one.
#
# Process-local on purpose. A file lock shared with the rebuild CLI is not
# added here; see the module note in the review bundle and STEP 2-4.
_INDEX_WRITE_LOCK = threading.RLock()

# The file name the index lives under, beside the database it is derived from.
INDEX_FILENAME = pathlib.Path(DEFAULT_INDEX_PATH).name


def index_path_for(database: Any) -> pathlib.Path:
    """Where this database's index belongs: next to the database file.

    Derived rather than defaulted, and that is a correctness matter rather
    than tidiness. A notifier that fell back to the packaged default would
    point every database at one shared file -- so a service built on a
    throwaway database would sync ITS population over the real index, see
    every real vector as an orphan and remove them all. That is not
    hypothetical: it emptied the index during this step's own test run, which
    is how the derivation got written.
    """

    path = getattr(database, "path", None)
    if path is None:
        # Not a fallback to the packaged default: that is the very mistake
        # this function exists to prevent, and falling back to it would
        # contradict the sentence above. A caller that cannot say where its
        # database is cannot be given an index path.
        raise ValueError(
            "database.path is required to derive the semantic index path")
    return pathlib.Path(path).parent / INDEX_FILENAME


def _counts(result: Any) -> dict[str, Any]:
    """The small summary an operator would ask for. No row content."""

    plan = result.plan
    return {
        "changed": result.changed,
        "saved": result.saved,
        "embedded": result.embedded_count,
        "removed": result.removed_count,
        "fresh": plan.fresh_count,
        "stale": plan.stale_count,
        "missing": plan.missing_count,
    }


def sync_semantic_index(database: Any, *,
                        client: Any = None,
                        index_path: pathlib.Path | str | None = None,
                        lock: Any = None) -> Any:
    """Bring the index in line with the corpus. Never raises.

    ``index_path`` defaults to the index beside ``database`` rather than to
    the packaged path. That is a safety default, not a convenience one: a
    caller that omitted it while the default pointed at the packaged file
    would sync ITS database's population over the real index and remove
    every vector the real corpus owns. That happened once during this work.
    A periodic repair calling ``sync_semantic_index(database)`` therefore
    gets the right file without having to know this.

    Returns the ``SyncResult`` on success and ``None`` on failure, so a
    caller that wants to report the outcome can, and a caller that does not
    care can ignore it.
    """

    guard = _INDEX_WRITE_LOCK if lock is None else lock
    try:
        resolved = (index_path_for(database) if index_path is None
                    else pathlib.Path(index_path))
        with guard:
            with database.connection() as connection:
                rows = eligible_rows(connection)
                known = all_row_ids(connection)
            result = learning_semantic_sync.sync(
                rows, index_path=resolved, client=client,
                known_row_ids=known)
    except Exception as error:  # noqa: BLE001 - best effort by design
        # Logged rather than raised: the Learning is already committed and
        # must not be reported as failed. Type and message only -- no answer
        # text, no inquiry text, no identifiers.
        LOGGER.warning(
            "semantic index sync failed after a Learning write: %s: %s",
            type(error).__name__, error,
        )
        return None
    if result.full_rebuild_required:
        LOGGER.error(
            "semantic index sync skipped, full rebuild required: %s",
            result.full_rebuild_reason,
        )
        return result
    if result.saved:
        LOGGER.info("semantic index synced: %s", _counts(result))
    return result


class _CoalescingWorker:
    """One sync at a time; everything that arrives during it becomes one more.

    The approval UI used to wait for the whole sequence. Measured on a filled
    66.5MB index: the database commit is 12-19ms and the sync that followed it
    inside the same request was 3.15-3.21s -- index load 1.16s, embedding
    0.45-0.53s, index save 1.42s. The row was already committed by then, so the
    wait bought nothing the next retrieval would not have got anyway.

    Why one slot and not a queue: ``learning_semantic_sync.sync`` takes the
    WHOLE eligible population, so one pass already covers every write that
    happened before it started. Queueing N passes for N approvals would repeat
    load -> embed -> save N times to reach the state one pass reaches. So a
    request that arrives while a pass is running does not enqueue work, it sets
    a flag; when the pass ends the flag causes exactly one more pass, however
    many requests set it. Ten approvals in a row cost two passes, not ten.

    The second pass is not an optimisation to be skipped: a row written after
    the running pass read the population would otherwise wait for the periodic
    repair.

    The thread is a daemon and nothing joins it on the request path. Failure is
    contained twice over -- ``sync_semantic_index`` does not raise, and the loop
    catches anything that reaches it anyway -- because the approval has already
    returned successfully and cannot be failed retroactively.
    """

    def __init__(self, spawn: Any = None) -> None:
        self._guard = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        self._spawn = spawn if spawn is not None else _spawn_daemon
        self._run: Any = None
        self._running = False
        self._pending = False
        # Observability, and what the tests assert on: a pass is one
        # load/embed/save sequence, a coalesced request is one that folded into
        # a pass instead of starting its own.
        self.requests = 0
        self.passes = 0
        self.coalesced = 0
        self.failures = 0

    @property
    def busy(self) -> bool:
        return not self._idle.is_set()

    def request(self, run: Any) -> bool:
        """Ask for a sync. Returns whether this call started a new pass.

        ``run`` is replaced on every request rather than captured once, so a
        pass always uses the most recent caller's database, client and path.
        That matters because the population sync is idempotent and whole --
        running the latest parameters is never less correct than running a
        stale closure, and a notifier rebuilt by a Streamlit rerun is the
        normal case rather than the exception.
        """

        with self._guard:
            self.requests += 1
            self._run = run
            if self._running:
                self._pending = True
                self.coalesced += 1
                return False
            self._running = True
            self._pending = False
            self._idle.clear()
        try:
            self._spawn(self._loop)
        except BaseException:
            # The slot was claimed above and nothing is going to run, so give
            # it back. Without this a thread the interpreter refused to start
            # -- ``RuntimeError: can't start new thread`` under resource
            # pressure -- would leave ``_running`` set for the life of the
            # process, and every later approval would coalesce into a pass that
            # never comes. Silent, and only visible as "the index stopped
            # updating". The periodic repair would still run, because it calls
            # ``sync_semantic_index`` directly rather than through a worker.
            with self._guard:
                self._running = False
                self._pending = False
                self._idle.set()
            raise
        return True

    def _loop(self) -> None:
        try:
            while True:
                with self._guard:
                    run = self._run
                    self.passes += 1
                try:
                    if run is not None:
                        run()
                except Exception as error:  # noqa: BLE001 - already committed
                    self.failures += 1
                    LOGGER.warning(
                        "background semantic index sync failed: %s: %s",
                        type(error).__name__, error,
                    )
                with self._guard:
                    if not self._pending:
                        self._running = False
                        self._idle.set()
                        return
                    self._pending = False
        except BaseException:
            # A thread that died holding ``_running`` would refuse every later
            # request for the life of the process. Releasing it here means the
            # next approval starts a fresh pass instead.
            with self._guard:
                self._running = False
                self._pending = False
                self._idle.set()
            raise

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Block until no pass is running. For tests and shutdown, not the UI."""

        return self._idle.wait(timeout)


def _spawn_daemon(target: Any) -> None:
    thread = threading.Thread(
        target=target, name="semantic-index-sync", daemon=True)
    thread.start()


# One worker per index file, not one per notifier.
#
# ``LearningService`` is rebuilt on every Streamlit rerun, so a worker owned by
# a notifier instance would be a fresh worker for each approval and nothing
# would ever coalesce. Keyed by the resolved index path because that is what a
# pass actually contends for, and it is the same key the write lock protects.
_WORKERS: dict[str, _CoalescingWorker] = {}
_WORKERS_GUARD = threading.Lock()


def worker_for(index_path: pathlib.Path | str) -> _CoalescingWorker:
    key = str(pathlib.Path(index_path).resolve())
    with _WORKERS_GUARD:
        worker = _WORKERS.get(key)
        if worker is None:
            worker = _CoalescingWorker()
            _WORKERS[key] = worker
        return worker


def wait_for_semantic_index_workers(timeout: float = 30.0) -> bool:
    """Drain every worker. Returns whether all of them went idle in time."""

    with _WORKERS_GUARD:
        workers = list(_WORKERS.values())
    return all(worker.wait_idle(timeout) for worker in workers)


def reset_semantic_index_workers(timeout: float = 30.0) -> bool:
    """Drain, then forget. Keeps one test's background pass out of the next."""

    drained = wait_for_semantic_index_workers(timeout)
    with _WORKERS_GUARD:
        _WORKERS.clear()
    return drained


class SemanticIndexNotifier:
    """The post-commit callback a repository invokes, with an off switch.

    Off switch, because the same repository methods are used by the backfill
    importers, which call them in a loop over hundreds of rows. A
    full-population sync per row there would cost hundreds of passes and
    hundreds of embedding requests to reach the state one pass reaches at the
    end. ``suppressed()`` is how a batch says "once, afterwards" -- or, as the
    importers do, "not at all; the CLI will rebuild".

    The sync itself runs on a background worker, so the caller returns as soon
    as the database commit is durable. ``sync_semantic_index`` is unchanged and
    still synchronous for everyone who calls it directly -- the periodic repair
    does, and it stays that way.
    """

    def __init__(self, database: Any, *,
                 index_path: pathlib.Path | str | None = None,
                 client_factory: Any = None,
                 worker: Any = None) -> None:
        self.database = database
        self.index_path = (index_path_for(database) if index_path is None
                           else pathlib.Path(index_path))
        self.client_factory = client_factory
        # Shared per index path by default. An explicit worker is for tests
        # that need to drive the pass themselves; production passes nothing.
        self.worker = (worker if worker is not None
                       else worker_for(self.index_path))
        self._depth = 0
        self.calls = 0

    @property
    def enabled(self) -> bool:
        return self._depth == 0

    def suppressed(self) -> "_Suppression":
        return _Suppression(self)

    def __call__(self) -> Any:
        """Hand the sync to the worker and return. Never blocks on the index.

        Returns None rather than a ``SyncResult``: the result does not exist
        yet when this returns, and inventing one would be the one way this
        change could lie. A caller that needs the outcome calls
        ``sync_semantic_index`` directly, as the periodic repair does.

        The client is built here, on the caller's thread, so a misconfigured
        factory still surfaces as a logged failure of the save's own hook
        rather than inside a thread nobody is reading.
        """

        if not self.enabled:
            return None
        self.calls += 1
        client = None if self.client_factory is None else self.client_factory()
        database, index_path = self.database, self.index_path

        def run() -> None:
            sync_semantic_index(database, client=client, index_path=index_path)

        self.worker.request(run)
        return None

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Block until the background sync has drained. Not for the UI path."""

        return self.worker.wait_idle(timeout)


class _Suppression:
    def __init__(self, notifier: SemanticIndexNotifier) -> None:
        self.notifier = notifier

    def __enter__(self) -> SemanticIndexNotifier:
        self.notifier._depth += 1
        return self.notifier

    def __exit__(self, *exc_info: Any) -> None:
        self.notifier._depth -= 1
