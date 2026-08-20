from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from answer.answer_format import extract_answer_body, format_final_answer
from services.historical_case_service import SUPPORTED_INQUIRY_TYPES, _seller_answer
from services.historical_learning_quality_service import HistoricalLearningQualityService
from services.historical_reaudit_service import HistoricalReauditService
from services.learning_privacy_service import LearningPrivacyService
from services.similar_answer_service import TOKEN, normalize_learning_question


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _rows(connection: sqlite3.Connection, sql: str, parameters=()) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, tuple(parameters)).fetchall()]


def _question(row: dict[str, Any]) -> str:
    return "\n".join(
        value for value in (
            str(row.get("title") or "").strip(),
            str(row.get("content") or "").strip(),
        ) if value
    ).strip()


def _normalized_answer(value: Any) -> str:
    return normalize_learning_question(extract_answer_body(str(value or "")))


def _tokens(value: str) -> set[str]:
    return {token.lower() for token in TOKEN.findall(value) if len(token) > 1}


def _near_duplicate(left: dict[str, Any], right: dict[str, Any]) -> tuple[bool, float, str]:
    left_q = str(left.get("question_normalized") or "")
    right_q = str(right.get("question_normalized") or "")
    left_a = str(left.get("answer_normalized") or "")
    right_a = str(right.get("answer_normalized") or "")
    if not left_q or not right_q or not left_a or not right_a:
        return False, 0.0, ""
    left_q_tokens, right_q_tokens = _tokens(left_q), _tokens(right_q)
    left_a_tokens, right_a_tokens = _tokens(left_a), _tokens(right_a)
    q_jaccard = len(left_q_tokens & right_q_tokens) / max(
        len(left_q_tokens | right_q_tokens), 1
    )
    a_jaccard = len(left_a_tokens & right_a_tokens) / max(
        len(left_a_tokens | right_a_tokens), 1
    )
    q_sequence = SequenceMatcher(None, left_q, right_q).ratio()
    a_sequence = SequenceMatcher(None, left_a, right_a).ratio()
    if (
        (q_jaccard < 0.35 or a_jaccard < 0.35)
        and (q_sequence < 0.58 or a_sequence < 0.72)
    ):
        return False, round((q_jaccard + a_jaccard) / 2, 4), ""
    score = (
        0.10 * q_jaccard + 0.10 * a_jaccard
        + 0.35 * q_sequence + 0.45 * a_sequence
    )
    matched = bool(
        (q_jaccard >= 0.72 and a_jaccard >= 0.66)
        or (q_sequence >= 0.58 and a_sequence >= 0.85)
        or score >= 0.75
    )
    return matched, round(score, 4), (
        f"q_jaccard={q_jaccard:.3f},a_jaccard={a_jaccard:.3f},"
        f"q_sequence={q_sequence:.3f},a_sequence={a_sequence:.3f}"
    )


def _category(question: str, answer: str) -> str:
    concepts = set(HistoricalLearningQualityService.concepts(question + "\n" + answer))
    for concept, label in (
        ("RETURN", "취소/교환/반품"),
        ("AFTER_SERVICE", "A/S"),
        ("PROMOTION", "결제/포인트"),
        ("PRODUCT_FUNCTION", "상품정보"),
        ("PRODUCT_OPTION", "상품정보"),
        ("INSTALLATION", "설치 일반정책"),
        ("DELIVERY_STATUS", "배송 일반정책"),
    ):
        if concept in concepts:
            return label
    return "기타"


def _sample_summary(row: dict[str, Any]) -> dict[str, Any]:
    decision = row["decision"]
    product = decision["product_metadata"]
    return {
        "source_inquiry_id": row["source_inquiry_id"],
        "external_inquiry_id": row["external_inquiry_id"],
        "historical_case_id": row["historical_case_id"],
        "category": row["category"],
        "question_summary": " ".join(row["question"].split())[:180],
        "answer_summary": " ".join(row["answer"].split())[:220],
        "disposition": decision["disposition"],
        "primary_reason": decision["primary_reason"],
        "runtime_status": decision["runtime_status"],
        "provenance": product.get("provenance"),
        "product_name": product.get("product_name"),
        "product_id": product.get("product_id"),
        "model_code": product.get("model_code"),
        "duplicate": row.get("duplicate"),
    }


def _balanced_samples(rows: list[dict[str, Any]], limit: int = 20) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in sorted(
        rows,
        key=lambda item: (
            -float(item["decision"]["trust_score"]),
            item["source_inquiry_id"],
        ),
    ):
        buckets[row["category"]].append(row)
    selected: list[dict[str, Any]] = []
    categories = sorted(buckets)
    while len(selected) < limit and any(buckets.values()):
        for category in categories:
            if buckets[category] and len(selected) < limit:
                selected.append(_sample_summary(buckets[category].pop(0)))
    return selected


def _source_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in SUPPORTED_INQUIRY_TYPES)
    rows = _rows(
        connection,
        f"""
        SELECT i.*,
          (SELECT COALESCE(le.seller_answer, le.final_answer)
           FROM learning_examples le
           WHERE le.inquiry_id=i.id AND le.learning_source='SELLER_ANSWER'
           ORDER BY le.id DESC LIMIT 1) historical_seller_answer,
          (SELECT le.quality_score FROM learning_examples le
           WHERE le.inquiry_id=i.id AND le.learning_source='SELLER_ANSWER'
           ORDER BY le.id DESC LIMIT 1) historical_quality_score
        FROM inquiries i
        WHERE i.source_type IN ({placeholders})
        ORDER BY i.id
        """,
        SUPPORTED_INQUIRY_TYPES,
    )
    for row in rows:
        raw = _json(row.get("raw_json"))
        row["raw_json"] = raw
        row["question"] = _question(row)
        row["seller_answer"] = str(
            row.get("historical_seller_answer") or _seller_answer(raw) or ""
        ).strip()
        row["quality_score"] = float(row.get("historical_quality_score") or 0.0)
        row["source_inquiry_product"] = row.get("product_name")
        row["source_payload_reference"] = f"INQUIRY_DB:{row['id']}"
        row["active"] = True
        row["metadata_json"] = {}
    return rows


def _historical_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = _rows(connection, "SELECT * FROM historical_cases ORDER BY id")
    for row in rows:
        row["metadata_json"] = _json(row.get("metadata_json"))
        row["raw_json"] = _json(row.get("raw_json"))
        row["active"] = bool(row.get("active"))
    return rows


def _learning_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = _rows(
        connection,
        """
        SELECT * FROM learning_examples
        WHERE active=1 AND validity_active=1
          AND COALESCE(json_extract(metadata_json, '$.learning_signal_type'),
                       'POSITIVE')='POSITIVE'
        ORDER BY id
        """,
    )
    for row in rows:
        row["metadata_json"] = _json(row.get("metadata_json"))
        row["question"] = str(row.get("question_original_masked") or "").strip()
        row["answer"] = str(row.get("final_answer") or "").strip()
        row["question_normalized"] = normalize_learning_question(row["question"])
        row["answer_normalized"] = _normalized_answer(row["answer"])
    return rows


def _original_skip_reason(row: dict[str, Any], imported: bool) -> str:
    if not str(row.get("question") or "").strip():
        return "MISSING_QUESTION"
    if not str(row.get("seller_answer") or "").strip():
        return "MISSING_ANSWER"
    if not bool(row.get("source_answered")):
        return "SOURCE_NOT_ANSWERED"
    if imported:
        return "ALREADY_IMPORTED"
    return "UNKNOWN_LEGACY_REASON"


def _match_duplicate(
    record: dict[str, Any], learning: list[dict[str, Any]],
    exact: dict[tuple[str, str], list[dict[str, Any]]],
    normalized: dict[tuple[str, str], list[dict[str, Any]]],
) -> dict[str, Any] | None:
    question = str(record["decision"]["masked_question"] or "").strip()
    answer = str(record["decision"]["masked_answer"] or "").strip()
    question_normalized = normalize_learning_question(question)
    answer_normalized = _normalized_answer(answer)
    exact_hit = exact.get((question, answer))
    if exact_hit:
        return {
            "duplicate_type": "EXACT",
            "existing_learning_id": int(exact_hit[0]["id"]),
            "similarity": 1.0,
            "reason": "MASKED_QUESTION_AND_ANSWER_EXACT_MATCH",
        }
    normalized_hit = normalized.get((question_normalized, answer_normalized))
    if normalized_hit:
        return {
            "duplicate_type": "NORMALIZED",
            "existing_learning_id": int(normalized_hit[0]["id"]),
            "similarity": 1.0,
            "reason": "NORMALIZED_QUESTION_AND_ANSWER_MATCH",
        }
    probe = {
        "question_normalized": question_normalized,
        "answer_normalized": answer_normalized,
    }
    probe_concepts = set(HistoricalLearningQualityService.concepts(question))
    best: tuple[float, dict[str, Any], str] | None = None
    for candidate in learning:
        candidate_concepts = set(
            HistoricalLearningQualityService.concepts(candidate["question"])
        )
        if probe_concepts and candidate_concepts and not (
            probe_concepts & candidate_concepts
        ):
            continue
        matched, score, reason = _near_duplicate(probe, candidate)
        if matched and (best is None or score > best[0]):
            best = score, candidate, reason
    if best is None:
        return None
    return {
        "duplicate_type": "SEMANTIC_NEAR",
        "existing_learning_id": int(best[1]["id"]),
        "similarity": best[0],
        "reason": best[2],
    }


def audit(database_path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(
        f"file:{database_path.resolve().as_posix()}?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    privacy = LearningPrivacyService()
    reaudit = HistoricalReauditService()
    try:
        sources = _source_rows(connection)
        historical = _historical_rows(connection)
        learning = _learning_rows(connection)
        runs = _rows(
            connection,
            "SELECT * FROM historical_import_runs ORDER BY id",
        )
        imported_by_inquiry = {
            int(row["inquiry_id"]): row
            for row in historical if row.get("inquiry_id") is not None
        }
        imported_by_external = {
            (
                str(row.get("store_code") or ""),
                str(row.get("inquiry_type") or ""),
                str(row.get("external_inquiry_id") or ""),
            ): row
            for row in historical
        }
        active_feedback = _rows(
            connection,
            """
            SELECT inquiry_id, historical_case_id, learning_signal_type,
                   original_answer_masked
            FROM learning_feedback
            WHERE active=1 AND learning_signal_type IN ('NEGATIVE','EXCLUDED')
            """,
        )
        feedback_by_inquiry: dict[int, list[dict[str, Any]]] = defaultdict(list)
        feedback_historical_ids: set[int] = set()
        for feedback in active_feedback:
            if feedback.get("inquiry_id") is not None:
                feedback_by_inquiry[int(feedback["inquiry_id"])].append(feedback)
            if feedback.get("historical_case_id") is not None:
                feedback_historical_ids.add(int(feedback["historical_case_id"]))

        exact: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        normalized: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for item in learning:
            exact[(item["question"], privacy.mask(item["answer"]))].append(item)
            normalized[(item["question_normalized"], item["answer_normalized"])].append(item)

        records: list[dict[str, Any]] = []
        skip_reasons: Counter[str] = Counter()
        decision_counts: Counter[str] = Counter()
        primary_reasons: Counter[str] = Counter()
        duplicate_counts: Counter[str] = Counter()
        for source in sources:
            historical_row = imported_by_inquiry.get(int(source["id"])) or imported_by_external.get(
                (
                    str(source.get("store_code") or ""),
                    str(source.get("source_type") or ""),
                    str(source.get("source_question_id") or ""),
                )
            )
            imported = historical_row is not None
            evaluation_row = dict(historical_row or source)
            # Historical import masked some numeric commerce product IDs as if
            # they were order IDs.  The source inquiry remains the read-only
            # provenance fallback for the later product-scope audit.
            for field in (
                "product_name", "product_id", "option_name",
                "customer_display", "masked_writer_id",
            ):
                current = evaluation_row.get(field)
                if current in (None, "") or str(current).startswith("<masked-"):
                    evaluation_row[field] = source.get(field)
            evaluation_row["source_inquiry_product"] = source.get("product_name")
            if historical_row and int(historical_row["id"]) in feedback_historical_ids:
                evaluation_row["learning_signal_type"] = "EXCLUDED"
            source_feedback = feedback_by_inquiry.get(int(source["id"]), [])
            canonical_answer = privacy.mask(
                format_final_answer(str(evaluation_row.get("seller_answer") or ""))
            )
            if any(
                str(item.get("original_answer_masked") or "") == canonical_answer
                for item in source_feedback
            ):
                evaluation_row["learning_signal_type"] = "EXCLUDED"
            decision = reaudit.assess(evaluation_row).to_dict()
            skip_reason = _original_skip_reason(source, imported)
            if not imported:
                skip_reasons[skip_reason] += 1
            decision_counts[decision["disposition"]] += 1
            primary_reasons[decision["primary_reason"]] += 1
            record = {
                "source_inquiry_id": int(source["id"]),
                "external_inquiry_id": str(source.get("source_question_id") or ""),
                "historical_case_id": (
                    int(historical_row["id"]) if historical_row else None
                ),
                "imported": imported,
                "original_skip_reason": skip_reason,
                "question": decision["masked_question"],
                "answer": decision["masked_answer"],
                "category": _category(
                    decision["masked_question"], decision["masked_answer"]
                ),
                "decision": decision,
                "duplicate": None,
            }
            if decision["disposition"] in {
                HistoricalReauditService.SAFE,
                HistoricalReauditService.REVIEW,
            }:
                duplicate = _match_duplicate(
                    record, learning, exact, normalized
                )
                record["duplicate"] = duplicate
                if duplicate:
                    duplicate_counts[duplicate["duplicate_type"]] += 1
            records.append(record)

        # Deduplicate net-new source pairs against each other without deleting
        # any row.  The earliest source remains the representative candidate.
        seen_source_pairs: dict[tuple[str, str], int] = {}
        source_duplicate_count = 0
        for record in records:
            if record["imported"] or record["decision"]["disposition"] != reaudit.SAFE:
                continue
            key = (
                normalize_learning_question(record["question"]),
                _normalized_answer(record["answer"]),
            )
            if key in seen_source_pairs and record["duplicate"] is None:
                record["duplicate"] = {
                    "duplicate_type": "SOURCE_NORMALIZED",
                    "historical_source_id": seen_source_pairs[key],
                    "similarity": 1.0,
                    "reason": "DUPLICATE_WITH_ANOTHER_HISTORICAL_SOURCE",
                }
                duplicate_counts["SOURCE_NORMALIZED"] += 1
                source_duplicate_count += 1
            else:
                seen_source_pairs.setdefault(key, record["source_inquiry_id"])

        net_new = [
            row for row in records
            if not row["imported"]
            and row["decision"]["disposition"] == reaudit.SAFE
            and row["duplicate"] is None
        ]
        review = [
            row for row in records
            if row["decision"]["disposition"] == reaudit.REVIEW
        ]
        imported_records = [row for row in records if row["imported"]]
        imported_counts = Counter(
            row["decision"]["disposition"] for row in imported_records
        )
        imported_runtime_blocked = sum(
            not bool(row["decision"]["runtime_context_eligible"])
            for row in imported_records
        )
        valid_pairs = sum(
            bool(str(row.get("question") or "").strip())
            and bool(str(row.get("seller_answer") or "").strip())
            and bool(row.get("source_answered"))
            for row in sources
        )
        source_with_positive = int(connection.execute(
            """
            SELECT COUNT(DISTINCT inquiry_id) FROM learning_examples
            WHERE inquiry_id IS NOT NULL
              AND COALESCE(json_extract(metadata_json,'$.learning_signal_type'),
                           'POSITIVE')='POSITIVE'
            """
        ).fetchone()[0])
        product_fields = Counter()
        for row in records:
            for key, value in row["decision"]["product_metadata"].items():
                if value not in (None, "", [], {}):
                    product_fields[key] += 1
        primary_run = next(
            (
                row for row in runs
                if row.get("source_mode") == "LOCAL_DB"
                and row.get("status") in {"COMPLETED", "PARTIAL"}
            ),
            None,
        )
        original_total = min(
            int((primary_run or {}).get("total_fetched") or 0), len(records)
        )
        original_cohort = records[:original_total]
        original_skipped = [row for row in original_cohort if not row["imported"]]
        original_skip_dispositions = Counter(
            row["decision"]["disposition"] for row in original_skipped
        )
        original_skip_current_reasons = Counter(
            row["decision"]["primary_reason"] for row in original_skipped
        )
        post_run_records = records[original_total:]
        post_run_dispositions = Counter(
            row["decision"]["disposition"] for row in post_run_records
        )
        safe_duplicates = [
            row for row in records
            if row["decision"]["disposition"] == reaudit.SAFE
            and row["duplicate"] is not None
        ]
        representative = _balanced_samples(net_new, 20)
        return {
            "mode": "READ_ONLY_DRY_RUN",
            "database": str(database_path),
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "source_definition": {
                "mode": "LOCAL_DB_CURRENT_SNAPSHOT",
                "inquiry_types": list(SUPPORTED_INQUIRY_TYPES),
                "historical_orphans_not_in_source": sum(
                    row.get("inquiry_id") is None for row in historical
                ),
            },
            "import_runs": runs,
            "funnel": {
                "total_historical_source": len(sources),
                "valid_qa_pairs": valid_pairs,
                "currently_imported_historical": len(imported_records),
                "currently_historical_table_total": len(historical),
                "currently_promoted_learning": sum(
                    row.get("promoted_learning_id") is not None for row in historical
                ),
                "source_inquiries_with_positive_learning": source_with_positive,
                "currently_unimported": len(sources) - len(imported_records),
                "safe_reusable": decision_counts[reaudit.SAFE],
                "review_required": decision_counts[reaudit.REVIEW],
                "unsafe_not_reusable": decision_counts[reaudit.UNSAFE],
                "duplicate_with_existing_learning": sum(duplicate_counts.values()),
                "net_new_safe_candidate": len(net_new),
            },
            "reconstructed_original_skip_reasons": dict(skip_reasons.most_common()),
            "original_import_cohort_reaudit": {
                "source_total": len(original_cohort),
                "imported": sum(row["imported"] for row in original_cohort),
                "skipped": len(original_skipped),
                "dispositions": dict(original_skip_dispositions),
                "primary_reasons": dict(original_skip_current_reasons.most_common()),
            },
            "post_import_snapshot_additions": {
                "total": len(post_run_records),
                "dispositions": dict(post_run_dispositions),
                "duplicate_with_existing_learning": sum(
                    row["duplicate"] is not None for row in post_run_records
                ),
            },
            "current_primary_reasons": dict(primary_reasons.most_common()),
            "current_dispositions": dict(decision_counts),
            "existing_learning_duplicates": dict(duplicate_counts),
            "existing_historical_reaudit": {
                "total": len(imported_records),
                "safe_reusable": imported_counts[reaudit.SAFE],
                "review_required": imported_counts[reaudit.REVIEW],
                "unsafe_not_reusable": imported_counts[reaudit.UNSAFE],
                "runtime_blocked": imported_runtime_blocked,
            },
            "safety_signals": {
                "temporary": sum(row["decision"]["temporary"] for row in records),
                "order_specific": sum(row["decision"]["order_specific"] for row in records),
                "personal_information_detected": sum(
                    row["decision"]["personal_information_detected"] for row in records
                ),
                "product_scope_uncertain": sum(
                    row["decision"]["product_scope_uncertain"] for row in records
                ),
            },
            "product_metadata_present": dict(product_fields),
            "representative_net_new_safe": representative,
            "representative_safe_existing_duplicates": _balanced_samples(
                safe_duplicates, 20
            ),
            "net_new_candidates": net_new,
            "review_records": review,
            "all_records": records,
        }
    finally:
        connection.close()


def write_artifacts(result: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "historical_reaudit_summary.json"
    candidate_path = output_dir / "historical_reaudit_candidates.csv"
    summary_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with candidate_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=(
                "source_inquiry_id", "external_inquiry_id", "historical_case_id",
                "category", "disposition", "primary_reason", "runtime_status",
                "duplicate_type", "existing_learning_id", "question", "answer",
                "product_name", "product_id", "model_code", "provenance",
            ),
        )
        writer.writeheader()
        for row in result["net_new_candidates"]:
            decision = row["decision"]
            duplicate = row.get("duplicate") or {}
            product = decision["product_metadata"]
            writer.writerow({
                "source_inquiry_id": row["source_inquiry_id"],
                "external_inquiry_id": row["external_inquiry_id"],
                "historical_case_id": row["historical_case_id"],
                "category": row["category"],
                "disposition": decision["disposition"],
                "primary_reason": decision["primary_reason"],
                "runtime_status": decision["runtime_status"],
                "duplicate_type": duplicate.get("duplicate_type"),
                "existing_learning_id": duplicate.get("existing_learning_id"),
                "question": row["question"],
                "answer": row["answer"],
                "product_name": product.get("product_name"),
                "product_id": product.get("product_id"),
                "model_code": product.get("model_code"),
                "provenance": product.get("provenance"),
            })
    return summary_path, candidate_path


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="Read-only Historical source and reuse-candidate audit"
    )
    parser.add_argument("database", nargs="?", type=Path, default=Path("data/oje_automation.db"))
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--full", action="store_true", help="Include per-record details on stdout")
    args = parser.parse_args()
    result = audit(args.database)
    artifacts = None
    if args.output_dir:
        artifacts = tuple(str(path) for path in write_artifacts(result, args.output_dir))
    output = result if args.full else {
        key: value for key, value in result.items()
        if key not in {"all_records", "review_records", "net_new_candidates"}
    }
    if artifacts:
        output["artifacts"] = artifacts
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
