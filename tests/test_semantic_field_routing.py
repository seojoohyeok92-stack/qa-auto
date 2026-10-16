"""The question decides the model; the question *and GPT ①* decide the field.

Product Knowledge field routing matched substrings of the customer's own
wording, so a paraphrase reached nothing even though the fact was stored:

* 32QN650 holds ``color_bit_depth 10bit (8bit + FRC)``. "10비트 지원하나요?"
  and "컬러 심도가 몇 비트인가요?" found it; "색을 몇 비트로 표현하나요?" did
  not.
* S32DM701 holds ``usb_c_present USB C타입``. "C타입 단자 있나요?" found it;
  "C 단자 있나요?" did not.

GPT ① has already read the question and named the fact that is wanted
(``requested_information``) and what to look for (``retrieval_queries``).
Those travel as extra *field-routing* text and nothing else. Identity stays
where it was: which product the inquiry is about is resolved from the
customer's own sentence, so a model code inside a semantic query cannot
retarget the lookup -- which is the property this file guards hardest.
"""
from __future__ import annotations

import pytest

from answer.models import AnswerRequest
from services.answer_service import AnswerService
from services.product_knowledge_service import (
    ProductKnowledgeService, select_prompt_facts,
)
from services.semantic_analysis import AtomicQuestion, SemanticAnalysis


@pytest.fixture(scope="module")
def service() -> ProductKnowledgeService:
    return ProductKnowledgeService()


def ask(service, model, question, routing=()):
    return service.facts_for_inquiry(
        product_id=None, questions=[question], question=question,
        model_code=model, product_name=f"삼성 {model}", option_name="",
        include_all_catalog_fields=False, field_routing_texts=routing,
    )


def values(result, field_key):
    return [str(item.value) for item in result.safe_facts
            if item.field_key == field_key]


# -- the two reported paraphrases ---------------------------------------------


PARAPHRASES = (
    ("32QN650", "색을 몇 비트로 표현하나요?",
     ("색상 비트 심도", "컬러 비트 심도 10bit 여부"),
     "color_bit_depth", "10bit (8bit + FRC)"),
    ("S32DM701", "C 단자 있나요?",
     ("USB-C 단자 지원 여부", "USB C타입 포트 유무"),
     "usb_c_present", "USB C타입"),
)


@pytest.mark.parametrize("model, question, routing, field, value", PARAPHRASES)
def test_the_paraphrase_finds_nothing_without_the_semantic_expansion(
    service, model, question, routing, field, value,
):
    """The failure, kept as a test so the fix cannot be mistaken for luck."""

    assert values(ask(service, model, question), field) == []


@pytest.mark.parametrize("model, question, routing, field, value", PARAPHRASES)
def test_the_paraphrase_reaches_the_prompt_with_the_semantic_expansion(
    service, model, question, routing, field, value,
):
    result = ask(service, model, question, routing)
    assert values(result, field) == [value]
    kept, _dropped, _selection = select_prompt_facts(
        tuple(result.safe_facts), requested_fields=result.requested_fields,
        topics=result.topics, question=question,
    )
    assert value in [str(item.value) for item in kept
                     if item.field_key == field]


@pytest.mark.parametrize("model, question, routing, field, value", PARAPHRASES)
def test_the_wording_that_already_worked_still_works(
    service, model, question, routing, field, value,
):
    direct = {"color_bit_depth": ("32QN650", "10비트 지원하나요?"),
              "usb_c_present": ("S32DM701", "C타입 단자 있나요?")}[field]
    assert values(ask(service, *direct), field) == [value]


# -- identity is not routed ---------------------------------------------------


@pytest.mark.parametrize("routing", [
    ("S49CG954 색상 비트 심도",),
    ("LS32DM501EKXKR 비트", "43BEF 사양"),
    ("EDGE 27CH3 화이트 컬러 심도",),
])
def test_a_model_code_in_the_routing_text_cannot_retarget_the_lookup(
    service, routing,
):
    result = ask(service, "32QN650", "색을 몇 비트로 표현하나요?", routing)
    assert result.resolved_pk_targets == ("32QN650",)
    assert {str(item.model_code) for item in result.safe_facts
            if item.scope == "MODEL_CATALOG"} <= {"32QN650"}


def test_the_customers_own_model_mention_still_retargets(service):
    """The separation is about *whose* text it is, not about model codes."""

    result = ask(
        service, "32QN650", "S49CG954 색을 몇 비트로 표현하나요?",
        ("색상 비트 심도",),
    )
    assert result.resolved_pk_targets == ("49CG954",)


def test_no_routing_text_leaves_the_lookup_exactly_as_it_was(service):
    plain = ask(service, "S32DM701", "HDMI 단자 몇 개인가요?")
    empty = ask(service, "S32DM701", "HDMI 단자 몇 개인가요?", ())
    assert plain.requested_fields == empty.requested_fields
    assert [item.field_key for item in plain.safe_facts] == [
        item.field_key for item in empty.safe_facts]


# -- what AnswerService passes on ---------------------------------------------


def _request(semantic=None) -> AnswerRequest:
    request = AnswerRequest(
        inquiry_id=1, question_id="ROUTING-1", inquiry_type="PRODUCT_INQUIRY",
        question="색을 몇 비트로 표현하나요?", product_name="삼성 32QN650",
        metadata={},
    )
    if semantic is not None:
        request.metadata["_semantic_routing_value"] = semantic
    return request


def _semantic(**kwargs) -> SemanticAnalysis:
    atom = AtomicQuestion(
        text="색을 몇 비트로 표현하나요?",
        action="PRODUCT_SPEC",
        requested_information=kwargs.get("requested_information",
                                         "색상 비트 심도"),
        requested_attribute=kwargs.get("requested_attribute", "QUANTITY"),
        retrieval_queries=kwargs.get("retrieval_queries",
                                     ("컬러 비트 심도", "10bit 패널")),
    )
    # ``usable`` is derived, not set: only a GPT-sourced understanding with a
    # known action counts, which is exactly the gate the routing respects.
    return SemanticAnalysis(
        primary_action=(
            "PRODUCT_SPEC" if kwargs.get("usable", True) else "OTHER"),
        atomic_questions=(atom,),
        source="GPT" if kwargs.get("usable", True) else "UNAVAILABLE",
    )


def test_the_requested_fact_and_the_queries_are_passed_on():
    texts = AnswerService._semantic_field_routing_texts(
        _request(_semantic()))
    assert texts == ("색상 비트 심도", "컬러 비트 심도", "10bit 패널")


def test_the_requested_attribute_is_not_passed_on():
    """It names the shape of the property, not its topic."""

    texts = AnswerService._semantic_field_routing_texts(
        _request(_semantic(requested_attribute="QUANTITY")))
    assert "QUANTITY" not in texts


def test_an_unusable_understanding_passes_nothing():
    assert AnswerService._semantic_field_routing_texts(
        _request(_semantic(usable=False))) == ()


def test_no_understanding_at_all_passes_nothing():
    assert AnswerService._semantic_field_routing_texts(_request()) == ()


def test_the_persisted_contract_is_read_when_the_object_is_gone():
    """A request rebuilt from storage keeps ``requested_information``."""

    request = _request()
    request.metadata["gpt_understanding"] = {
        "usable": True,
        "questions": [
            {"text": "색을 몇 비트로 표현하나요?",
             "requested_information": "색상 비트 심도",
             "requested_attribute": "QUANTITY"},
        ],
    }
    assert AnswerService._semantic_field_routing_texts(request) == (
        "색상 비트 심도",)


def test_what_answer_service_passes_actually_finds_the_fact(service):
    """The two halves, joined: the texts it extracts do the routing."""

    texts = AnswerService._semantic_field_routing_texts(
        _request(_semantic()))
    result = ask(service, "32QN650", "색을 몇 비트로 표현하나요?", texts)
    assert values(result, "color_bit_depth") == ["10bit (8bit + FRC)"]
