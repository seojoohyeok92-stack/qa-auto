"""Periodically re-run the semantic index sync, as a backstop.

This is NOT how a new Learning reaches the index. That happens the moment it
is saved, through the post-commit hook. What this exists for is everything
the hook cannot promise:

* an embedding call that failed once, or an index replace that lost a race
* the ``active=0`` paths -- revoke, deactivate, feedback revocation -- which
  make a vector removable without notifying anything
* the batch importers, which suppress the per-row sync on purpose
* a writer added later that nobody remembered to wire
* any other drift between ``learning_examples`` and the index file

So it runs rarely and does almost nothing. Thirty minutes, because new
Learning is already current within milliseconds of being saved and this only
has to shorten the window on a failure; and because a no-op pass costs about
1.09s against a filled index, most of which is parsing the file -- cheap
every half hour, wasteful every minute.

It adds no engine. The tick calls ``sync_semantic_index(database)`` and that
is the whole of it: the population query, the fingerprints, the
classification, the embedding and the removal all stay in one place, and this
module cannot drift from them because it does not restate them. It also
inherits that function's index-path derivation, so it writes the index beside
the database it read -- never a packaged default.

Shaped after ``naver_auto_sync_scheduler``: a single ``Timer`` per database,
held in a process-local registry keyed by the database path, stopped at exit.
What it deliberately does not have is that scheduler's SQLite leader lease --
there is no cross-process coordination here, and the manual rebuild CLI is
not to be run while the dashboard is serving.
"""
from __future__ import annotations

import atexit
import logging
from pathlib import Path
from threading import RLock, Timer
from typing import Any, Callable

from repositories.database import Database
from services.learning_index_sync_hook import sync_semantic_index

LOGGER = logging.getLogger(__name__)

# Half an hour. A constant rather than an environment variable: there is no
# operational decision to make here -- the hook keeps the index current, and
# this only bounds how long a failure can go unrepaired.
REPAIR_INTERVAL_MINUTES = 30


class SemanticIndexRepairScheduler:
    """One timer, one database, one call per tick."""

    def __init__(self, database: Database, *,
                 timer_factory: Callable[..., Any] = Timer,
                 interval_minutes: int = REPAIR_INTERVAL_MINUTES,
                 sync: Callable[..., Any] = sync_semantic_index) -> None:
        self.database = database
        self.timer_factory = timer_factory
        self.interval_minutes = max(1, int(interval_minutes))
        self.sync = sync
        self._guard = RLock()
        self._timer: Any = None
        self._started = False
        self._running = False
        self.ticks = 0
        self.repairs = 0

    @property
    def started(self) -> bool:
        with self._guard:
            return self._started

    @property
    def interval_seconds(self) -> float:
        return float(self.interval_minutes) * 60.0

    def _schedule(self, delay_seconds: float) -> None:
        with self._guard:
            if not self._started:
                return
            if self._timer is not None:
                self._timer.cancel()
            timer = self.timer_factory(max(0.05, float(delay_seconds)),
                                       self._tick)
            timer.daemon = True
            self._timer = timer
            timer.start()

    def start(self) -> bool:
        """Begin ticking, with the FIRST tick one whole interval away.

        Not immediately, and that matters once rather than in general: a
        server whose index predates fingerprints would have its first repair
        try to embed the entire corpus the moment the dashboard came up. The
        baseline belongs to a deliberate CLI run before the restart, not to
        startup.
        """

        with self._guard:
            if self._started:
                return True
            self._started = True
        self._schedule(self.interval_seconds)
        return True

    def stop(self) -> None:
        with self._guard:
            self._started = False
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()

    def _tick(self) -> None:
        """Run one repair, then schedule the next. Never raises.

        The next tick is scheduled only after this one returns, so passes
        cannot stack behind a slow embedding call; ``_running`` is the belt
        for anyone who calls this directly.
        """

        try:
            with self._guard:
                if not self._started:
                    return
                if self._running:
                    LOGGER.debug(
                        "semantic index repair already running; tick skipped")
                    return
                self._running = True
                self.ticks += 1
            try:
                # No index_path: the sync derives it from the database, which
                # is the contract that keeps a dashboard off another
                # database's index.
                result = self.sync(self.database)
                if result is None:
                    # The sync has already logged why, with no row content.
                    LOGGER.debug("semantic index repair tick did not complete")
                elif getattr(result, "saved", False):
                    self.repairs += 1
                    LOGGER.info(
                        "semantic index repaired: embedded=%s removed=%s",
                        result.embedded_count, result.removed_count)
                else:
                    LOGGER.debug("semantic index already current")
            except Exception:  # noqa: BLE001 - a tick must not kill the timer
                # sync_semantic_index does not raise, so reaching here means
                # something unforeseen did. The dashboard keeps running and
                # the next tick tries again.
                LOGGER.warning("semantic index repair tick failed",
                               exc_info=True)
            finally:
                with self._guard:
                    self._running = False
        finally:
            self._schedule(self.interval_seconds)


_REGISTRY_LOCK = RLock()
_SCHEDULERS: dict[str, SemanticIndexRepairScheduler] = {}


def ensure_semantic_index_repair_scheduler(
    database: Database, *,
    timer_factory: Callable[..., Any] = Timer,
) -> SemanticIndexRepairScheduler:
    """One scheduler per database path, however often this is called.

    Streamlit re-executes the startup block on every rerun, so this has to be
    idempotent or a long session would accumulate timers. Keyed by the
    resolved database path, the same way the Naver sync scheduler is.
    """

    key = str(Path(database.path).resolve())
    with _REGISTRY_LOCK:
        scheduler = _SCHEDULERS.get(key)
        if scheduler is None:
            scheduler = SemanticIndexRepairScheduler(
                database, timer_factory=timer_factory)
            _SCHEDULERS[key] = scheduler
    scheduler.start()
    return scheduler


def stop_all_semantic_index_repair_schedulers() -> None:
    with _REGISTRY_LOCK:
        schedulers = list(_SCHEDULERS.values())
    for scheduler in schedulers:
        scheduler.stop()


atexit.register(stop_all_semantic_index_repair_schedulers)
