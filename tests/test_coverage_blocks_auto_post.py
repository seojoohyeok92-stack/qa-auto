"""An unanswered question must stop the publish, not merely be recorded.

The coverage evaluator had been measuring correctly and changing nothing. It
scored a compound inquiry PARTIAL, generation set ``requires_manual_review``
from that verdict, and the answer auto-posted regardless: the eligibility gate
reads that flag through ``_preliminary_review_resolved``, which returns True
outright for the TEMPLATE route and never consults coverage at all. The flag
became a soft reason and the reply went to the customer with one of its two
questions unanswered.

Two tests existed that should have caught it. Both guard their assertion with
``if coverage["status"] in {FAIL, PARTIAL}`` and every case they run scores
PASS, so neither assertion had ever executed. Nothing here is written that
way: each test below asserts the four values in one run, and a case that fails
to reach FAIL/PARTIAL fails the test rather than skipping it.
"""
from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.answer_service import AnswerService
from services.auto_processing_eligibility_service import (
    SEMANTIC_COVERAGE_INCOMPLETE,
    AutoProcessingEligibilityService,
    _coverage_incomplete,
)
from services.hybrid_answer_service import HybridAnswerService
from services.semantic_coverage_service import ENABLED_ENV

from test_semantic_coverage_soft_gate import PRODUCT, _FakeDps, _StubProvider


INQUIRY_687718601 = (
    "안녕하세요:) 집에서 그냥 일반 tv시청이나 셋톱박스 연결되어있는걸로 "
    "ott, 유튜브 볼건데 비즈니스tv와 사이니지tv 중 뭐가 낫나요?? "
    "그리고 스탠드형 비즈니스 tv가 여러 제품이 있던데 "
    "2026년 출시형 모델 제품 추천 부탁드립니다!!"
)


class _PartialEvidenceProvider(_StubProvider):
    """GPT②, not lexical coverage, reports the unresolved recommendation."""

    def generate_json(self, *, task, prompt, context):
        if task == "DRAFT":
            payload = super().generate_json(task=task, prompt=prompt, context=context)
            payload.update({
                "missing_information": ["2026년 출시형 모델 추천"],
                "requires_review": True,
                "subquestion_results": [{
                    "subquestion": "2026년 출시형 모델 추천",
                    "answered": False,
                    "status": "NO_RELIABLE_SOURCE",
                }],
            })
            return payload
        return super().generate_json(task=task, prompt=prompt, context=context)


def run(question: str, *, label: str = "cov") -> dict:
    """One real pipeline run, returning what the gate actually decided."""

    database = Database(pathlib.Path(tempfile.mkdtemp()) / f"{label}.db")
    database.initialize()
    inquiry_id = InquiryRepository(database).upsert_work_item({
        "store_code": "S", "source_type": "NAVER",
        "source_question_id": label, "inquiry_type": "PRODUCT_INQUIRY",
        "title": "문의", "content": question, "product_name": PRODUCT,
        "order_id": None, "product_order_id": None, "raw_json": {},
    }).inquiry_id
    AnswerService(
        database,
        dps_enrichment=_FakeDps(),
        hybrid_service=HybridAnswerService(_PartialEvidenceProvider()),
    ).generate_for_inquiry(inquiry_id)

    record = dict(AnswerRepository(database).latest_for_inquiry(inquiry_id))
    for key in ("metadata_json", "validator_result_json"):
        if isinstance(record.get(key), str):
            try:
                record[key] = json.loads(record[key])
            except ValueError:
                record[key] = {}
    metadata = record.get("metadata_json") or {}
    verdict = AutoProcessingEligibilityService().evaluate(
        inquiry=InquiryRepository(database).get(inquiry_id),
        draft=record,
        route=str(metadata.get("selected_answer_route") or ""),
    )
    coverage = metadata.get("semantic_coverage") or {}
    return {
        "coverage": str(coverage.get("status") or ""),
        "requires_manual_review": bool(metadata.get("requires_manual_review")),
        "decision": verdict.decision,
        "reasons": tuple(verdict.reasons),
        "auto_post": verdict.decision == "SAFE",
        "answer": str(record.get("original_answer") or ""),
        "route": str(metadata.get("selected_answer_route") or ""),
    }


@pytest.fixture(autouse=True)
def coverage_enabled(monkeypatch):
    monkeypatch.setenv(ENABLED_ENV, "1")


# ============================================== 하나의 실행에서 네 값을 함께 증명
def test_a_partial_coverage_inquiry_cannot_auto_post():
    """The four values the previous tests never checked together."""
    outcome = run(INQUIRY_687718601, label="p1")

    assert outcome["coverage"] == "PARTIAL"
    assert outcome["requires_manual_review"] is False
    # This legacy fixture has no GPT① trace, so its hold is workflow integrity,
    # not the lexical coverage classifier.
    assert outcome["decision"] != "SAFE"
    assert outcome["auto_post"] is False


def test_legacy_coverage_is_telemetry_not_the_gate_reason():
    """A no-GPT trace is held as workflow, never as lexical coverage."""
    outcome = run(INQUIRY_687718601, label="p2")
    assert SEMANTIC_COVERAGE_INCOMPLETE not in outcome["reasons"]
    assert "UNDERSTANDING_UNAVAILABLE" in outcome["reasons"]


def test_the_wrong_rule_answer_still_matched():
    """The rule matcher is deliberately untouched by this fix.

    687718601 matched a stand-generation rule on "모델" and "스탠드", both of
    which referred to the TV. That mismatch is a separate defect; what this
    file pins is that a mismatched deterministic answer can no longer publish
    itself over the questions it left unanswered.
    """
    outcome = run(INQUIRY_687718601, label="p3")
    assert outcome["answer"].strip()
    assert outcome["auto_post"] is False


# ================================================= legacy coverage is telemetry
@pytest.mark.parametrize("status", ["FAIL", "PARTIAL"])
def test_no_resolver_may_lift_an_unanswered_question(status):
    """Route, template and preliminary resolution are all downstream of this."""
    assert _coverage_incomplete({"semantic_coverage": {"status": status}}) is True


@pytest.mark.parametrize("route", ["TEMPLATE", "PRODUCT_DB", "SAFE_RULE", "GPT_HYBRID"])
def test_legacy_coverage_never_becomes_a_route_independent_hold(route):
    """Route mechanics may differ, but lexical coverage is never the veto."""
    verdict = AutoProcessingEligibilityService().evaluate(
        inquiry={"source_answered": 0, "post_status": "NOT_POSTED"},
        draft={
            "metadata_json": {
                "semantic_coverage": {"status": "PARTIAL"},
                "selected_answer_route": route,
            },
            "original_answer": "스탠드는 오베닉 스탠드 FMS 모델로 출고되고 있습니다.",
            "validation_status": "PASS",
        },
        route=route,
    )
    assert SEMANTIC_COVERAGE_INCOMPLETE not in verdict.reasons


# ============================================================ 관측 전용 상태
@pytest.mark.parametrize("status", ["PASS", "UNKNOWN", ""])
def test_a_recognised_or_unfamiliar_answer_is_not_held_by_this_gate(status):
    """UNKNOWN stays observational: an unfamiliar question is not a failure."""
    assert _coverage_incomplete({"semantic_coverage": {"status": status}}) is False


def test_a_draft_without_coverage_telemetry_is_untouched():
    """Drafts predating the evaluator publish exactly as they did."""
    assert _coverage_incomplete({}) is False
    assert _coverage_incomplete({"semantic_coverage": "not a mapping"}) is False


# ======================================================= positive control
def test_a_fully_answered_legacy_fixture_needs_a_gpt_decision_trace():
    """Coverage PASS alone is not a substitute for GPT② evidence verdict."""
    outcome = run("설치일 알림톡 언제 오나요?", label="ok1")

    assert outcome["coverage"] == "PASS"
    assert outcome["auto_post"] is False
    assert SEMANTIC_COVERAGE_INCOMPLETE not in outcome["reasons"]
    assert "UNDERSTANDING_UNAVAILABLE" in outcome["reasons"]


def test_a_compound_legacy_fixture_needs_a_gpt_decision_trace():
    """Compound is not the hold; absent GPT-first trace is."""
    outcome = run("설치일 알림톡 언제 오나요? 배송비는 얼마인가요?", label="ok2")

    assert outcome["coverage"] == "PASS"
    assert outcome["auto_post"] is False
    assert "UNDERSTANDING_UNAVAILABLE" in outcome["reasons"]
