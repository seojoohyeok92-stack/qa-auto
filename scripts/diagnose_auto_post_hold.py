r"""Read-only diagnosis for inquiries held by the auto-post eligibility gate.

This command deliberately does not use ``repositories.database.Database``:
that class configures SQLite WAL mode on connection.  The diagnostic opens an
existing database with ``mode=ro``, enables ``PRAGMA query_only``, issues only
SELECT statements, and calls the same in-process deterministic eligibility
service used by Auto Post.  It never generates an answer or invokes Auto Post,
Naver, DPS, GPT, or OpenAI.

Example (PowerShell, from the project root)::

    python .\scripts\diagnose_auto_post_hold.py --database \
        ".\data\oje_automation.db" 686266827 686266840 686266865
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from answer.source_adapter import answer_request_from_inquiry  # noqa: E402
from services.auto_post_pipeline_service import (  # noqa: E402
    AutoPostPipelineService,
)
from services.auto_processing_eligibility_service import (  # noqa: E402
    AutoProcessingEligibilityService,
)
from services.inquiry_analysis_service import (  # noqa: E402
    InquiryAnalysisService,
)


AUTO_EVENT_PATTERNS = (
    "AUTO_PROCESSING%",
    "AUTO_POST%",
)
PRELIMINARY_DERIVED_REASONS = {
    "ANSWER_REQUIRES_MANUAL_REVIEW",
    "PROCESSING_PLAN_REQUIRES_REVIEW",
    "DRAFT_REVIEW_REQUIRED",
}


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {"_json_decode_error": True}
    return parsed if isinstance(parsed, dict) else {}


def _print_value(label: str, value: Any, *, indent: int = 2) -> None:
    prefix = " " * indent
    if isinstance(value, (dict, list, tuple)):
        rendered = json.dumps(value, ensure_ascii=False, indent=2, default=str)
        lines = rendered.splitlines() or [rendered]
        print(f"{prefix}{label}:")
        for line in lines:
            print(f"{prefix}  {line}")
        return
    print(f"{prefix}{label}: {value}")


def _print_section(letter: str, title: str) -> None:
    print(f"\n[{letter}] {title}")


def _connect_readonly(database: str) -> tuple[sqlite3.Connection, Path]:
    path = Path(database).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError(f"Database is not a file: {path}")
    connection = sqlite3.connect(
        path.as_uri() + "?mode=ro",
        uri=True,
        isolation_level=None,
        timeout=5,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
        connection.close()
        raise RuntimeError("READ_ONLY_GUARD_FAILED: PRAGMA query_only is not ON")
    return connection, path


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _inquiry(
    connection: sqlite3.Connection,
    requested_id: str,
) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT *
        FROM inquiries
        WHERE CAST(external_inquiry_id AS TEXT)=?
           OR CAST(source_question_id AS TEXT)=?
           OR CAST(id AS TEXT)=?
        ORDER BY CASE
            WHEN CAST(external_inquiry_id AS TEXT)=? THEN 0
            WHEN CAST(source_question_id AS TEXT)=? THEN 1
            ELSE 2
        END, id DESC
        LIMIT 1
        """,
        (requested_id, requested_id, requested_id, requested_id, requested_id),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    for key in ("raw_json", "source_metadata_json"):
        if key in result:
            result[key] = _json_object(result.get(key))
    return result


def _active_draft(
    connection: sqlite3.Connection,
    inquiry_id: int,
) -> tuple[dict[str, Any] | None, bool]:
    row = connection.execute(
        """
        SELECT * FROM answer_drafts
        WHERE inquiry_id=? AND is_active=1
        ORDER BY created_at DESC, id DESC LIMIT 1
        """,
        (inquiry_id,),
    ).fetchone()
    active = row is not None
    if row is None:
        row = connection.execute(
            """
            SELECT * FROM answer_drafts
            WHERE inquiry_id=?
            ORDER BY created_at DESC, id DESC LIMIT 1
            """,
            (inquiry_id,),
        ).fetchone()
    if row is None:
        return None, False
    result = dict(row)
    for key in (
        "metadata_json",
        "inquiry_analysis_json",
        "selected_facts_json",
        "validator_result_json",
    ):
        result[key] = _json_object(result.get(key))
    return result, active


def _latest_event_details(
    connection: sqlite3.Connection,
    inquiry_id: int,
    event_code: str,
    *,
    before: str | None = None,
) -> dict[str, Any]:
    time_clause = " AND created_at<=?" if before else ""
    parameters: tuple[Any, ...] = (
        (inquiry_id, event_code, before)
        if before
        else (inquiry_id, event_code)
    )
    row = connection.execute(
        "SELECT id, event_code, level, message, details_json, created_at "
        "FROM activity_logs WHERE inquiry_id=? AND event_code=?"
        + time_clause
        + " ORDER BY created_at DESC, id DESC LIMIT 1",
        parameters,
    ).fetchone()
    if row is None:
        return {}
    result = dict(row)
    result["details"] = _json_object(result.pop("details_json", None))
    return result


def _auto_events(
    connection: sqlite3.Connection,
    inquiry_id: int,
) -> list[dict[str, Any]]:
    clauses = " OR ".join("event_code LIKE ?" for _ in AUTO_EVENT_PATTERNS)
    rows = connection.execute(
        f"""
        SELECT id, event_code, level, message, details_json, created_at
        FROM activity_logs
        WHERE inquiry_id=? AND ({clauses})
        ORDER BY created_at DESC, id DESC
        LIMIT 30
        """,
        (inquiry_id, *AUTO_EVENT_PATTERNS),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["details"] = _json_object(item.pop("details_json", None))
        result.append(item)
    return result


def _workflow(
    connection: sqlite3.Connection,
    inquiry_id: int,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT step_code, step_status, last_error_code,
                   last_error_message, updated_at
            FROM workflow_steps
            WHERE inquiry_id=?
            ORDER BY id
            """,
            (inquiry_id,),
        ).fetchall()
    ]


def _provenance(
    connection: sqlite3.Connection,
    draft_id: int,
) -> list[dict[str, Any]]:
    if not _table_exists(connection, "answer_learning_provenance"):
        return []
    available = {
        str(row["name"])
        for row in connection.execute(
            "PRAGMA table_info(answer_learning_provenance)"
        ).fetchall()
    }
    wanted = (
        "id",
        "context_run_id",
        "reference_kind",
        "learning_example_id",
        "historical_case_id",
        "source_label",
        "relevance",
        "included_in_prompt",
        "usage_status",
        "usage_reason",
        "matched_subquestion",
        "answer_support_score",
        "evidence_coverage",
        "provider_claimed_usage",
        "system_verified_usage",
        "created_at",
        "evaluated_at",
    )
    selected = ", ".join(
        name if name in available else f"NULL AS {name}"
        for name in wanted
    )
    result = [
        dict(row)
        for row in connection.execute(
            f"SELECT {selected} FROM answer_learning_provenance "
            "WHERE answer_draft_id=? "
            "ORDER BY included_in_prompt DESC, relevance DESC, id",
            (draft_id,),
        ).fetchall()
    ]
    for item in result:
        item["prompt_reference"] = _prompt_reference(connection, item)
    return result


def _prompt_reference(
    connection: sqlite3.Connection,
    provenance: dict[str, Any],
) -> dict[str, Any]:
    kind = str(provenance.get("reference_kind") or "").upper()
    if kind == "LEARNING":
        table = "learning_examples"
        reference_id = provenance.get("learning_example_id")
        wanted = (
            "id",
            "question_original_masked",
            "final_answer",
            "product_name",
            "learning_source",
        )
    elif kind == "HISTORICAL":
        table = "historical_cases"
        reference_id = provenance.get("historical_case_id")
        wanted = (
            "id",
            "question",
            "seller_answer",
            "product_name",
            "source",
        )
    else:
        return {}
    if reference_id is None or not _table_exists(connection, table):
        return {}
    available = {
        str(row["name"])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    selected = [name for name in wanted if name in available]
    if "id" not in selected:
        return {}
    row = connection.execute(
        f"SELECT {', '.join(selected)} FROM {table} WHERE id=?",
        (int(reference_id),),
    ).fetchone()
    return dict(row) if row is not None else {}


def _all_with(
    evidence: list[Any],
    key: str,
    expected: str,
) -> bool:
    return bool(evidence) and all(
        isinstance(item, dict)
        and str(item.get(key) or "").upper() == expected
        for item in evidence
    )


def _current_analysis(inquiry: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    try:
        result = InquiryAnalysisService().analyze(
            answer_request_from_inquiry(inquiry)
        )
    except Exception as exc:  # diagnostic: preserve the failure in output
        return {}, f"{type(exc).__name__}: {exc}"
    return result.to_dict(), None


def _analysis_view(analysis: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "inquiry_type",
        "inquiry_subtype",
        "question_category",
        "detected_intent",
        "answer_strategy",
        "confidence",
        "manual_review_required",
        "auto_answerable",
        "requires_order_lookup",
        "requires_dps_lookup",
        "reasons",
    )
    return {key: analysis.get(key) for key in keys}


def _used_reference_ids(generated: dict[str, Any]) -> dict[str, list[Any]]:
    learning = generated.get("learning_usage")
    historical = generated.get("historical_usage")
    return {
        "learning_usage": learning if isinstance(learning, list) else [],
        "historical_usage": historical if isinstance(historical, list) else [],
    }


def _resolution_checks(
    *,
    service: AutoProcessingEligibilityService,
    inquiry: dict[str, Any],
    plan: dict[str, Any],
    hybrid: dict[str, Any],
    draft: dict[str, Any],
    validator: dict[str, Any],
) -> tuple[dict[str, bool], bool]:
    evidence_value = hybrid.get("subquestion_evidence")
    evidence = evidence_value if isinstance(evidence_value, list) else []
    generated_value = hybrid.get("draft")
    generated = generated_value if isinstance(generated_value, dict) else None
    review_value = hybrid.get("self_review")
    self_review = review_value if isinstance(review_value, dict) else None
    validation_status = str(draft.get("validation_status") or "").upper()
    pass_statuses = {"PASS", "PASS_WITH_WARNING"}
    checks = {
        "current_analysis_safe": service._current_analysis_clears_review(inquiry),
        "all_subquestions_answerable": _all_with(
            evidence, "status", "ANSWERABLE"
        ),
        "all_subquestions_supported": _all_with(
            evidence, "evidence_coverage", "SUPPORTED"
        ),
        "validator_pass": bool(
            validation_status in pass_statuses
            and validator.get("passed") is True
            and str(validator.get("status") or "PASS").upper() in pass_statuses
        ),
        "validator_errors_empty": not bool(validator.get("errors")),
        "validator_review_signals_empty": not bool(
            validator.get("review_signals")
        ),
        "draft_requires_review_false": bool(
            generated is not None
            and not bool(generated.get("requires_review"))
        ),
        "missing_information_empty": bool(
            generated is not None
            and not bool(generated.get("missing_information"))
        ),
        "self_review_clear": bool(
            self_review is not None
            and not bool(self_review.get("requires_review"))
        ),
        "not_high_risk": not bool(plan.get("is_high_risk")),
        "metadata_present": bool(
            isinstance(hybrid, dict)
            and hybrid
            and generated is not None
            and self_review is not None
            and evidence
            and validator
        ),
    }
    return checks, all(checks.values())


def _classification(
    hard_reasons: Iterable[str],
    *,
    plan_high_risk: bool,
    preliminary_resolved: bool,
) -> tuple[list[str], str]:
    hard = list(hard_reasons)
    independent = [
        reason
        for reason in hard
        if reason not in PRELIMINARY_DERIVED_REASONS
        and not (
            reason == "POLICY_OR_HIGH_RISK_REVIEW"
            and not plan_high_risk
        )
    ]
    if not hard:
        label = "현재 SAFE; 당시 event/metadata와 차이가 있는지 확인"
    elif independent:
        label = "독립 hard reason 존재: 정상 차단 여부를 reason별 확인"
    elif not preliminary_resolved:
        label = "preliminary review 미해소: 저장 metadata/분류 신호 진단 후보"
    else:
        label = "파생 reason만 남음: 현재/당시 코드 버전 차이 확인 필요"
    return independent, label


def _report_one(
    connection: sqlite3.Connection,
    requested_id: str,
    service: AutoProcessingEligibilityService,
) -> dict[str, Any]:
    inquiry = _inquiry(connection, requested_id)
    if inquiry is None:
        print("\n" + "=" * 88)
        print(f"{requested_id}: INQUIRY_NOT_FOUND")
        return {"requested_id": requested_id, "error": "INQUIRY_NOT_FOUND"}

    local_id = int(inquiry["id"])
    draft, is_active = _active_draft(connection, local_id)
    if draft is None:
        print("\n" + "=" * 88)
        print(f"{requested_id}: ACTIVE_OR_LATEST_DRAFT_NOT_FOUND")
        return {
            "requested_id": requested_id,
            "local_inquiry_id": local_id,
            "error": "ACTIVE_OR_LATEST_DRAFT_NOT_FOUND",
        }

    metadata = draft["metadata_json"]
    plan_value = metadata.get("processing_plan")
    plan = plan_value if isinstance(plan_value, dict) else {}
    analysis_value = plan.get("analysis")
    analysis = analysis_value if isinstance(analysis_value, dict) else {}
    hybrid_value = metadata.get("hybrid")
    hybrid = hybrid_value if isinstance(hybrid_value, dict) else {}
    generated_value = hybrid.get("draft")
    generated = generated_value if isinstance(generated_value, dict) else {}
    self_review_value = hybrid.get("self_review")
    self_review = (
        self_review_value if isinstance(self_review_value, dict) else {}
    )
    validator = draft["validator_result_json"]
    evidence_value = hybrid.get("subquestion_evidence")
    evidence = evidence_value if isinstance(evidence_value, list) else []
    product_guard_value = metadata.get("product_fact_guard")
    product_guard = (
        product_guard_value if isinstance(product_guard_value, dict) else {}
    )
    route = AutoPostPipelineService._route(draft)
    eligibility = service.evaluate(
        inquiry=inquiry,
        draft=draft,
        route=route,
    )
    checks, checks_all_true = _resolution_checks(
        service=service,
        inquiry=inquiry,
        plan=plan,
        hybrid=hybrid,
        draft=draft,
        validator=validator,
    )
    has_preliminary = bool(
        metadata.get("requires_manual_review")
        or plan.get("needs_staff_review")
        or str(draft.get("review_status") or "").upper() == "NEEDS_REVIEW"
        or analysis.get("manual_review_required")
    )
    preliminary_resolved = bool(
        has_preliminary
        and service._preliminary_review_resolved(
            inquiry=inquiry,
            analysis=analysis,
            plan=plan,
            hybrid=hybrid,
            validation_status=str(draft.get("validation_status") or "").upper(),
            validator=validator,
        )
    )
    failed_checks = [key for key, passed in checks.items() if not passed]
    current_analysis, current_analysis_error = _current_analysis(inquiry)

    retrieval_event = _latest_event_details(
        connection,
        local_id,
        "LEARNING_RETRIEVAL_COMPLETED",
        before=str(draft.get("created_at") or "") or None,
    )
    retrieval = retrieval_event.get("details") or {}
    provenance = _provenance(connection, int(draft["id"]))
    prompt_materials = [
        item for item in provenance if bool(item.get("included_in_prompt"))
    ]
    used_materials = [
        item for item in provenance if str(item.get("usage_status")) == "USED"
    ]
    used_from_draft = _used_reference_ids(generated)
    non_pass_rules = [
        item
        for item in (validator.get("rules") or [])
        if isinstance(item, dict)
        and str(item.get("status") or "").upper() != "PASS"
    ]
    events = _auto_events(connection, local_id)
    recorded_decisions = [
        item
        for item in events
        if item.get("event_code")
        in {"AUTO_PROCESSING_BLOCKED", "AUTO_PROCESSING_REVIEW_REQUIRED"}
    ]
    latest_recorded = recorded_decisions[0] if recorded_decisions else {}
    latest_recorded_details = latest_recorded.get("details") or {}
    independent, disposition = _classification(
        eligibility.reasons,
        plan_high_risk=bool(plan.get("is_high_risk")),
        preliminary_resolved=preliminary_resolved,
    )

    print("\n" + "=" * 88)
    print(
        f"{requested_id}  local_inquiry_id={local_id}  "
        f"draft_id={draft.get('id')}  active_draft={is_active}"
    )

    _print_section("A", "inquiry 기본 정보")
    _print_value("inquiry_id", inquiry.get("external_inquiry_id") or requested_id)
    _print_value("local_inquiry_id", local_id)
    _print_value("inquiry_text", inquiry.get("content") or inquiry.get("title"))
    _print_value("title", inquiry.get("title"))
    _print_value("product", inquiry.get("product_name"))
    _print_value("option", inquiry.get("option_name"))
    _print_value(
        "current_status",
        {
            "workflow_status": inquiry.get("workflow_status"),
            "answer_status": inquiry.get("answer_status"),
            "post_status": inquiry.get("post_status"),
            "approval_status": inquiry.get("approval_status"),
            "source_answered": bool(inquiry.get("source_answered")),
        },
    )
    _print_value(
        "answer_draft_status",
        {
            "draft_id": draft.get("id"),
            "is_active": is_active,
            "program_status": draft.get("program_status"),
            "review_status": draft.get("review_status"),
            "validation_status": draft.get("validation_status"),
            "posted": bool(draft.get("posted")),
            "provider": draft.get("provider"),
        },
    )

    _print_section("B", "저장된 InquiryAnalysis")
    _print_value("processing_plan.analysis", _analysis_view(analysis))
    _print_value("answer_drafts.inquiry_analysis_json", _analysis_view(
        draft["inquiry_analysis_json"]
    ))
    _print_value("processing_plan.is_high_risk", bool(plan.get("is_high_risk")))
    _print_value("processing_plan.risk_level", plan.get("risk_level"))
    _print_value("processing_plan.risk_reasons", plan.get("risk_reasons"))
    _print_value("current_deterministic_analysis", _analysis_view(current_analysis))
    _print_value("current_analysis_error", current_analysis_error)

    _print_section("C", "답변 생성 결과")
    _print_value("generation_mode", metadata.get("generation_mode"))
    _print_value("selected_route", route)
    _print_value("answer_status", draft.get("program_status"))
    _print_value("draft.review_status", draft.get("review_status"))
    _print_value(
        "processing_plan.needs_staff_review",
        bool(plan.get("needs_staff_review")),
    )
    _print_value("generated.requires_review", generated.get("requires_review"))
    _print_value("generated.missing_information", generated.get("missing_information"))
    _print_value("self_review", self_review)

    _print_section("D", "Grounding / Learning")
    _print_value("candidate_count", retrieval.get("candidate_count"))
    _print_value("selected_count", retrieval.get("selected_count"))
    _print_value("selected_learning_ids", retrieval.get("selected_learning_ids"))
    _print_value("retrieval_event", retrieval_event)
    _print_value("prompt_included_materials", prompt_materials)
    _print_value("provenance_usage_status_USED", used_materials)
    _print_value("generated_reported_usage", used_from_draft)
    _print_value("subquestion_evidence", evidence)
    _print_value(
        "all_subquestions_answerable",
        checks["all_subquestions_answerable"],
    )
    _print_value(
        "all_subquestions_supported",
        checks["all_subquestions_supported"],
    )
    _print_value("selected_facts", draft["selected_facts_json"])
    _print_value("hybrid.confirmed_facts", hybrid.get("confirmed_facts"))
    _print_value("product_fact_guard", product_guard)
    _print_value(
        "product_fact_guard_reason", metadata.get("product_fact_guard_reason")
    )

    _print_section("E", "Validator")
    _print_value("column.validation_status", draft.get("validation_status"))
    _print_value("validator.status", validator.get("status"))
    _print_value("validator.passed", validator.get("passed"))
    _print_value("validator.errors", validator.get("errors"))
    _print_value("validator.review_signals", validator.get("review_signals"))
    _print_value("validator.warnings", validator.get("warnings"))
    _print_value("validator.rules", validator.get("rules"))
    _print_value("validator.non_pass_rules", non_pass_rules)

    _print_section("F", "현재 AutoProcessingEligibilityService 재계산")
    _print_value("decision", eligibility.decision)
    _print_value("stage", eligibility.stage)
    _print_value("safe", eligibility.safe)
    _print_value("hard_reasons", list(eligibility.reasons))
    _print_value("soft_reasons", list(eligibility.soft_reasons))

    _print_section("G", "실제 자동처리 당시 DB 기록")
    _print_value("latest_recorded_decision_event", latest_recorded)
    _print_value("recorded_decision", latest_recorded_details.get("decision"))
    _print_value("recorded_stage", latest_recorded_details.get("stage"))
    _print_value("recorded_hard_reasons", latest_recorded_details.get("reasons"))
    _print_value("recorded_soft_reasons", latest_recorded_details.get("soft_reasons"))
    _print_value("recorded_timestamp", latest_recorded.get("created_at"))
    _print_value("all_auto_processing_and_post_events", events)
    _print_value("workflow_steps", _workflow(connection, local_id))

    _print_section("H", "preliminary review 해소 조건")
    for key, passed in checks.items():
        _print_value(key, passed)
    _print_value("has_preliminary_review", has_preliminary)
    _print_value("preliminary_review_resolved", preliminary_resolved)
    _print_value("all_displayed_resolution_checks_true", checks_all_true)
    _print_value("failed_conditions", failed_checks)
    _print_value(
        "resolution_failure",
        (
            "preliminary review는 위 FALSE 조건 때문에 해소되지 않음"
            if has_preliminary and not preliminary_resolved
            else "해당 없음"
        ),
    )

    _print_section("I", "최종 요약")
    _print_value(
        "현재 판정",
        {
            "decision": eligibility.decision,
            "safe": eligibility.safe,
            "hard_reasons": list(eligibility.reasons),
            "soft_reasons": list(eligibility.soft_reasons),
        },
    )
    _print_value(
        "실제 당시 판정",
        {
            "event_code": latest_recorded.get("event_code"),
            "decision": latest_recorded_details.get("decision"),
            "stage": latest_recorded_details.get("stage"),
            "reasons": latest_recorded_details.get("reasons"),
            "soft_reasons": latest_recorded_details.get("soft_reasons"),
            "timestamp": latest_recorded.get("created_at"),
        },
    )
    _print_value("자동등록을 막은 직접 reason", list(eligibility.reasons))
    _print_value("preliminary review 해소 실패 조건", failed_checks)
    _print_value("독립 안전 차단", independent)
    _print_value("수정 후보인지 / 정상 차단인지", disposition)

    return {
        "requested_id": requested_id,
        "current_decision": eligibility.decision,
        "current_safe": eligibility.safe,
        "current_hard_reasons": list(eligibility.reasons),
        "current_soft_reasons": list(eligibility.soft_reasons),
        "recorded_event_code": latest_recorded.get("event_code"),
        "recorded_decision": latest_recorded_details.get("decision"),
        "recorded_hard_reasons": latest_recorded_details.get("reasons"),
        "recorded_timestamp": latest_recorded.get("created_at"),
        "preliminary_review_resolved": preliminary_resolved,
        "failed_conditions": failed_checks,
        "independent_hard_reasons": independent,
        "classification": disposition,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only diagnosis of current and recorded auto-post eligibility"
        )
    )
    parser.add_argument(
        "--database",
        required=True,
        help="Existing SQLite database path; opened with mode=ro",
    )
    parser.add_argument(
        "inquiry_ids",
        nargs="+",
        help="External/source/local inquiry ids to inspect",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    connection, database_path = _connect_readonly(args.database)
    try:
        print("READ-ONLY AUTO POST HOLD DIAGNOSTIC")
        print(f"database: {database_path}")
        print("sqlite mode: ro")
        print("PRAGMA query_only: ON")
        print("Auto Post / Naver / DPS / GPT calls: DISABLED BY DESIGN")
        service = AutoProcessingEligibilityService()
        summaries = [
            _report_one(connection, requested_id, service)
            for requested_id in args.inquiry_ids
        ]
        total_changes = connection.total_changes
        print("\n" + "=" * 88)
        print("THREE-INQUIRY COMPARISON")
        print(json.dumps(summaries, ensure_ascii=False, indent=2, default=str))
        print(f"\nSQLite connection total_changes: {total_changes}")
        if total_changes != 0:
            raise RuntimeError(
                f"READ_ONLY_INVARIANT_FAILED: total_changes={total_changes}"
            )
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
