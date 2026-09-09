"""Deterministic replays of the five 2026-09-08 production specimens.

The production export deliberately contains the original run's stored outcome,
not the unavailable GPT①/② payload bodies.  These tests therefore preserve the
read-only inquiry/source observations and replay them through the *current*
persisted GPT-first contract.  They are not inquiry-specific runtime rules:
the only per-case data is the forensic evidence verdict that GPT② would have
made after reading the candidate bodies.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.auto_processing_eligibility_service import (
    AutoProcessingEligibilityService,
)
from ui.answer_status_presenter import build_decision_trace


_RAW_DIR = Path(__file__).parents[1] / "diagnostics" / "five_inquiry_raw"


def _raw(inquiry_id: str) -> dict:
    matching = list(_RAW_DIR.glob(f"inquiry_{inquiry_id}_*.json"))
    assert len(matching) == 1, inquiry_id
    return json.loads(matching[0].read_text(encoding="utf-8"))


def _replayed_draft(*, evidence: list[dict], unresolved: list[str]) -> dict:
    """A persisted current-run GPT①/② decision, with hostile legacy fields.

    The deliberately hostile fields prove that the worker reads the persisted
    evidence contract rather than reviving the old UNCLASSIFIED/review state
    from the archived production run.
    """

    return {
        "original_answer": "제공된 근거에 따른 답변입니다.",
        "validation_status": "PASS",
        "review_status": "NEEDS_REVIEW",  # old semantic output: telemetry
        "validator_result_json": {
            "passed": True,
            "status": "PASS",
            "review_signals": ["legacy advisory"],
        },
        "metadata_json": {
            "semantic_routing": {"understanding": {"usable": True}},
            "processing_plan": {
                "needs_staff_review": True,
                "is_high_risk": True,
                "analysis": {
                    "inquiry_subtype": "UNCLASSIFIED",
                    "manual_review_required": True,
                },
            },
            "hybrid": {
                "answer_pipeline": "GPT_UNDERSTAND_RETRIEVE_ANSWER",
                "draft": {
                    "requires_review": bool(unresolved),
                    "can_auto_post": not bool(unresolved),
                    "unresolved": unresolved,
                    "subquestion_results": [
                        {"answered": not bool(unresolved), "status": "ANSWERABLE"}
                    ],
                },
                "subquestion_evidence": evidence,
                "retrieval": {"learning": {"candidate_count": len(evidence)}},
            },
        },
    }


@pytest.mark.parametrize(
    ("inquiry_id", "expected_learning_ids", "evidence", "unresolved", "safe", "root"),
    [
        (
            "687992357", (312192, 120),
            [{"status": "NO_RELIABLE_SOURCE"}],
            ["기본 구성품", "별도 준비물"], False,
            ("SOURCE", "SOURCE_MISSING"),
        ),
        (
            "687992384", (184444, 157023, 19278),
            [{"status": "CANDIDATE"}, {"status": "CANDIDATE"}], [], True,
            ("", "NONE"),
        ),
        (
            "687992413", (223107, 66122, 169, 162),
            # The current invariant is candidate delivery of the general
            # policy, followed by an explicit unresolved atom for the missing
            # application procedure.  This is not a retrieval failure.
            [{"status": "CANDIDATE"}, {"status": "NO_RELIABLE_SOURCE"}],
            ["별도 신청 절차"], False,
            ("GPT_EVIDENCE", "EVIDENCE_UNRESOLVED"),
        ),
        (
            "687992436", (312192, 120),
            [{"status": "NO_RELIABLE_SOURCE"}],
            ["노트북 연결 사양", "필요 케이블"], False,
            ("SOURCE", "SOURCE_MISSING"),
        ),
        (
            "687992464", (312192, 120),
            [{"status": "CANDIDATE"}, {"status": "CANDIDATE"}], [], True,
            ("", "NONE"),
        ),
    ],
)
def test_forensic_specimen_replays_through_single_gpt_first_authority(
    inquiry_id: str,
    expected_learning_ids: tuple[int, ...],
    evidence: list[dict],
    unresolved: list[str],
    safe: bool,
    root: tuple[str, str],
) -> None:
    raw = _raw(inquiry_id)
    inquiry = raw["inquiry"]
    assert inquiry["naver_inquiry_id"] == inquiry_id
    observed_ids = {
        row["learning_example_id"]
        for row in raw["evidence"]["learning_references"]
        if row["learning_example_id"] is not None
    }
    assert set(expected_learning_ids) <= observed_ids

    draft = _replayed_draft(evidence=evidence, unresolved=unresolved)
    verdict = AutoProcessingEligibilityService().evaluate(
        inquiry={"source_answered": False, "post_status": "NOT_POSTED"},
        draft=draft,
        route="GPT_FALLBACK",
    )
    trace = build_decision_trace(
        inquiry={"post_status": "NOT_POSTED"}, draft=draft,
        eligibility=verdict, route="GPT_FALLBACK",
    )

    assert verdict.safe is safe, verdict
    assert (trace.root_stage, trace.root_cause) == root
    if safe:
        assert not set(verdict.reasons) & {
            "PROCESSING_PLAN_REQUIRES_REVIEW", "DRAFT_REVIEW_REQUIRED",
            "GPT_WITHHELD_AUTO_POST", "POLICY_OR_HIGH_RISK_REVIEW",
        }
    else:
        assert "GPT_REPORTED_UNRESOLVED" in verdict.reasons
