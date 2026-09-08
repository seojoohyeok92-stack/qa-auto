"""Bring the derived semantic index back in line with the Learning corpus.

The index is derived data: ``learning_examples`` is the source of truth and
this file is an embedding of it. Nothing in the runtime writes it -- retrieval
loads it and tolerates its absence -- so it only ever changes when somebody
runs this. That is exactly how twenty rows went missing: every Learning
approved after the last build had no vector, and the newest rows are the ones
an operator most wants found.

``--report`` answers "how stale is it" without a network call or a key, and is
the mode to run on a schedule. A rebuild embeds only the rows that have no
vector yet and drops the vectors of rows that are no longer active, so the
usual cost is a handful of texts rather than the whole corpus; ``--full``
re-embeds everything, for a change of embedding model.

Eligibility is deliberately the retrieval one and nothing more: a row is in the
index when it is active and carries text to embed. Quality, provenance and
product compatibility are decided later, by the same stages that decide them
for every other candidate -- an index that pre-judged them would be a filter
wearing a cache's clothes.
"""
from __future__ import annotations

import argparse
import json
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

DEFAULT_DB = ROOT / "data" / "oje_automation.db"


def eligible_rows(database: Path) -> list[dict[str, Any]]:
    """Active Learning that has something to embed. Read-only, always."""

    connection = sqlite3.connect(
        "file:%s?mode=ro" % database.resolve().as_posix(), uri=True
    )
    connection.row_factory = sqlite3.Row
    try:
        rows = [
            dict(row) for row in connection.execute(
                """
                SELECT id, question_original_masked AS question,
                       final_answer AS answer
                FROM learning_examples
                WHERE active=1
                """
            )
        ]
    finally:
        connection.close()
    return [row for row in rows if _text_for(row)]


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


def main(argv: list[str] | None = None) -> int:
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
    arguments = parser.parse_args(argv)

    if arguments.report:
        print(json.dumps(coverage(arguments.database, arguments.index),
                         ensure_ascii=False, indent=2))
        return 0
    if not __import__("os").environ.get(API_KEY_ENV):
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
