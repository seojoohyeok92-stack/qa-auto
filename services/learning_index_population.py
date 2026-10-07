"""Which Learning rows the semantic index covers. One definition, two callers.

The rebuild CLI and the post-commit hook must agree exactly about this. They
do not share a connection -- the CLI opens the file read-only by path, the
hook borrows the service's ``Database`` -- so what is shared here is the
query and the filter, not the plumbing. A second copy of either would be a
bug that stays invisible until the two drift, and the symptom would be the
hook quietly removing vectors the CLI considers eligible.

Eligibility is deliberately the index one and nothing more: a row belongs
when it is active and has text to embed. Quality, provenance, validity and
product compatibility are decided later, by the stages that decide them for
every other candidate. In particular ``validity_active`` is NOT consulted
here, so invalidating a Learning does not remove its vector -- that is the
existing behaviour and this module does not change it.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from services.learning_semantic_index import _text_for

# Exactly the columns the embedding text is built from, named as _text_for
# expects them.
ELIGIBLE_SQL = """
    SELECT id, question_original_masked AS question,
           final_answer AS answer
    FROM learning_examples
    WHERE active=1
"""

# Every id in the table, eligible or not. The sync needs it to tell a vector
# whose row stopped being eligible from a vector whose row no longer exists.
ALL_IDS_SQL = "SELECT id FROM learning_examples"


def eligible_from(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Apply the half of eligibility that SQL cannot express.

    A row with neither a question nor an answer has nothing to embed, and the
    index module would produce an empty string for it.

    Each row is converted to a dict BEFORE the filter: ``sqlite3.Row`` has no
    ``get``, which is what ``_text_for`` uses, and a cursor can only be walked
    once.
    """

    converted = [dict(row) for row in rows]
    return [row for row in converted if _text_for(row)]


def eligible_rows(connection: Any) -> list[dict[str, Any]]:
    """The index population, read through whatever connection is given."""

    return eligible_from(connection.execute(ELIGIBLE_SQL))


def all_row_ids(connection: Any) -> set[int]:
    return {int(row[0]) for row in connection.execute(ALL_IDS_SQL)}
