"""The Pre-generation Gate, and what it must never stop answering.

The gate exists to stop paying for an answer that could not be published
whatever it said.  Its danger is entirely in the other direction: a gate that
skips too eagerly silently stops answering inquiries the system used to answer,
and nothing fails loudly when it does.  So most of this file pins down the
inquiries that must still reach the provider.

The safety rule it must never take over: an answer that *can* be composed is
still judged after it exists.  The gate is not allowed to be the reason
something was published, only the reason nothing was written.
"""
from __future__ import annotations

import pytest

from answer.exceptions import GenerationSkippedError
from answer.governance_models import GptProviderSettings
from answer.models import AnswerResult, AnswerStatus
from answer.providers.fake_gpt_provider import FakeGptProvider
from answer.providers.resilient_json_provider import ResilientJsonProvider
from answer.source_adapter import answer_request_from_inquiry
from services.hybrid_answer_service import HybridAnswerService
from services.inquiry_analysis_service import InquiryAnalysisService
from services.pre_generation_gate import PreGenerationGate

ANALYSIS = InquiryAnalysisService()

STAND_Q = "43인치 TV 스탠드 탈부착 되나요?"
STAND_LEARNED_Q = "스탠드 탈부착 가능한가요?"
STAND_YES = "네, 스탠드는 탈부착 가능합니다."
STAND_NO = "스탠드는 탈부착이 불가능합니다."


# --------------------------------------------------------------- harness
def inquiry_row(content: str, **overrides) -> dict:
    row = {
        "id": 9001,
        "source_question_id": "900100",
        "store_code": "OJE_PLUS",
        "inquiry_type": "PRODUCT_INQUIRY",
        "title": content.splitlines()[0],
        "content": content,
        "product_name": "삼성 43인치 TV UN43DU7030",
        "product_id": "P-43",
        "raw_json": {},
    }
    row.update(overrides)
    return row


def request_for(content: str, *, plan: dict | None = None, **overrides):
    request = answer_request_from_inquiry(inquiry_row(content, **overrides))
    request.metadata["dps"] = {
        "lookup_required": False, "lookup_status": "NOT_REQUIRED", "warnings": [],
    }
    request.metadata["phase9_analysis"] = ANALYSIS.analyze(request).to_dict()
    request.metadata["processing_plan"] = plan or {}
    return request


def rule(answer: str = "안내드립니다.", *, needs_review: bool = True):
    return AnswerResult(
        status=AnswerStatus.NEEDS_REVIEW if needs_review else AnswerStatus.GENERATED,
        category="상품", reason="Rule", answer=answer, provider="rules",
        auto_answerable=not needs_review, needs_review=needs_review,
        matched_rule="상품",
    )


def provider_for(answer: str, **overrides) -> FakeGptProvider:
    draft = {
        "answer": answer, "confidence": 0.9, "used_facts": [],
        "missing_information": [], "requires_review": False, "warnings": [],
    }
    draft.update(overrides)
    return FakeGptProvider(responses={"DRAFT": draft})


def learning(*items, question: str = STAND_Q, evidence_source: str = "ACTIVE_POSITIVE_LEARNING"):
    """A retrieval result shaped exactly like the real one."""

    return {
        "similar_approved_answers": list(items),
        "seller_style_examples": [],
        "historical_cases": [],
        "subquestion_evidence": [{
            "subquestion": question,
            "status": "ANSWERABLE" if items else "NO_RELIABLE_SOURCE",
            "source": evidence_source if items else None,
            "learning_ids": [i.get("learning_example_id") for i in items],
            "historical_case_ids": [],
            "feedback_signal_ids": [],
            "answer_required": bool(items),
            "evidence_coverage": "SUPPORTED" if items else "UNSUPPORTED",
        }],
    }


def approved(
    learning_id: int,
    answer: str,
    *,
    question: str = STAND_Q,
    match: str = "EXACT_MODEL",
    authority: str = "APPROVED",
    support: float = 0.9,
):
    return {
        "learning_example_id": learning_id,
        "learning_source": "APPROVED_ANSWER",
        "question": STAND_LEARNED_Q,
        "answer": answer,
        "rating": 5,
        "relevance": 0.8,
        "answer_support": support,
        "authority": authority,
        "compatibility": {"product_match": match, "product_scope": "MODEL"},
        "matched_subquestion": question,
    }


def run(request, provider, rule_result, context=None):
    service = HybridAnswerService(
        ResilientJsonProvider(provider, GptProviderSettings()),
        learning_context_provider=(lambda facts, intent: dict(context or {})),
    )
    outcome = service.generate(request, rule_result)
    return outcome, outcome.result.metadata.get("hybrid") or {}


def calls(provider: FakeGptProvider) -> int:
    return len(getattr(provider, "calls", []) or [])


# =========================================================== gate unit
def test_gate_skips_only_what_no_answer_could_clear():
    skip = PreGenerationGate.evaluate_plan(
        analysis={"manual_review_required": True,
                  "manual_review_sources": ["HIGH_RISK_OR_DISPUTE"],
                  "inquiry_subtype": "HIGH_RISK_OR_DISPUTE"},
        plan={"needs_staff_review": True, "is_high_risk": True},
    )
    assert skip.skip_generation is True
    assert "POLICY_OR_HIGH_RISK_REVIEW" in skip.reasons


def test_gate_never_skips_a_classifier_gap():
    """The hold an answer plus a clean validator is allowed to clear."""

    decision = PreGenerationGate.evaluate_plan(
        analysis={"manual_review_required": True,
                  "manual_review_sources": ["UNCLASSIFIED"],
                  "inquiry_subtype": "UNCLASSIFIED"},
        plan={"needs_staff_review": True},
    )
    assert decision.skip_generation is False


def test_gate_never_skips_a_compound_inquiry():
    """Five answerable parts are not worthless because a sixth is high risk."""

    decision = PreGenerationGate.evaluate_plan(
        analysis={"manual_review_required": True,
                  "inquiry_subtype": "COMPOUND_MULTI_INTENT",
                  "manual_review_sources": ["UNCLASSIFIED", "HIGH_RISK_OR_DISPUTE"]},
        plan={"needs_staff_review": True},
    )
    assert decision.skip_generation is False


def test_gate_ignores_soft_signals():
    for analysis in ({"confidence": 0.1}, {"question_category": "UNKNOWN"}):
        assert PreGenerationGate.evaluate_plan(
            analysis=analysis, plan={}
        ).skip_generation is False


@pytest.mark.parametrize("status", ["NO_RELIABLE_SOURCE", "NEEDS_DPS", "ANSWERABLE"])
def test_gate_only_skips_on_conflict_not_on_a_gap(status):
    """A missing source may still be answered by asking the customer."""

    decision = PreGenerationGate.evaluate_evidence(
        {"subquestion_evidence": [{"subquestion": "q", "status": status}]}
    )
    assert decision.skip_generation is False


# ================================================= A. hard review, no provider
def test_A_staff_action_inquiry_never_reaches_the_provider():
    request = request_for("배송 중 파손되면 어떻게 하나요?",
                          plan={"needs_staff_review": True, "is_high_risk": True})
    provider = provider_for("무엇이든 답변")
    with pytest.raises(GenerationSkippedError) as raised:
        run(request, provider, rule(), learning())
    assert calls(provider) == 0
    assert raised.value.stage == "PROCESSING_PLAN"
    assert "POLICY_OR_HIGH_RISK_REVIEW" in raised.value.reasons


# ====================== B. no product fact + approved learning -> still answers
def test_B_missing_product_fact_alone_does_not_block_generation():
    """The whole point: an empty DB row is not a negative fact."""

    request = request_for(STAND_Q)
    provider = provider_for(STAND_YES)
    outcome, hybrid = run(
        request, provider, rule(STAND_YES),
        learning(approved(11, STAND_YES)),
    )
    # >= 1, not == 1: a validator finding may trigger the existing bounded
    # corrective regeneration. What matters here is that generation ran at
    # all -- a missing product fact did not stop it.
    assert calls(provider) >= 1, "approved Learning was treated as no evidence"
    verdict = hybrid["approved_learning_evidence"]
    assert verdict["usable"] is True
    assert verdict["reason"] == "APPROVED_LEARNING_SUPPORTED"
    assert verdict["learning_ids"] == [11]


def test_B_approved_learning_satisfies_the_product_fact_requirement():
    from services.learning_evidence_policy import evaluate

    decision = evaluate(
        learning_context=learning(approved(11, STAND_YES)), safe_facts=(),
    )
    assert decision.usable is True
    assert decision.conflict is False


# ========================================== C. another model must not propagate
@pytest.mark.parametrize(
    "match", ["CATEGORY_UNCERTAIN", "POLICY_COMPATIBLE", "MISMATCH", ""]
)
def test_C_learning_from_another_model_is_not_evidence(match):
    from services.learning_evidence_policy import evaluate

    decision = evaluate(
        learning_context=learning(approved(11, STAND_YES, match=match)),
        safe_facts=(),
    )
    assert decision.usable is False
    assert decision.reason == "NO_QUALIFYING_APPROVED_LEARNING"


def test_C_only_an_exact_product_identity_counts():
    from services.learning_evidence_policy import EXACT_PRODUCT_MATCHES

    assert EXACT_PRODUCT_MATCHES == {"EXACT_MODEL", "EXACT_PRODUCT", "EXACT_NAME"}


# ================================== D. product fact vs learning -> no auto-post
class _Fact:
    def __init__(self, field_key, value):
        self.field_key, self.value = field_key, value


def test_D_verified_fact_contradicting_learning_is_a_conflict():
    from services.learning_evidence_policy import evaluate

    decision = evaluate(
        learning_context=learning(approved(11, STAND_YES)),
        safe_facts=[_Fact("stand_detachable", "false")],
    )
    assert decision.conflict is True
    assert decision.usable is False
    assert decision.reason == "PRODUCT_FACT_VS_LEARNING_CONFLICT"


def test_D_learning_never_silently_overwrites_a_verified_fact():
    request = request_for(STAND_Q)
    provider = provider_for(STAND_YES)
    request.metadata["product_knowledge"] = _Knowledge(
        [_Fact("stand_detachable", "false")]
    )
    with pytest.raises(GenerationSkippedError) as raised:
        run(request, provider, rule(), learning(approved(11, STAND_YES)))
    assert calls(provider) == 0
    assert raised.value.reasons == ("EVIDENCE_CONFLICT",)


class _Knowledge:
    """Minimal stand-in for ProductKnowledgeResult's read surface."""

    def __init__(self, facts):
        self.safe_facts = tuple(facts)

    def prompt_block(self):
        return ""

    def evidence_text(self):
        return ""


# ================================ E. approved learning contradicting itself
def test_E_two_approved_answers_that_disagree_are_never_picked_between():
    from services.learning_evidence_policy import evaluate

    decision = evaluate(
        learning_context=learning(
            approved(11, STAND_YES), approved(12, STAND_NO)
        ),
        safe_facts=(),
    )
    assert decision.conflict is True
    assert decision.reason == "APPROVED_LEARNING_CONFLICT"
    # Neither was chosen -- not the newer one, not the higher scoring one.
    assert decision.learning_ids == ()


def test_E_conflicting_learning_skips_generation_entirely():
    request = request_for(STAND_Q)
    provider = provider_for(STAND_YES)
    with pytest.raises(GenerationSkippedError):
        run(request, provider,
            rule(), learning(approved(11, STAND_YES), approved(12, STAND_NO)))
    assert calls(provider) == 0


def test_E_agreeing_answers_are_not_a_conflict():
    from services.learning_evidence_policy import evaluate

    decision = evaluate(
        learning_context=learning(
            approved(11, STAND_YES),
            approved(12, "스탠드 분리도 가능합니다."),
        ),
        safe_facts=(),
    )
    assert decision.conflict is False


# ========================================================= K. AirPlay conflict
def test_K_contradicting_airplay_learning_is_not_answered_automatically():
    """지원/미지원 both approved, no verified fact -- a person decides."""

    question = "아이폰 데이터로 미러링하면 인터넷 연결 없이 가능한가요?"
    context = learning(
        approved(21, "에어플레이 미러링을 지원합니다.", question=question),
        approved(22, "해당 모델은 에어플레이를 지원하지 않습니다.", question=question),
        question=question,
    )
    request = request_for(question)
    provider = provider_for("가능합니다.")
    with pytest.raises(GenerationSkippedError) as raised:
        run(request, provider, rule(), context)
    assert calls(provider) == 0
    assert raised.value.reasons == ("EVIDENCE_CONFLICT",)


# ============================================== governance / authority tiers
def test_unapproved_learning_is_not_evidence():
    from services.learning_evidence_policy import evaluate

    assert evaluate(
        learning_context=learning(approved(11, STAND_YES, authority="AUTO")),
        safe_facts=(),
    ).usable is False


def test_historical_style_examples_are_never_promoted_to_evidence():
    """seller_style_examples carry tone, not facts. They must stay out."""

    from services.learning_evidence_policy import evaluate

    context = learning()
    context["seller_style_examples"] = [approved(31, STAND_YES)]
    assert evaluate(learning_context=context, safe_facts=()).usable is False


@pytest.mark.parametrize(
    "answer",
    [
        "탈부착 가능할 것 같습니다.",
        "확인이 필요합니다.",
        "아마 가능합니다.",
        "탈부착 여부는 확인 후 안내드리겠습니다.",
    ],
)
def test_a_hedged_approved_answer_is_not_a_verified_fact(answer):
    from services.learning_evidence_policy import evaluate

    assert evaluate(
        learning_context=learning(approved(11, answer)), safe_facts=(),
    ).usable is False


def test_weakly_supporting_learning_is_not_evidence():
    from services.learning_evidence_policy import evaluate

    assert evaluate(
        learning_context=learning(approved(11, STAND_YES, support=0.1)),
        safe_facts=(),
    ).usable is False


def test_learning_not_mapped_to_answerable_evidence_is_not_used():
    """Passing every identity check does not make it answer *this* part."""

    from services.learning_evidence_policy import evaluate

    context = learning(approved(11, STAND_YES))
    context["subquestion_evidence"][0]["status"] = "NEEDS_DPS"
    assert evaluate(learning_context=context, safe_facts=()).reason == (
        "LEARNING_NOT_MAPPED_TO_EVIDENCE"
    )


def test_evidence_from_dps_is_not_mistaken_for_learning():
    from services.learning_evidence_policy import evaluate

    context = learning(approved(11, STAND_YES))
    context["subquestion_evidence"][0]["source"] = "CURRENT_DPS"
    assert evaluate(learning_context=context, safe_facts=()).usable is False


# =========================================== F. verified product fact path
def test_F_verified_product_fact_keeps_the_existing_b5_route():
    request = request_for("HDMI 포트는 몇 개인가요?")
    request.metadata["product_knowledge"] = _Knowledge([_Fact("hdmi_port_count", "2")])
    provider = provider_for("HDMI 포트는 2개입니다.")
    outcome, hybrid = run(request, provider, rule("HDMI 포트는 2개입니다."), learning())
    assert calls(provider) >= 1
    assert hybrid["approved_learning_evidence"]["usable"] is False


# ============================================== G. post-generation validator
def test_G_the_gate_does_not_replace_the_validator():
    """An error only visible in the written answer must still be caught."""

    request = request_for("HDMI 포트는 몇 개인가요?")
    request.metadata["product_knowledge"] = _Knowledge([_Fact("hdmi_port_count", "2")])
    provider = provider_for("HDMI 포트는 3개입니다.")
    outcome, hybrid = run(request, provider, rule("HDMI 포트는 2개입니다."), learning())
    assert calls(provider) >= 1, "generation must happen for this to be findable"
    validation = hybrid.get("validation") or {}
    assert outcome.result.auto_answerable is not True


# ================================================== L. placeholder guard kept
def test_L_placeholder_leakage_is_still_caught_after_generation():
    from services.auto_post_validation_service import AutoPostTechnicalValidator

    leak = "확인이 안 될 경우 <masked-phone>로 문의 바랍니다."
    request = request_for("고객센터 번호 알려주세요")
    provider = provider_for(leak)
    run(request, provider, rule(leak), learning())
    assert calls(provider) >= 1
    result = AutoPostTechnicalValidator().validate_answer(leak)
    assert result.passed is False
    assert "INTERNAL_PLACEHOLDER_EXPOSURE" in result.errors


def test_official_contact_numbers_are_untouched_by_this_work():
    from answer.text_utils import OFFICIAL_CONTACT_NUMBERS

    assert OFFICIAL_CONTACT_NUMBERS == ("1588-3366", "02-706-2678")


# ============================== H / I / J -- paths that must stay untouched
def _analysis_for(content: str) -> dict:
    return ANALYSIS.analyze(request_for(content)).to_dict()


def test_H_a_missing_order_number_still_takes_the_template_path():
    """The fixed "send us your order number" reply is not a GPT answer.

    Skipping generation must never remove it: the customer would get nothing.
    """

    analysis = _analysis_for("배송 언제 오나요?")
    assert analysis["answer_strategy"] == "REQUEST_ORDER_ID"
    assert PreGenerationGate.evaluate_plan(
        analysis=analysis, plan={}
    ).skip_generation is False


def test_I_686352380_is_not_blocked_by_the_gate():
    """The order number is in the body; this must reach normal generation."""

    analysis = _analysis_for(
        "23일 주문했는데요..\n주문번호 2026082351391541 입니다..\n"
        "혹시 배송일정을 알수 있을까요??"
    )
    assert analysis["order_id_status"] == "VALIDATED"
    assert analysis["answer_strategy"] != "REQUEST_ORDER_ID"
    assert PreGenerationGate.evaluate_plan(
        analysis=analysis,
        plan={"needs_staff_review": bool(analysis["manual_review_required"])},
    ).skip_generation is False


def test_J_a_schedule_change_request_with_no_order_is_still_drafted():
    """Updated expectation, and why it changed.

    This used to assert the skip: changing a booked date is an action only a
    person can take, so composing an answer looked like pure cost. What it
    actually produced was a blank reply -- staff opened the inquiry to nothing
    at all, and the customer's request was nowhere in the record.

    With no order number there is no schedule to look up, so generation makes
    no external call and phase9 answers from a deterministic safe template that
    states no date and names what was asked. The hold is untouched: the reasons
    below are the same, and the publishing gate still refuses it.
    """

    analysis = _analysis_for("설치일 변경 가능한가요?")
    assert analysis["inquiry_subtype"] == "SCHEDULE_CHANGE_REQUEST"
    assert analysis["can_execute_dps_lookup"] is False
    decision = PreGenerationGate.evaluate_plan(
        analysis=analysis, plan={"needs_staff_review": True}
    )
    assert decision.skip_generation is False


def test_J_a_schedule_change_request_with_an_order_still_skips():
    """The order number makes it a real lookup, and the skip saves that call."""

    analysis = _analysis_for("설치일 변경 가능한가요?")
    analysis = {
        **analysis,
        "can_execute_dps_lookup": True,
        "order_id_validated": True,
    }
    decision = PreGenerationGate.evaluate_plan(
        analysis=analysis, plan={"needs_staff_review": True}
    )
    assert decision.skip_generation is True
    assert "PROCESSING_PLAN_REQUIRES_REVIEW" in decision.reasons


def test_J_a_classifier_gap_is_generated_and_then_held_by_the_real_gate():
    """"배송 좀 땡겨주실 수 없나요?" is UNCLASSIFIED, not a named risk.

    The gate lets it through -- an answer plus a clean validator is allowed to
    clear a classifier gap -- and the publishing gate holds it on its own hard
    reasons. Recorded because the two verdicts must stay consistent: pinning
    the skip here would undo the deliberate UNCLASSIFIED relaxation, and that
    same relaxation is what lets "삼성센터AS무상기간알려주세요" be answered.
    """

    from services.auto_processing_eligibility_service import (
        AutoProcessingEligibilityService,
    )

    content = "배송 좀 땡겨주실 수 없나요?"
    analysis = _analysis_for(content)
    plan = {"needs_staff_review": True, "analysis": analysis}
    assert PreGenerationGate.evaluate_plan(
        analysis=analysis, plan=plan
    ).skip_generation is False

    passed = {"status": "PASS", "passed": True, "errors": [],
              "review_signals": [], "warnings": []}
    verdict = AutoProcessingEligibilityService().evaluate(
        inquiry={"id": 1, "content": content, "source_answered": 0,
                 "post_status": "NOT_POSTED"},
        draft={"id": 1, "original_answer": "확인 후 안내드리겠습니다.",
               "review_status": "NEEDS_REVIEW", "validation_status": "PASS",
               "validator_result_json": passed, "posted": 0,
               "metadata_json": {
                   "selected_answer_route": "GPT_FALLBACK",
                   "requires_manual_review": True,
                   "processing_plan": {**plan, "analysis": analysis},
                   "product_fact_guard": {"sensitive": False,
                                          "current_fact_verified": False},
                   "hybrid": {"validation": passed}}},
        route="GPT_FALLBACK",
    )
    assert verdict.decision == "REVIEW_REQUIRED"
    assert verdict.reasons, "held with no hard reason to show the operator"


def test_the_gate_and_the_publishing_gate_share_their_vocabulary():
    """A reason invented here would be untranslatable in the notification."""

    from answer.hold_reasons import REASON_LABELS

    for analysis, plan in (
        ({"manual_review_required": True,
          "manual_review_sources": ["HIGH_RISK_OR_DISPUTE"]},
         {"needs_staff_review": True, "is_high_risk": True}),
    ):
        for code in PreGenerationGate.evaluate_plan(
            analysis=analysis, plan=plan
        ).reasons:
            assert code in REASON_LABELS, code
    assert "EVIDENCE_CONFLICT" in REASON_LABELS


# ================ the contract the product-fact guard feeds the final gate
def _gate_verdict(guard: dict, route: str = "GPT_FALLBACK"):
    from services.auto_processing_eligibility_service import (
        AutoProcessingEligibilityService,
    )

    passed = {"status": "PASS", "passed": True, "errors": [],
              "review_signals": [], "warnings": []}
    return AutoProcessingEligibilityService().evaluate(
        inquiry={"id": 1, "content": STAND_Q, "source_answered": 0,
                 "post_status": "NOT_POSTED"},
        draft={"id": 1, "original_answer": STAND_YES,
               "review_status": "PENDING", "validation_status": "PASS",
               "validator_result_json": passed, "posted": 0,
               "metadata_json": {
                   "selected_answer_route": route,
                   "processing_plan": {"analysis": {}},
                   "product_fact_guard": guard,
                   "hybrid": {"validation": passed}}},
        route=route,
    )


def test_a_missing_product_fact_alone_no_longer_blocks_publishing():
    """What approved Learning buys: this one reason stops firing.

    Everything else the gate can find is untouched -- this only removes the
    hold that said "the Product DB has no row", which was never a statement
    about the product.
    """

    verdict = _gate_verdict({
        "sensitive": True,
        "current_fact_verified": True,
        "current_fact_source": "APPROVED_LEARNING",
    })
    assert "PRODUCT_FACT_NOT_VERIFIED" not in verdict.reasons


def test_without_qualifying_learning_the_product_fact_hold_stays():
    verdict = _gate_verdict({
        "sensitive": True,
        "current_fact_verified": False,
        "current_fact_source": None,
    })
    assert "PRODUCT_FACT_NOT_VERIFIED" in verdict.reasons
    assert verdict.decision == "REVIEW_REQUIRED"


def test_the_learning_route_does_not_unlock_compatibility_claims():
    """A different rule, deliberately left alone.

    PRODUCT_COMPATIBILITY still needs an exact template or Product DB fact.
    Approved Learning satisfying the product-fact requirement must not be
    read as satisfying that one too.
    """

    verdict = _gate_verdict({
        "sensitive": True, "current_fact_verified": True,
        "current_fact_source": "APPROVED_LEARNING",
    })
    assert verdict.reasons == () or "PRODUCT_FACT_NOT_VERIFIED" not in verdict.reasons
    from services.auto_processing_eligibility_service import (
        AutoProcessingEligibilityService,
    )
    passed = {"status": "PASS", "passed": True, "errors": [],
              "review_signals": [], "warnings": []}
    compat = AutoProcessingEligibilityService().evaluate(
        inquiry={"id": 1, "content": STAND_Q, "source_answered": 0,
                 "post_status": "NOT_POSTED"},
        draft={"id": 1, "original_answer": STAND_YES,
               "review_status": "PENDING", "validation_status": "PASS",
               "validator_result_json": passed, "posted": 0,
               "metadata_json": {
                   "selected_answer_route": "GPT_FALLBACK",
                   "processing_plan": {
                       "analysis": {"detected_intent": "PRODUCT_COMPATIBILITY"}},
                   "product_fact_guard": {
                       "sensitive": True, "current_fact_verified": True,
                       "current_fact_source": "APPROVED_LEARNING"},
                   "hybrid": {"validation": passed}}},
        route="GPT_FALLBACK",
    )
    assert "PRODUCT_COMPATIBILITY_NOT_VERIFIED" in compat.reasons


# ============================ authority tiers and validity stay as they were
@pytest.mark.parametrize(
    "source",
    ["SAFE_HISTORICAL_LEARNING", "VERIFIED_FEEDBACK_SIGNAL",
     "CURRENT_DPS", "CURRENT_DPS_REQUIRED"],
)
def test_only_approved_learning_takes_the_approved_learning_route(source):
    """Each evidence kind keeps the standing it already had.

    Historical answers in particular: they are reference material, not an
    approved statement of fact, and this work must not quietly promote them.
    """

    from services.learning_evidence_policy import evaluate

    context = learning(approved(11, STAND_YES))
    context["subquestion_evidence"][0]["source"] = source
    assert evaluate(learning_context=context, safe_facts=()).usable is False


def test_expired_learning_is_filtered_before_it_reaches_this_policy():
    """Validity is retrieval's job, and it still does it.

    Recorded so that nobody later "fixes" an apparent gap here by adding a
    second, differently-worded expiry rule.
    """

    import inspect

    from services import similar_answer_service
    from services.learning_validity_service import (
        is_learning_usable, validity_status,
    )

    assert "filtered_by_validity" in inspect.getsource(similar_answer_service)
    assert callable(is_learning_usable) and callable(validity_status)


def test_the_policy_reuses_the_existing_conflict_detector():
    """Not a second opinion on what "conflict" means."""

    from answer.learning_signal import facts_conflict
    from services import learning_evidence_policy

    assert learning_evidence_policy.facts_conflict is facts_conflict


def test_the_policy_reuses_the_existing_support_threshold():
    from answer.evidence_support import SUPPORTED_THRESHOLD
    from services.learning_evidence_policy import SUPPORTED_THRESHOLD as used

    assert used is SUPPORTED_THRESHOLD
