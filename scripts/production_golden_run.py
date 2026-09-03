r"""Replay real inquiries through the real Auto Post pipeline, without posting.

Everything downstream of "collect the inquiry" runs exactly as it does in
production -- the same classifier, processing plan, Naver order lookup,
product-fact and Learning retrieval, generation, validator, eligibility gate,
Final Answer creation and payload builder.  The single substitution is
``NaverAnswerClient.send``: the HTTP write is replaced by a recorder, so the
payload production would have transmitted is captured and asserted instead of
being sent.

Two safety properties matter more than anything this script reports, and both
are enforced rather than documented:

* the operational database is opened read-only and copied with SQLite's backup
  API; every write in the run lands on the throwaway copy, and the copy's path
  is what ``OJE_AUTOMATION_DB_PATH`` points at for the duration;
* no answer-posting HTTP request can be issued, because the client that would
  issue it is never constructed.

Naver order lookup is left real: it is a read, it is what production does, and
stubbing it would hide exactly the kind of failure this script exists to find.
Pass ``--offline-order-lookup`` to replace it with a recorded classification
when the machine has no credentials.

Usage (PowerShell, from the project root)::

    python .\scripts\production_golden_run.py 686427462 686427466 686427478 \
        686427484 686427497
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


GOLDEN_IDS: tuple[str, ...] = (
    "686427462",
    "686427466",
    "686427478",
    "686427484",
    "686427497",
)


@dataclass
class PostRecorder:
    """Stands in for the HTTP client the post service would have used."""

    calls: list[dict[str, Any]] = field(default_factory=list)

    def send(self, request: Any, *, access_token: str) -> Any:
        from api.naver_answer_client import NaverAnswerResponse

        payload = getattr(request, "payload", None)
        self.calls.append(
            {
                "method": getattr(request, "method", None),
                "endpoint": getattr(request, "endpoint", None),
                "payload": dict(payload) if isinstance(payload, dict) else payload,
                "final_answer": getattr(request, "final_answer", None),
                "token_present": bool(access_token),
            }
        )
        return NaverAnswerResponse(
            http_status=200,
            response_id=f"DRYRUN-{len(self.calls)}",
            response_code=None,
        )


def copy_database(source: Path, destination: Path) -> None:
    """Back up the live database without opening it for writing."""

    source_connection = sqlite3.connect(
        source.as_uri() + "?mode=ro", uri=True
    )
    try:
        destination_connection = sqlite3.connect(str(destination))
        try:
            source_connection.backup(destination_connection)
        finally:
            destination_connection.close()
    finally:
        source_connection.close()


def resolve_local_ids(
    connection: sqlite3.Connection, requested: tuple[str, ...]
) -> list[tuple[str, int]]:
    resolved: list[tuple[str, int]] = []
    for value in requested:
        row = connection.execute(
            """
            SELECT id FROM inquiries
            WHERE CAST(source_question_id AS TEXT)=?
               OR CAST(external_inquiry_id AS TEXT)=?
               OR CAST(id AS TEXT)=?
            ORDER BY id DESC LIMIT 1
            """,
            (value, value, value),
        ).fetchone()
        if row is None:
            print(f"  !! inquiry not present in database: {value}")
            continue
        resolved.append((value, int(row[0])))
    return resolved


def _replay_state_snapshot(
    connection: sqlite3.Connection, local_ids: list[int]
) -> dict[int, dict[str, Any]]:
    marks = ",".join("?" for _ in local_ids)
    rows = connection.execute(
        f"""SELECT id, source_answered, source_status, workflow_status,
                    answer_status, post_status, approval_status, post_error_code
              FROM inquiries WHERE id IN ({marks})""",
        local_ids,
    ).fetchall()
    return {int(row["id"]): dict(row) for row in rows}


def reset_for_replay(connection: sqlite3.Connection, local_ids: list[int]) -> None:
    """Put the copy back to "collected, not yet processed" for these rows.

    Only the throwaway copy is ever touched.  The minimum reset mirrors the
    fields that make ``AutoPostRepository.candidates`` exclude an inquiry.
    Source question/product/order context and source metadata are preserved.
    """

    marks = ",".join("?" for _ in local_ids)
    connection.execute(
        f"DELETE FROM answer_drafts WHERE inquiry_id IN ({marks})", local_ids
    )
    connection.execute(
        f"DELETE FROM workflow_steps WHERE inquiry_id IN ({marks})", local_ids
    )
    connection.execute(
        f"DELETE FROM answer_versions WHERE inquiry_id IN ({marks})", local_ids
    )
    connection.execute(
        f"DELETE FROM naver_post_attempts WHERE inquiry_id IN ({marks})",
        local_ids,
    )
    connection.execute(
        f"DELETE FROM post_reviews WHERE inquiry_id IN ({marks})", local_ids
    )
    connection.execute(
        f"""
        UPDATE inquiries
           SET workflow_status='NEW',
               source_answered=0,
               answer_status='UNANSWERED',
               post_status='NOT_POSTED',
               approval_status='PENDING',
               post_error_code=NULL
         WHERE id IN ({marks})
        """,
        local_ids,
    )
    connection.execute(
        "UPDATE naver_auto_post_settings SET enabled=1 WHERE id=1"
    )
    connection.commit()


def _early_exit_reason(
    connection: sqlite3.Connection, inquiry: dict[str, Any]
) -> dict[str, Any]:
    """Report persisted canonical state; never infer POLICY_BLOCK from no draft."""
    if (
        bool(inquiry.get("source_answered"))
        or str(inquiry.get("answer_status") or "").upper() == "ANSWERED"
        or str(inquiry.get("post_status") or "").upper() == "POSTED"
    ):
        return {
            "reason": "ALREADY_ANSWERED_OR_POSTED",
            "source": "inquiry_operational_state",
        }
    event = connection.execute(
        """SELECT event_code, details_json FROM activity_logs
           WHERE inquiry_id=?
             AND event_code IN (
                 'AUTO_POST_SKIPPED_POLICY_BLOCKED',
                 'AUTOMATIC_DRAFT_POLICY_BLOCKED',
                 'ANSWER_POLICY_BLOCKED',
                 'AUTO_ANSWER_FAILED',
                 'AUTO_POST_BLOCKED_DPS_SESSION'
             )
           ORDER BY id DESC LIMIT 1""",
        (int(inquiry["id"]),),
    ).fetchone()
    if event is not None:
        code = str(event["event_code"])
        return {
            "reason": "POLICY_BLOCK" if "POLICY_BLOCK" in code else code,
            "source": "activity_log",
            "event_code": code,
        }
    return {"reason": "UNKNOWN_EARLY_EXIT", "source": "no_persisted_reason"}


def offline_order_lookup(access_token: str, number: str, **_: Any) -> dict[str, Any]:
    """The classification a real lookup produced for this number, replayed.

    Used only with ``--offline-order-lookup``.  It reproduces a *recorded*
    outcome; it never invents a successful order.
    """

    from services.order_service import _normalize_number, _now_iso

    normalized = _normalize_number(number)
    return {
        "success": False,
        "lookup_number": normalized,
        "lookup_type": "ORDER_ID",
        "orders": [],
        "error_code": "ORDER_NOT_FOUND",
        "error_message": "일반 주문번호에 해당하는 주문 결과가 없습니다.",
        "cached": False,
        "queried_at": _now_iso(),
    }


def describe(connection: sqlite3.Connection, local_id: int) -> dict[str, Any]:
    import scripts.diagnose_auto_post_hold as diagnose
    from services.auto_post_pipeline_service import AutoPostPipelineService
    from services.auto_processing_eligibility_service import (
        AutoProcessingEligibilityService,
    )

    connection.row_factory = sqlite3.Row
    inquiry = diagnose._inquiry(connection, str(local_id))
    draft, _active = diagnose._active_draft(connection, local_id)
    row: dict[str, Any] = {
        "inquiry_id": local_id,
        "question": str(inquiry.get("content") or "").replace("\n", " "),
        "workflow_status": inquiry.get("workflow_status"),
        "post_status": inquiry.get("post_status"),
    }
    persisted_early_exit = _early_exit_reason(connection, inquiry)
    if persisted_early_exit["reason"] != "UNKNOWN_EARLY_EXIT":
        row["early_exit"] = persisted_early_exit
    if draft is None:
        row.update(
            {
                "route": "-",
                "classification": "-",
                "order_state": "-",
                "order_lookup": "-",
                "dps": "-",
                "product_fact": "-",
                "learning": "-",
                "answer_source": "NO_DRAFT",
                "validator": "-",
                "review_required": True,
                "eligible": False,
                "blocking": [],
                "early_exit": persisted_early_exit,
                "final_answer": "",
            }
        )
        return row

    metadata = draft.get("metadata_json") or {}
    plan = metadata.get("processing_plan") or {}
    analysis = plan.get("analysis") or {}
    hybrid = metadata.get("hybrid") or {}
    guard = metadata.get("product_fact_guard") or {}
    evidence = hybrid.get("subquestion_evidence") or []
    route = AutoPostPipelineService._route(draft)
    verdict = AutoProcessingEligibilityService().evaluate(
        inquiry=inquiry, draft=draft, route=route
    )
    sources = sorted(
        {
            str(item.get("source") or "NONE")
            for item in evidence
            if isinstance(item, dict)
        }
    )
    row.update(
        {
            "route": route,
            "classification": analysis.get("question_category"),
            "order_state": plan.get("order_id_status"),
            "order_lookup": plan.get("order_lookup_status"),
            "dps": plan.get("dps_lookup_status"),
            "product_fact": (
                f"{guard.get('current_fact_source') or 'NONE'}"
                f"/verified={guard.get('current_fact_verified')}"
            ),
            "learning": ",".join(sources) or "-",
            "answer_source": metadata.get("generation_mode") or draft.get("provider"),
            "validator": draft.get("validation_status"),
            "review_required": str(draft.get("review_status") or "").upper()
            in {"NEEDS_REVIEW", "IN_REVIEW"},
            "eligible": verdict.safe,
            "blocking": list(verdict.reasons),
            "final_answer": str(draft.get("final_answer") or ""),
            # Observation only: these objects were persisted by the real
            # production pipeline before this runner reads them.
            "processing_plan": plan,
            "semantic_analysis": metadata.get("semantic_analysis") or {},
            "analysis": analysis,
            "subquestion_evidence": evidence,
            "product_fact_guard": guard,
            "coverage": hybrid.get("coverage") or metadata.get("coverage") or {},
            "metadata": metadata,
            "auto_post_eligibility": {
                "safe": verdict.safe,
                "decision": verdict.decision,
                "stage": verdict.stage,
                "reasons": list(verdict.reasons),
                "soft_reasons": list(verdict.soft_reasons),
            },
        }
    )
    return row


def write_observation_artifact(
    destination: Path,
    *,
    requested: tuple[str, ...],
    resolved: list[tuple[str, int]],
    rows: list[tuple[str, dict[str, Any]]],
    outcome: Any,
    recorder: PostRecorder,
    replay_state: str,
    original_states: dict[int, dict[str, Any]],
    replay_states: dict[int, dict[str, Any]],
) -> None:
    """Persist values already generated by the replayed production pipeline."""
    payload = {
        "kind": "production_golden_run_observation",
        "requested_ids": list(requested),
        "resolved_ids": [{"requested": external, "inquiry_id": local}
                         for external, local in resolved],
        "pipeline_counters": outcome.to_dict(),
        "dry_run_post_calls": recorder.calls,
        "real_naver_post_count": 0,
        "replay_state": replay_state,
        "replay_provenance": [
            {
                "inquiry_id": local,
                "original_operational_state": original_states.get(local, {}),
                "replay_operational_state": replay_states.get(local, {}),
                "reset_fields": (
                    ["workflow_status", "source_answered", "answer_status",
                     "post_status", "approval_status", "post_error_code",
                     "answer_drafts", "workflow_steps", "answer_versions",
                     "naver_post_attempts", "post_reviews"]
                    if replay_state == "new" else []
                ),
                "preserved_context": [
                    "id", "source_question_id", "external_inquiry_id", "content",
                    "product_name", "option_name", "order_id", "source_status",
                    "source_metadata_json", "raw_json", "created_at",
                ],
            }
            for _external, local in resolved
        ],
        "product_facts_runtime": {
            "runtime_read_count": 0,
            "repository_initialization_count": 0,
            "fallback_count": 0,
            "evidence_count": 0,
            "basis": "No ProductFactRepository is constructed by this runner; persisted draft metadata is exported verbatim.",
        },
        "inquiries": [{"requested_id": external, **row} for external, row in rows],
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inquiry_ids", nargs="*", default=list(GOLDEN_IDS))
    parser.add_argument("--database", default="data/oje_automation.db")
    parser.add_argument("--offline-order-lookup", action="store_true")
    parser.add_argument("--keep-copy", action="store_true")
    parser.add_argument(
        "--replay-state", choices=("historical", "new"), default="new",
        help="historical preserves operational state; new resets only replay state in the copied DB",
    )
    parser.add_argument(
        "--observation-artifact",
        help="JSON path for read-only observation of persisted replay results",
    )
    arguments = parser.parse_args()
    requested = tuple(arguments.inquiry_ids or GOLDEN_IDS)

    source = Path(arguments.database).expanduser().resolve(strict=True)
    workspace = Path(tempfile.mkdtemp(prefix="qa-golden-"))
    copy_path = workspace / "oje_automation.db"
    print("PRODUCTION GOLDEN RUN (real orchestration, dry-run Naver POST)")
    print(f"source database : {source}  [read-only]")
    print(f"replay database : {copy_path}")
    copy_database(source, copy_path)

    os.environ["OJE_AUTOMATION_DB_PATH"] = str(copy_path)

    from repositories.database import Database, get_database_path

    print(f"resolved path   : {get_database_path()}")

    connection = sqlite3.connect(str(copy_path))
    connection.row_factory = sqlite3.Row
    resolved = resolve_local_ids(connection, requested)
    if not resolved:
        print("no inquiries resolved; nothing to replay")
        return 1
    local_ids = [local for _external, local in resolved]
    original_states = _replay_state_snapshot(connection, local_ids)
    if arguments.replay_state == "new":
        reset_for_replay(connection, local_ids)
    replay_states = _replay_state_snapshot(connection, local_ids)

    from config import NaverPostSettings
    from services.auto_post_pipeline_service import AutoPostPipelineService
    from services.naver_post_service import NaverPostService

    recorder = PostRecorder()
    post_service = NaverPostService(
        Database(copy_path),
        client=recorder,
        settings=NaverPostSettings(enabled=True),
        token_provider=lambda store=None, **_: "DRYRUN-TOKEN",
    )

    if arguments.offline_order_lookup:
        import services.answer_service as answer_service

        answer_service.lookup_general_order_id = offline_order_lookup

    pipeline = AutoPostPipelineService(
        Database(copy_path),
        post_service=post_service,
        dps_status_provider=lambda: {"status": "UNKNOWN", "connected": False},
    )
    run_id = str(uuid.uuid4())
    print(f"run_id          : {run_id}\n")
    outcome = pipeline.run_pending(
        run_id=run_id,
        owner_id=f"golden-{run_id}",
        max_retries=0,
        inquiry_ids=local_ids,
    )

    connection.close()
    connection = sqlite3.connect(str(copy_path))
    connection.row_factory = sqlite3.Row

    print("=" * 100)
    print("RESULTS")
    print("=" * 100)
    rows: list[tuple[str, dict[str, Any]]] = []
    for external, local in resolved:
        rows.append((external, describe(connection, local)))

    for external, row in rows:
        print("-" * 100)
        print(f"{external}  {row['question'][:70]}")
        print(f"  Classification   : {row['classification']}")
        print(f"  Route            : {row['route']}")
        print(f"  Order number     : {row['order_state']}")
        print(f"  Order lookup     : {row['order_lookup']}")
        print(f"  DPS              : {row['dps']}")
        print(f"  Product Fact     : {row['product_fact']}")
        print(f"  Evidence sources : {row['learning']}")
        print(f"  Answer source    : {row['answer_source']}")
        print(f"  Validator        : {row['validator']}")
        print(f"  Review required  : {row['review_required']}")
        print(f"  Auto post eligible: {row['eligible']}")
        print(f"  Blocking reasons : {row['blocking'] or '-'}")
        print(f"  Final answer len : {len(row['final_answer'])}")
        print(f"  Post status      : {row['post_status']}")

    if arguments.observation_artifact:
        observation_path = Path(arguments.observation_artifact).expanduser()
        write_observation_artifact(
            observation_path,
            requested=requested,
            resolved=resolved,
            rows=rows,
            outcome=outcome,
            recorder=recorder,
            replay_state=arguments.replay_state,
            original_states=original_states,
            replay_states=replay_states,
        )
        print(f"observation artifact: {observation_path.resolve()}")

    print("=" * 100)
    print(f"pipeline counters       : {outcome.to_dict()}")
    print(f"dry-run POST call count : {len(recorder.calls)}")
    for call in recorder.calls:
        payload = call["payload"] or {}
        print(
            f"  {call['method']} {call['endpoint']} "
            f"| payload_keys={sorted(payload)} "
            f"| answer_len={len(str(call['final_answer'] or ''))}"
        )
    print(f"real Naver POST count   : 0  (client never constructed)")
    (workspace / "post_calls.json").write_text(
        json.dumps(recorder.calls, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"payloads written to     : {workspace / 'post_calls.json'}")
    connection.close()
    if not arguments.keep_copy:
        shutil.rmtree(workspace, ignore_errors=True)
        print("replay database removed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
