"""The inquiry decides which product is being asked about, not the listing.

A customer reads one listing and asks about another model by name. Until now
the Product Knowledge lookup was aimed entirely by the listing -- the question
text was never scanned for a model -- so the answer described the product the
customer was *looking at* rather than the one they *named*. Over eighteen
representative inquiries, thirteen answered about the wrong model and 1,155
facts belonging to a different product reached the evidence.

What is pinned here is the resolution, not any particular product: every case
goes through the catalogue's own ``match``, so nothing compares a model code to
a literal and no product line has a branch of its own. The BE cases and the
DM/D4xx cases exercise one mechanism, which is the point of having both.

The boundaries matter as much as the resolution:

* naming a model the catalogue does not hold withholds the lookup rather than
  answering it from the listing -- substituting the listing's record is the
  exact failure this exists to prevent, and a near-miss answer is worse than
  no answer;
* a customer quoting the listing's own code, in whole or in part, is the
  listing referring to itself and changes nothing;
* the listing identity survives the override, because delivery, installation
  and DPS are about the order the customer placed, which no mention of another
  model can move.
"""

from __future__ import annotations

import pytest

from repositories.product_catalog_repository import ProductCatalogRepository
from services.product_knowledge_service import (
    INQUIRY_EXPLICIT_MODEL,
    LISTING_FALLBACK,
    MULTI_MODEL_COMPARISON,
    UNRESOLVED_EXPLICIT_MODEL,
    ProductKnowledgeService,
)

BED = "삼성 107.9cm(43인치) 비즈니스TV 4K UHD 1등급 LH43BEDHLGFXKR 스탠드형"
BEH = "삼성 107.9cm(43인치) UHD 4K 1등급 비즈니스 TV LH43BEHHLGFXKR 스탠드형"
DM500 = "삼성 80.1cm M5 스마트 모니터 블랙 LS32DM500EKXKR"
D22 = "삼성 54.6cm(22인치) 모니터 LS22D400GAKXKR"


@pytest.fixture(scope="module")
def service():
    return ProductKnowledgeService(ProductCatalogRepository())


def _ask(service, listing, question, *, product_id=""):
    from services.product_fact_guard import extract_model_code

    return service.facts_for_inquiry(
        product_id=product_id,
        question=question,
        model_code=extract_model_code(listing),
        product_name=listing,
        include_all_catalog_fields=True,
    )


def _identities(service, result):
    from repositories.product_catalog_repository import canonical_model_identity

    aliases = service.catalog_repository.catalog()["aliases"]
    return sorted({
        canonical_model_identity(fact.model_code, aliases=aliases)
        for fact in result.safe_facts if fact.model_code
    } - {None})


# --- 1. no model named: the listing is the subject, exactly as before -------

def test_an_inquiry_naming_no_model_still_answers_about_the_listing(service):
    result = _ask(service, BED, "이 제품 크기랑 무게가 어떻게 되나요?")

    assert result.target_resolution_reason == LISTING_FALLBACK
    assert result.inquiry_target_models == ()
    assert result.resolved_pk_targets == ("43BED",)
    assert _identities(service, result) == ["43BED"]


# --- 2. one model named: that model, and not the listing -------------------

@pytest.mark.parametrize("question", [
    "LH43BEHHLGFXKR 모델 크기랑 무게가 어떻게 되나요?",
    "43BEH 무게가 얼마예요?",
    # The hyphen is how the box writes it. The listing scan splits on it
    # because a title's hyphens are punctuation as often as notation; a
    # customer's sentence has to keep "LH43BE-H" whole or the model is lost.
    "LH43BE-H 크기 알려주세요",
])
def test_a_named_model_becomes_the_subject(service, question):
    result = _ask(service, BED, question)

    assert result.target_resolution_reason == INQUIRY_EXPLICIT_MODEL
    assert result.resolved_pk_targets == ("43BEH",)
    assert _identities(service, result) == ["43BEH"]
    assert "43BED" not in _identities(service, result)


def test_the_listing_record_does_not_travel_with_another_model(service):
    """The listing's own rows are keyed by product id, not by model.

    Carrying the id onto a different model's lookup would readmit the listing
    evidence through the back door, which is the whole of what is being
    excluded.
    """

    result = _ask(service, BED, "43BEH 무게가 얼마예요?", product_id="689945648")

    assert result.safe_facts
    assert not [
        fact for fact in result.safe_facts
        if fact.applies_to_product_id == "689945648"
    ]


# --- 3. the same mechanism, on product lines with nothing in common --------

@pytest.mark.parametrize("listing,question,expected", [
    # A Korean particle runs straight into the code with no space. Hangul is a
    # word character, so a word-boundary scan reads "32DM501은" as no model at
    # all; the catalogue's own tokeniser treats it as a separator.
    (DM500, "32DM501은 피벗 되나요?", "32DM501"),
    (DM500, "S32DM501 응답속도 알려주세요", "32DM501"),
    (DM500, "LS32DM501EKXKR 해상도 알려주세요", "32DM501"),
    # Size is part of the identity: 22D400 and 24D400 are different products.
    (D22, "24D400 크기는 어떻게 돼요?", "24D400"),
    (D22, "27D400 무게 알려주세요", "27D400"),
    # And it works in the other direction, which a listing-shaped special case
    # would not.
    (BEH, "LH43BEDHLGFXKR 무게 알려주세요", "43BED"),
])
def test_resolution_is_general_across_product_lines(
    service, listing, question, expected,
):
    result = _ask(service, listing, question)

    assert result.target_resolution_reason == INQUIRY_EXPLICIT_MODEL
    assert result.resolved_pk_targets == (expected,)
    assert _identities(service, result) == [expected]


def test_a_code_written_with_an_extra_suffix_still_resolves(service):
    """Customers write the code off their own box, not off the catalogue.

    "S32CM703UK" and "LS32DM501E" are the same products as S32CM703 and
    LS32DM501EKXKR, written with a suffix this catalogue does not carry. The
    whole-key-inside-the-text lookup already handled that for listing titles;
    the only change is that a single token from the question is now offered to
    it too. Measured over the live corpus this recovered twelve inquiries that
    would otherwise have been withheld.
    """

    accessory = "삼탠바이미 이동식 거치대 삼성 M5 M7 LG 룸앤티비 TV 모니터 스탠드"
    result = _ask(service, accessory, "S32CM703UK 제품과 호환 가능할지 문의 드립니다!")

    assert result.target_resolution_reason == INQUIRY_EXPLICIT_MODEL
    assert result.resolved_pk_targets == ("32CM703",)


@pytest.mark.parametrize("token", [
    "NPAY5000", "BENECIA17", "3DCHECKOUT", "PD2026061016670941", "I9-13900KS",
])
def test_a_code_shaped_token_that_is_not_a_product_is_not_one(service, token):
    """Coupons, payment references and a CPU part number all have a model
    code's shape. The catalogue refuses each, and refusing is what keeps the
    lookup from closing over a question that named no product."""

    from repositories.product_catalog_repository import ProductCatalogRepository

    repository = ProductCatalogRepository()
    assert repository.models_stated_in(f"{token} 관련해서 문의드립니다").resolved == ()


# --- 4. several models named: every one is kept -----------------------------

def test_a_comparison_keeps_each_model_as_its_own_target(service):
    result = _ask(service, BED, "43BEH랑 43BEF 차이가 뭐예요?")

    assert result.target_resolution_reason == MULTI_MODEL_COMPARISON
    assert sorted(result.resolved_pk_targets) == ["43BEF", "43BEH"]
    assert _identities(service, result) == ["43BEF", "43BEH"]


def test_a_comparison_against_this_listing_keeps_the_listing_too(service):
    """"이 제품" is deixis: it means whichever listing is being read.

    Dropping it would answer half the question -- the named model only -- for
    a customer who asked for a difference between two things.
    """

    result = _ask(service, BED, "이 제품이랑 43BEF 차이가 뭐예요?")

    assert result.target_resolution_reason == MULTI_MODEL_COMPARISON
    assert sorted(result.resolved_pk_targets) == ["43BED", "43BEF"]
    assert _identities(service, result) == ["43BED", "43BEF"]


def test_three_models_all_survive(service):
    result = _ask(service, BED, "43BEC, 43BED, 43BEF 차이 알려주세요")

    assert sorted(result.resolved_pk_targets) == ["43BEC", "43BED", "43BEF"]
    assert _identities(service, result) == ["43BEC", "43BED", "43BEF"]


# --- 5. a model the catalogue does not hold: nothing, not the listing ------

def test_an_unknown_named_model_withholds_rather_than_substitutes(service):
    result = _ask(service, BED, "LH99ZZZZ 모델 무게 알려주세요")

    assert result.target_resolution_reason == UNRESOLVED_EXPLICIT_MODEL
    assert result.resolved_pk_targets == ()
    assert result.safe_facts == ()
    assert result.matched is False
    assert result.unavailable_reason == "INQUIRY_MODEL_NOT_IN_CATALOG"
    # The listing was never offered in its place.
    assert _identities(service, result) == []


def test_quoting_the_listings_own_code_is_not_naming_another_product(service):
    """Customers paste the title, and partial codes fall out of it.

    "LH43B" is a fragment of this listing's own code, not a model the customer
    went and named, so it must not close the lookup.
    """

    result = _ask(
        service, BED,
        "삼성 107.9cm(43인치) 4K UHD LH43B 스마트 비즈니스TV 이 제품 무게 알려주세요",
    )

    assert result.target_resolution_reason != UNRESOLVED_EXPLICIT_MODEL
    assert _identities(service, result) == ["43BED"]


@pytest.mark.parametrize("question", [
    "거래명세서 부탁드립니다. gksdms156@naver.com 으로 보내주세요. 이 제품 무게도 알려주세요",
    "https://naver.me/G9UPcDAb 이 링크 상품이랑 같은 건가요? 크기 알려주세요",
])
def test_an_address_is_not_a_model_mention(service, question):
    """An email local part and a link path have a model code's exact shape.

    Reading one as a named model would withhold the answer to a question that
    named no product at all.
    """

    result = _ask(service, BED, question)

    assert result.target_resolution_reason == LISTING_FALLBACK
    assert _identities(service, result) == ["43BED"]


# --- 6. nothing is guessed --------------------------------------------------

@pytest.mark.parametrize("question", [
    "다른 43인치 모델은 어때요?",
    "더 큰 사이즈도 있나요?",
])
def test_an_unnamed_alternative_is_not_guessed_at(service, question):
    """"다른 모델" names nothing. The listing stays the subject and no
    candidate is elected to stand for what the customer might have meant."""

    result = _ask(service, BED, question)

    assert result.target_resolution_reason == LISTING_FALLBACK
    assert result.inquiry_target_models == ()
    assert _identities(service, result) in ([], ["43BED"])


# --- 7. the listing identity survives the override --------------------------

def test_the_listing_identity_is_kept_alongside_the_inquiry_target(service):
    """Delivery, installation and DPS are about the order that was placed.

    A compound inquiry asks a specification question about one model and a
    delivery question about the order. The specification follows the named
    model; the listing identity has to remain available and unchanged, because
    nothing the customer says about another model moves their own order.
    """

    result = _ask(
        service, BED, "43BEH 무게는 몇 kg이고 제 주문 배송은 언제 와요?",
        product_id="689945648",
    )

    assert result.resolved_pk_targets == ("43BEH",)
    assert result.listing_model == "43BED"
    assert result.inquiry_target_models == ("43BEH",)


def test_the_reported_target_names_what_the_evidence_is_about(service):
    """The report is what a reviewer reads to see which product was answered
    about, so it has to agree with the facts that were actually returned."""

    for listing, question in (
        (BED, "이 제품 크기 알려주세요"),
        (BED, "43BEH 무게가 얼마예요?"),
        (BED, "43BEH랑 43BEF 차이가 뭐예요?"),
    ):
        result = _ask(service, listing, question)
        assert set(_identities(service, result)) <= set(result.resolved_pk_targets)


# --- 8. no product-specific branch --------------------------------------

def test_no_model_literal_decides_the_outcome():
    """The rule is the catalogue's, not a list of strings.

    A special case for one pair of models would have passed every test above
    and helped no other product, so what is checked here is that the
    implementation contains no such case.
    """

    import ast
    from pathlib import Path

    tree = ast.parse(Path("services/product_knowledge_service.py").read_text(
        encoding="utf-8"))
    resolver = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "_resolve_inquiry_targets"
    )
    # Comments and the docstring name models freely -- they are how the rule
    # explains itself, and the AST does not carry comments at all. What must
    # not exist is a model code the code *runs*: a string it compares against
    # or a name it looks up.
    body = resolver.body[1:] if ast.get_docstring(resolver) else resolver.body
    written = [
        str(node.value) for statement in body
        for node in ast.walk(statement)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ] + [
        node.id for statement in body for node in ast.walk(statement)
        if isinstance(node, ast.Name)
    ]
    for literal in ("43BED", "43BEH", "BEDH", "BEHH", "DM501", "D400"):
        assert not [text for text in written if literal in text.upper()], literal
