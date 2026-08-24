"""Explain, for one inquiry's latest draft, why it is or is not auto-postable.

Read-only. Opens the database through a SQLite read-only URI, issues SELECT
only, and never generates, posts or writes anything.

    python scripts/show_auto_registration_gate.py --database <path> \
        --external-id 686125753

The dashboard's "직원 검토" line and the auto-registration gate are different
decisions read from different fields, and they can disagree. This prints both
side by side: the stored validator verdict (split into review signals and
advisory notes), the stored InquiryAnalysis that drives the screen labels, and
the eligibility the Auto Post pipeline itself would compute -- by calling the
very same service with the very same arguments the pipeline uses.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.auto_post_pipeline_service import (  # noqa: E402
    AutoPostPipelineService,
)
from services.auto_processing_eligibility_service import (  # noqa: E402
    AutoProcessingEligibilityService,
)


def connect_readonly(database: str) -> sqlite3.Connection:
    path = Path(database).resolve(strict=True)
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        original = os.getcwd()
        os.chdir(path.parent)
        try:
            connection = sqlite3.connect(f"file:{path.name}?mode=ro", uri=True)
        finally:
            os.chdir(original)
    connection.row_factory = sqlite3.Row
    return connection


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _lines(label: str, values: Any) -> None:
    items = list(values or [])
    print(f"  {label} ({len(items)})")
    for item in items:
        print(f"      - {item}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--external-id", required=True)
    args = parser.parse_args()

    connection = connect_readonly(args.database)
    try:
        inquiry_row = connection.execute(
            "SELECT * FROM inquiries WHERE external_inquiry_id=?"
            " OR source_question_id=? ORDER BY id DESC LIMIT 1",
            (args.external_id, args.external_id),
        ).fetchone()
        if inquiry_row is None:
            print(f"Inquiry not found: {args.external_id}")
            return 1
        inquiry = dict(inquiry_row)
        inquiry_id = int(inquiry["id"])
        # The pipeline posts the active draft; fall back to the newest one so
        # the report still explains a draft that was never activated.
        draft_row = connection.execute(
            "SELECT * FROM answer_drafts WHERE inquiry_id=? AND is_active=1"
            " ORDER BY id DESC LIMIT 1",
            (inquiry_id,),
        ).fetchone() or connection.execute(
            "SELECT * FROM answer_drafts WHERE inquiry_id=?"
            " ORDER BY id DESC LIMIT 1",
            (inquiry_id,),
        ).fetchone()
        if draft_row is None:
            print(f"inquiry_id={inquiry_id}: no draft stored")
            return 1
        draft = dict(draft_row)
        recent = [
            dict(row)
            for row in connection.execute(
                "SELECT id, created_at, is_active, review_status,"
                " validation_status, provider FROM answer_drafts"
                " WHERE inquiry_id=? ORDER BY id DESC LIMIT 5",
                (inquiry_id,),
            ).fetchall()
        ]
    finally:
        connection.close()

    for key in (
        "metadata_json",
        "validator_result_json",
        "inquiry_analysis_json",
        "selected_facts_json",
    ):
        draft[key] = _json(draft.get(key))
    inquiry["raw_json"] = _json(inquiry.get("raw_json"))

    validator = draft["validator_result_json"]
    analysis = draft["inquiry_analysis_json"]
    metadata = draft["metadata_json"]

    print(f"inquiry_id = {inquiry_id}   external_id = {args.external_id}")
    print(f"source_answered={inquiry.get('source_answered')}"
          f"  post_status={inquiry.get('post_status')}"
          f"  approval_status={inquiry.get('approval_status')}")

    print("\n=== recent drafts ===")
    for row in recent:
        mark = " <- active" if row["is_active"] else ""
        print(f"  draft {row['id']}  {row['created_at']}"
              f"  review_status={row['review_status']}"
              f"  validation_status={row['validation_status']}"
              f"  provider={row['provider']}{mark}")

    print(f"\n=== draft {draft['id']} : stored validator verdict ===")
    print(f"  validation_status (column) : {draft.get('validation_status')}")
    print(f"  validator.status           : {validator.get('status')}")
    print(f"  validator.passed           : {validator.get('passed')}")
    warnings = list(validator.get("warnings") or [])
    signals = list(validator.get("review_signals") or [])
    # warnings = advisory + review_signals; whatever is not a review signal is
    # an advisory note, which is shown to staff but never forces review.
    advisory = [item for item in warnings if item not in set(signals)]
    _lines("errors        ", validator.get("errors"))
    _lines("review_signals", signals)
    _lines("advisory      ", advisory)
    _lines("warnings (screen '경고' count)", warnings)
    non_pass = [
        rule
        for rule in (validator.get("rules") or [])
        if isinstance(rule, dict) and str(rule.get("status")) != "PASS"
    ]
    print(f"  non-PASS rules ({len(non_pass)})")
    for rule in non_pass:
        print(f"      - {rule.get('status'):<16}{rule.get('code')}")
        print(f"        {rule.get('message')}")

    # The dashboard's "경고 N건" counts a different collection from the one
    # above: it merges the hybrid block's draft/facts/validation warnings.
    hybrid = metadata.get("hybrid")
    hybrid = hybrid if isinstance(hybrid, dict) else {}

    def _sub(name: str) -> dict[str, Any]:
        value = hybrid.get(name)
        return value if isinstance(value, dict) else {}

    screen: list[str] = []
    for section in ("draft", "facts", "validation"):
        for item in _sub(section).get("warnings") or []:
            if item not in screen:
                screen.append(item)
    print("\n=== what the screen counts as '경고' (metadata.hybrid) ===")
    _lines("hybrid.draft.warnings     ", _sub("draft").get("warnings"))
    _lines("hybrid.facts.warnings     ", _sub("facts").get("warnings"))
    _lines("hybrid.validation.warnings", _sub("validation").get("warnings"))
    _lines("=> screen '경고' total    ", screen)
    print(f"  hybrid.validation.status        : "
          f"{_sub('validation').get('status')}")
    print(f"  hybrid.validation.passed        : "
          f"{_sub('validation').get('passed')}")
    _lines("hybrid.validation.review_signals",
           _sub("validation").get("review_signals"))
    print(f"  hybrid.fallback_used            : {hybrid.get('fallback_used')}")

    print("\n=== stored InquiryAnalysis (drives the screen labels) ===")
    for key in (
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
    ):
        print(f"  {key:<24}{analysis.get(key)}")

    route = AutoPostPipelineService._route(draft)
    print("\n=== auto-registration gate (same call the pipeline makes) ===")
    print(f"  route derived by pipeline  : {route}")
    print(f"  metadata.selected_answer_route: "
          f"{metadata.get('selected_answer_route')}")
    print(f"  metadata.generation_mode   : {metadata.get('generation_mode')}")
    eligibility = AutoProcessingEligibilityService().evaluate(
        inquiry=inquiry, draft=draft, route=route
    )
    print(f"\n  decision                   : {eligibility.decision}")
    print(f"  stage                      : {eligibility.stage}")
    _lines("hard reasons (blocking)", eligibility.reasons)
    _lines("soft reasons (recorded only)", eligibility.soft_reasons)
    print(f"\n  AUTO-REGISTRATION ELIGIBLE : "
          f"{'YES' if eligibility.safe else 'NO'}")

    print("\n=== what the dashboard shows, and from where ===")
    approved = str(inquiry.get("approval_status") or "") == "APPROVED"
    print(f"  card '자동 답변'  = "
          f"{'가능' if _sub('validation').get('passed') else '검토 필요'}"
          f"   <- hybrid.validation.passed (True also when REVIEW_REQUIRED)")
    print(f"  card '직원 검토'  = {'완료' if approved else '필요'}"
          f"   <- approval_status, NOT the gate")
    print(f"  caption '전략'    = {analysis.get('answer_strategy')}"
          f"   <- stored InquiryAnalysis")
    caption_review = bool(
        analysis.get("manual_review_required")
    ) or validator.get("status") == "REVIEW_REQUIRED"
    print(f"  caption           = "
          f"{'직원 검토 필요' if caption_review else '자동 답변 가능'}"
          f"   <- manual_review_required OR validator REVIEW_REQUIRED")
    print(f"  card '경고'       = "
          f"{str(len(screen)) + '건' if screen else '없음'}"
          f"   <- metadata.hybrid warnings")
    print("\n  None of those four consult the auto-registration gate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
