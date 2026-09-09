"""Architecture holdouts for the GPT-thinks / CODE-acts boundary.

These are deliberately not tied to the five forensic inquiry texts.  They pin
the authority boundary: unresolved evidence holds, a vague review preference
does not, and policy evidence can reach GPT② without weakening model facts.
"""
from __future__ import annotations

from services.draft_generation_service import DraftGenerationService
from services.learning_compatibility_service import (
    LearningCompatibilityService,
    extract_product_identity,
)
from services.auto_processing_eligibility_service import (
    AutoProcessingEligibilityService,
)
from ui.answer_status_presenter import build_decision_trace


def _trace_draft(*, evidence, unresolved=(), post_status="NOT_POSTED"):
    return {
        "original_answer": "근거 기반 답변",
        "validation_status": "PASS",
        "review_status": "PENDING",
        "validator_result_json": {"passed": True, "status": "PASS"},
        "metadata_json": {
            "semantic_routing": {"understanding": {"usable": True}},
            "hybrid": {
                "answer_pipeline": "GPT_UNDERSTAND_RETRIEVE_ANSWER",
                "draft": {
                    "requires_review": bool(unresolved),
                    "can_auto_post": not bool(unresolved),
                    "unresolved": list(unresolved),
                    "subquestion_results": [{"answered": not bool(unresolved)}],
                },
                "subquestion_evidence": evidence,
                "retrieval": {"learning": {"candidate_count": 1}},
            },
        },
    }


def test_resolved_atoms_override_conservative_provider_publish_preference() -> None:
    result = DraftGenerationService._apply_reported_resolution({
        "answer": "근거 기반 답변",
        "requires_review": True,
        "can_auto_post": False,
        "missing_information": ["세부 조건"],
        "unresolved": [],
        "subquestion_results": [
            {"subquestion": "일반 정책 문의", "answered": True,
             "status": "ANSWERABLE"},
        ],
    })

    assert result["provider_requires_review"] is True
    assert result["provider_can_auto_post"] is False
    assert result["requires_review"] is False
    assert result["can_auto_post"] is True


def test_unresolved_atom_remains_the_only_gpt_evidence_review_authority() -> None:
    result = DraftGenerationService._apply_reported_resolution({
        "requires_review": False,
        "can_auto_post": True,
        "missing_information": ["신청 절차"],
        "unresolved": [],
        "subquestion_results": [
            {"subquestion": "별도 신청 절차", "answered": False,
             "status": "NO_RELIABLE_SOURCE"},
        ],
    })

    assert result["requires_review"] is True
    assert result["can_auto_post"] is False
    assert result["unresolved"] == ["별도 신청 절차"]


def test_policy_candidate_survives_product_fact_question_but_model_fact_does_not() -> None:
    service = LearningCompatibilityService()
    current = extract_product_identity(
        product_id="current-50", product_name="삼성 비즈니스 TV 50인치"
    )
    other = extract_product_identity(
        product_id="other-43", product_name="삼성 비즈니스 TV 43인치"
    )

    policy = service.evaluate(
        current_question="새 제품 설치 때 기존 제품 수거 절차가 궁금합니다.",
        current_product=current,
        candidate_question="기존 제품 수거는 동일 수량 기준으로 진행됩니다.",
        candidate_answer="추가 수거는 설치 기사에게 문의해 주세요.",
        candidate_product=other,
        candidate_metadata={"product_scope": "POLICY"},
        query_is_product_fact=True,
    )
    model_fact = service.evaluate(
        current_question="새 제품 설치 때 기존 제품 수거 절차가 궁금합니다.",
        current_product=current,
        candidate_question="HDMI 단자 수가 궁금합니다.",
        candidate_answer="HDMI 단자는 3개입니다.",
        candidate_product=other,
        candidate_metadata={"product_scope": "MODEL"},
        query_is_product_fact=True,
    )

    assert policy.hard_reject is False
    assert model_fact.hard_reject is True
    assert model_fact.product_match == "MISMATCH"


def test_legacy_hostile_metadata_cannot_veto_resolved_gpt2_draft() -> None:
    """Persistence/eligibility must not resurrect legacy semantic authority."""

    draft = {
        "original_answer": "근거에 따른 일반 안내입니다.",
        "validation_status": "PASS",
        # Persisted legacy state is deliberately hostile: none of it may
        # override a resolved current GPT② evidence verdict.
        "review_status": "NEEDS_REVIEW",
        "validator_result_json": {"passed": True, "status": "PASS"},
        "metadata_json": {
            "requires_manual_review": True,
            "processing_plan": {
                "needs_staff_review": False,
                "is_high_risk": True,
                "analysis": {
                    "inquiry_subtype": "UNCLASSIFIED",
                    "confidence": 0.01,
                    "manual_review_required": True,
                    "auto_answerable": False,
                },
            },
            "semantic_routing": {"understanding": {"usable": True}},
            "hybrid": {
                "answer_pipeline": "GPT_UNDERSTAND_RETRIEVE_ANSWER",
                "draft": {
                    "requires_review": False,
                    "can_auto_post": True,
                    "unresolved": [],
                    "subquestion_results": [
                        {"answered": True, "status": "ANSWERABLE"},
                    ],
                    "missing_information": [],
                    "required_missing_information": [],
                },
                "self_review": {"requires_review": False},
            },
        },
    }
    verdict = AutoProcessingEligibilityService().evaluate(
        inquiry={"source_answered": False, "post_status": "NOT_POSTED"},
        draft=draft,
        route="GPT_FALLBACK",
    )

    assert verdict.safe, verdict
    assert not set(verdict.reasons) & {
        "ANSWER_REQUIRES_MANUAL_REVIEW", "PROCESSING_PLAN_REQUIRES_REVIEW",
        "DRAFT_REVIEW_REQUIRED",
        "GPT_WITHHELD_AUTO_POST", "POLICY_OR_HIGH_RISK_REVIEW",
    }


def test_dashboard_trace_keeps_source_retrieval_evidence_safety_and_execution_distinct() -> None:
    no_source = build_decision_trace(
        inquiry={"post_status": "NOT_POSTED"},
        draft=_trace_draft(evidence=[{"status": "NO_RELIABLE_SOURCE"}],
                           unresolved=["질문"]), route="GPT_FALLBACK",
    )
    assert (no_source.root_stage, no_source.root_cause) == (
        "SOURCE", "SOURCE_MISSING"
    )

    unresolved = build_decision_trace(
        inquiry={"post_status": "NOT_POSTED"},
        draft=_trace_draft(evidence=[{"status": "ANSWERABLE"}],
                           unresolved=["근거 부족 atom"]), route="GPT_FALLBACK",
    )
    assert (unresolved.root_stage, unresolved.root_cause) == (
        "GPT_EVIDENCE", "EVIDENCE_UNRESOLVED"
    )

    execution = build_decision_trace(
        inquiry={"post_status": "POST_FAILED"},
        draft=_trace_draft(evidence=[{"status": "ANSWERABLE"}]),
        route="GPT_FALLBACK",
    )
    assert (execution.auto_post, execution.root_stage, execution.root_cause) == (
        "FAILED", "EXECUTION", "EXECUTION_FAILURE"
    )

    success = build_decision_trace(
        inquiry={"post_status": "POSTED", "source_answered": True},
        draft=_trace_draft(evidence=[{"status": "ANSWERABLE"}]),
        route="GPT_FALLBACK",
    )
    assert (success.gpt1, success.source, success.gpt2, success.hard_safety,
            success.auto_post, success.root_cause) == (
        "PASS", "SUFFICIENT", "RESOLVED", "PASS", "SUCCESS", "NONE"
    )
