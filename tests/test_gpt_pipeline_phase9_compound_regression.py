"""Regression for 687844809-style compound delivery inquiries.

This test is deliberately provider-free.  It protects the routing boundary:
when GPT UNDERSTAND has identified multiple customer questions, Phase9 may
prepare Order/DPS evidence but cannot terminate the entire inquiry before the
common Product/Learning/GPT Answer path sees it.
"""
from answer.models import AnswerRequest, AnswerResult, AnswerStatus
from answer.providers.fake_gpt_provider import FakeGptProvider
from services.answer_service import AnswerService
from services.hybrid_answer_service import HybridAnswerService
from services.semantic_analysis import (
    AtomicQuestion,
    DELIVERY_POLICY,
    INSTALLATION_METHOD,
    PRE_PURCHASE,
    PRODUCT_SPEC,
    SemanticAnalysis,
)


QUESTION = (
    "이 상품 UHD 맞나요? 벽걸이 가능한가요? 벽걸이 추가비용 있나요? "
    "지금 주문하면 서울까지 얼마나 걸리나요?"
)


def _understanding(
    *questions: AtomicQuestion,
    purchase_state: str = "UNKNOWN",
    requires_order_context: bool = False,
) -> SemanticAnalysis:
    return SemanticAnalysis(
        primary_action=PRODUCT_SPEC,
        secondary_actions=(INSTALLATION_METHOD, DELIVERY_POLICY),
        atomic_questions=questions,
        source="GPT",
        confidence=0.95,
        purchase_state=purchase_state,
        requires_order_context=requires_order_context,
    )


def test_687844809_style_compound_inquiry_cannot_take_phase9_shortcut() -> None:
    request = AnswerRequest(question=QUESTION)
    request.metadata["_semantic_routing_value"] = _understanding(
        AtomicQuestion("이 상품 UHD 맞나요?", PRODUCT_SPEC),
        AtomicQuestion("벽걸이 가능한가요?", INSTALLATION_METHOD),
        AtomicQuestion("벽걸이 추가비용 있나요?", INSTALLATION_METHOD),
        AtomicQuestion("지금 주문하면 서울까지 얼마나 걸리나요?", DELIVERY_POLICY),
        purchase_state=PRE_PURCHASE,
    )

    assert AnswerService._phase9_shortcut_allowed(request) is False

    contract = AnswerService._understanding_contract(
        request.metadata["_semantic_routing_value"]
    )
    assert contract["usable"] is True
    assert contract["need_product"] is True
    assert contract["need_template"] is True
    assert contract["need_learning"] is True
    assert contract["need_order"] is False
    assert contract["need_dps"] is False
    assert contract["purchase_state"] == PRE_PURCHASE


def test_single_schedule_question_keeps_phase9_shortcut() -> None:
    request = AnswerRequest(question="어제 주문했는데 설치일 언제인가요?")
    request.metadata["_semantic_routing_value"] = _understanding(
        AtomicQuestion("어제 주문했는데 설치일 언제인가요?", DELIVERY_POLICY),
        purchase_state="CURRENT_ORDER",
        requires_order_context=True,
    )

    assert AnswerService._phase9_shortcut_allowed(request) is True


def test_usable_understanding_turns_rule_into_gpt_candidate() -> None:
    """Template/Product/Learning context is composed for GPT②, not shortcut."""

    request = AnswerRequest(question="product and delivery question")
    request.metadata["_semantic_routing_value"] = _understanding(
        AtomicQuestion("product question", PRODUCT_SPEC),
        AtomicQuestion("delivery question", DELIVERY_POLICY),
        purchase_state=PRE_PURCHASE,
    )
    request.metadata["gpt_understanding"] = AnswerService._understanding_contract(
        request.metadata["_semantic_routing_value"]
    )
    request.metadata["dps"] = {"lookup_required": False, "lookup_status": "NOT_REQUIRED"}
    request.metadata["product_knowledge"] = type(
        "Knowledge",
        (),
        {
            "matched": False,
            "has_safe_facts": False,
            "prompt_block": staticmethod(lambda: "resolution = 4K UHD"),
            "safe_facts": (),
            "product_id": "9645661432",
        },
    )()
    rule = AnswerResult(
        status=AnswerStatus.GENERATED,
        category="DELIVERY_POLICY",
        reason="candidate only",
        answer="template policy answer",
        provider="rules",
        auto_answerable=True,
        needs_review=False,
        matched_rule="FIXED_POLICY_SHIPPING",
        metadata={"template_match_kind": "FIXED_POLICY_SHIPPING"},
    )
    AnswerService._record_template_candidate(
        request, rule, source="ANSWER_ENGINE",
    )

    provider = FakeGptProvider(responses={"DRAFT": {
        "answer": "product evidence and template policy answer",
        "confidence": 0.9,
        "used_facts": [],
        "missing_information": ["delivery confirmation"],
        "requires_review": True,
        "warnings": [],
    }})
    service = HybridAnswerService(
        provider,
        learning_context_provider=lambda *_args, **_kwargs: {
            "similar_approved_answers": [{"learning_id": 101, "answer": "learning evidence"}],
            "subquestion_evidence": [],
        },
        legacy_evidence_verification=False,
    )
    service.generate(request, rule)

    draft_call = next(call for call in provider.calls if call["task"] == "DRAFT")
    context = draft_call["context"]
    assert AnswerService._deterministic_shortcut_allowed(request) is False
    assert context["template_candidates"][0]["template_id"] == "FIXED_POLICY_SHIPPING"
    assert context["product_catalog"]["facts"] == []
    assert "4K UHD" in context["product_catalog"]["instructions"]
    assert context["similar_approved_answers"][0]["learning_id"] == 101


def test_gpt_understanding_need_template_controls_candidate_retrieval() -> None:
    request = AnswerRequest(question="product-only question")
    request.metadata["_semantic_routing_value"] = SemanticAnalysis(
        primary_action=PRODUCT_SPEC,
        atomic_questions=(AtomicQuestion("product-only question", PRODUCT_SPEC),),
        source="GPT",
        confidence=0.95,
    )
    request.metadata["gpt_understanding"] = AnswerService._understanding_contract(
        request.metadata["_semantic_routing_value"]
    )

    assert request.metadata["gpt_understanding"]["need_template"] is False
    assert AnswerService._template_candidate_retrieval_requested(
        request, prefer_template=True,
    ) is False
    assert AnswerService._deterministic_shortcut_allowed(request) is False
