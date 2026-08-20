from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

from repositories.database import Database
from services.historical_case_service import HistoricalCaseService
from services.historical_learning_quality_service import HistoricalLearningQualityService


def _rows(connection: sqlite3.Connection, sql: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, parameters).fetchall()]


def audit(database_path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(
        f"file:{database_path.resolve().as_posix()}?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    try:
        counts = {}
        queries = {
            "total_positive": """
                SELECT COUNT(*) FROM learning_examples
                WHERE COALESCE(json_extract(metadata_json, '$.learning_signal_type'), 'POSITIVE')='POSITIVE'
            """,
            "active_positive": """
                SELECT COUNT(*) FROM learning_examples
                WHERE active=1
                  AND COALESCE(json_extract(metadata_json, '$.learning_signal_type'), 'POSITIVE')='POSITIVE'
            """,
            "human_verified": """
                SELECT COUNT(*) FROM learning_examples
                WHERE COALESCE(json_extract(metadata_json, '$.learning_signal_type'), 'POSITIVE')='POSITIVE'
                  AND COALESCE(json_extract(metadata_json, '$.human_verified'), 0)=1
            """,
            "auto_non_human": """
                SELECT COUNT(*) FROM learning_examples
                WHERE COALESCE(json_extract(metadata_json, '$.learning_signal_type'), 'POSITIVE')='POSITIVE'
                  AND COALESCE(json_extract(metadata_json, '$.human_verified'), 0)=0
            """,
            "negative_active": """
                SELECT COUNT(*) FROM learning_feedback
                WHERE active=1 AND learning_signal_type='NEGATIVE'
            """,
            "excluded_all": """
                SELECT COUNT(*) FROM learning_feedback
                WHERE learning_signal_type='EXCLUDED'
            """,
            "excluded_active": """
                SELECT COUNT(*) FROM learning_feedback
                WHERE active=1 AND learning_signal_type='EXCLUDED'
            """,
            "intent_corrected_all": """
                SELECT COUNT(*) FROM learning_feedback
                WHERE learning_signal_type='INTENT_CORRECTION'
            """,
            "intent_corrected_active": """
                SELECT COUNT(*) FROM learning_feedback
                WHERE active=1 AND learning_signal_type='INTENT_CORRECTION'
            """,
        }
        for name, query in queries.items():
            counts[name] = int(connection.execute(query).fetchone()[0])

        cases = _rows(
            connection,
            """
            SELECT id AS historical_case_id, external_inquiry_id, inquiry_id,
                   store_code, inquiry_type,
                   question, seller_answer, inquiry_created_at, active,
                   quality_score, policy_risk, promoted_learning_id, metadata_json
            FROM historical_cases
            WHERE id IN (135, 166, 183)
               OR inquiry_id IN (135, 166, 183)
               OR question LIKE '%반품 박스%'
               OR question LIKE '%TV스탠드%'
               OR question LIKE '%완료로 되어%'
            ORDER BY id
            """,
        )
        inquiry_ids = tuple(
            sorted({int(row["inquiry_id"]) for row in cases if row["inquiry_id"]})
        )
        related_learning: list[dict[str, Any]] = []
        for row in cases:
            row["metadata_json"] = json.loads(row["metadata_json"] or "{}")
            row["runtime_assessment"] = HistoricalLearningQualityService().assess(
                question=str(row.get("question") or ""),
                answer=str(row.get("seller_answer") or ""),
                stored_quality=float(row.get("quality_score") or 0),
                policy_risk=str(row.get("policy_risk") or "NONE"),
                active=bool(row.get("active")),
            ).to_dict()
        if inquiry_ids:
            placeholders = ",".join("?" for _ in inquiry_ids)
            related_learning = _rows(
                connection,
                f"""
                SELECT id, inquiry_id, learning_source, active, validity_active,
                       question_original_masked, validator_result, created_at,
                       metadata_json
                FROM learning_examples
                WHERE inquiry_id IN ({placeholders})
                ORDER BY inquiry_id, id
                """,
                inquiry_ids,
            )
            for row in related_learning:
                row["metadata_json"] = json.loads(row["metadata_json"] or "{}")

        snapshot_path: Path | None = None
        traces: list[dict[str, Any]] = []
        try:
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as target:
                snapshot_path = Path(target.name)
            snapshot = sqlite3.connect(snapshot_path)
            try:
                connection.backup(snapshot)
            finally:
                snapshot.close()
            service = HistoricalCaseService(Database(snapshot_path))
            for case in cases:
                trace = service.search_detailed(
                    str(case.get("question") or ""),
                    store_code=str(case.get("store_code") or "") or None,
                    inquiry_type=str(case.get("inquiry_type") or "") or None,
                )
                case_id = int(case["historical_case_id"])
                selected_ids = {
                    int(item["id"]) for item in trace.get("selected", [])
                }
                rejected = next(
                    (
                        item for item in trace.get("rejected_samples", [])
                        if int(item["historical_case_id"]) == case_id
                    ),
                    None,
                )
                traces.append(
                    {
                        "historical_case_id": case_id,
                        "candidate_count": trace["candidate_count"],
                        "active_candidate_count": trace["candidate_count"],
                        "selected_count": trace["selected_count"],
                        "target_selected": case_id in selected_ids,
                        "target_rejection": rejected,
                        "rejection_counts": trace["rejection_counts"],
                    }
                )
        finally:
            if snapshot_path is not None:
                snapshot_path.unlink(missing_ok=True)

        return {
            "database": str(database_path),
            "schema_state": {
                "max_migration": int(connection.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0] or 0),
                "usage_status_column_present": bool(connection.execute(
                    """
                    SELECT 1 FROM pragma_table_info('answer_learning_provenance')
                    WHERE name='usage_status'
                    """
                ).fetchone()),
            },
            "counts": counts,
            "provenance_groups": _rows(
                connection,
                """
                SELECT learning_source,
                       COALESCE(json_extract(metadata_json, '$.source_origin'), '-') AS source_origin,
                       COALESCE(json_extract(metadata_json, '$.answer_provenance'), '-') AS answer_provenance,
                       COALESCE(json_extract(metadata_json, '$.human_verified'), 0) AS human_verified,
                       active, COUNT(*) AS count
                FROM learning_examples
                GROUP BY 1, 2, 3, 4, 5
                ORDER BY count DESC
                """,
            ),
            "cases": cases,
            "related_learning": related_learning,
            "retrieval_traces": traces,
        }
    finally:
        connection.close()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Read-only Learning lifecycle audit")
    parser.add_argument("database", nargs="?", default="data/oje_automation.db", type=Path)
    args = parser.parse_args()
    print(json.dumps(audit(args.database), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
