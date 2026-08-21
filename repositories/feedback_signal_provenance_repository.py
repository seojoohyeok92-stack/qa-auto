from __future__ import annotations

from typing import Any

from answer.evidence_support import answer_support_recall, coverage_label
from answer.learning_signal import FACTUAL_SIGNAL_KINDS
from repositories.database import Database


class FeedbackSignalProvenanceRepository:
    """Tracks Structured Learning Signals actually attached to generation context.

    Mirrors ``LearningProvenanceRepository`` (retrieval -> attachment ->
    usage) but for GOOD_PATTERN/BAD_PATTERN/CORRECTION/VERIFIED_FACT signals,
    which live in a separate table from Learning/Historical references.
    """

    def __init__(self, database: Database) -> None:
        self.database = database

    def record_context(
        self, *, inquiry_id: int, context_run_id: str | None, signals: list[dict[str, Any]]
    ) -> str | None:
        if not signals:
            return None
        run_id = context_run_id
        rows: list[tuple[Any, ...]] = []
        for item in signals:
            signal_id = item.get("signal_id")
            if signal_id is None:
                continue
            support = float(item.get("answer_support") or 0)
            kind = str(item.get("signal_kind") or "")
            usage_status = (
                "PENDING" if kind in {k.value for k in FACTUAL_SIGNAL_KINDS}
                else "NOT_APPLICABLE"
            )
            rows.append((
                run_id, int(inquiry_id), int(signal_id), kind,
                str(item.get("source_label") or kind),
                str(item.get("matched_subquestion") or "") or None,
                float(item.get("relevance") or 0), support,
                coverage_label(support),
                1 if item.get("conflict") else 0,
                usage_status,
            ))
        if not rows:
            return None
        with self.database.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO answer_feedback_signal_provenance(
                    context_run_id, inquiry_id, learning_signal_id, signal_kind,
                    source_label, matched_subquestion, relevance,
                    answer_support_score, evidence_coverage, conflict_detected,
                    usage_status, included_in_prompt
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                rows,
            )
        return run_id

    def attach_latest_context(self, *, inquiry_id: int, draft_id: int) -> int:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE answer_feedback_signal_provenance
                SET answer_draft_id=?
                WHERE inquiry_id=? AND answer_draft_id IS NULL
                  AND context_run_id=(
                    SELECT context_run_id FROM answer_feedback_signal_provenance
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
        hybrid = result_metadata.get("hybrid")
        hybrid = hybrid if isinstance(hybrid, dict) else {}
        generated = hybrid.get("draft")
        generated = generated if isinstance(generated, dict) else {}
        validation = hybrid.get("validation")
        validation = validation if isinstance(validation, dict) else {}
        fallback_used = bool(hybrid.get("fallback_used"))
        final_answer_text = str(generated.get("answer") or "")
        usage: dict[int, dict[str, Any]] = {}
        reported = generated.get("feedback_signal_usage")
        for item in (reported if isinstance(reported, list) else []):
            if not isinstance(item, dict) or item.get("signal_id") is None:
                continue
            usage[int(item["signal_id"])] = item
        with self.database.transaction() as connection:
            rows = connection.execute(
                """
                SELECT p.id, p.learning_signal_id, p.signal_kind,
                       p.conflict_detected, ls.content_text
                FROM answer_feedback_signal_provenance p
                LEFT JOIN learning_signals ls ON ls.id=p.learning_signal_id
                WHERE p.answer_draft_id=? AND p.included_in_prompt=1
                """,
                (int(draft_id),),
            ).fetchall()
            for row in rows:
                signal_kind = str(row["signal_kind"] or "")
                is_factual = signal_kind in {k.value for k in FACTUAL_SIGNAL_KINDS}
                if not is_factual:
                    connection.execute(
                        """
                        UPDATE answer_feedback_signal_provenance
                        SET usage_status='NOT_APPLICABLE',
                            usage_reason='GUIDANCE_SIGNAL_NOT_FACT_CHECKED',
                            evaluated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                        WHERE id=?
                        """,
                        (int(row["id"]),),
                    )
                    continue
                signal_id = int(row["learning_signal_id"])
                item = usage.get(signal_id)
                reason = str((item or {}).get("reason") or "")[:500]
                if bool(row["conflict_detected"]):
                    status = "REJECTED_CONFLICT"
                elif item is not None and item.get("answer_supported"):
                    status = "USED"
                elif fallback_used or not bool(validation.get("passed", True)):
                    status = "REJECTED_LOW_CONFIDENCE"
                else:
                    status = "NOT_USED"
                content_text = str(row["content_text"] or "")
                system_support = (
                    answer_support_recall(content_text, final_answer_text)
                    if content_text and final_answer_text
                    else 0.0
                )
                system_verified_usage = (
                    "CONFIRMED" if system_support >= 0.5 else "UNCONFIRMED"
                )
                connection.execute(
                    """
                    UPDATE answer_feedback_signal_provenance
                    SET usage_status=?, usage_reason=?,
                        provider_claimed_usage=?, system_verified_usage=?,
                        evaluated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                    WHERE id=?
                    """,
                    (
                        status,
                        reason or "PROVIDER_DID_NOT_USE_SIGNAL",
                        1 if (item is not None and item.get("answer_supported")) else 0,
                        system_verified_usage,
                        int(row["id"]),
                    ),
                )
        return len(rows)

    def for_draft(self, draft_id: int) -> list[dict[str, Any]]:
        """Include each signal's original-platform inquiry number, mirroring
        ``LearningProvenanceRepository.for_draft`` -- see its docstring."""

        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT p.*, ls.content_text, ls.question_masked,
                       ls.product_scope, ls.origin_kind,
                       ls.product_identity_json, ls.learning_example_id,
                       ls.historical_case_id,
                       si.external_inquiry_id AS signal_external_inquiry_id,
                       si.source_question_id AS signal_source_question_id,
                       hc.external_inquiry_id AS historical_external_inquiry_id,
                       hc.question AS historical_question,
                       hc.product_name AS historical_product_name
                FROM answer_feedback_signal_provenance p
                LEFT JOIN learning_signals ls ON ls.id=p.learning_signal_id
                LEFT JOIN inquiries si ON si.id=ls.inquiry_id
                LEFT JOIN historical_cases hc ON hc.id=ls.historical_case_id
                WHERE p.answer_draft_id=? AND p.included_in_prompt=1
                ORDER BY p.relevance DESC, p.id
                """,
                (int(draft_id),),
            ).fetchall()
        return [dict(row) for row in rows]
