"""Saving a Learning must keep the semantic index current -- and must not
depend on it.

Two properties, pulling in opposite directions. A Learning that is stored but
not indexed cannot be found by meaning, which is how 914 approved rows became
invisible; so a save has to trigger a sync. But the row is the fact and the
vector is derived, so the sync must never be able to fail the save. Every
failure path here therefore asserts BOTH that the Learning is still stored and
that the index is untouched.

No test reaches the network: the embedding client is a fake.
"""
from __future__ import annotations

import json
import logging
import pathlib
import sqlite3
import threading

import pytest

from repositories.learning_repository import LearningRepository
from services import learning_index_population, learning_semantic_sync
from services.learning_index_sync_hook import (
    SemanticIndexNotifier, sync_semantic_index,
)
from services.learning_semantic_index import (
    EMBEDDING_MODEL, LearningSemanticIndex, fingerprint_for,
)
from test_semantic_index_incremental_sync import (
    FakeClient, index_with, rows_for,
)

SCHEMA = """
CREATE TABLE learning_examples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_original_masked TEXT,
    final_answer TEXT,
    active INTEGER DEFAULT 1,
    validity_active INTEGER DEFAULT 1,
    updated_at TEXT
)
"""


class FakeDatabase:
    """Just enough Database for the hook: a connection context manager."""

    def __init__(self, path):
        self.path = str(path)
        self.connections = 0
        self.open_connections = 0

    def _connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    class _Ctx:
        def __init__(self, owner):
            self.owner = owner

        def __enter__(self):
            self.owner.connections += 1
            self.owner.open_connections += 1
            self.connection = self.owner._connect()
            return self.connection

        def __exit__(self, *exc):
            self.connection.close()
            self.owner.open_connections -= 1

    def connection(self):
        return FakeDatabase._Ctx(self)


def make_database(tmp_path, rows=()):
    path = tmp_path / "learning.db"
    connection = sqlite3.connect(path)
    connection.execute(SCHEMA)
    connection.executemany(
        "INSERT INTO learning_examples "
        "(id, question_original_masked, final_answer, active) VALUES (?,?,?,1)",
        [(int(r["id"]), r["question"], r["answer"]) for r in rows])
    connection.commit()
    connection.close()
    return FakeDatabase(path)


def example(source_key, answer="답변입니다"):
    """The minimum the real schema accepts: source_key, source, two texts."""

    return {
        "source_key": source_key,
        # left null: inquiry_id carries a foreign key and these tests are
        # about the hook, not about inquiry wiring
        "inquiry_id": None,
        "learning_source": "APPROVED_EDITED",
        "question_original_masked": "질분입니까",
        "question_normalized": "질분입니까",
        "final_answer": answer,
        # the upsert passes every column explicitly, so a NOT NULL column with
        # a schema default still needs a value here
        "rating": 5,
        "edit_ratio": 0.0,
        "quality_score": 0.0,
        "version": 1,
        "active": 1,
    }


def insert(database, identifier, question, answer, *, active=1):
    with database.connection() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO learning_examples "
            "(id, question_original_masked, final_answer, active) "
            "VALUES (?,?,?,?)", (identifier, question, answer, active))
        connection.commit()


# ------------------------------------------------- new Learning (§19)
def test_a_newly_saved_learning_is_embedded_and_fingerprinted(tmp_path):
    existing = rows_for((1, "기존 답변입니다"))
    database = make_database(tmp_path, existing)
    target = tmp_path / "index.json"
    index_with(existing, fingerprints=True, salt=0).save(target)

    insert(database, 2, "질분 2 입니까",
           "새로 생긴 답변입니다")
    client = FakeClient(salt=1)
    result = sync_semantic_index(database, client=client, index_path=target,
                                 lock=threading.RLock())

    assert result is not None
    assert result.plan.classification[2] == learning_semantic_sync.MISSING
    assert result.embedded_count == 1
    assert result.saved is True
    reloaded = LearningSemanticIndex.load(target)
    assert set(reloaded.vectors) == {1, 2}
    assert 2 in reloaded.fingerprints


def test_a_second_sync_after_a_save_does_nothing(tmp_path):
    existing = rows_for((1, "기존 답변입니다"))
    database = make_database(tmp_path, existing)
    target = tmp_path / "index.json"
    index_with(existing, fingerprints=True, salt=0).save(target)
    insert(database, 2, "질분 2 입니까", "새 답변")

    sync_semantic_index(database, client=FakeClient(salt=1), index_path=target,
                        lock=threading.RLock())
    settled = target.read_bytes()
    mtime = target.stat().st_mtime_ns

    client = FakeClient(salt=2)
    again = sync_semantic_index(database, client=client, index_path=target,
                                lock=threading.RLock())

    assert again.plan.fresh_count == 2
    assert again.embedded_count == 0
    assert client.calls == 0
    assert again.saved is False
    assert target.read_bytes() == settled
    assert target.stat().st_mtime_ns == mtime


# ------------------------------------------------ modified Learning (§18)
def test_an_edited_answer_re_embeds_only_that_row(tmp_path):
    both = rows_for((1, "첫 답변입니다"),
                    (2, "둘째 답변입니다"))
    database = make_database(tmp_path, both)
    target = tmp_path / "index.json"
    index_with(both, fingerprints=True, salt=0).save(target)
    untouched = LearningSemanticIndex.load(target).vectors[2]

    insert(database, 1, both[0]["question"],
           "정책이 바띀어 고친 답변입니다")
    client = FakeClient(salt=1)
    result = sync_semantic_index(database, client=client, index_path=target,
                                 lock=threading.RLock())

    assert result.plan.classification[1] == learning_semantic_sync.STALE
    assert result.plan.classification[2] == learning_semantic_sync.FRESH
    assert result.plan.to_embed == (1,)
    assert client.calls == 1
    reloaded = LearningSemanticIndex.load(target)
    assert reloaded.vectors[2] == untouched      # bit for bit


# ---------------------------------------------------------- no-op (§17)
def test_a_save_that_does_not_change_the_text_writes_nothing(tmp_path):
    rows = rows_for((1, "그대로입니다"))
    database = make_database(tmp_path, rows)
    target = tmp_path / "index.json"
    index_with(rows, fingerprints=True, salt=0).save(target)
    before = target.read_bytes()
    mtime = target.stat().st_mtime_ns

    # a metadata-shaped write: same question and answer
    insert(database, 1, rows[0]["question"], rows[0]["answer"])
    client = FakeClient(salt=1)
    result = sync_semantic_index(database, client=client, index_path=target,
                                 lock=threading.RLock())

    assert result.plan.fresh_count == 1
    assert client.calls == 0
    assert result.saved is False
    assert target.read_bytes() == before
    assert target.stat().st_mtime_ns == mtime


# ------------------------------------------- removal via the same path (§14)
def test_deactivating_a_learning_drops_its_vector_on_the_next_sync(tmp_path):
    both = rows_for((1, "남습니다"), (2, "바집니다"))
    database = make_database(tmp_path, both)
    target = tmp_path / "index.json"
    index_with(both, fingerprints=True, salt=0).save(target)

    with database.connection() as connection:
        connection.execute(
            "UPDATE learning_examples SET active=0 WHERE id=2")
        connection.commit()

    client = FakeClient(salt=1)
    result = sync_semantic_index(database, client=client, index_path=target,
                                 lock=threading.RLock())

    assert result.plan.classification[2] == (
        learning_semantic_sync.INELIGIBLE_EXISTING)
    assert result.removed_count == 1
    assert client.calls == 0                      # a removal needs no request
    reloaded = LearningSemanticIndex.load(target)
    assert set(reloaded.vectors) == {1}
    assert set(reloaded.fingerprints) == {1}


# ------------------------------------- embedding failure is harmless (§15)
def test_an_embedding_failure_leaves_the_index_and_the_row_alone(
        tmp_path, caplog):
    existing = rows_for((1, "기존 답변입니다"))
    database = make_database(tmp_path, existing)
    target = tmp_path / "index.json"
    index_with(existing, fingerprints=True, salt=0).save(target)
    before = target.read_bytes()
    mtime = target.stat().st_mtime_ns

    insert(database, 2, "질분 2 입니까", "새 답변")
    with caplog.at_level(logging.WARNING):
        result = sync_semantic_index(
            database, client=FakeClient(salt=1, fail_on_call=1),
            index_path=target, lock=threading.RLock())

    assert result is None                       # reported, not raised
    assert target.read_bytes() == before
    assert target.stat().st_mtime_ns == mtime
    with database.connection() as connection:
        stored = connection.execute(
            "SELECT final_answer, active FROM learning_examples WHERE id=2"
        ).fetchone()
    assert stored["final_answer"] == "새 답변"
    assert stored["active"] == 1
    messages = [record.getMessage() for record in caplog.records]
    assert any("semantic index sync failed" in message
               for message in messages)
    assert any("RuntimeError" in message for message in messages)
    # the log must not carry the answer text
    assert not any("새 답변" in message for message in messages)


# ------------------------------------ index save failure is harmless (§16)
def test_an_index_save_failure_leaves_the_index_and_the_row_alone(
        tmp_path, caplog, monkeypatch):
    existing = rows_for((1, "기존 답변입니다"))
    database = make_database(tmp_path, existing)
    target = tmp_path / "index.json"
    index_with(existing, fingerprints=True, salt=0).save(target)
    before = target.read_bytes()
    mtime = target.stat().st_mtime_ns
    insert(database, 2, "질분 2 입니까", "새 답변")

    import os

    monkeypatch.setattr(
        os, "replace",
        lambda source, destination: (_ for _ in ()).throw(
            OSError("target is locked")))

    with caplog.at_level(logging.WARNING):
        result = sync_semantic_index(database, client=FakeClient(salt=1),
                                     index_path=target,
                                     lock=threading.RLock())

    assert result is None
    assert target.read_bytes() == before
    assert target.stat().st_mtime_ns == mtime
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "index.json", "learning.db"]          # no temporary left behind
    assert any("semantic index sync failed" in record.getMessage()
               for record in caplog.records)


def test_a_model_mismatch_is_logged_as_needing_a_full_rebuild(
        tmp_path, caplog):
    rows = rows_for((1, "답변입니다"))
    database = make_database(tmp_path, rows)
    target = tmp_path / "index.json"
    index_with(rows, fingerprints=True, model="old-model").save(target)
    before = target.read_bytes()
    insert(database, 2, "질분 2 입니까", "새 답변")

    client = FakeClient(salt=1)
    with caplog.at_level(logging.ERROR):
        result = sync_semantic_index(database, client=client,
                                     index_path=target,
                                     lock=threading.RLock())

    assert result is not None
    assert result.full_rebuild_required is True
    assert client.calls == 0
    assert target.read_bytes() == before
    assert any("full rebuild required" in record.getMessage()
               for record in caplog.records)


# ------------------------------- the repository seam is post-commit (§20)
def test_the_hook_runs_after_the_transaction_has_committed(tmp_path):
    """The hook must see the row, and must not be inside the write.

    If it ran inside the transaction it would hold the write open across a
    network call. So it is asserted from the hook itself: the row is already
    visible to a SEPARATE connection, which can only be true after commit.
    """

    from repositories.database import Database

    path = tmp_path / "real.db"
    database = Database(path)
    database.initialize()
    repository = LearningRepository(database)

    observed: dict[str, object] = {}

    def hook():
        with database.connection() as connection:
            row = connection.execute(
                "SELECT final_answer FROM learning_examples "
                "WHERE source_key=?", ("hook-test",)).fetchone()
            # read while the connection is still open: a fresh connection
            # that is not mid-transaction can only see committed rows
            observed["in_transaction"] = connection.in_transaction
        observed["visible_to_another_connection"] = row is not None
        observed["answer_committed"] = (
            None if row is None else row["final_answer"])

    repository.on_committed = hook
    saved = repository.upsert(example("hook-test"))

    assert saved["id"]
    assert observed["visible_to_another_connection"] is True
    assert observed["answer_committed"] == "답변입니다"
    assert observed["in_transaction"] is False


def test_a_hook_that_raises_does_not_fail_the_save(tmp_path, caplog):
    from repositories.database import Database

    database = Database(tmp_path / "real.db")
    database.initialize()
    repository = LearningRepository(database)
    repository.on_committed = lambda: (_ for _ in ()).throw(
        RuntimeError("index exploded"))

    with caplog.at_level(logging.WARNING):
        saved = repository.upsert(example("raise-test"))

    assert saved["id"]
    with database.connection() as connection:
        row = connection.execute(
            "SELECT id FROM learning_examples WHERE source_key=?",
            ("raise-test",)).fetchone()
    assert row is not None
    assert any("post-commit Learning hook failed" in record.getMessage()
               for record in caplog.records)


def test_a_repository_with_no_hook_behaves_exactly_as_before(tmp_path):
    from repositories.database import Database

    database = Database(tmp_path / "real.db")
    database.initialize()
    repository = LearningRepository(database)

    assert repository.on_committed is None
    saved = repository.upsert(example("no-hook"))
    assert saved["id"]


# --------------------------------- the service wires it, batches suppress it
def test_the_learning_service_attaches_the_notifier(tmp_path):
    from repositories.database import Database
    from services.learning_service import LearningService

    database = Database(tmp_path / "real.db")
    database.initialize()
    service = LearningService(database)

    assert isinstance(service.index_notifier, SemanticIndexNotifier)
    assert service.repository.on_committed is service.index_notifier
    assert service.index_notifier.enabled is True


def test_the_index_path_is_derived_from_the_database_not_defaulted(tmp_path):
    """A throwaway database must never be able to write the real index.

    This is a regression test for a defect this step introduced: the notifier
    defaulted to the packaged index path, so a service built on a temporary
    database synced ITS population over the production index, classified every
    real vector as an orphan and removed all 1,025 of them. Deriving the path
    from the database is what makes the accident impossible.
    """

    from repositories.database import Database
    from services.learning_index_sync_hook import index_path_for
    from services.learning_semantic_index import DEFAULT_INDEX_PATH
    from services.learning_service import LearningService

    production = pathlib.Path(DEFAULT_INDEX_PATH).resolve()

    database = Database(tmp_path / "throwaway.db")
    database.initialize()
    notifier = SemanticIndexNotifier(database)

    assert notifier.index_path.parent.resolve() == tmp_path.resolve()
    assert notifier.index_path.resolve() != production
    assert index_path_for(database).resolve() != production

    # and the same through the service, which is what wires it in production
    service = LearningService(database)
    assert service.index_notifier.index_path.resolve() != production
    assert service.index_notifier.index_path.parent.resolve() == (
        tmp_path.resolve())


def test_saving_through_the_service_does_not_touch_the_packaged_index(
        tmp_path):
    """End to end on the property above: a real save, a real notifier."""

    from repositories.database import Database
    from services.learning_semantic_index import DEFAULT_INDEX_PATH
    from services.learning_service import LearningService

    production = pathlib.Path(DEFAULT_INDEX_PATH)
    before = production.read_bytes() if production.exists() else None
    before_mtime = (production.stat().st_mtime_ns if production.exists()
                    else None)

    database = Database(tmp_path / "throwaway.db")
    database.initialize()
    service = LearningService(database)
    saved = service.repository.upsert(example("service-save"))

    assert saved["id"]
    assert service.index_notifier.calls == 1
    if before is None:
        assert not production.exists()
    else:
        assert production.read_bytes() == before
        assert production.stat().st_mtime_ns == before_mtime


def test_a_direct_call_without_an_index_path_uses_the_database_sibling(
        tmp_path):
    """Omitting index_path must not reach for the packaged file.

    The notifier always passed a resolved path, so the hook was safe; a direct
    caller was not. A periodic repair written as
    ``sync_semantic_index(database)`` would have synced that database's
    population over the packaged index -- the same shape of accident that
    emptied it once during this work.
    """

    from services.learning_semantic_index import DEFAULT_INDEX_PATH

    packaged = pathlib.Path(DEFAULT_INDEX_PATH)
    before = packaged.read_bytes() if packaged.exists() else None
    before_mtime = (packaged.stat().st_mtime_ns if packaged.exists()
                    else None)

    rows = rows_for((1, "\ud558\ub098"))
    database = make_database(tmp_path, rows)
    sibling = tmp_path / "learning_semantic_index.json"
    assert not sibling.exists()

    result = sync_semantic_index(database, client=FakeClient(salt=1),
                                 lock=threading.RLock())

    assert result is not None
    assert result.saved is True
    assert sibling.exists()
    assert set(LearningSemanticIndex.load(sibling).vectors) == {1}

    if before is None:
        assert not packaged.exists()
    else:
        assert packaged.read_bytes() == before
        assert packaged.stat().st_mtime_ns == before_mtime


def test_a_database_without_a_path_fails_closed(tmp_path):
    """No path means no derivation -- not a quiet fall back to the package."""

    from services.learning_index_sync_hook import index_path_for
    from services.learning_semantic_index import DEFAULT_INDEX_PATH

    packaged = pathlib.Path(DEFAULT_INDEX_PATH)
    before = packaged.read_bytes() if packaged.exists() else None
    before_mtime = (packaged.stat().st_mtime_ns if packaged.exists()
                    else None)

    class Pathless:
        def connection(self):
            raise AssertionError("must not be reached")

    with pytest.raises(ValueError) as caught:
        index_path_for(Pathless())
    assert "database.path is required" in str(caught.value)

    # and through the hook, which turns it into a logged best-effort failure
    # rather than an exception, with nothing written anywhere
    saver_called = sync_semantic_index(Pathless(), client=FakeClient(salt=1),
                                       lock=threading.RLock())
    assert saver_called is None

    if before is None:
        assert not packaged.exists()
    else:
        assert packaged.read_bytes() == before
        assert packaged.stat().st_mtime_ns == before_mtime


def test_an_explicit_index_path_is_still_honoured(tmp_path):
    """The override has to keep working: every other test here relies on it."""

    from services.learning_semantic_index import DEFAULT_INDEX_PATH

    packaged = pathlib.Path(DEFAULT_INDEX_PATH)
    before = packaged.read_bytes() if packaged.exists() else None

    rows = rows_for((1, "\ud558\ub098"))
    database = make_database(tmp_path, rows)
    custom = tmp_path / "somewhere_else" / "custom_index.json"
    sibling = tmp_path / "learning_semantic_index.json"

    result = sync_semantic_index(database, client=FakeClient(salt=1),
                                 index_path=custom,
                                 lock=threading.RLock())

    assert result.saved is True
    assert custom.exists()
    assert set(LearningSemanticIndex.load(custom).vectors) == {1}
    assert not sibling.exists()          # the default was not used as well
    if before is not None:
        assert packaged.read_bytes() == before


def test_a_suppressed_notifier_does_not_sync(tmp_path):
    database = make_database(tmp_path)
    notifier = SemanticIndexNotifier(database)

    with notifier.suppressed():
        assert notifier.enabled is False
        assert notifier() is None
    assert notifier.enabled is True
    assert notifier.calls == 0


def test_suppression_nests_and_restores(tmp_path):
    notifier = SemanticIndexNotifier(make_database(tmp_path))

    with notifier.suppressed():
        with notifier.suppressed():
            assert notifier.enabled is False
        assert notifier.enabled is False
    assert notifier.enabled is True


def test_the_batch_importers_suppress_the_sync(tmp_path):
    """A backfill must not run one full-population sync per imported row."""

    import inspect

    from services import learning_service as module

    for name in ("import_existing_approved",
                 "import_existing_seller_answers"):
        source = inspect.getsource(getattr(module.LearningService, name))
        assert "self.index_notifier.suppressed()" in source, name


# --------------------------- the hook never calls sync with a subset (§3)
def test_the_hook_passes_the_whole_population_not_one_row(tmp_path):
    """sync(rows) removes whatever rows omits, so a subset would be fatal.

    Measured in STEP 2-2: index {1,2} with rows [2] removes 1. The hook must
    therefore hand over every eligible row, and this asserts the call it makes
    rather than trusting the comment.
    """

    both = rows_for((1, "첫 답변입니다"),
                    (2, "둘째 답변입니다"))
    database = make_database(tmp_path, both)
    target = tmp_path / "index.json"
    index_with(both, fingerprints=True, salt=0).save(target)

    seen: dict[str, object] = {}
    genuine = learning_semantic_sync.sync

    def recording_sync(rows, **kwargs):
        seen["ids"] = sorted(int(row["id"]) for row in rows)
        seen["known"] = sorted(kwargs.get("known_row_ids") or [])
        return genuine(rows, **kwargs)

    import services.learning_index_sync_hook as hook_module

    original = hook_module.learning_semantic_sync.sync
    hook_module.learning_semantic_sync.sync = recording_sync
    try:
        sync_semantic_index(database, client=FakeClient(salt=1),
                            index_path=target, lock=threading.RLock())
    finally:
        hook_module.learning_semantic_sync.sync = original

    assert seen["ids"] == [1, 2]
    assert seen["known"] == [1, 2]
    assert set(LearningSemanticIndex.load(target).vectors) == {1, 2}


# ------------------------------------ one population definition (§5)
def test_the_hook_and_the_cli_read_the_same_population(tmp_path):
    from scripts import rebuild_learning_semantic_index as script

    rows = rows_for((1, "하나"), (2, "둘"), (3, "셋"))
    database = make_database(tmp_path, rows)
    with database.connection() as connection:
        connection.execute("UPDATE learning_examples SET active=0 WHERE id=3")
        connection.execute(
            "INSERT INTO learning_examples "
            "(id, question_original_masked, final_answer, active) "
            "VALUES (4, '', '', 1)")
        connection.commit()

    cli_rows = script.eligible_rows(tmp_path / "learning.db")
    cli_ids = script.all_row_ids(tmp_path / "learning.db")
    with database.connection() as connection:
        hook_rows = learning_index_population.eligible_rows(connection)
        hook_ids = learning_index_population.all_row_ids(connection)

    assert sorted(int(r["id"]) for r in cli_rows) == [1, 2]
    assert sorted(int(r["id"]) for r in cli_rows) == sorted(
        int(r["id"]) for r in hook_rows)
    assert cli_ids == hook_ids == {1, 2, 3, 4}
    for left, right in zip(sorted(cli_rows, key=lambda r: r["id"]),
                           sorted(hook_rows, key=lambda r: r["id"])):
        assert fingerprint_for(left) == fingerprint_for(right)


# ------------------------------------------- writer serialisation (§21)
def test_two_syncs_do_not_run_their_critical_sections_at_once(tmp_path):
    """The lock is held across load -> classify -> embed -> save.

    Asserted deterministically, by observing the lock itself from outside
    while a sync is provably inside its critical section: a second sync that
    loaded the index before the first saved would write back a view predating
    it, and the first sync's work would be lost.

    A second thread is then released by the first and both outcomes are
    checked, so the serialisation is shown end to end rather than inferred.
    """

    rows = rows_for((1, "하나"))
    database = make_database(tmp_path, rows)
    target = tmp_path / "index.json"
    index_with(rows, fingerprints=True, salt=0).save(target)
    insert(database, 2, "질분 2 입니까", "둘")

    lock = threading.RLock()
    first_inside = threading.Event()
    release_first = threading.Event()
    second_reached_embed = threading.Event()
    concurrent = []
    depth = {"n": 0}

    class BlockingClient(FakeClient):
        def embed(self, texts):
            depth["n"] += 1
            if depth["n"] > 1:
                concurrent.append(True)
            first_inside.set()
            release_first.wait(timeout=5)
            try:
                return super().embed(texts)
            finally:
                depth["n"] -= 1

    class WatchingClient(FakeClient):
        def embed(self, texts):
            depth["n"] += 1
            if depth["n"] > 1:
                concurrent.append(True)
            second_reached_embed.set()
            try:
                return super().embed(texts)
            finally:
                depth["n"] -= 1

    done = []

    def run(client):
        def body():
            sync_semantic_index(database, client=client, index_path=target,
                                lock=lock)
            done.append(client)
        return body

    first = threading.Thread(target=run(BlockingClient(salt=1)), daemon=True)
    first.start()
    assert first_inside.wait(timeout=5), "the first sync never reached embed"

    # THE assertion: the first sync is inside its critical section, so the
    # lock must not be available. Without the lock this acquire succeeds.
    acquired = lock.acquire(blocking=False)
    if acquired:
        lock.release()
    second = threading.Thread(target=run(WatchingClient(salt=2)), daemon=True)
    second.start()
    # and the second cannot reach its own embed while the first holds the lock
    reached_early = second_reached_embed.wait(timeout=1.0)

    release_first.set()
    first.join(timeout=10)
    second.join(timeout=10)

    assert acquired is False, (
        "the index write lock was free while a sync was inside its "
        "critical section")
    assert reached_early is False, (
        "a second sync entered its critical section before the first left")
    assert len(done) == 2
    assert concurrent == []
    assert set(LearningSemanticIndex.load(target).vectors) == {1, 2}


def test_the_lock_is_not_held_while_the_database_writes(tmp_path):
    """The lock is taken after the commit, so it never wraps a transaction."""

    from repositories.database import Database

    database = Database(tmp_path / "real.db")
    database.initialize()
    repository = LearningRepository(database)
    import services.learning_index_sync_hook as hook_module

    held = {"during_write": None}
    acquired = hook_module._INDEX_WRITE_LOCK.acquire(blocking=False)
    assert acquired, "lock should be free before the test"
    try:
        # With the module lock held, a write must still complete: the hook is
        # the only thing that waits on it, and the hook runs after the commit.
        def hook():
            held["during_write"] = "hook ran"

        repository.on_committed = hook
        saved = repository.upsert(example("lock-test"))
    finally:
        hook_module._INDEX_WRITE_LOCK.release()

    assert saved["id"]
    assert held["during_write"] == "hook ran"


# -------------------------------------------------- logging hygiene (§7)
def test_no_answer_text_or_identifier_reaches_the_log(tmp_path, caplog):
    secret_answer = "주방 01012345678 주방"
    database = make_database(tmp_path)
    target = tmp_path / "index.json"
    index_with(rows_for((9, "기존")), fingerprints=True).save(target)
    insert(database, 1, "질분입니까", secret_answer)

    with caplog.at_level(logging.DEBUG):
        sync_semantic_index(database, client=FakeClient(salt=1,
                                                       fail_on_call=1),
                            index_path=target, lock=threading.RLock())
        sync_semantic_index(database, client=FakeClient(salt=1),
                            index_path=target, lock=threading.RLock())

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert secret_answer not in text
    assert "01012345678" not in text
    assert "질분입니까" not in text
