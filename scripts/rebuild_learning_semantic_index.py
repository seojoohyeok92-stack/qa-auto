"""Bring the derived semantic index back in line with the Learning corpus.

The index is derived data: ``learning_examples`` is the source of truth and
this file is an embedding of it. Nothing in the runtime writes it -- retrieval
loads it and tolerates its absence -- so it only ever changes when somebody
runs this. That is exactly how twenty rows went missing: every Learning
approved after the last build had no vector, and the newest rows are the ones
an operator most wants found.

The modes:

``--report``
    A read-only health check: coverage and the gap, with no network call, no
    key and no write. Answers "how stale is it" and nothing else.

``--incremental``
    The repair. Embeds rows that have no vector and rows whose embedded text
    has changed since it was indexed, drops vectors whose rows are gone or no
    longer eligible, and -- when none of those apply -- does not write the
    file at all. "Changed" is decided by the stored text fingerprint, not by
    ``updated_at``, which moves whenever a row is merely retrieved. This is
    the mode a periodic repair should call.

``--full``
    Re-embeds every eligible row, for a change of embedding model. Note that
    re-embedding is not free of effect: the endpoint does not reproduce a
    vector exactly, so this moves every vector slightly and with it every
    ranking.

The bare rebuild (no flag) is the original behaviour, kept for compatibility:
it embeds only rows with no vector and therefore never notices an edit.
``--incremental`` is the one that does.

Eligibility is deliberately the retrieval one and nothing more: a row is in the
index when it is active and carries text to embed. Quality, provenance and
product compatibility are decided later, by the same stages that decide them
for every other candidate -- an index that pre-judged them would be a filter
wearing a cache's clothes.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.learning_semantic_index import (  # noqa: E402
    DEFAULT_INDEX_PATH, EMBEDDING_DIMENSIONS, EMBEDDING_MODEL, API_KEY_ENV,
    EmbeddingClient, LearningSemanticIndex, _text_for,
)
from services import learning_index_population  # noqa: E402
from services import learning_semantic_sync  # noqa: E402

DEFAULT_DB = ROOT / "data" / "oje_automation.db"


def _read_only(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        "file:%s?mode=ro" % database.resolve().as_posix(), uri=True
    )
    connection.row_factory = sqlite3.Row
    return connection


def eligible_rows(database: Path) -> list[dict[str, Any]]:
    """Active Learning that has something to embed. Read-only, always.

    The query and the filter come from ``learning_index_population`` so that
    this CLI and the post-commit hook cover exactly the same rows. They do not
    share a connection -- this one is deliberately read-only -- but a second
    definition of the population would be a bug that only shows up when the
    two disagree, and the symptom would be the hook removing vectors this
    considers eligible.
    """

    connection = _read_only(database)
    try:
        return learning_index_population.eligible_rows(connection)
    finally:
        connection.close()


def all_row_ids(database: Path) -> set[int]:
    """Every Learning id, eligible or not. Read-only, always.

    The incremental sync needs this to tell a vector whose row stopped being
    eligible from a vector whose row no longer exists. Without it both are
    simply "not eligible any more", which is enough to drop them but not
    enough to report honestly.
    """

    connection = _read_only(database)
    try:
        return learning_index_population.all_row_ids(connection)
    finally:
        connection.close()


def incremental(database: Path, index_path: Path, *,
                legacy_policy: str = learning_semantic_sync.DEFAULT_LEGACY_POLICY,
                ) -> dict[str, Any]:
    """Embed only what is missing or changed, and write only if something is.

    Shares this module's eligibility and the index module's text helper with
    the engine rather than restating either: a second opinion about which
    rows belong in the index would be a bug nobody sees until the two
    disagree.
    """

    rows = eligible_rows(database)
    known = all_row_ids(database)
    index = LearningSemanticIndex.load(index_path)
    # The configured model is passed explicitly. Without it classify compares
    # the index against its own recorded model, which always matches, so an
    # index built by a different model passed preflight and was then reported
    # as merely missing an API key -- the wrong diagnosis for a state no key
    # can fix.
    plan = learning_semantic_sync.classify(
        rows, index, known_row_ids=known, legacy_policy=legacy_policy,
        model=EMBEDDING_MODEL)
    if plan.full_rebuild_required:
        # Checked before the key, because an incompatible index is the more
        # fundamental problem and holding a key would not change it. Nothing
        # is embedded and nothing is written either way.
        return {
            "full_rebuild_required": True,
            "full_rebuild_reason": plan.full_rebuild_reason,
            "index_model": index.model,
            "configured_model": EMBEDDING_MODEL,
            "eligible": plan.eligible_count,
            "existing": plan.existing_count,
            "embedded": 0,
            "saved": False,
        }
    if plan.to_embed and not os.environ.get(API_KEY_ENV):
        # Said plainly, the way --full says it, instead of surfacing as a
        # traceback from inside the client. Nothing has been written: the
        # plan is all that has been computed.
        return {
            "blocked": "%s is not set" % API_KEY_ENV,
            "would_embed": len(plan.to_embed),
            "would_remove": len(plan.to_remove),
            "would_bootstrap": len(plan.to_bootstrap),
            "full_rebuild_required": plan.full_rebuild_required,
            "full_rebuild_reason": plan.full_rebuild_reason,
        }
    result = learning_semantic_sync.sync(
        rows,
        index=index,
        index_path=index_path,
        known_row_ids=known,
        legacy_policy=legacy_policy,
    )
    plan = result.plan
    return {
        "eligible": plan.eligible_count,
        "existing": plan.existing_count,
        "missing": plan.missing_count,
        "fresh": plan.fresh_count,
        "stale": plan.stale_count,
        "legacy_unknown": plan.legacy_unknown_count,
        "ineligible_existing": plan.ineligible_existing_count,
        "orphan": plan.orphan_count,
        "no_text": plan.no_text_count,
        "embedded": result.embedded_count,
        "reused": result.reused_count,
        "removed": result.removed_count,
        "bootstrapped": result.bootstrapped_count,
        "vectors_before": result.vectors_before,
        "vectors_after": result.vectors_after,
        "fingerprints_before": result.fingerprints_before,
        "fingerprints_after": result.fingerprints_after,
        "embedding_requests": result.embedding_requests,
        "embedding_tokens": result.embedding_tokens,
        "changed": result.changed,
        "saved": result.saved,
        "model": result.model,
        "full_rebuild_required": result.full_rebuild_required,
        "full_rebuild_reason": result.full_rebuild_reason,
    }


def coverage(database: Path, index_path: Path) -> dict[str, Any]:
    rows = eligible_rows(database)
    index = LearningSemanticIndex.load(index_path)
    eligible = {int(row["id"]) for row in rows}
    indexed = set(index.vectors)
    missing = sorted(eligible - indexed)
    return {
        "eligible": len(eligible),
        "indexed": len(eligible & indexed),
        "vectors": len(indexed),
        "missing": len(missing),
        "missing_ids": missing[:50],
        "stale": len(indexed - eligible),
        "coverage_pct": round(100.0 * len(eligible & indexed) / max(len(eligible), 1), 2),
        "model": index.model,
        "dimensions": EMBEDDING_DIMENSIONS,
    }


def rebuild(database: Path, index_path: Path, *, full: bool) -> dict[str, Any]:
    rows = eligible_rows(database)
    index = LearningSemanticIndex.load(index_path)
    eligible = {int(row["id"]) for row in rows}
    todo = rows if full else [
        row for row in rows if int(row["id"]) not in index.vectors
    ]
    before = len(index.vectors)
    if todo:
        client = EmbeddingClient()
        built = LearningSemanticIndex.build(todo, client=client)
        index = built if full else index.merge(built)
    # Rows that are no longer active keep their row and lose their vector.
    index = index.drop(set(index.vectors) - eligible)
    index.save(index_path)
    return {
        "embedded": len(todo),
        "vectors_before": before,
        "vectors_after": len(index.vectors),
        "eligible": len(eligible),
        "model": index.model,
    }


def build_parser() -> argparse.ArgumentParser:
    """Built here rather than inside main so the defaults are testable.

    The legacy policy default decides whether 1,025 existing vectors are
    trusted or verified, so it has one home (``learning_semantic_sync.
    DEFAULT_LEGACY_POLICY``) and the CLI reads it rather than restating it.
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    parser.add_argument("--index", type=Path, default=ROOT / DEFAULT_INDEX_PATH)
    parser.add_argument(
        "--report", action="store_true",
        help="coverage only; no embedding call and no key required",
    )
    parser.add_argument(
        "--full", action="store_true",
        help="re-embed every eligible row, not only the ones with no vector",
    )
    parser.add_argument(
        "--incremental", action="store_true",
        help="embed only rows that are missing or whose text changed, drop "
             "rows that are no longer eligible, and write nothing if neither "
             "applies",
    )
    parser.add_argument(
        "--legacy-policy", choices=learning_semantic_sync.LEGACY_POLICIES,
        default=learning_semantic_sync.DEFAULT_LEGACY_POLICY,
        help="what to do with vectors written before fingerprints existed: "
             "'reembed' (default) embeds them again so every vector is known "
             "to match the text it is stored against; 'bootstrap' keeps them "
             "and records their current text hash without checking it",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)

    if arguments.report:
        print(json.dumps(coverage(arguments.database, arguments.index),
                         ensure_ascii=False, indent=2))
        return 0
    if arguments.incremental:
        # No key gate here on purpose: a sync with nothing missing and
        # nothing stale makes no request at all, and a repair that refused to
        # report "already current" without a key would be useless on a
        # schedule. If an embedding *is* needed the client says so, before
        # anything is written.
        outcome = incremental(arguments.database, arguments.index,
                              legacy_policy=arguments.legacy_policy)
        print(json.dumps(outcome, ensure_ascii=False, indent=2))
        if outcome.get("blocked"):
            print("%s. %d row(s) need embedding from the %s endpoint."
                  % (outcome["blocked"], outcome["would_embed"],
                     EMBEDDING_MODEL), file=sys.stderr)
            return 2
        return 1 if outcome["full_rebuild_required"] else 0
    if not os.environ.get(API_KEY_ENV):
        # Said plainly rather than raised from inside the client: this script
        # is the one place an operator finds out what a rebuild costs.
        print("%s is not set. A rebuild calls the %s embeddings endpoint."
              % (API_KEY_ENV, EMBEDDING_MODEL), file=sys.stderr)
        return 2
    print(json.dumps(rebuild(arguments.database, arguments.index,
                             full=arguments.full),
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
