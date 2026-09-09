"""Who is eligible to be a Golden case, and how the corpus is shaped.

Read-only throughout: the production copy is opened with ``mode=ro`` and this
module never writes anywhere.

The population is not "every inquiry". A Golden case has to support a
*comparison*, and that needs two things the corpus does not always have:

* something to replay -- the question and the product, which every row has; and
* something to compare against that is **not** this program's own output.

The second is what narrows it. The seller answer actually posted to Naver is
the one independent record of what the right reply looked like, so an inquiry
that has one can carry a real label; an inquiry that has only a draft this
program wrote can only ever be scored against itself.

Both populations are reported rather than one being silently chosen, because
which one a metric is computed over changes what the number means.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services.product_fact_guard import extract_model_code

# The current pipeline stamps this on every draft it writes. A draft without
# it came from the pre-GPT-first code and its stored decisions are not a
# baseline for the code running now.
CURRENT_PIPELINE = "GPT_UNDERSTAND_RETRIEVE_ANSWER"


def connect_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


@dataclass
class PopulationRow:
    inquiry_id: int
    source_question_id: str
    question: str
    product_name: str
    source_type: str
    registered_at: str
    production_answer: str | None
    draft_id: int | None
    draft_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def has_current_pipeline_draft(self) -> bool:
        hybrid = self.draft_metadata.get("hybrid")
        return (
            isinstance(hybrid, dict)
            and hybrid.get("answer_pipeline") == CURRENT_PIPELINE
        )

    @property
    def understanding(self) -> dict[str, Any]:
        routing = self.draft_metadata.get("semantic_routing")
        routing = routing if isinstance(routing, dict) else {}
        value = routing.get("understanding")
        return value if isinstance(value, dict) else {}

    @property
    def replayable_understanding(self) -> bool:
        """Can GPT ① be replayed from storage instead of called again?

        Only the compacted contract is persisted, not the full semantic
        payload, so this asks whether the compaction still carries an
        understanding: usable, and at least one atom with an action.
        """

        understanding = self.understanding
        questions = understanding.get("questions") or []
        return bool(
            understanding.get("usable") is True
            and questions
            and all(str(item.get("action") or "").strip() for item in questions)
        )


def load(path: Path) -> list[PopulationRow]:
    """Every inquiry that could in principle be replayed, with its context."""

    connection = connect_readonly(path)
    try:
        rows = connection.execute(
            """
            SELECT i.id, i.source_question_id, i.content, i.product_name,
                   i.source_type, i.registered_at,
                   p.answer_body AS production_answer,
                   d.id AS draft_id, d.metadata_json
              FROM inquiries i
              LEFT JOIN naver_posted_answers p
                     ON p.inquiry_id = i.id AND p.is_current = 1
              LEFT JOIN answer_drafts d
                     ON d.inquiry_id = i.id AND d.is_active = 1
             WHERE COALESCE(i.content, '') <> ''
               AND COALESCE(i.product_name, '') <> ''
            """
        ).fetchall()
    finally:
        connection.close()

    population: list[PopulationRow] = []
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        population.append(
            PopulationRow(
                inquiry_id=int(row["id"]),
                source_question_id=str(row["source_question_id"] or ""),
                question=str(row["content"] or ""),
                product_name=str(row["product_name"] or ""),
                source_type=str(row["source_type"] or ""),
                registered_at=str(row["registered_at"] or ""),
                production_answer=(
                    str(row["production_answer"])
                    if row["production_answer"]
                    else None
                ),
                draft_id=int(row["draft_id"]) if row["draft_id"] else None,
                draft_metadata=metadata if isinstance(metadata, dict) else {},
            )
        )
    return population


def stratum_of(row: PopulationRow, *, catalog_status: str) -> dict[str, Any]:
    """The axes a Golden sample has to stay balanced across.

    Read from stored production data wherever it exists. ``catalog_status`` is
    passed in rather than looked up here because resolving it means loading the
    catalogue, which the caller does once for the whole corpus.
    """

    understanding = row.understanding
    questions = understanding.get("questions") or []
    actions = [
        str(item.get("action") or "").upper()
        for item in questions
        if str(item.get("action") or "").strip()
    ]
    trace = row.draft_metadata.get("pipeline_trace")
    trace = trace if isinstance(trace, dict) else {}
    retrieval = trace.get("retrieval") if isinstance(trace.get("retrieval"), dict) else {}
    decision = row.draft_metadata.get("production_decision_trace")
    decision = decision if isinstance(decision, dict) else {}
    evidence = retrieval.get("subquestion_evidence") or []

    atom_count = len(questions)
    return {
        # A. meaning
        "primary_action": actions[0] if actions else "UNOBSERVED",
        "actions": sorted(set(actions)),
        # B. product identity -- the axis the audit found at 30% NOT_FOUND
        "product_identity": catalog_status,
        "model_code_in_name": bool(extract_model_code(row.product_name)),
        # C. compound
        "atom_bucket": (
            "UNOBSERVED" if not atom_count
            else "1" if atom_count == 1
            else "2" if atom_count == 2
            else "3+"
        ),
        # D. order / DPS
        "need_order": understanding.get("need_order"),
        "need_dps": understanding.get("need_dps"),
        "need_product": understanding.get("need_product"),
        # E. evidence availability, as the stored run saw it
        "learning_selected": retrieval.get("learning_selected"),
        "verified_product_facts": retrieval.get("verified_product_facts"),
        "evidence_statuses": sorted({
            str(item.get("status") or "") for item in evidence
            if isinstance(item, dict)
        }),
        # F. outcome
        "eligibility": decision.get("eligibility"),
        "blocking_reasons": list(decision.get("blocking_reason_codes") or []),
        # provenance of the row itself
        "source_type": row.source_type,
        "has_production_answer": row.production_answer is not None,
        "has_current_pipeline_draft": row.has_current_pipeline_draft,
        "replayable_understanding": row.replayable_understanding,
    }


def summarise(rows: list[PopulationRow], strata: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """Counts a human can check the sampling against."""

    def tally(key: str) -> list[tuple[Any, int]]:
        return Counter(
            str(strata[row.inquiry_id].get(key)) for row in rows
        ).most_common()

    with_answer = [row for row in rows if row.production_answer]
    current = [row for row in rows if row.has_current_pipeline_draft]
    replayable = [row for row in current if row.replayable_understanding]
    return {
        "total_replayable_inquiries": len(rows),
        "with_production_answer": len(with_answer),
        "with_active_draft": len([r for r in rows if r.draft_id]),
        "with_current_pipeline_draft": len(current),
        "with_replayable_understanding": len(replayable),
        "by_source_type": tally("source_type"),
        "by_product_identity": tally("product_identity"),
        "by_model_code_in_name": tally("model_code_in_name"),
        "by_atom_bucket": tally("atom_bucket"),
        "by_primary_action": tally("primary_action"),
        "by_eligibility": tally("eligibility"),
    }
