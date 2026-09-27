"""An inquiry that carries an order keeps its Product Knowledge.

The order lookup rebuilds the request from the refreshed inquiry, and a rebuilt
request starts with empty metadata. Market identity, semantic routing, the
analysis and the plan are all re-attached there. The Product Knowledge was not,
so the lookup ran, produced its facts, and the result was dropped between the
lookup and the prompt -- ``_product_facts_context`` found nothing and returned
``{}``. The prompt then carried no verified specification and no product
identity block at all.

The inquiry that suffers most from this is the one most likely to need both:
"43BEH 무게는 몇 kg이고 제 주문 배송은 언제 와요?" asks a specification
question about a model the customer named and a delivery question about their
own order. Measured through the live path, it reached GPT with 0 product facts
although the lookup had returned 88, all of them the right model.

These run the production adapter and the production helper. Nothing here
reimplements the rebuild.
"""

from __future__ import annotations

import pytest

from answer.source_adapter import answer_request_from_inquiry
from repositories.product_catalog_repository import (
    ProductCatalogRepository, canonical_model_identity,
)
from services.answer_service import AnswerService
from services.hybrid_answer_service import HybridAnswerService
from services.product_fact_guard import extract_model_code
from services.product_knowledge_service import ProductKnowledgeService

BED_TITLE = "삼성 107.9cm(43인치) 비즈니스TV 4K UHD 1등급 LH43BEDHLGFXKR 스탠드형"
QUESTION = "43BEH 무게는 몇 kg이고 제 주문 배송은 언제 와요?"

# A real inquiry's shape: the listing is the BED business TV and the customer
# has an order on it.
ROW = {
    "id": 4390,
    "source_question_id": "order-path-fixture",
    "store_code": "OJE_PLUS",
    "inquiry_type": "PRODUCT_INQUIRY",
    "title": "",
    "content": QUESTION,
    "product_name": BED_TITLE,
    "option_name": None,
    "order_id": "2026070585324901",
    "product_order_id": "2026070553222641",
    "raw_json": {},
}


@pytest.fixture(scope="module")
def knowledge():
    """The lookup exactly as AnswerService performs it, before the order call."""

    service = ProductKnowledgeService(ProductCatalogRepository())
    return service.facts_for_inquiry(
        product_id="",
        question=QUESTION,
        model_code=extract_model_code(BED_TITLE),
        product_name=BED_TITLE,
        include_all_catalog_fields=True,
    )


@pytest.fixture(scope="module")
def aliases():
    return ProductCatalogRepository().catalog()["aliases"]


def _before_the_order_lookup(knowledge):
    """The request as it stands when the Product Knowledge is attached."""

    request = answer_request_from_inquiry(ROW)
    request.metadata["product_knowledge"] = knowledge
    request.metadata["product_knowledge_target"] = {
        "listing_model": knowledge.listing_model,
        "explicit_inquiry_models": list(knowledge.inquiry_target_models),
        "resolved_pk_targets": list(knowledge.resolved_pk_targets),
        "target_resolution_reason": knowledge.target_resolution_reason,
    }
    return request


def _identities(facts, aliases):
    return sorted({
        str(canonical_model_identity(fact.get("model_code"), aliases=aliases))
        for fact in facts
    })


# --- A: the lookup really is on the request before the order call ----------

def test_the_request_carries_product_knowledge_before_the_order_lookup(knowledge):
    request = _before_the_order_lookup(knowledge)

    assert request.metadata["product_knowledge"] is knowledge
    assert knowledge.safe_facts


# --- B: and the rebuild really does lose it --------------------------------

def test_the_rebuild_starts_without_it(knowledge):
    """The defect itself, pinned.

    If this ever stops being true the carry below is dead code, and a reader
    should find that out here rather than by deleting it.
    """

    rebuilt = answer_request_from_inquiry(ROW)

    assert "product_knowledge" not in rebuilt.metadata
    assert "product_knowledge_target" not in rebuilt.metadata


# --- C: the carry puts it back, unchanged ----------------------------------

def test_the_carry_restores_the_same_lookup(knowledge):
    before = _before_the_order_lookup(knowledge)
    rebuilt = answer_request_from_inquiry(ROW)

    AnswerService._carry_product_knowledge(before, rebuilt)

    # The same object: carried, not recomputed. A second lookup would be a
    # second chance for the prompt and the gate to disagree.
    assert rebuilt.metadata["product_knowledge"] is knowledge
    assert rebuilt.metadata["product_knowledge_target"] == (
        before.metadata["product_knowledge_target"])


def test_the_carry_leaves_the_rebuilds_own_state_alone(knowledge):
    """Only the named keys travel.

    The rebuild exists because the order lookup refreshed the inquiry; its
    order, plan and DPS state is newer than the old request's and must win.
    """

    before = _before_the_order_lookup(knowledge)
    before.metadata["processing_plan"] = {"stale": True}
    rebuilt = answer_request_from_inquiry(ROW)
    rebuilt.metadata["processing_plan"] = {"fresh": True}

    AnswerService._carry_product_knowledge(before, rebuilt)

    assert rebuilt.metadata["processing_plan"] == {"fresh": True}
    assert "stale" not in str(rebuilt.metadata["processing_plan"])


# --- D and E: the two identities stay apart across the rebuild -------------

def test_the_listing_stays_the_listing_and_the_target_stays_the_target(
    knowledge,
):
    before = _before_the_order_lookup(knowledge)
    rebuilt = answer_request_from_inquiry(ROW)
    AnswerService._carry_product_knowledge(before, rebuilt)

    carried = rebuilt.metadata["product_knowledge"]
    report = rebuilt.metadata["product_knowledge_target"]

    assert carried.listing_model == "43BED"
    assert report["explicit_inquiry_models"] == ["43BEH"]
    assert report["resolved_pk_targets"] == ["43BEH"]
    assert report["target_resolution_reason"] == "INQUIRY_EXPLICIT_MODEL"
    # The order identity is the inquiry's own and no mention of another model
    # moves it.
    assert rebuilt.order_id == "2026070585324901"
    assert "LH43BEDHLGFXKR" in rebuilt.product_name


# --- F: and the prompt built from the rebuilt request carries both ---------

def test_the_prompt_from_the_rebuilt_request_has_facts_and_an_identity_block(
    knowledge, aliases,
):
    before = _before_the_order_lookup(knowledge)
    rebuilt = answer_request_from_inquiry(ROW)
    AnswerService._carry_product_knowledge(before, rebuilt)

    context = HybridAnswerService._product_facts_context(rebuilt)

    identity = context["product_identity"]
    assert identity["listing_model"] == "43BED"
    assert identity["answer_target_models"] == ["43BEH"]

    facts = context["product_catalog"]["facts"]
    assert facts, "the rebuilt request must reach the prompt with facts"
    assert _identities(facts, aliases) == ["43BEH"]


def test_without_the_carry_the_prompt_has_neither(knowledge):
    """Both halves fail together, which is why one test cannot cover it.

    A prompt with an identity block and no facts, or facts and no block, is
    just as wrong as this -- so the case above asserts both, and this one
    shows that the carry is what produces both.
    """

    rebuilt = answer_request_from_inquiry(ROW)

    context = HybridAnswerService._product_facts_context(rebuilt)

    assert context == {}


# --- G: nothing belonging to the listing model rides along -----------------

def test_no_listing_model_specification_reaches_the_prompt(knowledge, aliases):
    before = _before_the_order_lookup(knowledge)
    rebuilt = answer_request_from_inquiry(ROW)
    AnswerService._carry_product_knowledge(before, rebuilt)

    facts = HybridAnswerService._product_facts_context(
        rebuilt)["product_catalog"]["facts"]

    assert not [
        fact for fact in facts
        if str(canonical_model_identity(fact.get("model_code"),
                                        aliases=aliases)) == "43BED"
    ]
    assert not [fact for fact in facts if "BEDH" in str(fact.get("model_code"))]
