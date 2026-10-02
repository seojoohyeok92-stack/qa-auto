"""Replays of the five 2026-09-08 production specimens' persisted decisions.

What these cases actually pin down is how a *stored* GPT①/② decision is graded:
which combinations of evidence status and unresolved atoms may auto-post, and
which stage gets named as the root cause when one may not.  The five specimens
are where the combinations came from -- inquiries 687992357, 687992384,
687992413, 687992436 and 687992464, whose original run retrieved Learning rows
312192/120, 184444/157023/19278 and 223107/66122/169/162 respectively.

They used to be loaded from ``diagnostics/five_inquiry_raw/*.json``, an
untracked export of the operational database.  Two things were wrong with
that.  It made the tests pass only on a machine that happened to hold that
export, so a clean clone ran five silent false failures; and the raw dump
carries real customer inquiry text and order identifiers, which is why it is
deliberately not in the repository and must not be copied in.

So the dump is gone and nothing here reads the filesystem.  Note what was
actually taken from it: a self-check that the loaded file matched the inquiry
id, and a check that the hardcoded Learning ids appeared among that run's
``learning_references``.  Neither touched the behaviour under test -- the draft
below was always built from the parameters, never from the export.  The
provenance claim about a 2026-09-08 run is not something current production
code can be regressed against, so it is recorded above as history rather than
asserted against a synthetic file that would only be agreeing with itself.

The grading assertions are stricter than before in exchange: the exact
decision, stage and reason tuple, and the operator-facing root message, are
all pinned now, so a verdict that comes out right for the wrong reason fails.
"""
from __future__ import annotations

import pytest

from services.auto_processing_eligibility_service import (
    AutoProcessingEligibilityService,
)
from ui.answer_status_presenter import build_decision_trace


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


NO_SOURCE = "답변에 필요한 신뢰 가능한 근거가 없습니다."
UNRESOLVED = "GPT가 제공된 근거만으로 질문을 해결할 수 없다고 판단했습니다."
NO_FAILURE = "최초 실패 원인이 기록되지 않았습니다."
WITHHELD = ("GPT_REPORTED_UNRESOLVED", "GPT_WITHHELD_AUTO_POST")


# The five specimens reduce to three distinct gradings: 687992357 and
# 687992436 are the same shape with different atom wording, as are 687992384
# and 687992464.  All five are kept because the specimens are the record of
# which shapes production actually produced, and the atom text is what the
# operator reads.
@pytest.mark.parametrize(
    (
        "specimen", "evidence", "unresolved",
        "safe", "decision", "reasons", "root", "root_message",
    ),
    [
        # No usable source at all: the source stage owns the failure, ahead of
        # the unresolved atoms it caused.
        (
            "687992357",
            [{"status": "NO_RELIABLE_SOURCE"}],
            ["기본 구성품", "별도 준비물"],
            False, "REVIEW_REQUIRED", WITHHELD,
            ("SOURCE", "SOURCE_MISSING"), NO_SOURCE,
        ),
        (
            "687992436",
            [{"status": "NO_RELIABLE_SOURCE"}],
            ["노트북 연결 사양", "필요 케이블"],
            False, "REVIEW_REQUIRED", WITHHELD,
            ("SOURCE", "SOURCE_MISSING"), NO_SOURCE,
        ),
        # Every atom answered from candidate evidence: auto-postable, and the
        # hostile legacy review flags above must not revive a block.
        (
            "687992384",
            [{"status": "CANDIDATE"}, {"status": "CANDIDATE"}],
            [],
            True, "SAFE", (),
            ("", "NONE"), NO_FAILURE,
        ),
        (
            "687992464",
            [{"status": "CANDIDATE"}, {"status": "CANDIDATE"}],
            [],
            True, "SAFE", (),
            ("", "NONE"), NO_FAILURE,
        ),
        # Mixed: the general policy was delivered as a candidate and one atom
        # stayed open.  That is an evidence-resolution outcome, not a retrieval
        # failure, so the root cause must be EVIDENCE_UNRESOLVED and not
        # SOURCE_MISSING -- the distinction this case exists for.
        (
            "687992413",
            [{"status": "CANDIDATE"}, {"status": "NO_RELIABLE_SOURCE"}],
            ["별도 신청 절차"],
            False, "REVIEW_REQUIRED", WITHHELD,
            ("GPT_EVIDENCE", "EVIDENCE_UNRESOLVED"), UNRESOLVED,
        ),
    ],
)
def test_forensic_specimen_replays_through_single_gpt_first_authority(
    specimen: str,
    evidence: list[dict],
    unresolved: list[str],
    safe: bool,
    decision: str,
    reasons: tuple[str, ...],
    root: tuple[str, str],
    root_message: str,
) -> None:
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

    assert verdict.safe is safe, (specimen, verdict)
    assert verdict.decision == decision, (specimen, verdict)
    assert verdict.stage == "AUTO_POST_ELIGIBILITY", (specimen, verdict)
    assert verdict.reasons == reasons, (specimen, verdict)
    assert verdict.soft_reasons == (), (specimen, verdict)
    assert (trace.root_stage, trace.root_cause) == root, (specimen, trace)
    assert trace.root_message == root_message, (specimen, trace)

    if safe:
        assert not set(verdict.reasons) & {
            "PROCESSING_PLAN_REQUIRES_REVIEW", "DRAFT_REVIEW_REQUIRED",
            "GPT_WITHHELD_AUTO_POST", "POLICY_OR_HIGH_RISK_REVIEW",
        }
    else:
        assert "GPT_REPORTED_UNRESOLVED" in verdict.reasons
