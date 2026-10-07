"""Bring the derived semantic index in line with the corpus, incrementally.

A full rebuild embeds every eligible row. That is the wrong shape for keeping
the index current: the corpus takes on roughly seven rows a day, so a full
pass spends a thousand embeddings to learn seven things, and -- measured --
re-embedding a row does not reproduce its vector bit for bit. Fourteen rows
re-embedded from unchanged text came back with cosine 0.99972 to 1.0 against
what was stored, differing by up to 2.5e-3 per component. So a full rebuild
does not merely cost more; it perturbs every vector in the corpus, and with
it every ranking, for no gain on the rows that did not change.

That argument is about the steady state, and it is where almost all the runs
are. The first run against an index written before fingerprints existed is
the exception: it re-embeds those rows once, deliberately, because nothing in
such a file records which text each vector came from and a cheaper adoption
would have to assume it. One measured baseline, then the steady state.

This module answers the narrower question instead: which rows does the index
not know, and which rows has it learned under text that has since changed.
The first is a missing key. The second is what ``fingerprint_for`` exists for,
because the obvious signal does not work -- ``updated_at`` moves when a row is
merely *retrieved*, and all 104 rows it flagged on the 26.10.4 snapshot were
usage bumps rather than edits.

What this module deliberately does not do: decide which rows are eligible, or
how a row becomes text. Both already have one home -- the rebuild script's
``eligible_rows`` and the index module's ``_text_for`` -- and a second
opinion about either would be a bug that is invisible until the two disagree.
Rows arrive as an argument, the way ``LearningSemanticIndex.build`` takes
them.
"""
from __future__ import annotations

import dataclasses
import pathlib
from typing import Any, Iterable, Mapping, Sequence

from services.learning_semantic_index import (
    BATCH_SIZE, EMBEDDING_DIMENSIONS, DEFAULT_INDEX_PATH, EmbeddingClient,
    LearningSemanticIndex, _text_for, fingerprint_for,
)

# How a row stands relative to the index.
MISSING = "MISSING"                  # eligible, no vector -> embed
FRESH = "FRESH"                      # vector, fingerprint matches -> reuse
STALE = "STALE"                      # vector, fingerprint differs -> re-embed
LEGACY_UNKNOWN = "LEGACY_UNKNOWN"    # vector, no fingerprint -> see below
INELIGIBLE_EXISTING = "INELIGIBLE_EXISTING"   # vector, no longer eligible
ORPHAN = "ORPHAN"                    # vector, no row at all
NO_TEXT = "NO_TEXT"                  # row carries nothing to embed

# What to do with a vector that predates fingerprints. ``reembed`` treats it
# as stale; ``bootstrap`` keeps the vector and records the row's current text
# hash beside it.
#
# ``reembed`` is the default, and the reason is provenance rather than cost.
# A bootstrap writes "this vector was built from this text" without having
# checked, and once written that claim reads as fresh forever. Fourteen
# indexed rows were re-embedded from their current text and compared with
# what the index held -- the most used, the most recently touched, the
# longest, and a random spread -- and every one came back at cosine 0.99972
# or better, which is the same text and numerical noise rather than a
# different text. But fourteen rows are not 1,025, and the evidence cannot be
# extended to the rest: nothing in the file records which text each vector
# came from, which is precisely the gap fingerprints close. Retrieval
# correctness is worth more here than the 1,025 embeddings a clean baseline
# costs, so the baseline is established by measurement instead of assumption.
#
# This is a once-per-index cost, not a policy of rebuilding. After the first
# clean repair every row carries a fingerprint, so a sync embeds only what is
# new or edited and writes nothing when neither applies.
#
# ``bootstrap`` stays available as an explicit choice -- for diagnosis, and
# for comparing what a cheap adoption would have produced -- but nothing
# selects it by default.
BOOTSTRAP = "bootstrap"
REEMBED = "reembed"

# One home for the default, so the engine, its callers and the CLI cannot
# drift into disagreeing about what an unqualified sync does.
DEFAULT_LEGACY_POLICY = REEMBED
LEGACY_POLICIES = (BOOTSTRAP, REEMBED)


@dataclasses.dataclass(frozen=True)
class SyncPlan:
    """What a sync would do. Decided without a network call or a write."""

    classification: dict[int, str]
    to_embed: tuple[int, ...]
    to_remove: tuple[int, ...]
    to_bootstrap: tuple[int, ...]
    eligible_count: int
    existing_count: int
    missing_count: int
    fresh_count: int
    stale_count: int
    legacy_unknown_count: int
    ineligible_existing_count: int
    orphan_count: int
    no_text_count: int
    full_rebuild_required: bool = False
    full_rebuild_reason: str = ""

    @property
    def changed(self) -> bool:
        """Whether anything at all would be written.

        The bootstrap list counts: writing fingerprints for the first time
        changes the file even though no vector moves. The second identical
        sync has nothing left to bootstrap and so changes nothing.
        """

        return bool(self.to_embed or self.to_remove or self.to_bootstrap)


@dataclasses.dataclass(frozen=True)
class SyncResult:
    """What a sync did, in the terms an operator would ask about."""

    plan: SyncPlan
    embedded_count: int
    reused_count: int
    removed_count: int
    bootstrapped_count: int
    changed: bool
    saved: bool
    vectors_before: int
    vectors_after: int
    fingerprints_before: int
    fingerprints_after: int
    model: str
    dimensions: int
    embedding_requests: int
    embedding_tokens: int
    full_rebuild_required: bool = False
    full_rebuild_reason: str = ""


def classify(rows: Iterable[Mapping[str, Any]],
             index: LearningSemanticIndex,
             *,
             known_row_ids: Iterable[int] | None = None,
             legacy_policy: str = DEFAULT_LEGACY_POLICY,
             model: str | None = None) -> SyncPlan:
    """Compare the corpus with the index. Pure: no network, no file, no DB.

    ``rows`` is THE WHOLE ELIGIBLE POPULATION, exactly as the rebuild script
    defines it -- not "the rows that changed". Anything the index holds and
    ``rows`` does not mention is taken to have left the population, and is
    therefore scheduled for removal. Passing a subset is not a cheaper call;
    it is a different instruction.

        classify([one_row], index_holding_1000)  ->  999 removals

    So a caller that wants to react to a single edited row cannot reach that
    by handing this function only that row. Either the full population is
    passed, or a separate targeted path is needed. STEP 2-2 implements no
    targeted path, and the write-hook step decides which of the two to use.

    ``known_row_ids`` is every id in the table, which is what separates a
    vector whose row stopped being eligible from a vector whose row is gone
    entirely; without it the two are reported together as ineligible, because
    the distinction cannot be invented here.
    """

    # Checked before anything else, because the branch below used to treat
    # "anything that is not REEMBED" as BOOTSTRAP. A typo, an empty string or
    # a wrongly-cased "REEMBED" therefore selected the policy that keeps
    # unverified vectors -- the exact behaviour the default was changed away
    # from, reachable by accident. Failing closed is the only safe direction:
    # a caller who cannot spell the policy has not chosen one.
    if legacy_policy not in LEGACY_POLICIES:
        raise ValueError(
            "invalid legacy_policy %r; allowed values are %s"
            % (legacy_policy, ", ".join(repr(p) for p in LEGACY_POLICIES)))

    eligible: dict[int, str] = {}
    no_text: list[int] = []
    for row in rows:
        if row.get("id") is None:
            continue
        identifier = int(row["id"])
        if not _text_for(row):
            no_text.append(identifier)
            continue
        eligible[identifier] = fingerprint_for(row)

    indexed = set(index.vectors)
    classification: dict[int, str] = {}
    to_embed: list[int] = []
    to_bootstrap: list[int] = []
    fresh = stale = legacy = 0

    for identifier, current in sorted(eligible.items()):
        if identifier not in indexed:
            classification[identifier] = MISSING
            to_embed.append(identifier)
            continue
        stored = index.fingerprints.get(identifier)
        if stored is None:
            classification[identifier] = LEGACY_UNKNOWN
            legacy += 1
            # Exhaustive over LEGACY_POLICIES, which the guard above has
            # already narrowed the value to. No fall-through.
            if legacy_policy == REEMBED:
                to_embed.append(identifier)
            elif legacy_policy == BOOTSTRAP:
                to_bootstrap.append(identifier)
        elif stored == current:
            classification[identifier] = FRESH
            fresh += 1
        else:
            classification[identifier] = STALE
            stale += 1
            to_embed.append(identifier)

    known = None if known_row_ids is None else {int(i) for i in known_row_ids}
    to_remove: list[int] = []
    ineligible = orphans = 0
    for identifier in sorted(indexed - set(eligible)):
        if known is not None and identifier not in known:
            classification[identifier] = ORPHAN
            orphans += 1
        else:
            classification[identifier] = INELIGIBLE_EXISTING
            ineligible += 1
        to_remove.append(identifier)

    for identifier in no_text:
        classification.setdefault(identifier, NO_TEXT)

    # A vector built by a different model, or of a different width, cannot sit
    # beside a new one: cosine would compare two unrelated spaces and return a
    # number anyway. So this is reported as a state, not worked around.
    wanted_model = model or index.model
    reason = ""
    if indexed and index.model != wanted_model:
        reason = ("index model %r does not match the configured model %r"
                  % (index.model, wanted_model))
    else:
        widths = {len(vector) for vector in index.vectors.values()}
        if widths - {EMBEDDING_DIMENSIONS}:
            reason = ("index holds %s-dimensional vectors, configured %d"
                      % (sorted(widths), EMBEDDING_DIMENSIONS))

    return SyncPlan(
        classification=classification,
        to_embed=tuple(sorted(to_embed)),
        to_remove=tuple(to_remove),
        to_bootstrap=tuple(to_bootstrap),
        eligible_count=len(eligible),
        existing_count=len(indexed),
        missing_count=sum(1 for kind in classification.values() if kind == MISSING),
        fresh_count=fresh,
        stale_count=stale,
        legacy_unknown_count=legacy,
        ineligible_existing_count=ineligible,
        orphan_count=orphans,
        no_text_count=len(no_text),
        full_rebuild_required=bool(reason),
        full_rebuild_reason=reason,
    )


def sync(rows: Sequence[Mapping[str, Any]],
         *,
         index_path: pathlib.Path | str = DEFAULT_INDEX_PATH,
         index: LearningSemanticIndex | None = None,
         client: EmbeddingClient | None = None,
         known_row_ids: Iterable[int] | None = None,
         legacy_policy: str = DEFAULT_LEGACY_POLICY,
         batch_size: int = BATCH_SIZE,
         save: Any = None) -> SyncResult:
    """Embed what is missing or changed, drop what is gone, save once.

    ``rows`` is the whole eligible population, with the consequence spelled
    out in ``classify``: ids the index holds and ``rows`` omits are REMOVED.
    Calling this with one freshly-saved row would empty the index of
    everything else, so it is not a single-row hook and must not be used as
    one.

    Every embedding is obtained before the index is touched, so a failure
    part way through a batch sequence leaves the file exactly as it was
    rather than half updated. And when there is nothing to do the file is not
    written at all -- no rewrite of tens of megabytes, no ``os.replace``, no
    new mtime, so no reader reloads. That matters more than it looks: a
    periodic repair that rewrote the index every time it found nothing would
    cost every running process a fresh parse, hourly, forever.
    """

    rows = list(rows)
    index = LearningSemanticIndex.load(index_path) if index is None else index
    client = client or EmbeddingClient()
    saver = save if save is not None else LearningSemanticIndex.save
    if not index.vectors:
        # Nothing stored yet, so there is no existing space to stay
        # compatible with and the label should be whatever is about to fill
        # it -- otherwise a first run under a new model records the old name.
        index = LearningSemanticIndex({}, model=client.model)

    plan = classify(rows, index, known_row_ids=known_row_ids,
                    legacy_policy=legacy_policy, model=client.model)

    def result(*, embedded=0, removed=0, bootstrapped=0, after=None,
               saved=False) -> SyncResult:
        final = index if after is None else after
        return SyncResult(
            plan=plan, embedded_count=embedded,
            reused_count=plan.fresh_count + len(plan.to_bootstrap),
            removed_count=removed, bootstrapped_count=bootstrapped,
            changed=plan.changed, saved=saved,
            vectors_before=len(index.vectors),
            vectors_after=len(final.vectors),
            fingerprints_before=len(index.fingerprints),
            fingerprints_after=len(final.fingerprints),
            model=index.model, dimensions=EMBEDDING_DIMENSIONS,
            embedding_requests=client.calls, embedding_tokens=client.tokens,
            full_rebuild_required=plan.full_rebuild_required,
            full_rebuild_reason=plan.full_rebuild_reason,
        )

    if plan.full_rebuild_required:
        return result()
    if not plan.changed:
        return result()

    by_id = {int(row["id"]): row for row in rows if row.get("id") is not None}
    updated = index
    if plan.to_embed:
        # One index built from the rows that need embedding, then folded in.
        # build() raises on a failed request before anything is folded, which
        # is what keeps the file untouched on a partial failure.
        embedded_index = LearningSemanticIndex.build(
            [by_id[identifier] for identifier in plan.to_embed],
            client=client, batch_size=batch_size)
        widths = {len(vector) for vector in embedded_index.vectors.values()}
        if widths - {EMBEDDING_DIMENSIONS}:
            # Refusing beats saving: a vector of the wrong width still returns
            # a number from cosine, so a mixed index is wrong quietly.
            raise ValueError(
                "embedding returned %s-dimensional vectors, expected %d"
                % (sorted(widths), EMBEDDING_DIMENSIONS))
        updated = updated.merge(embedded_index)
    if plan.to_remove:
        updated = updated.drop(plan.to_remove)
    if plan.to_bootstrap:
        # The vector is kept exactly as stored -- never recomputed, never
        # renormalised -- and only the fingerprint it was missing is recorded.
        prints = dict(updated.fingerprints)
        for identifier in plan.to_bootstrap:
            prints[identifier] = fingerprint_for(by_id[identifier])
        updated = LearningSemanticIndex(updated.vectors, model=updated.model,
                                        fingerprints=prints)

    saver(updated, index_path)
    return result(embedded=len(plan.to_embed), removed=len(plan.to_remove),
                  bootstrapped=len(plan.to_bootstrap), after=updated,
                  saved=True)
