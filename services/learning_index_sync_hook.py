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


class SemanticIndexNotifier:
    """The post-commit callback a repository invokes, with an off switch.

    Off switch, because the same repository methods are used by the backfill
    importers, which call them in a loop over hundreds of rows. A
    full-population sync per row there would cost hundreds of passes and
    hundreds of embedding requests to reach the state one pass reaches at the
    end. ``suppressed()`` is how a batch says "once, afterwards" -- or, as the
    importers do, "not at all; the CLI will rebuild".
    """

    def __init__(self, database: Any, *,
                 index_path: pathlib.Path | str | None = None,
                 client_factory: Any = None) -> None:
        self.database = database
        self.index_path = (index_path_for(database) if index_path is None
                           else pathlib.Path(index_path))
        self.client_factory = client_factory
        self._depth = 0
        self.calls = 0

    @property
    def enabled(self) -> bool:
        return self._depth == 0

    def suppressed(self) -> "_Suppression":
        return _Suppression(self)

    def __call__(self) -> Any:
        if not self.enabled:
            return None
        self.calls += 1
        client = None if self.client_factory is None else self.client_factory()
        return sync_semantic_index(self.database, client=client,
                                   index_path=self.index_path)


class _Suppression:
    def __init__(self, notifier: SemanticIndexNotifier) -> None:
        self.notifier = notifier

    def __enter__(self) -> SemanticIndexNotifier:
        self.notifier._depth += 1
        return self.notifier

    def __exit__(self, *exc_info: Any) -> None:
        self.notifier._depth -= 1
