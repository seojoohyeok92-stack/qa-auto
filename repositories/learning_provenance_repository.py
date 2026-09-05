from __future__ import annotations

import uuid
from typing import Any

from answer.evidence_support import answer_support_recall, coverage_label
from repositories.database import Database


class LearningProvenanceRepository:
    """Tracks references that were actually included in generation context."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def record_context(
        self, *, inquiry_id: int,
        learning: list[dict[str, Any]], historical: list[dict[str, Any]],
        context_run_id: str | None = None,
    ) -> str | None:
        rows: list[tuple[Any, ...]] = []
        run_id = context_run_id or str(uuid.uuid4())
        for item in learning:
            reference_id = item.get("learning_example_id")
            if reference_id is None:
                continue
            support = float(item.get("answer_support") or 0)
            rows.append((
                run_id, int(inquiry_id), "LEARNING", int(reference_id), None,
                str(item.get("learning_source") or "LEARNING"),
                float(item.get("relevance") or 0),
                support, coverage_label(support),
            ))
        for item in historical:
            reference_id = item.get("historical_case_id")
            if reference_id is None:
                continue
            support = float(item.get("answer_support") or 0)
            rows.append((
                run_id, int(inquiry_id), "HISTORICAL", None, int(reference_id),
                str(item.get("source") or "HISTORICAL_VERIFIED_LEARNING"),
                float(item.get("relevance") or 0),
                support, coverage_label(support),
            ))
        if not rows:
            return None
        with self.database.transaction() as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO answer_learning_provenance(
                    context_run_id, inquiry_id, reference_kind,
                    learning_example_id, historical_case_id,
                    source_label, relevance, answer_support_score,
                    evidence_coverage, included_in_prompt
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                rows,
            )
        return run_id

    def attach_latest_context(self, *, inquiry_id: int, draft_id: int) -> int:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE answer_learning_provenance
                SET answer_draft_id=?
                WHERE inquiry_id=? AND answer_draft_id IS NULL
                  AND context_run_id=(
                    SELECT context_run_id FROM answer_learning_provenance
                    WHERE inquiry_id=? AND answer_draft_id IS NULL
                    ORDER BY created_at DESC, id DESC LIMIT 1
                  )
                """,
                (int(draft_id), int(inquiry_id), int(inquiry_id)),
            )
        return int(cursor.rowcount)

    def finalize_for_draft(
        self, *, draft_id: int, result_metadata: dict[str, Any]
    ) -> int:
        """Persist actual use separately from retrieval and prompt attachment."""

        hybrid = result_metadata.get("hybrid")
        hybrid = hybrid if isinstance(hybrid, dict) else {}
        generated = hybrid.get("draft")
        generated = generated if isinstance(generated, dict) else {}
        validation = hybrid.get("validation")
        validation = validation if isinstance(validation, dict) else {}
        fallback_used = bool(hybrid.get("fallback_used"))
        final_answer_text = str(generated.get("answer") or "")
        usage: dict[tuple[str, int], dict[str, Any]] = {}
        for kind, field, identifier in (
            ("LEARNING", "learning_usage", "learning_id"),
            ("HISTORICAL", "historical_usage", "historical_case_id"),
        ):
            values = generated.get(field)
            for item in (values if isinstance(values, list) else []):
                if not isinstance(item, dict) or item.get(identifier) is None:
                    continue
                usage[(kind, int(item[identifier]))] = item
        with self.database.transaction() as connection:
            rows = connection.execute(
                """
                SELECT p.id, p.reference_kind, p.learning_example_id,
                       p.historical_case_id,
                       le.final_answer AS learning_answer_text,
                       hc.seller_answer AS historical_answer_text
                FROM answer_learning_provenance p
                LEFT JOIN learning_examples le ON le.id=p.learning_example_id
                LEFT JOIN historical_cases hc ON hc.id=p.historical_case_id
                WHERE p.answer_draft_id=? AND p.included_in_prompt=1
                """,
                (int(draft_id),),
            ).fetchall()
            for row in rows:
                reference_id = int(
                    row["learning_example_id"]
                    if row["reference_kind"] == "LEARNING"
                    else row["historical_case_id"]
                )
                item = usage.get((str(row["reference_kind"]), reference_id))
                reason = str((item or {}).get("reason") or "")[:500]
                if item is not None and item.get("answer_supported"):
                    status = "USED"
                elif "CURRENT" in reason.upper() or "DPS" in reason.upper():
                    status = "BLOCKED_BY_CURRENT_FACT"
                elif "CONFLICT" in reason.upper():
                    status = "REJECTED_CONFLICT"
                elif fallback_used or not bool(validation.get("passed", True)):
                    status = "REJECTED_LOW_CONFIDENCE"
                else:
                    status = "NOT_USED"
                # System-verified usage is independent of the provider's own
                # learning_usage/historical_usage self-report: it checks
                # whether the reference's own answer content actually shows
                # up in the final generated answer, using the same
                # Answer-Support signal as retrieval re-ranking.  This is a
                # heuristic (paraphrasing can under-count), not a semantic
                # entailment check -- see answer/evidence_support.py.
                reference_answer_text = str(
                    row["learning_answer_text"]
                    if row["reference_kind"] == "LEARNING"
                    else row["historical_answer_text"]
                    or ""
                )
                system_support = (
                    answer_support_recall(
                        reference_answer_text, final_answer_text
                    )
                    if reference_answer_text and final_answer_text
                    else 0.0
                )
                system_verified_usage = (
                    "CONFIRMED" if system_support >= 0.5 else "UNCONFIRMED"
                )
                connection.execute(
                    """
                    UPDATE answer_learning_provenance
                    SET usage_status=?, usage_reason=?, matched_subquestion=?,
                        provider_claimed_usage=?, system_verified_usage=?,
                        evaluated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                    WHERE id=?
                    """,
                    (
                        status,
                        reason or (
                            "FINAL_FALLBACK_OR_VALIDATOR_REJECTION"
                            if status == "REJECTED_LOW_CONFIDENCE"
                            else "PROVIDER_DID_NOT_USE_REFERENCE"
                        ),
                        str((item or {}).get("matched_subquestion") or "")[:1000]
                        or None,
                        1 if (item is not None and item.get("answer_supported")) else 0,
                        system_verified_usage,
                        int(row["id"]),
                    ),
                )
            used_learning_ids = [
                int(row["learning_example_id"])
                for row in rows
                if row["reference_kind"] == "LEARNING"
                and usage.get(("LEARNING", int(row["learning_example_id"])), {}).get(
                    "answer_supported"
                )
            ]
            if used_learning_ids:
                placeholders = ",".join("?" for _ in used_learning_ids)
                connection.execute(
                    f"""
                    UPDATE learning_examples
                    SET usage_count=usage_count+1,
                        last_used_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                        updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                    WHERE id IN ({placeholders}) AND active=1
                    """,
                    used_learning_ids,
                )
        return len(rows)

    def for_draft(self, draft_id: int) -> list[dict[str, Any]]:
        """Include each reference's original inquiry number and stored answer.

        Internal PKs (``learning_example_id``/``historical_case_id``) are
        preserved untouched for DB/debug traceability; these extra columns
        only let the Dashboard *display* the original Naver inquiry number
        instead, since operators track sources by that number, not by
        internal row id (many Learning rows have no order number to show).

        The answers are here because the Dashboard listed which sources were
        selected and what they were asked, and never what they answered. An
        operator checking a retrieval could see that a bracket-compatibility
        inquiry pulled two plausible-looking sources, and could not tell
        whether retrieval had gone wrong or the stored answers simply did not
        settle the question. The columns are the ones the prompt itself reads:
        ``final_answer`` for Learning (learning_context_service) and
        ``seller_answer`` for Historical, so what an operator reads is what the
        model was given.
        """

        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT p.*,
                       le.learning_source, le.metadata_json AS learning_metadata,
                       le.question_original_masked AS learning_question,
                       le.final_answer AS learning_answer,
                       le.product_name AS learning_product_name,
                       li.external_inquiry_id AS learning_external_inquiry_id,
                       li.source_question_id AS learning_source_question_id,
                       hc.question AS historical_question,
                       hc.seller_answer AS historical_answer,
                       hc.product_name AS historical_product_name,
                       hc.external_inquiry_id AS historical_external_inquiry_id,
                       hi.source_question_id AS historical_source_question_id
                FROM answer_learning_provenance p
                LEFT JOIN learning_examples le ON le.id=p.learning_example_id
                LEFT JOIN inquiries li ON li.id=le.inquiry_id
                LEFT JOIN historical_cases hc ON hc.id=p.historical_case_id
                LEFT JOIN inquiries hi ON hi.id=hc.inquiry_id
                WHERE p.answer_draft_id=? AND p.included_in_prompt=1
                ORDER BY p.relevance DESC, p.id
                """,
                (int(draft_id),),
            ).fetchall()
        return [dict(row) for row in rows]
