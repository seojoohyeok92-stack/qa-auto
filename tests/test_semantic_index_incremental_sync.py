"""The index must learn what changed and leave alone what did not.

Two properties carry the weight here. One: a row whose embedded text is
unchanged keeps the exact vector it already had -- not a recomputed one,
because re-embedding the same text does not return the same vector. Measured
on fourteen rows of the real corpus, a fresh embedding of unchanged text came
back at cosine 0.99972-1.0 with components differing by up to 2.5e-3. So
"re-embed everything" is not a slower route to the same index; it is a
different index, with every ranking nudged.

Two: a sync that finds nothing to do must not write. The file is tens of
megabytes and a write gives it a new mtime, which makes every running process
re-parse it. A periodic repair built on a save-anyway sync would charge that
to every process, every hour, forever.

No test here reaches the network. The embedding client is a fake that counts
its calls and returns deterministic vectors.
"""
from __future__ import annotations

import hashlib
import json
import pathlib

import pytest

from services import learning_semantic_sync as sync_module
from services.learning_semantic_index import (
    EMBEDDING_DIMENSIONS, EMBEDDING_MODEL, LearningSemanticIndex, _text_for,
    fingerprint_for,
)
from services.learning_semantic_sync import (
    BOOTSTRAP, FRESH, INELIGIBLE_EXISTING, LEGACY_UNKNOWN, MISSING, ORPHAN,
    REEMBED, STALE, classify, sync,
)

DIMENSIONS = EMBEDDING_DIMENSIONS


class FakeClient:
    """Stand-in for the embeddings endpoint, including its unreliability.

    ``salt`` exists because the real endpoint does not return the same vector
    twice for the same text -- measured on fourteen real rows, a re-embedding
    of unchanged text came back at cosine 0.99972-1.0 with components
    differing by up to 2.5e-3. A fake that ignored that would make "the
    vector was reused" and "the vector was computed again" indistinguishable,
    and every reuse assertion in this file would pass for the wrong reason.
    So a client with a different salt stands for a later call to the same
    endpoint: same meaning, different bits.
    """

    def __init__(self, *, model: str = EMBEDDING_MODEL,
                 dimensions: int = DIMENSIONS, fail_on_call: int | None = None,
                 salt: int = 0):
        self.model = model
        self.dimensions = dimensions
        self.calls = 0
        self.tokens = 0
        self.texts: list[str] = []
        self.fail_on_call = fail_on_call
        self.salt = salt

    def embed(self, texts):
        self.calls += 1
        if self.fail_on_call is not None and self.calls >= self.fail_on_call:
            raise RuntimeError("embedding endpoint is down")
        self.texts.extend(texts)
        self.tokens += sum(len(text) for text in texts)
        vectors = []
        for text in texts:
            seed = int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)
            raw = [((seed + offset * 7919) % 1000) / 1000.0 + 0.001
                   + self.salt * 0.0007
                   for offset in range(self.dimensions)]
            norm = sum(value * value for value in raw) ** 0.5
            vectors.append([value / norm for value in raw])
        return vectors


def row(identifier: int, question: str, answer: str) -> dict:
    return {"id": identifier, "question": question, "answer": answer}


def rows_for(*specs) -> list[dict]:
    return [row(i, "질문 %d 입니까" % i, text) for i, text in specs]


def index_with(rows_, *, fingerprints: bool, model: str = EMBEDDING_MODEL,
               dimensions: int = DIMENSIONS, salt: int = 0
               ) -> LearningSemanticIndex:
    client = FakeClient(model=model, dimensions=dimensions, salt=salt)
    built = LearningSemanticIndex.build(rows_, client=client)
    if fingerprints:
        return built
    return LearningSemanticIndex(built.vectors, model=built.model)


class CountingSaver:
    def __init__(self):
        self.calls = 0
        self.paths: list[pathlib.Path] = []

    def __call__(self, index, path):
        self.calls += 1
        self.paths.append(pathlib.Path(path))
        return LearningSemanticIndex.save(index, path)


# ----------------------------------------------------------- A. MISSING
def test_a_a_missing_row_is_embedded_and_fingerprinted(tmp_path):
    target = tmp_path / "index.json"
    existing = rows_for((1, "기존 답변입니다"))
    index_with(existing, fingerprints=True).save(target)
    corpus = existing + rows_for((2, "새로 추가된 답변입니다"))
    client = FakeClient()
    saver = CountingSaver()

    result = sync(corpus, index_path=target, client=client, save=saver)

    assert result.plan.classification[2] == MISSING
    assert result.plan.to_embed == (2,)
    assert result.embedded_count == 1
    assert client.texts == [_text_for(corpus[1])]
    assert saver.calls == 1
    assert result.saved is True

    reloaded = LearningSemanticIndex.load(target)
    assert set(reloaded.vectors) == {1, 2}
    assert reloaded.fingerprints[2] == fingerprint_for(corpus[1])


# ------------------------------------------------------------- B. FRESH
def test_b_a_fresh_row_is_reused_and_not_embedded(tmp_path):
    target = tmp_path / "index.json"
    corpus = rows_for((1, "그대로인 답변입니다"), (2, "역시 그대로입니다"))
    index_with(corpus, fingerprints=True).save(target)
    client = FakeClient()
    saver = CountingSaver()

    result = sync(corpus, index_path=target, client=client, save=saver)

    assert set(result.plan.classification.values()) == {FRESH}
    assert result.plan.to_embed == ()
    assert client.calls == 0
    assert result.embedded_count == 0
    assert result.reused_count == 2
    assert saver.calls == 0


# ------------------------------------------------------------- C. STALE
def test_c_a_row_whose_text_changed_is_re_embedded(tmp_path):
    target = tmp_path / "index.json"
    before = rows_for((1, "변경 전 답변입니다"), (2, "건드리지 않습니다"))
    index_with(before, fingerprints=True).save(target)
    stored_for_two = LearningSemanticIndex.load(target).vectors[2]

    after = rows_for((1, "변경 후 전혀 다른 답변입니다"), (2, "건드리지 않습니다"))
    client = FakeClient()
    saver = CountingSaver()

    result = sync(after, index_path=target, client=client, save=saver)

    assert result.plan.classification[1] == STALE
    assert result.plan.classification[2] == FRESH
    assert result.plan.to_embed == (1,)
    assert client.texts == [_text_for(after[0])]
    assert saver.calls == 1

    reloaded = LearningSemanticIndex.load(target)
    assert reloaded.fingerprints[1] == fingerprint_for(after[0])
    assert reloaded.vectors[2] == stored_for_two      # untouched


def test_c_a_a_usage_style_metadata_change_is_not_a_text_change(tmp_path):
    """updated_at moves when a row is merely retrieved; the text hash does not.

    This is the whole reason the fingerprint is a text hash: on the real
    snapshot every one of the 104 rows that updated_at flagged as stale was a
    usage bump, not an edit.
    """

    target = tmp_path / "index.json"
    corpus = rows_for((1, "그대로인 답변입니다"))
    index_with(corpus, fingerprints=True).save(target)

    touched = [dict(corpus[0], usage_count=99,
                    last_used_at="2026-10-06T00:00:00Z",
                    updated_at="2026-10-06T00:00:00Z")]
    client = FakeClient()
    saver = CountingSaver()

    result = sync(touched, index_path=target, client=client, save=saver)

    assert result.plan.classification[1] == FRESH
    assert client.calls == 0
    assert saver.calls == 0


# ------------------------------------------------- D. INELIGIBLE_EXISTING
def test_d_a_vector_for_a_row_no_longer_eligible_is_removed(tmp_path):
    target = tmp_path / "index.json"
    corpus = rows_for((1, "남는 답변입니다"), (2, "빠지는 답변입니다"))
    index_with(corpus, fingerprints=True).save(target)
    client = FakeClient()
    saver = CountingSaver()

    result = sync(corpus[:1], index_path=target, client=client,
                  known_row_ids=[1, 2], save=saver)

    assert result.plan.classification[2] == INELIGIBLE_EXISTING
    assert result.plan.to_remove == (2,)
    assert result.removed_count == 1
    reloaded = LearningSemanticIndex.load(target)
    assert set(reloaded.vectors) == {1}
    assert set(reloaded.fingerprints) == {1}      # fingerprint goes too


# ------------------------------------------------------------ E. ORPHAN
def test_e_a_vector_whose_row_is_gone_is_removed_and_named_orphan(tmp_path):
    target = tmp_path / "index.json"
    corpus = rows_for((1, "남는 답변입니다"), (2, "삭제된 행입니다"))
    index_with(corpus, fingerprints=True).save(target)
    client = FakeClient()
    saver = CountingSaver()

    result = sync(corpus[:1], index_path=target, client=client,
                  known_row_ids=[1], save=saver)

    assert result.plan.classification[2] == ORPHAN
    assert result.plan.orphan_count == 1
    assert result.plan.ineligible_existing_count == 0
    assert set(LearningSemanticIndex.load(target).vectors) == {1}


def test_e_a_a_without_the_row_id_set_a_removal_is_not_called_an_orphan(
        tmp_path):
    """The distinction needs the table; it is not guessed."""

    target = tmp_path / "index.json"
    corpus = rows_for((1, "남는 답변입니다"), (2, "빠지는 답변입니다"))
    index_with(corpus, fingerprints=True).save(target)

    result = sync(corpus[:1], index_path=target, client=FakeClient(),
                  save=CountingSaver())

    assert result.plan.classification[2] == INELIGIBLE_EXISTING
    assert result.plan.orphan_count == 0
    assert result.plan.to_remove == (2,)


# ------------------------------------------------------------- F. no-op
def test_f_a_sync_with_nothing_to_do_does_not_save(tmp_path):
    target = tmp_path / "index.json"
    corpus = rows_for((1, "그대로입니다"), (2, "이것도 그대로입니다"))
    index_with(corpus, fingerprints=True).save(target)
    before_bytes = target.read_bytes()
    before_mtime = target.stat().st_mtime_ns
    client = FakeClient()
    saver = CountingSaver()

    result = sync(corpus, index_path=target, client=client, save=saver)

    assert result.changed is False
    assert result.saved is False
    assert saver.calls == 0
    assert client.calls == 0
    assert target.read_bytes() == before_bytes
    assert target.stat().st_mtime_ns == before_mtime


# ------------------------------------------------ G. second identical sync
def test_g_a_second_identical_sync_embeds_nothing_and_saves_nothing(tmp_path):
    target = tmp_path / "index.json"
    existing = rows_for((1, "기존 답변입니다"))
    index_with(existing, fingerprints=True).save(target)
    corpus = existing + rows_for((2, "새 답변입니다"))

    first_client, first_saver = FakeClient(), CountingSaver()
    first = sync(corpus, index_path=target, client=first_client,
                 save=first_saver)
    assert first.saved is True
    assert first_saver.calls == 1
    after_first = target.read_bytes()
    mtime_after_first = target.stat().st_mtime_ns

    second_client, second_saver = FakeClient(), CountingSaver()
    second = sync(corpus, index_path=target, client=second_client,
                  save=second_saver)

    assert second.embedded_count == 0
    assert second_client.calls == 0
    assert second.saved is False
    assert second_saver.calls == 0
    assert second.changed is False
    assert target.read_bytes() == after_first
    assert target.stat().st_mtime_ns == mtime_after_first


# ------------------------------------------------- H. embedding failure
def test_h_an_embedding_failure_leaves_the_index_byte_identical(tmp_path):
    target = tmp_path / "index.json"
    existing = rows_for((1, "기존 답변입니다"))
    index_with(existing, fingerprints=True).save(target)
    before = target.read_bytes()
    before_mtime = target.stat().st_mtime_ns

    corpus = existing + rows_for((2, "새 답변입니다"), (3, "또 다른 답변입니다"))
    client = FakeClient(fail_on_call=1)
    saver = CountingSaver()

    with pytest.raises(RuntimeError, match="endpoint is down"):
        sync(corpus, index_path=target, client=client, save=saver)

    assert saver.calls == 0
    assert target.read_bytes() == before
    assert target.stat().st_mtime_ns == before_mtime


def test_h_a_a_failure_on_a_later_batch_still_saves_nothing(tmp_path):
    """Partial success must not become a partially updated index."""

    target = tmp_path / "index.json"
    index_with(rows_for((1, "기존 답변입니다")), fingerprints=True).save(target)
    before = target.read_bytes()

    corpus = rows_for((1, "기존 답변입니다"), (2, "둘"), (3, "셋"), (4, "넷"))
    client = FakeClient(fail_on_call=2)
    saver = CountingSaver()

    with pytest.raises(RuntimeError):
        sync(corpus, index_path=target, client=client, save=saver,
             batch_size=1)

    assert client.calls == 2          # one succeeded, the next failed
    assert saver.calls == 0
    assert target.read_bytes() == before


# ------------------------------------------- I/J. model and dimension
def test_i_a_model_mismatch_refuses_to_update_incrementally(tmp_path):
    target = tmp_path / "index.json"
    corpus = rows_for((1, "기존 답변입니다"))
    index_with(corpus, fingerprints=True, model="text-embedding-ada-002"
               ).save(target)
    client = FakeClient(model=EMBEDDING_MODEL)
    saver = CountingSaver()

    result = sync(corpus + rows_for((2, "새 답변입니다")), index_path=target,
                  client=client, save=saver)

    assert result.full_rebuild_required is True
    assert "does not match" in result.full_rebuild_reason
    assert client.calls == 0
    assert saver.calls == 0


def test_j_a_dimension_mismatch_refuses_to_update_incrementally(tmp_path):
    target = tmp_path / "index.json"
    corpus = rows_for((1, "기존 답변입니다"))
    index_with(corpus, fingerprints=True, dimensions=8).save(target)
    client = FakeClient()
    saver = CountingSaver()

    result = sync(corpus + rows_for((2, "새 답변입니다")), index_path=target,
                  client=client, save=saver)

    assert result.full_rebuild_required is True
    assert "dimensional" in result.full_rebuild_reason
    assert client.calls == 0
    assert saver.calls == 0


def test_j_a_a_an_embedding_of_the_wrong_width_is_refused_not_stored(tmp_path):
    target = tmp_path / "index.json"
    index_with(rows_for((1, "기존 답변입니다")), fingerprints=True).save(target)
    before = target.read_bytes()
    saver = CountingSaver()

    with pytest.raises(ValueError, match="dimensional"):
        sync(rows_for((1, "기존 답변입니다"), (2, "새 답변입니다")),
             index_path=target, client=FakeClient(dimensions=8), save=saver)

    assert saver.calls == 0
    assert target.read_bytes() == before


# --------------------- embedding response cardinality (fail closed)
#
# ``build`` paired the batch with the response using ``zip``, which stops at
# the shorter side. A response one vector short therefore left a row
# unembedded while the result still reported the whole batch as embedded and
# saved -- a success that did not match the file. Measured before the fix: two
# rows in, one vector back, embedded_count 2, saved True, and only one vector
# on disk.


class MiscountingClient(FakeClient):
    """Returns the wrong NUMBER of vectors, each of the right width.

    Separate from a width mismatch on purpose: every vector here is valid, so
    nothing but the count can catch it.
    """

    def __init__(self, *, delta: int, **kwargs):
        super().__init__(**kwargs)
        self.delta = delta

    def embed(self, texts):
        vectors = super().embed(texts)
        if self.delta < 0:
            return vectors[:max(len(vectors) + self.delta, 0)]
        return vectors + [list(vectors[-1])] * self.delta


@pytest.mark.parametrize("delta,expected", [(-1, 1), (-2, 0), (1, 3), (2, 4)])
def test_a_response_of_the_wrong_length_is_refused_not_zipped(
        tmp_path, delta, expected):
    target = tmp_path / "index.json"
    existing = rows_for((9, "\uae30\uc874 \ub2f5\ubcc0\uc785\ub2c8\ub2e4"))
    index_with(existing, fingerprints=True, salt=0).save(target)
    before_bytes = target.read_bytes()
    before_mtime = target.stat().st_mtime_ns
    before = LearningSemanticIndex.load(target)

    corpus = existing + rows_for(
        (1, "\uc0c8 \ub2f5\ubcc0 \ud558\ub098"),
        (2, "\uc0c8 \ub2f5\ubcc0 \ub458"))
    saver = CountingSaver()

    with pytest.raises(ValueError) as caught:
        sync(corpus, index_path=target, client=MiscountingClient(delta=delta,
                                                                 salt=1),
             known_row_ids={1, 2, 9}, save=saver)

    assert str(caught.value) == (
        "embedding returned %d vectors for 2 texts" % expected)
    assert saver.calls == 0
    assert target.read_bytes() == before_bytes
    assert target.stat().st_mtime_ns == before_mtime

    after = LearningSemanticIndex.load(target)
    assert sorted(after.vectors) == sorted(before.vectors) == [9]
    assert sorted(after.fingerprints) == sorted(before.fingerprints) == [9]
    assert after.vectors == before.vectors
    assert after.fingerprints == before.fingerprints


def test_a_response_of_the_right_length_still_works(tmp_path):
    target = tmp_path / "index.json"
    existing = rows_for((9, "\uae30\uc874 \ub2f5\ubcc0\uc785\ub2c8\ub2e4"))
    index_with(existing, fingerprints=True, salt=0).save(target)
    corpus = existing + rows_for(
        (1, "\uc0c8 \ub2f5\ubcc0 \ud558\ub098"),
        (2, "\uc0c8 \ub2f5\ubcc0 \ub458"))
    saver = CountingSaver()

    result = sync(corpus, index_path=target,
                  client=MiscountingClient(delta=0, salt=1),
                  known_row_ids={1, 2, 9}, save=saver)

    assert result.embedded_count == 2
    assert result.saved is True
    assert saver.calls == 1
    after = LearningSemanticIndex.load(target)
    assert sorted(after.vectors) == [1, 2, 9]
    assert sorted(after.fingerprints) == [1, 2, 9]
    # the reported count is the number that actually arrived on disk
    assert result.embedded_count == len(
        set(after.vectors) - {9})


def test_the_count_is_checked_per_batch_not_per_call(tmp_path):
    """A short response on a LATER batch must fail too.

    With batch_size=1 the first batch succeeds, so this also pins that an
    earlier success is not persisted when a later batch is wrong.
    """

    target = tmp_path / "index.json"
    existing = rows_for((9, "\uae30\uc874 \ub2f5\ubcc0\uc785\ub2c8\ub2e4"))
    index_with(existing, fingerprints=True, salt=0).save(target)
    before_bytes = target.read_bytes()

    class EmptyAfterFirst(FakeClient):
        def embed(self, texts):
            vectors = super().embed(texts)
            return vectors if self.calls == 1 else []

    corpus = existing + rows_for(
        (1, "\uc0c8 \ub2f5\ubcc0 \ud558\ub098"),
        (2, "\uc0c8 \ub2f5\ubcc0 \ub458"))
    client = EmptyAfterFirst(salt=1)
    saver = CountingSaver()

    with pytest.raises(ValueError, match="returned 0 vectors for 1 texts"):
        sync(corpus, index_path=target, client=client,
             known_row_ids={1, 2, 9}, save=saver, batch_size=1)

    assert client.calls == 2
    assert saver.calls == 0
    assert target.read_bytes() == before_bytes
    assert sorted(LearningSemanticIndex.load(target).vectors) == [9]


# ------------------------------------------------- K. legacy behaviour
#
# A vector written before fingerprints existed carries no record of the text
# it was built from. The default is to embed it again rather than assume:
# fourteen real rows were checked by re-embedding and all fourteen matched at
# cosine 0.99972 or better, but fourteen is not 1,025 and the file itself
# holds no provenance. Retrieval correctness outweighs the one-off cost of a
# measured baseline.


def test_k_legacy_vectors_are_re_embedded_by_default(tmp_path):
    target = tmp_path / "index.json"
    corpus = rows_for((1, "옛 답변입니다"), (2, "다른 옛 답변입니다"))
    index_with(corpus, fingerprints=False, salt=0).save(target)
    stored = dict(LearningSemanticIndex.load(target).vectors)
    assert LearningSemanticIndex.load(target).fingerprints == {}

    client = FakeClient(salt=1)          # a later call to the same endpoint
    saver = CountingSaver()
    result = sync(corpus, index_path=target, client=client, save=saver)

    assert set(result.plan.classification.values()) == {LEGACY_UNKNOWN}
    assert result.plan.to_embed == (1, 2)
    assert result.plan.to_bootstrap == ()
    assert result.bootstrapped_count == 0
    assert client.calls == 1
    assert sorted(client.texts) == sorted(
        [_text_for(corpus[0]), _text_for(corpus[1])])
    assert saver.calls == 1

    reloaded = LearningSemanticIndex.load(target)
    assert reloaded.fingerprints == {1: fingerprint_for(corpus[0]),
                                     2: fingerprint_for(corpus[1])}
    # the stored values are replaced, because they were never shown to match
    assert reloaded.vectors != stored


def test_k_a_a_the_module_default_is_reembed_in_one_place_only(tmp_path):
    """The engine, its callers and the CLI must not disagree.

    A default repeated per function is a default that drifts, and this one
    decides whether 1,025 vectors are trusted or verified.
    """

    import inspect

    assert sync_module.DEFAULT_LEGACY_POLICY == REEMBED
    assert sync_module.LEGACY_POLICIES == (BOOTSTRAP, REEMBED)
    for function in (sync_module.classify, sync_module.sync):
        default = inspect.signature(function).parameters[
            "legacy_policy"].default
        assert default is sync_module.DEFAULT_LEGACY_POLICY

    from scripts import rebuild_learning_semantic_index as script

    assert inspect.signature(script.incremental).parameters[
        "legacy_policy"].default is sync_module.DEFAULT_LEGACY_POLICY

    # and the CLI, which is the surface an operator actually uses
    parser = script.build_parser()
    assert parser.get_default("legacy_policy") is (
        sync_module.DEFAULT_LEGACY_POLICY)
    choices = [action.choices for action in parser._actions
               if action.dest == "legacy_policy"][0]
    assert tuple(choices) == sync_module.LEGACY_POLICIES


def test_k_b_bootstrap_is_still_available_when_asked_for_explicitly(tmp_path):
    """Kept for diagnosis: what a cheap adoption would have produced."""

    target = tmp_path / "index.json"
    corpus = rows_for((1, "옛 답변입니다"), (2, "다른 옛 답변입니다"))
    index_with(corpus, fingerprints=False, salt=0).save(target)
    stored = dict(LearningSemanticIndex.load(target).vectors)

    client, saver = FakeClient(salt=1), CountingSaver()
    result = sync(corpus, index_path=target, client=client, save=saver,
                  legacy_policy=BOOTSTRAP)

    assert result.plan.to_embed == ()
    assert result.plan.to_bootstrap == (1, 2)
    assert result.bootstrapped_count == 2
    assert client.calls == 0
    assert saver.calls == 1                  # metadata changed, so one write

    reloaded = LearningSemanticIndex.load(target)
    assert reloaded.vectors == stored        # bit-for-bit the old vectors
    assert reloaded.fingerprints == {1: fingerprint_for(corpus[0]),
                                     2: fingerprint_for(corpus[1])}


def test_k_c_a_clean_baseline_then_a_second_sync_does_nothing(tmp_path):
    """§7 C: the one-off cost is one-off."""

    target = tmp_path / "index.json"
    corpus = rows_for((1, "옛 답변입니다"), (2, "다른 옛 답변입니다"))
    index_with(corpus, fingerprints=False).save(target)

    first_client, first_saver = FakeClient(), CountingSaver()
    first = sync(corpus, index_path=target, client=first_client,
                 save=first_saver)
    assert first.embedded_count == 2
    assert first_saver.calls == 1
    settled = target.read_bytes()
    mtime = target.stat().st_mtime_ns

    client, saver = FakeClient(), CountingSaver()
    again = sync(corpus, index_path=target, client=client, save=saver)

    assert set(again.plan.classification.values()) == {FRESH}
    assert again.plan.fresh_count == 2
    assert again.changed is False
    assert again.saved is False
    assert saver.calls == 0
    assert client.calls == 0
    assert target.read_bytes() == settled
    assert target.stat().st_mtime_ns == mtime


def test_k_d_a_legacy_index_with_missing_rows_bootstraps_nothing(tmp_path):
    """§7 D: the shape of the real snapshot -- vectors present, rows missing.

    1,025 fingerprint-less vectors and 914 rows with none, in miniature: the
    default must put every one of them in the embed list and none of them in
    the bootstrap list.
    """

    target = tmp_path / "index.json"
    existing = rows_for((1, "옛 답변 하나"), (2, "옛 답변 둘"), (3, "옛 답변 셋"))
    index_with(existing, fingerprints=False).save(target)
    corpus = existing + rows_for((4, "새 답변 하나"), (5, "새 답변 둘"))

    plan = classify(corpus, LearningSemanticIndex.load(target),
                    known_row_ids=[1, 2, 3, 4, 5])

    assert plan.legacy_unknown_count == 3
    assert plan.missing_count == 2
    assert plan.fresh_count == 0
    assert plan.stale_count == 0
    assert plan.to_bootstrap == ()
    assert plan.to_embed == (1, 2, 3, 4, 5)
    assert plan.to_remove == ()
    assert plan.changed is True


def test_k_e_a_legacy_vector_is_still_dropped_when_it_stops_being_eligible(
        tmp_path):
    target = tmp_path / "index.json"
    corpus = rows_for((1, "남습니다"), (2, "빠집니다"))
    index_with(corpus, fingerprints=False).save(target)

    client, saver = FakeClient(), CountingSaver()
    result = sync(corpus[:1], index_path=target, client=client, save=saver,
                  known_row_ids=[1, 2])

    assert result.plan.to_remove == (2,)
    reloaded = LearningSemanticIndex.load(target)
    assert set(reloaded.vectors) == {1}
    assert set(reloaded.fingerprints) == {1}


def test_k_f_removal_alone_still_makes_no_call_on_a_legacy_index(tmp_path):
    """Dropping a vector never needs the endpoint, whatever the policy."""

    target = tmp_path / "index.json"
    corpus = rows_for((1, "남습니다"), (2, "빠집니다"))
    index_with(corpus, fingerprints=False).save(target)

    client, saver = FakeClient(), CountingSaver()
    result = sync(corpus[:1], index_path=target, client=client, save=saver,
                  known_row_ids=[1, 2], legacy_policy=BOOTSTRAP)

    assert result.plan.to_embed == ()
    assert client.calls == 0
    assert result.removed_count == 1


# ------------------------- legacy_policy validation (fail closed)
#
# The branch on the policy used to be "REEMBED or else bootstrap", so every
# unrecognised value -- a typo, an empty string, a wrongly-cased "REEMBED" --
# silently selected the policy that keeps unverified vectors. That is the
# behaviour the default was deliberately changed away from, reachable by
# accident, and through sync() it wrote fingerprints to the live file.


@pytest.mark.parametrize("bad", ["typo", "", "REEMBED", "Bootstrap",
                                 "bootstrap ", None, 0, True])
def test_classify_refuses_a_legacy_policy_it_does_not_recognise(bad):
    corpus = rows_for((1, "여분 답변입니다"))
    index = index_with(corpus, fingerprints=False)

    with pytest.raises(ValueError) as caught:
        classify(corpus, index, legacy_policy=bad)

    message = str(caught.value)
    assert "invalid legacy_policy" in message
    assert repr(bad) in message
    assert "allowed values" in message
    for allowed in sync_module.LEGACY_POLICIES:
        assert repr(allowed) in message


def test_an_unrecognised_policy_reaches_neither_the_endpoint_nor_the_file(
        tmp_path):
    """Fail closed: nothing embedded, nothing written, file untouched."""

    target = tmp_path / "index.json"
    corpus = rows_for((1, "여분 답변입니다"),
                      (2, "다른 답변입니다"))
    index_with(corpus, fingerprints=False).save(target)
    before_bytes = target.read_bytes()
    before_mtime = target.stat().st_mtime_ns
    client, saver = FakeClient(salt=1), CountingSaver()

    with pytest.raises(ValueError, match="invalid legacy_policy"):
        sync(corpus, index_path=target, client=client, save=saver,
             legacy_policy="typo")

    assert client.calls == 0
    assert saver.calls == 0
    assert target.read_bytes() == before_bytes
    assert target.stat().st_mtime_ns == before_mtime
    assert LearningSemanticIndex.load(target).fingerprints == {}


@pytest.mark.parametrize("good", ["bootstrap", "reembed"])
def test_both_recognised_policies_are_accepted(tmp_path, good):
    target = tmp_path / "index.json"
    corpus = rows_for((1, "여분 답변입니다"))
    index_with(corpus, fingerprints=False, salt=0).save(target)
    stored = dict(LearningSemanticIndex.load(target).vectors)

    result = sync(corpus, index_path=target, client=FakeClient(salt=1),
                  save=CountingSaver(), legacy_policy=good)

    assert result.plan.classification[1] == LEGACY_UNKNOWN
    if good == REEMBED:
        assert result.plan.to_embed == (1,)
        assert result.plan.to_bootstrap == ()
        assert LearningSemanticIndex.load(target).vectors != stored
    else:
        assert result.plan.to_embed == ()
        assert result.plan.to_bootstrap == (1,)
        assert LearningSemanticIndex.load(target).vectors == stored
    assert LearningSemanticIndex.load(target).fingerprints == {
        1: fingerprint_for(corpus[0])}


def test_the_unqualified_default_is_still_reembed(tmp_path):
    target = tmp_path / "index.json"
    corpus = rows_for((1, "여분 답변입니다"))
    index_with(corpus, fingerprints=False).save(target)

    result = sync(corpus, index_path=target, client=FakeClient(salt=1),
                  save=CountingSaver())

    assert result.plan.to_embed == (1,)
    assert result.plan.to_bootstrap == ()


# ------------------- incremental preflight: which problem is reported
#
# An index built by a different model cannot be extended incrementally, and no
# API key changes that. The preflight used to compare the index against its
# OWN recorded model -- which always matches -- so a model mismatch slipped
# through and the run was reported as merely missing a key.


def _tiny_corpus_db(directory, rows):
    import sqlite3

    path = directory / "learning.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE learning_examples (id INTEGER PRIMARY KEY, "
        "question_original_masked TEXT, final_answer TEXT, active INTEGER)")
    connection.executemany(
        "INSERT INTO learning_examples VALUES (?,?,?,1)",
        [(int(r["id"]), r["question"], r["answer"]) for r in rows])
    connection.commit()
    connection.close()
    return path


def test_a_model_mismatch_outranks_a_missing_api_key(tmp_path, monkeypatch):
    from scripts import rebuild_learning_semantic_index as script

    corpus = rows_for((1, "여분 답변입니다"),
                      (2, "새 답변입니다"))
    database = _tiny_corpus_db(tmp_path, corpus)
    target = tmp_path / "index.json"
    index_with(corpus[:1], fingerprints=True, model="old-model").save(target)
    before_bytes = target.read_bytes()
    before_mtime = target.stat().st_mtime_ns
    monkeypatch.delenv("QNA_GPT_API_KEY", raising=False)

    outcome = script.incremental(database, target)

    assert outcome["full_rebuild_required"] is True
    assert "does not match" in outcome["full_rebuild_reason"]
    assert outcome["index_model"] == "old-model"
    assert outcome["configured_model"] == EMBEDDING_MODEL
    assert outcome["embedded"] == 0
    assert outcome["saved"] is False
    assert "blocked" not in outcome          # not diagnosed as a key problem
    assert target.read_bytes() == before_bytes
    assert target.stat().st_mtime_ns == before_mtime


def test_a_model_mismatch_is_reported_with_a_key_present_too(
        tmp_path, monkeypatch):
    from scripts import rebuild_learning_semantic_index as script

    corpus = rows_for((1, "여분 답변입니다"),
                      (2, "새 답변입니다"))
    database = _tiny_corpus_db(tmp_path, corpus)
    target = tmp_path / "index.json"
    index_with(corpus[:1], fingerprints=True, model="old-model").save(target)
    before_bytes = target.read_bytes()
    monkeypatch.setenv("QNA_GPT_API_KEY", "sk-not-used-because-it-refuses")

    outcome = script.incremental(database, target)

    assert outcome["full_rebuild_required"] is True
    assert outcome["embedded"] == 0
    assert outcome["saved"] is False
    assert target.read_bytes() == before_bytes


def test_a_missing_key_alone_is_still_reported_as_a_missing_key(
        tmp_path, monkeypatch):
    from scripts import rebuild_learning_semantic_index as script

    corpus = rows_for((1, "여분 답변입니다"),
                      (2, "새 답변입니다"))
    database = _tiny_corpus_db(tmp_path, corpus)
    target = tmp_path / "index.json"
    index_with(corpus[:1], fingerprints=True).save(target)
    before_bytes = target.read_bytes()
    monkeypatch.delenv("QNA_GPT_API_KEY", raising=False)

    outcome = script.incremental(database, target)

    assert outcome["blocked"] == "QNA_GPT_API_KEY is not set"
    assert outcome["would_embed"] == 1
    assert outcome["full_rebuild_required"] is False
    assert target.read_bytes() == before_bytes


def test_the_cli_exit_codes_separate_the_two_states(tmp_path, monkeypatch,
                                                    capsys):
    """1 for an index that needs a full rebuild, 2 for a missing key."""

    from scripts import rebuild_learning_semantic_index as script

    corpus = rows_for((1, "여분 답변입니다"),
                      (2, "새 답변입니다"))
    database = _tiny_corpus_db(tmp_path, corpus)
    monkeypatch.delenv("QNA_GPT_API_KEY", raising=False)

    mismatched = tmp_path / "old.json"
    index_with(corpus[:1], fingerprints=True,
               model="old-model").save(mismatched)
    assert script.main(["--incremental", "--database", str(database),
                        "--index", str(mismatched)]) == 1

    same = tmp_path / "same.json"
    index_with(corpus[:1], fingerprints=True).save(same)
    assert script.main(["--incremental", "--database", str(database),
                        "--index", str(same)]) == 2

    settled = tmp_path / "settled.json"
    index_with(corpus, fingerprints=True).save(settled)
    before = settled.stat().st_mtime_ns
    assert script.main(["--incremental", "--database", str(database),
                        "--index", str(settled)]) == 0
    assert settled.stat().st_mtime_ns == before       # no-op wrote nothing


# ----------------------------------------- L. the 319062-shaped case
def test_l_a_row_approved_after_the_last_rebuild_is_missing_then_fresh(
        tmp_path):
    """The shape of Learning 319062: in the corpus, absent from the index.

    It is the case the whole step exists for -- a row approved after the last
    manual rebuild has no vector, so it cannot be found by meaning however
    good an answer it is.
    """

    target = tmp_path / "index.json"
    older = rows_for((319061, "이전에 색인된 답변입니다"))
    index_with(older, fingerprints=True).save(target)
    golden = row(319062, "터치스크린 기능도 있나요?",
                 "터치 기능은 지원하지 않는 제품 입니다.")
    corpus = older + [golden]

    client, saver = FakeClient(), CountingSaver()
    first = sync(corpus, index_path=target, client=client, save=saver)

    assert first.plan.classification[319062] == MISSING
    assert first.plan.to_embed == (319062,)
    assert client.texts == [_text_for(golden)]
    assert first.saved is True

    reloaded = LearningSemanticIndex.load(target)
    assert 319062 in reloaded.vectors
    assert len(reloaded.vectors[319062]) == DIMENSIONS
    assert reloaded.fingerprints[319062] == fingerprint_for(golden)

    second_client, second_saver = FakeClient(), CountingSaver()
    second = sync(corpus, index_path=target, client=second_client,
                  save=second_saver)

    assert second.plan.classification[319062] == FRESH
    assert second_client.calls == 0
    assert second_saver.calls == 0
    assert second.saved is False


# ---------------------------------------------- M. reuse is bit-identical
def test_m_fresh_vectors_are_byte_identical_across_a_sync(tmp_path):
    """Reuse means the stored vector, not an equal-looking one.

    The endpoint does not reproduce a vector exactly for the same text --
    measured cosine 0.99972-1.0 on real rows -- so "recompute and it will
    match" is false. The stored values have to survive untouched.
    """

    target = tmp_path / "index.json"
    keep = rows_for((1, "유지되는 답변입니다"), (2, "이것도 유지됩니다"))
    index_with(keep, fingerprints=True, salt=0).save(target)
    before = {i: list(v) for i, v in
              LearningSemanticIndex.load(target).vectors.items()}
    raw_before = json.loads(target.read_text(encoding="utf-8"))["vectors"]

    corpus = keep + rows_for((3, "새로 들어온 답변입니다"))
    # A client that answers differently for the same text, so "kept" cannot
    # be mistaken for "recomputed and happened to match".
    sync(corpus, index_path=target, client=FakeClient(salt=1),
         save=CountingSaver())

    after = LearningSemanticIndex.load(target)
    raw_after = json.loads(target.read_text(encoding="utf-8"))["vectors"]
    for identifier in (1, 2):
        assert after.vectors[identifier] == before[identifier]
        assert raw_after[str(identifier)] == raw_before[str(identifier)]


# --------------------------------------------- N/O. schema compatibility
def test_n_an_index_written_before_fingerprints_loads_normally(tmp_path):
    target = tmp_path / "index.json"
    corpus = rows_for((1, "옛 답변입니다"), (2, "다른 옛 답변입니다"))
    built = index_with(corpus, fingerprints=True)
    # exactly the four-key document every previous version produced
    target.write_text(json.dumps({
        "model": EMBEDDING_MODEL,
        "dimensions": EMBEDDING_DIMENSIONS,
        "count": len(built.vectors),
        "vectors": {str(k): v for k, v in built.vectors.items()},
    }, ensure_ascii=False), encoding="utf-8")

    loaded = LearningSemanticIndex.load(target)

    assert loaded.vectors == built.vectors
    assert loaded.fingerprints == {}
    assert loaded.available is True
    assert loaded.model == EMBEDDING_MODEL
    hits = loaded.similar(built.vectors[1], limit=2)
    assert hits[0][0] == 1


def test_n_a_an_index_with_no_fingerprints_saves_the_legacy_four_keys(
        tmp_path):
    """Adopting this code must not rewrite an index that has nothing new.

    The fingerprints key is omitted when empty, so a save of a
    fingerprint-less index is byte-identical to what the old code wrote.
    """

    target = tmp_path / "index.json"
    built = index_with(rows_for((1, "답변입니다")), fingerprints=False)
    built.save(target)

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert sorted(payload) == ["count", "dimensions", "model", "vectors"]
    assert target.read_bytes() == json.dumps({
        "model": built.model,
        "dimensions": EMBEDDING_DIMENSIONS,
        "count": len(built.vectors),
        "vectors": {str(k): v for k, v in built.vectors.items()},
    }, ensure_ascii=False).encode("utf-8")


def test_o_an_index_with_fingerprints_round_trips_and_still_searches(tmp_path):
    target = tmp_path / "index.json"
    corpus = rows_for((1, "첫 답변입니다"), (2, "둘째 답변입니다"))
    built = index_with(corpus, fingerprints=True)
    built.save(target)

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert sorted(payload) == ["count", "dimensions", "fingerprints", "model",
                               "vectors"]
    assert payload["fingerprints"] == {
        "1": fingerprint_for(corpus[0]), "2": fingerprint_for(corpus[1])}

    loaded = LearningSemanticIndex.load(target)
    assert loaded.vectors == built.vectors
    assert loaded.fingerprints == built.fingerprints
    assert loaded.similar(built.vectors[2], limit=1)[0][0] == 2


def test_o_a_a_reader_that_ignores_fingerprints_still_gets_the_vectors(
        tmp_path):
    """What an older deployment would see in a new file."""

    target = tmp_path / "index.json"
    built = index_with(rows_for((1, "답변입니다")), fingerprints=True)
    built.save(target)

    payload = json.loads(target.read_text(encoding="utf-8"))
    as_old_reader_would = {
        int(key): value for key, value in (payload.get("vectors") or {}).items()
    }
    assert as_old_reader_would == built.vectors
    assert payload["count"] == len(built.vectors)
    assert payload["dimensions"] == EMBEDDING_DIMENSIONS


# ---------------------------------------------------------- P. remove only
def test_p_a_removal_only_sync_makes_no_embedding_call(tmp_path):
    target = tmp_path / "index.json"
    corpus = rows_for((1, "남습니다"), (2, "빠집니다"), (3, "이것도 빠집니다"))
    index_with(corpus, fingerprints=True).save(target)
    client, saver = FakeClient(), CountingSaver()

    result = sync(corpus[:1], index_path=target, client=client,
                  known_row_ids=[1, 2, 3], save=saver)

    assert result.plan.to_embed == ()
    assert result.plan.to_remove == (2, 3)
    assert client.calls == 0
    assert result.embedded_count == 0
    assert result.removed_count == 2
    assert saver.calls == 1
    assert set(LearningSemanticIndex.load(target).vectors) == {1}


# ------------------------------------------- Q. atomic save contract kept
def test_q_the_sync_saves_through_the_atomic_path(tmp_path, monkeypatch):
    """The engine must not acquire its own way of writing the file."""

    target = tmp_path / "index.json"
    index_with(rows_for((1, "기존 답변입니다")), fingerprints=True).save(target)
    seen: dict[str, object] = {}
    import tempfile as tempfile_module
    genuine = tempfile_module.mkstemp

    def recording_mkstemp(*args, **kwargs):
        seen.update(kwargs)
        return genuine(*args, **kwargs)

    monkeypatch.setattr(tempfile_module, "mkstemp", recording_mkstemp)
    sync(rows_for((1, "기존 답변입니다"), (2, "새 답변입니다")),
         index_path=target, client=FakeClient())

    assert seen["dir"] == str(target.parent)
    assert str(seen["prefix"]).startswith(target.name)
    assert sorted(p.name for p in tmp_path.iterdir()) == [target.name]
    assert set(LearningSemanticIndex.load(target).vectors) == {1, 2}


def test_q_a_a_row_with_nothing_to_embed_is_not_indexed(tmp_path):
    target = tmp_path / "index.json"
    corpus = [row(1, "질문 있습니까", "답변입니다"), row(2, "", "")]

    result = sync(corpus, index_path=target, client=FakeClient(),
                  save=CountingSaver())

    assert result.plan.no_text_count == 1
    assert result.plan.classification[2] == sync_module.NO_TEXT
    assert set(LearningSemanticIndex.load(target).vectors) == {1}


# ------------------------------------------------- classify is side-effect free
def test_classify_touches_neither_the_network_nor_the_disk(tmp_path):
    target = tmp_path / "index.json"
    corpus = rows_for((1, "하나"), (2, "둘"))
    index_with(corpus[:1], fingerprints=True).save(target)
    before = target.read_bytes()

    plan = classify(corpus, LearningSemanticIndex.load(target),
                    known_row_ids=[1, 2])

    assert plan.missing_count == 1
    assert plan.fresh_count == 1
    assert plan.changed is True
    assert target.read_bytes() == before
