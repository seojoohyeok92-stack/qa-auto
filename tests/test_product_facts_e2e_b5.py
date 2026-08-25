"""B5 end-to-end: the evidence chain that lets a product fact settle a gate.

The chain asserted here is the whole point of B5:

    ProductKnowledgeService (safe facts only)
      -> provider prompt actually contains them
        -> provider answers from them
          -> validator grounds the claim against the same facts
            -> and only then may the product-fact hold be settled

Every link is checked by capturing the real prompt string a fake provider
received. No network, no GPT, no Naver, no DPS. ``data/product_facts.db`` is
read READ-ONLY; tests needing it skip when it is absent.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from answer.models import AnswerRequest, AnswerResult, AnswerStatus
from answer.providers.fake_gpt_provider import FakeGptProvider
from repositories.product_fact_repository import ProductFactRepository
from services.hybrid_answer_service import HybridAnswerService
from services.product_knowledge_service import ProductKnowledgeService

REAL_DB = Path("data") / "product_facts.db"
real_db = pytest.mark.skipif(
    not REAL_DB.is_file(), reason="data/product_facts.db not present"
)

# Verified in the shipped DB: hdmi_port_count=2, vesa 100x100, speaker YES.
M5_PRODUCT_ID = "10198648691"
M5_NAME = "삼성 M5 LS32DM501 80.1cm 스마트모니터"


def _provider(answer: str) -> FakeGptProvider:
    return FakeGptProvider(responses={
        "DRAFT": {
            "answer": answer, "used_facts": [], "missing_information": [],
            "requires_review": False, "confidence": 0.95,
        },
        "SELF_REVIEW": {
            "passed": True, "answered_all_questions": True,
            "facts_consistent": True, "has_speculation": False,
            "requires_review": False, "warnings": [], "reason": "ok",
        },
    })


def _rule_result() -> AnswerResult:
    return AnswerResult(
        status=AnswerStatus.NOT_SUPPORTED, category="기타/직원확인",
        reason="no rule", answer="", provider="rules",
        auto_answerable=False, needs_review=True, metadata={},
    )


def _knowledge(**kwargs):
    service = ProductKnowledgeService(ProductFactRepository(REAL_DB))
    return service.facts_for_inquiry(**kwargs)


def _run(question: str, answer: str, *, product_id: str = M5_PRODUCT_ID,
         knowledge=None):
    resolved = (
        knowledge if knowledge is not None
        else _knowledge(product_id=product_id, question=question)
    )
    provider = _provider(answer)
    request = AnswerRequest(
        question=question, product_name=M5_NAME,
        inquiry_type="PRODUCT_INQUIRY", store_code="OJE_PLUS",
        metadata={"product_id": product_id, "product_knowledge": resolved},
    )
    outcome = HybridAnswerService(provider).generate(request, _rule_result())
    drafts = [call for call in provider.calls if call["task"] == "DRAFT"]
    return outcome, (drafts[0]["prompt"] if drafts else ""), resolved


# ------------------------------------------------------- A. full chain works
@real_db
def test_A_verified_fact_reaches_prompt_and_grounds_the_answer():
    outcome, prompt, knowledge = _run(
        "HDMI 단자가 몇 개인가요?", "HDMI 단자는 2개입니다."
    )
    assert "hdmi_port_count" in knowledge.safe_field_keys()
    assert "PRODUCT_FACTS" in prompt
    assert "hdmi_port_count" in prompt
    assert outcome.validation is not None
    assert outcome.validation.passed is True, list(outcome.validation.errors)
    hybrid = outcome.result.metadata.get("hybrid", {})
    assert hybrid.get("product_facts_in_prompt") is True
    assert "hdmi_port_count" in hybrid.get("product_fact_fields", [])


@real_db
def test_A_prompt_carries_the_unknown_is_not_absent_rule():
    _, prompt, _ = _run("HDMI 단자가 몇 개인가요?", "HDMI 단자는 2개입니다.")
    assert "Never say a feature is absent" in prompt
    assert "Never infer a value from another model" in prompt
    assert "UNKNOWN" in prompt


# --------------------------------------------- B/C. wrong or invented numbers
@real_db
def test_B_number_that_contradicts_the_fact_is_not_grounded():
    outcome, _, _ = _run("HDMI 단자가 몇 개인가요?", "HDMI 단자는 3개입니다.")
    assert outcome.validation is not None
    assert outcome.validation.passed is False
    assert any("3개" in error for error in outcome.validation.errors)


@real_db
def test_C_number_invented_without_any_fact_is_not_grounded():
    """This listing has no verified ethernet count, yet the model states one."""

    outcome, prompt, knowledge = _run(
        "랜포트가 몇 개인가요?", "랜포트는 2개입니다.",
    )
    assert not knowledge.has_safe_facts, sorted(knowledge.safe_field_keys())
    assert "PRODUCT_FACTS" not in prompt
    assert outcome.validation is not None
    assert outcome.validation.passed is False
    assert any("2개" in error for error in outcome.validation.errors)


@real_db
@pytest.mark.parametrize(
    "question,answer",
    [
        ("베사홀 규격이 어떻게 되나요?", "베사 규격은 100x100mm입니다."),
        ("무게가 얼마인가요?", "무게는 6.6kg입니다."),
        ("스피커 출력이 얼마인가요?", "스피커 출력은 10W입니다."),
    ],
)
def test_R_units_ground_correctly_against_verified_values(question, answer):
    outcome, _, _ = _run(question, answer)
    assert outcome.validation is not None, question
    assert outcome.validation.passed is True, list(outcome.validation.errors)


@real_db
@pytest.mark.parametrize(
    "question,answer",
    [
        ("베사홀 규격이 어떻게 되나요?", "베사 규격은 200x200mm입니다."),
        ("무게가 얼마인가요?", "무게는 9.9kg입니다."),
        ("스피커 출력이 얼마인가요?", "스피커 출력은 20W입니다."),
    ],
)
def test_R_units_reject_values_that_contradict_the_fact(question, answer):
    outcome, _, _ = _run(question, answer)
    assert outcome.validation is not None, question
    assert outcome.validation.passed is False, question


# ------------------------------------------- D-I. unsafe facts never reach it
@real_db
@pytest.mark.parametrize(
    "reason",
    [
        "VERIFICATION_NEEDS_REVIEW",
        "RESOLUTION_CONFLICT",
        "SUPERSEDED_BY_LATER_RUN",
        "NO_ACTIVE_PROVENANCE",
        "PROVENANCE_NOT_VERIFIED",
        "MODEL_SCOPE_MISMATCH",
    ],
)
def test_DEFGHI_unsafe_fact_is_absent_from_prompt_and_evidence(reason):
    """Whatever made a fact unsafe, the model must never be shown it."""

    field = "hdmi_port_count"
    base = _knowledge(
        product_id=M5_PRODUCT_ID, question="HDMI 단자가 몇 개인가요?"
    )
    target = next(item for item in base.safe_facts if item.field_key == field)
    unsafe = dataclasses.replace(
        target, safe_for_answer=False, exclusion_reason=reason
    )
    knowledge = dataclasses.replace(
        base,
        safe_facts=tuple(
            item for item in base.safe_facts if item.field_key != field
        ),
        excluded_facts=base.excluded_facts + (unsafe,),
    )
    outcome, prompt, _ = _run(
        "HDMI 단자가 몇 개인가요?", "HDMI 단자는 2개입니다.",
        knowledge=knowledge,
    )
    assert field not in prompt, reason
    assert outcome.validation is not None
    assert outcome.validation.passed is False, reason


# ------------------------------------------------------------- J. compound
@real_db
def test_J_compound_keeps_the_answerable_fact_and_drops_the_unknown_one():
    knowledge = _knowledge(
        product_id=M5_PRODUCT_ID,
        questions=["HDMI 단자가 몇 개인가요", "높낮이 조절이 되나요"],
    )
    keys = knowledge.safe_field_keys()
    assert "hdmi_port_count" in keys
    assert "accessory_height_adjustment_mm" not in keys

    outcome, prompt, _ = _run(
        "HDMI 단자가 몇 개인가요? 높낮이 조절이 되나요?",
        "HDMI 단자는 2개입니다. 높낮이 조절 여부는 확인이 필요합니다.",
        knowledge=knowledge,
    )
    assert "hdmi_port_count" in prompt
    assert outcome.validation is not None
    assert outcome.validation.passed is True, list(outcome.validation.errors)


@real_db
def test_J_one_fact_never_vouches_for_another_field():
    """A verified HDMI count must not make a height claim groundable."""

    outcome, _, _ = _run(
        "높낮이 조절 범위가 얼마인가요?", "높낮이는 130mm 조절됩니다.",
    )
    assert outcome.validation is not None
    assert outcome.validation.passed is False


# ------------------------------------------------------- Q. missing != absent
@real_db
def test_Q_missing_fact_is_never_rendered_as_unsupported():
    knowledge = _knowledge(
        product_id=M5_PRODUCT_ID, question="높낮이 조절이 되나요?"
    )
    assert not knowledge.has_safe_facts
    _, prompt, _ = _run(
        "높낮이 조절이 되나요?", "정확한 사양은 확인이 필요합니다.",
        knowledge=knowledge,
    )
    assert "PRODUCT_FACTS" not in prompt
    for negative in ("지원하지 않습니다", "없습니다", "미지원"):
        assert negative not in knowledge.prompt_block()


# ------------------------------------------- K. DPS keeps its own authority
@real_db
def test_K_schedule_question_pulls_no_product_facts():
    knowledge = _knowledge(
        product_id=M5_PRODUCT_ID, question="설치 예정일이 언제인가요?"
    )
    assert knowledge.unavailable_reason == "NO_PRODUCT_FACT_TOPIC"
    assert not knowledge.has_safe_facts
    assert knowledge.prompt_block() == ""


@real_db
def test_K_mixed_schedule_and_spec_question_keeps_sources_separate():
    knowledge = _knowledge(
        product_id=M5_PRODUCT_ID,
        questions=["설치 예정일이 언제인가요", "HDMI 단자가 몇 개인가요"],
    )
    assert "hdmi_port_count" in knowledge.safe_field_keys()
    block = knowledge.prompt_block()
    assert "설치" not in block and "예정일" not in block


# --------------------------------------- other real products stay consistent
@real_db
@pytest.mark.parametrize(
    "product_id,question,answer,expected_pass",
    [
        # 비즈니스 TV: hdmi_port_count = 3
        ("12139453925", "HDMI 단자가 몇 개인가요?", "HDMI 단자는 3개입니다.", True),
        ("12139453925", "HDMI 단자가 몇 개인가요?", "HDMI 단자는 2개입니다.", False),
        # 오디세이 게이밍: refresh_rate = 180
        ("12601323000", "주사율이 어떻게 되나요?", "주사율은 180Hz입니다.", True),
        ("12601323000", "주사율이 어떻게 되나요?", "주사율은 240Hz입니다.", False),
        # 일반 사무용: refresh_rate = 100
        ("11844406044", "주사율이 어떻게 되나요?", "주사율은 100Hz입니다.", True),
    ],
)
def test_real_products_ground_against_their_own_values(
    product_id, question, answer, expected_pass
):
    outcome, _, _ = _run(question, answer, product_id=product_id)
    assert outcome.validation is not None
    assert outcome.validation.passed is expected_pass, (
        product_id, list(outcome.validation.errors)
    )


@real_db
def test_package_listing_separates_accessory_from_base_device():
    """이동식 스탠드 패키지: accessory facts stay labelled as accessory."""

    knowledge = _knowledge(
        product_id="13239109816",
        questions=["거치대 최대 하중", "받침대 규격"],
    )
    accessory = [
        item for item in knowledge.safe_facts
        if item.component_scope == "ACCESSORY"
    ]
    assert accessory, "package listing should expose accessory facts"
    assert all(
        item.field_key.startswith("accessory_") for item in accessory
    )
    block = knowledge.prompt_block()
    assert "ACCESSORY" in block


# --------------------------------------------------------------------------
# Gate: a verified fact settles the product-fact hold and nothing else
# --------------------------------------------------------------------------
_V_PASS = {"status": "PASS", "passed": True, "errors": [],
           "review_signals": [], "warnings": []}


def _gate(guard, *, plan_extra=None, review_status="PENDING",
          validator=None, route="GPT_FALLBACK", validation_status="PASS"):
    from services.auto_processing_eligibility_service import (
        AutoProcessingEligibilityService,
    )

    resolved = validator or _V_PASS
    plan = {
        "analysis": {"manual_review_required": False,
                     "inquiry_subtype": "PRODUCT_SPEC_OR_FEATURE",
                     "question_category": "PRODUCT_GENERAL",
                     "confidence": 0.92, "manual_review_sources": []},
        "is_high_risk": False, "needs_staff_review": False,
        "requires_order_lookup": False, "requires_dps_lookup": False,
        "order_id_status": "MISSING", "order_lookup_status": "NOT_STARTED",
        "dps_lookup_status": "NOT_STARTED",
        "valid_dps_snapshot_available": False,
    }
    if plan_extra:
        plan.update(plan_extra)
    return AutoProcessingEligibilityService().evaluate(
        inquiry={"id": 1, "content": "HDMI 단자가 몇 개인가요?",
                 "inquiry_type": "PRODUCT_INQUIRY", "source_answered": 0,
                 "post_status": "NOT_POSTED"},
        draft={"id": 1, "original_answer": "HDMI 단자는 2개입니다.",
               "review_status": review_status,
               "validation_status": validation_status,
               "validator_result_json": resolved, "posted": 0,
               "metadata_json": {"selected_answer_route": route,
                                 "processing_plan": plan,
                                 "product_fact_guard": guard,
                                 "hybrid": {"validation": resolved}}},
        route=route,
    )


def test_gate_verified_product_fact_settles_the_product_fact_hold():
    verdict = _gate({"sensitive": True, "current_fact_verified": True,
                     "current_fact_source": "PRODUCT_FACTS_DB"})
    assert verdict.decision == "SAFE", verdict.reasons


def test_gate_unverified_product_fact_still_holds():
    verdict = _gate({"sensitive": True, "current_fact_verified": False})
    assert verdict.decision == "REVIEW_REQUIRED"
    assert "PRODUCT_FACT_NOT_VERIFIED" in verdict.reasons


@pytest.mark.parametrize(
    "label,kwargs,expected_reason",
    [
        ("high risk", {"plan_extra": {"is_high_risk": True}},
         "POLICY_OR_HIGH_RISK_REVIEW"),
        ("dps not trusted",
         {"plan_extra": {"requires_dps_lookup": True,
                         "dps_lookup_status": "FAILED"}},
         "DPS_RESULT_NOT_TRUSTED"),
        ("dps snapshot",
         {"plan_extra": {"requires_dps_lookup": True,
                         "dps_lookup_status": "SUCCESS"}},
         "DPS_SNAPSHOT_NOT_VALIDATED"),
        ("in review", {"review_status": "IN_REVIEW"},
         "DRAFT_REVIEW_REQUIRED"),
        ("validator errors",
         {"validation_status": "FAILED_INVALID_CONTENT",
          "validator": {"status": "BLOCK", "passed": False,
                        "errors": ["근거 없는 수치"], "review_signals": []}},
         "VALIDATOR_NOT_PASS"),
        ("validator review signal",
         {"validation_status": "PASS_REVIEW_REQUIRED",
          "validator": {"status": "PASS_REVIEW_REQUIRED", "passed": True,
                        "errors": [], "review_signals": ["확인 필요"]}},
         "VALIDATOR_REVIEW_REQUIRED"),
    ],
)
def test_gate_verified_product_fact_settles_nothing_else(
    label, kwargs, expected_reason
):
    """A product fact may only ever answer the product-fact question."""

    verdict = _gate(
        {"sensitive": True, "current_fact_verified": True}, **kwargs
    )
    assert verdict.decision == "REVIEW_REQUIRED", label
    assert expected_reason in verdict.reasons, (label, verdict.reasons)


def test_gate_idempotency_beats_a_verified_product_fact():
    from services.auto_processing_eligibility_service import (
        AutoProcessingEligibilityService,
    )

    verdict = AutoProcessingEligibilityService().evaluate(
        inquiry={"id": 1, "content": "HDMI 단자가 몇 개인가요?",
                 "source_answered": 1, "post_status": "POSTED"},
        draft={"id": 1, "original_answer": "HDMI 단자는 2개입니다.",
               "review_status": "PENDING", "validation_status": "PASS",
               "validator_result_json": _V_PASS, "posted": 0,
               "metadata_json": {
                   "selected_answer_route": "GPT_FALLBACK",
                   "processing_plan": {"analysis": {}},
                   "product_fact_guard": {"sensitive": True,
                                          "current_fact_verified": True},
                   "hybrid": {"validation": _V_PASS}}},
        route="GPT_FALLBACK",
    )
    assert verdict.decision == "BLOCKED"
    assert verdict.stage == "IDEMPOTENCY"


# --------------------------------------------------------------------------
# O/P. Only SAFE reaches the post client; unsafe never does.
# --------------------------------------------------------------------------
def _pipeline_run(tmp_path, *, guard, review_status="PENDING"):
    from answer.models import AnswerResult as _AR, AnswerStatus as _AS
    from repositories.answer_repository import AnswerRepository
    from services.auto_post_pipeline_service import AutoPostPipelineService
    from tests.test_auto_post_pipeline import (
        MockClient, make_database, make_inquiry, post_service,
    )

    database = make_database(tmp_path)
    inquiry_id = make_inquiry(database)
    AnswerRepository(database).create_program_draft(
        inquiry_id,
        _AR(status=_AS.GENERATED, category="상품사양", reason="test",
            answer="HDMI 단자는 2개입니다.", provider="rules",
            auto_answerable=True, needs_review=False,
            metadata={
                "selected_answer_route": "TEMPLATE",
                "generation_mode": "TEMPLATE",
                "validator_result": {"status": "PASS", "passed": True},
                "product_fact_guard": guard,
                "hybrid": {"validation": {"status": "PASS", "passed": True,
                                          "errors": [], "review_signals": []}},
            }),
    )
    if review_status != "PENDING":
        draft = AnswerRepository(database).active_for_inquiry(inquiry_id)
        AnswerRepository(database).update_review_status(
            int(draft["id"]), review_status
        )
    client = MockClient()
    outcome = AutoPostPipelineService(
        database,
        post_service=post_service(database, client),
        dps_status_provider=lambda: {"session_status": "READY"},
    ).run_pending(run_id="B5", owner_id="B5", max_retries=1)
    return outcome, client


def test_P_safe_product_fact_answer_reaches_the_post_client(tmp_path):
    outcome, client = _pipeline_run(
        tmp_path,
        guard={"sensitive": True, "current_fact_verified": True,
               "current_fact_source": "PRODUCT_FACTS_DB"},
    )
    assert outcome.succeeded_count == 1
    assert client.calls == 1


def test_O_unverified_product_fact_never_reaches_the_post_client(tmp_path):
    outcome, client = _pipeline_run(
        tmp_path,
        guard={"sensitive": True, "current_fact_verified": False},
    )
    assert outcome.succeeded_count == 0
    assert client.calls == 0


def test_O_in_review_never_reaches_the_post_client(tmp_path):
    outcome, client = _pipeline_run(
        tmp_path,
        guard={"sensitive": True, "current_fact_verified": True},
        review_status="IN_REVIEW",
    )
    assert outcome.succeeded_count == 0
    assert client.calls == 0
