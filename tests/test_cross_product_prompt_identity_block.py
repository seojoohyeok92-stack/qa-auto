"""The prompt has to say which product the specification belongs to.

Target resolution aims the Product Knowledge lookup at the model the customer
named. That fixes which facts are retrieved; it does not by itself fix what the
prompt calls them. The identity block reports the result's ``listing_id``, and
for a cross-model lookup that field carries the *target's* model key -- so a
prompt built for "43BEH 무게가 얼마예요?" on a 43BED listing announced
``listing_id: LH43BEHH`` and nothing else. Read plainly, that tells the model
the listing is BEH, which is the same substitution the resolution exists to
prevent, reintroduced one layer later.

So the block now names both, and says which one the evidence is about. These
run without a provider: the context is built from a request, and no draft is
generated.
"""

from __future__ import annotations

import pytest

from answer.models import AnswerRequest
from repositories.product_catalog_repository import ProductCatalogRepository
from services.hybrid_answer_service import HybridAnswerService
from services.product_fact_guard import extract_model_code
from services.product_knowledge_service import ProductKnowledgeService

BED = "삼성 107.9cm(43인치) 비즈니스TV 4K UHD 1등급 LH43BEDHLGFXKR 스탠드형"


def _identity_block(question: str, *, listing: str = BED) -> dict:
    """The identity the prompt would carry for this inquiry."""

    service = ProductKnowledgeService(ProductCatalogRepository())
    knowledge = service.facts_for_inquiry(
        product_id="", question=question, model_code=extract_model_code(listing),
        product_name=listing, include_all_catalog_fields=True,
    )
    request = AnswerRequest(question=question, product_name=listing)
    request.metadata["product_knowledge"] = knowledge
    context = HybridAnswerService._product_facts_context(request)
    return context.get("product_identity") or {}


def test_the_named_model_is_the_answer_target_and_not_the_listing():
    block = _identity_block("43BEH 무게가 얼마예요?")

    assert block["listing_model"] == "43BED"
    assert block["answer_target_models"] == ["43BEH"]
    # The listing is stated as the listing. Whatever else the block carries,
    # the target must not be the only model in it -- that is the reading that
    # turned another product's specification into this listing's.
    assert block["listing_model"] != block["answer_target_models"][0]
    assert "43BED" in str(block["usage"]) or "listing_model" in str(block["usage"])


def test_the_usage_note_keeps_the_order_on_the_customers_own_order():
    """A cross-model question must not move the delivery answer with it."""

    usage = str(_identity_block(
        "43BEH 무게는 몇 kg이고 제 주문 배송은 언제 와요?")["usage"])

    assert "주문" in usage
    assert "설치일" in usage or "배송" in usage


def test_a_comparison_names_every_target():
    block = _identity_block("43BEH랑 43BEF 차이가 뭐예요?")

    assert block["listing_model"] == "43BED"
    assert sorted(block["answer_target_models"]) == ["43BEF", "43BEH"]


def test_an_ordinary_listing_question_adds_no_target_block():
    """Nothing changes for the inquiry that names no model.

    The overwhelming majority of inquiries are this one, and a block telling
    the model to answer about something other than the listing would be wrong
    for every one of them.
    """

    block = _identity_block("이 제품 크기랑 무게가 어떻게 되나요?")

    assert "answer_target_models" not in block
    assert "usage" not in block


def test_an_unknown_named_model_says_so_instead_of_offering_the_listing():
    """CASE D reaches the prompt as an instruction, not as silence.

    With no facts and no explanation the model is left with the listing title
    and a question about another product, which is exactly the situation that
    produces a confident wrong answer.
    """

    block = _identity_block("LH99ZZZZ 모델 무게 알려주세요")

    assert block["listing_model"] == "43BED"
    assert block["answer_target_models"] == []
    assert "대신" in str(block["usage"])


@pytest.mark.parametrize("question,expected", [
    ("43BEH 무게가 얼마예요?", "43BEH"),
    ("32DM501은 피벗 되나요?", "32DM501"),
])
def test_the_block_agrees_with_the_facts_it_travels_with(question, expected):
    """The block and the evidence are read together, so they must agree."""

    service = ProductKnowledgeService(ProductCatalogRepository())
    knowledge = service.facts_for_inquiry(
        product_id="", question=question, model_code=extract_model_code(BED),
        product_name=BED, include_all_catalog_fields=True,
    )
    request = AnswerRequest(question=question, product_name=BED)
    request.metadata["product_knowledge"] = knowledge
    context = HybridAnswerService._product_facts_context(request)

    assert context["product_identity"]["answer_target_models"] == [expected]
    assert knowledge.resolved_pk_targets == (expected,)
