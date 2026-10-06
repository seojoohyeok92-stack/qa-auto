"""Saving the semantic index must never expose a half-written file.

``LearningSemanticIndex.load`` turns a parse failure into an empty index, which
is the right answer for a missing file and the wrong one for a file that is
mid-write: retrieval silently loses its whole semantic channel for as long as
the write lasts, and nothing logs it. The file is tens of megabytes, so the
window is real -- about 1.4 seconds at the size the corpus is heading for.

These tests pin the property that removes the window: the live path only ever
holds a complete index. Every failure case asserts the *old* file survived
byte for byte, because "the save failed" must never mean "the index is gone".
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import tempfile
import threading

import pytest

from services import learning_semantic_index as semantic_index
from services.learning_semantic_index import (
    EMBEDDING_DIMENSIONS, LearningSemanticIndex, _LOADED,
)

MODEL = "text-embedding-3-small"


def index_of(count: int, *, dimensions: int = 4, seed: float = 0.0
             ) -> LearningSemanticIndex:
    vectors = {
        identifier: [seed + identifier + offset / 1000.0
                     for offset in range(dimensions)]
        for identifier in range(1, count + 1)
    }
    return LearningSemanticIndex(vectors, model=MODEL)


def digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def strays(directory: pathlib.Path, target: pathlib.Path) -> list[str]:
    return sorted(item.name for item in directory.iterdir()
                  if item.name != target.name)


def legacy_bytes(index: LearningSemanticIndex) -> str:
    """Exactly what the previous direct-write implementation produced."""

    return json.dumps({
        "model": index.model,
        "dimensions": EMBEDDING_DIMENSIONS,
        "count": len(index.vectors),
        "vectors": {str(key): value for key, value in index.vectors.items()},
    }, ensure_ascii=False)


# --------------------------------------------------------------- A. 정상 저장
def test_a_save_writes_the_target_and_leaves_no_temporary(tmp_path):
    index = index_of(5)
    target = tmp_path / "learning_semantic_index.json"

    returned = index.save(target)

    assert returned == target
    assert target.exists()
    assert strays(tmp_path, target) == []
    assert LearningSemanticIndex.load(target).vectors == index.vectors


def test_a_a_round_trip_preserves_model_ids_and_values(tmp_path):
    index = index_of(7, dimensions=6, seed=0.25)
    target = tmp_path / "index.json"

    index.save(target)
    reloaded = LearningSemanticIndex.load(target)

    assert reloaded.model == index.model
    assert set(reloaded.vectors) == set(index.vectors)
    assert reloaded.vectors == index.vectors


# ----------------------------------------------------- B. 기존 target 교체
def test_b_replacing_an_existing_index_leaves_only_the_new_content(tmp_path):
    target = tmp_path / "index.json"
    index_of(3).save(target)
    first = digest(target)

    replacement = index_of(9, seed=100.0)
    replacement.save(target)

    assert digest(target) != first
    assert strays(tmp_path, target) == []
    reloaded = LearningSemanticIndex.load(target)
    assert reloaded.vectors == replacement.vectors
    assert len(reloaded.vectors) == 9


# ----------------------------------------- C. serialization failure 시 보호
def test_c_a_serialisation_failure_cannot_touch_the_existing_index(tmp_path):
    target = tmp_path / "index.json"
    index_of(4).save(target)
    before = digest(target)

    broken = index_of(4)
    broken.vectors[99] = [object()]        # json.dumps cannot encode this

    with pytest.raises(TypeError):
        broken.save(target)

    assert digest(target) == before
    assert strays(tmp_path, target) == []


# ------------------------------------------- D. replace 이전 failure 시 보호
def test_d_a_write_failure_before_replace_cannot_touch_the_existing_index(
        tmp_path, monkeypatch):
    target = tmp_path / "index.json"
    index_of(4).save(target)
    before = digest(target)

    def exploding_fsync(descriptor):
        raise OSError("disk went away mid-write")

    monkeypatch.setattr(os, "fsync", exploding_fsync)

    with pytest.raises(OSError):
        index_of(50, seed=7.0).save(target)

    assert digest(target) == before
    assert strays(tmp_path, target) == []


def test_d_a_a_write_failure_with_no_previous_index_creates_nothing(
        tmp_path, monkeypatch):
    target = tmp_path / "index.json"

    monkeypatch.setattr(os, "fsync", lambda descriptor: (_ for _ in ()).throw(
        OSError("no space left on device")))

    with pytest.raises(OSError):
        index_of(4).save(target)

    assert not target.exists()
    assert sorted(item.name for item in tmp_path.iterdir()) == []


# ------------------------------------------------ E. os.replace failure 보호
def test_e_a_replace_failure_cannot_touch_the_existing_index(
        tmp_path, monkeypatch):
    target = tmp_path / "index.json"
    index_of(4).save(target)
    before = digest(target)

    def refusing_replace(source, destination):
        raise OSError("target is locked by another process")

    monkeypatch.setattr(semantic_index, "REPLACE_ATTEMPTS", 1)
    monkeypatch.setattr(os, "replace", refusing_replace)

    with pytest.raises(OSError):
        index_of(60, seed=3.0).save(target)

    assert digest(target) == before
    assert LearningSemanticIndex.load(target).vectors == index_of(4).vectors
    assert strays(tmp_path, target) == []


def test_e_a_a_a_reader_holding_the_file_open_does_not_lose_the_save(
        tmp_path, monkeypatch):
    """Windows refuses to replace a file another handle has open.

    It surfaces as PermissionError/WinError 5 rather than a wait, and the
    handle in production is retrieval reading tens of megabytes -- roughly
    half a second per read, long enough to collide with a rebuild. The move
    is all-or-nothing, so a refused attempt has changed nothing and is simply
    retried. Without the retry the index silently stays stale.
    """

    target = tmp_path / "index.json"
    index_of(4).save(target)

    attempts: list[int] = []
    genuine_replace = os.replace

    def busy_twice(source, destination):
        attempts.append(1)
        if len(attempts) <= 2:
            raise PermissionError(5, "Access is denied")
        return genuine_replace(source, destination)

    monkeypatch.setattr(semantic_index, "REPLACE_BACKOFF_SECONDS", 0.001)
    monkeypatch.setattr(os, "replace", busy_twice)

    index_of(21, seed=8.0).save(target)

    assert len(attempts) == 3
    assert len(LearningSemanticIndex.load(target).vectors) == 21
    assert strays(tmp_path, target) == []


def test_e_a_a_b_a_permanently_blocked_replace_is_reported_not_swallowed(
        tmp_path, monkeypatch):
    target = tmp_path / "index.json"
    index_of(4).save(target)
    before = digest(target)
    attempts: list[int] = []

    def always_busy(source, destination):
        attempts.append(1)
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(semantic_index, "REPLACE_ATTEMPTS", 3)
    monkeypatch.setattr(semantic_index, "REPLACE_BACKOFF_SECONDS", 0.001)
    monkeypatch.setattr(os, "replace", always_busy)

    with pytest.raises(PermissionError):
        index_of(21, seed=8.0).save(target)

    assert len(attempts) == 3
    assert digest(target) == before
    assert strays(tmp_path, target) == []


def test_e_a_a_c_a_zero_attempt_setting_still_moves_the_file(tmp_path,
                                                             monkeypatch):
    """A degenerate retry count must not turn the move into a silent no-op.

    The loop is driven by a module constant, so a zero would otherwise skip
    the body and return as though the index had been replaced -- a save that
    reports success and changes nothing.
    """

    target = tmp_path / "index.json"
    index_of(4).save(target)
    monkeypatch.setattr(semantic_index, "REPLACE_ATTEMPTS", 0)

    index_of(13, seed=2.0).save(target)

    assert len(LearningSemanticIndex.load(target).vectors) == 13
    assert strays(tmp_path, target) == []


# ------------------------------------------------------- F. temp cleanup
def test_f_a_cleanup_failure_does_not_mask_the_real_error(
        tmp_path, monkeypatch):
    """The exception that explains the failure must survive cleanup."""

    target = tmp_path / "index.json"
    index_of(4).save(target)
    before = digest(target)

    monkeypatch.setattr(semantic_index, "REPLACE_ATTEMPTS", 1)
    monkeypatch.setattr(os, "replace", lambda source, destination: (
        _ for _ in ()).throw(OSError("replace refused")))
    monkeypatch.setattr(os, "unlink", lambda path: (_ for _ in ()).throw(
        OSError("cleanup refused too")))

    with pytest.raises(OSError, match="replace refused"):
        index_of(4).save(target)

    assert digest(target) == before


# -------------------------------------------------------- G. cache reload
def test_g_an_atomic_replace_is_picked_up_without_a_restart(tmp_path):
    target = tmp_path / "index.json"
    old = index_of(3)
    old.save(target)
    # Stand the file in the past, the way a real index written earlier sits,
    # so the new mtime is unambiguously different.
    stamp = target.stat().st_mtime - 60
    os.utime(target, (stamp, stamp))

    _LOADED.clear()
    try:
        first = LearningSemanticIndex.load_cached(target)
        assert len(first.vectors) == 3
        assert LearningSemanticIndex.load_cached(target) is first

        new = index_of(11, seed=50.0)
        new.save(target)

        second = LearningSemanticIndex.load_cached(target)
        assert second is not first
        assert len(second.vectors) == 11
        assert second.vectors == new.vectors
        assert len(_LOADED) == 1          # the stale 33MB entry is dropped
    finally:
        _LOADED.clear()


# ------------------------------------------------------- H. schema 무변
def test_h_the_saved_schema_gains_no_new_keys(tmp_path):
    target = tmp_path / "index.json"
    index = index_of(6)
    index.save(target)

    payload = json.loads(target.read_text(encoding="utf-8"))

    assert set(payload) == {"model", "dimensions", "count", "vectors"}
    assert "fingerprints" not in payload
    assert "generated_at" not in payload
    assert payload["model"] == MODEL
    assert payload["dimensions"] == EMBEDDING_DIMENSIONS
    assert payload["count"] == 6
    assert sorted(payload["vectors"]) == sorted(
        str(key) for key in index.vectors)


def test_h_a_the_bytes_match_the_previous_direct_write_exactly(tmp_path):
    """Atomicity is the only change: the file content is unchanged.

    Without this an index saved by the new code would differ from one saved by
    the old, and every rebuild would look like a total rewrite.
    """

    index = index_of(12, dimensions=5, seed=0.5)
    target = tmp_path / "index.json"
    index.save(target)

    assert target.read_bytes() == legacy_bytes(index).encode("utf-8")


# ---------------------------------------------------- I. Windows-safe path
def test_i_the_temporary_file_is_created_beside_the_target(
        tmp_path, monkeypatch):
    """os.replace is only atomic within one filesystem.

    A temporary file in the system temp directory is routinely on another
    volume, where the move stops being atomic -- and on Windows fails
    outright. So the directory passed to mkstemp has to be the target's own.
    """

    target = tmp_path / "nested" / "index.json"
    seen: dict[str, object] = {}
    genuine = tempfile.mkstemp

    def recording_mkstemp(*args, **kwargs):
        seen.update(kwargs)
        return genuine(*args, **kwargs)

    monkeypatch.setattr(tempfile, "mkstemp", recording_mkstemp)
    index_of(3).save(target)

    assert seen["dir"] == str(target.parent)
    assert pathlib.Path(seen["dir"]).resolve() == target.parent.resolve()
    assert str(seen["prefix"]).startswith(target.name)
    assert target.exists()
    assert strays(target.parent, target) == []


def test_i_a_a_missing_parent_directory_is_created(tmp_path):
    target = tmp_path / "a" / "b" / "index.json"

    index_of(2).save(target)

    assert target.exists()
    assert LearningSemanticIndex.load(target).vectors == index_of(2).vectors


# --------------------------- §9 concurrency: the window, deterministically
def test_the_target_is_a_complete_old_index_while_a_save_is_in_flight(
        tmp_path):
    """The property that closes the race, pinned without relying on timing.

    The save is held open in the middle of writing its temporary file, and a
    reader reads the live path throughout. Every observation has to be the
    complete *old* index: not empty, and not yet the new one. That is the
    whole point of writing elsewhere and moving into place.

    Timing decides nothing here -- the reader is released by the save itself
    and the save does not finish until the reader is done -- so this says
    something the alternating-save smoke test below cannot.
    """

    target = tmp_path / "index.json"
    old = index_of(4)
    old.save(target)

    inside_write = threading.Event()
    reader_finished = threading.Event()
    observations: list[int] = []
    failures: list[str] = []
    genuine_fsync = os.fsync

    def reader():
        inside_write.wait(timeout=10)
        for _ in range(40):
            try:
                observations.append(
                    len(LearningSemanticIndex.load(target).vectors))
            except Exception as error:      # noqa: BLE001 - recorded, not raised
                failures.append(repr(error))
        reader_finished.set()

    def pausing_fsync(descriptor):
        genuine_fsync(descriptor)
        inside_write.set()
        reader_finished.wait(timeout=10)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(os, "fsync", pausing_fsync)
            index_of(31, seed=9.0).save(target)
    finally:
        inside_write.set()
        reader_finished.set()
        thread.join(timeout=10)

    assert not failures
    assert observations, "the reader never ran; the test proves nothing"
    assert set(observations) == {4}, (
        "while the save was in flight the live path was not the intact old "
        "index: observed %s" % sorted(set(observations)))
    # And the move did land once the write completed.
    assert len(LearningSemanticIndex.load(target).vectors) == 31
    assert strays(tmp_path, target) == []


def test_a_reader_never_observes_a_partial_index(tmp_path):
    """Smoke test: repeated saves under a concurrent reader.

    Deliberately not a stress harness, and it does not on its own distinguish
    the new implementation from the old -- at these sizes a direct overwrite
    can finish between two reads. The deterministic statement about the
    in-flight window is the test above; this one guards the cruder property
    that a stream of real saves never hands a reader something unparseable.
    """

    target = tmp_path / "index.json"
    small = index_of(600, dimensions=48)
    large = index_of(900, dimensions=48, seed=5.0)
    expected = {len(small.vectors), len(large.vectors)}

    small.save(target)
    observations: list[int] = []
    failures: list[str] = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                observations.append(len(LearningSemanticIndex.load(target).vectors))
            except Exception as error:          # noqa: BLE001 - recorded, not raised
                failures.append(repr(error))

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        for turn in range(8):
            (large if turn % 2 == 0 else small).save(target)
    finally:
        stop.set()
        thread.join(timeout=10)

    assert not failures
    assert observations, "the reader never ran; the test proves nothing"
    assert 0 not in observations, (
        "a reader saw an empty index: load() swallowed a parse error on a "
        "partially written file")
    assert set(observations) <= expected, (
        "unexpected vector counts observed: %s" % sorted(set(observations)))
