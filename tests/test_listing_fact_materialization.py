"""A value taken out of model scope is still answerable for the listing that said it.

Some facts are not model-wide constants. The manufacturing origin printed on a
listing depends on which plant filled that batch, the service contact depends on
who sells the bundle, so those values were separated out of ``model_facts`` and
marked CONTEXT_SEPARATED. Separating them was right; the step that never happened
was writing them where runtime can read them.

Measured over the merged file: of 83 context-separated (model, field) pairs, 52
had landed nowhere a lookup reads -- ``as_contact`` for 23 models (the Samsung
service number), ``additional_cost`` for 4 (무료설치배송), ``origin_country`` for
26 (in a second decision list the first audit missed). The inquiry that exposed
it asked "27인치 모니터 생산지가 어디인가요?" on a listing whose record held the
answer, and got nothing.

72 rows now carry those values as listing facts. What these pin is the boundary:
a listing fact answers for its own listing and for nothing else, which is the
property that makes materialising them safe at all.
"""

from __future__ import annotations

import pytest

from repositories.product_catalog_repository import ProductCatalogRepository
from services.product_fact_guard import extract_model_code
from services.product_knowledge_service import (
    ProductKnowledgeService,
    select_prompt_facts,
)

CASE_LISTING = "10843799303"
CASE_TITLE = "삼성 삼탠바이미 스마트 모니터 M5 27인치(68cm) IPTV 화이트+이동식 거치대"
CASE_QUESTION = "27인치 모니터 생산지가 어디인가요? 중국이나 베트남일거같은데"
MATERIALIZED_FIELDS = frozenset({
    "as_contact", "additional_cost", "origin_country", "warranty_policy",
})


@pytest.fixture(scope="module")
def service():
    return ProductKnowledgeService(ProductCatalogRepository())


@pytest.fixture(scope="module")
def materialized(service):
    """The rows this work added, found by their recorded reason."""

    return [
        row for row in service.catalog_repository.product_knowledge()["listing_facts"]
        if isinstance(row, dict)
        and str(row.get("notes") or "").startswith("CONTEXT_SEPARATED_FROM_MODEL")
    ]


def _ask(service, listing, title, question):
    return service.facts_for_inquiry(
        product_id=listing, question=question,
        model_code=extract_model_code(title), product_name=title,
        include_all_catalog_fields=True,
    )


# --- the rows themselves ----------------------------------------------------

def test_the_materialized_rows_are_present_and_shaped_like_listing_facts(
    materialized,
):
    assert len(materialized) == 72
    for row in materialized:
        assert row["scope"] == "LISTING"
        assert row["subject"] == "LISTING"
        # A listing fact must not claim to be a model's specification.
        assert row["model_code"] is None
        assert row["scope_status"] == "RESOLVED"
        assert row["operational_status"] == "CANDIDATE_NOT_APPROVED"


def test_every_row_names_exactly_one_listing_and_proves_it(materialized):
    """Provenance is what makes it this listing's fact rather than a guess."""

    for row in materialized:
        listings = [str(value) for value in row["source_product_ids"] if value]
        assert len(listings) == 1, row["field"]
        assert row["provenance"], row["field"]
        for entry in row["provenance"]:
            assert str(entry.get("source_product_id")) == listings[0]
            assert entry.get("source_type")


def test_only_the_four_audited_fields_were_materialized(materialized):
    """``item_name`` was deliberately left out.

    "삼성 80.1cm M5 스마트 모니터 화이트" is listing display text; it answers no
    question a customer asks, and 24 of those groups stay out of runtime.
    """

    assert {row["field"] for row in materialized} == MATERIALIZED_FIELDS
    counts = {
        field: sum(1 for row in materialized if row["field"] == field)
        for field in MATERIALIZED_FIELDS
    }
    assert counts == {"as_contact": 41, "additional_cost": 16,
                      "origin_country": 11, "warranty_policy": 4}


# --- the inquiry that exposed the gap --------------------------------------

def test_the_origin_question_now_reaches_the_origin_fact(service):
    result = _ask(service, CASE_LISTING, CASE_TITLE, CASE_QUESTION)

    origin = [fact for fact in result.safe_facts
              if str(fact.field_key) == "origin_country"]
    assert origin, [f.field_key for f in result.safe_facts][:20]

    # This listing's own row is what the materialisation added, and it must be
    # among them. Not the only one: the model-scoped origin fact for the same
    # product became runtime-eligible separately, and both describe this product.
    # Which order they arrive in is the selector's business, so the listing row
    # is looked for rather than assumed first.
    listing_scoped = [fact for fact in origin
                      if fact.scope == "LISTING"
                      and fact.applies_to_product_id == CASE_LISTING]
    assert listing_scoped, [(f.scope, f.applies_to_product_id) for f in origin]

    # Several plants, and the answer must be able to say so -- for every copy.
    for fact in origin:
        assert "베트남" in str(fact.value) and "한국" in str(fact.value)


def test_the_origin_fact_survives_the_prompt_budget(service):
    """Retrieved is not the same as delivered.

    The Product Knowledge budget ranks and cuts, and a fact the customer
    directly asked for must not be what gets cut.
    """

    result = _ask(service, CASE_LISTING, CASE_TITLE, CASE_QUESTION)
    kept, _facts, _report = select_prompt_facts(
        result.safe_facts, requested_fields=result.requested_fields,
        topics=result.topics, question=CASE_QUESTION,
    )

    assert any(str(fact.field_key) == "origin_country" for fact in kept)


# --- the boundary ----------------------------------------------------------

def test_a_listing_fact_does_not_reach_a_sibling_listing(service, materialized):
    """The nearest neighbours are the dangerous ones.

    These listings sell the same family through the same bundles, so a rule that
    leaked would leak here first.
    """

    # Keyed by the row, not by its text. Consumer-protection boilerplate and the
    # Samsung service number are word-for-word identical across many listings, so
    # matching on the value would accuse a listing of leaking a fact it states
    # itself -- and its own ``model_facts`` row states exactly that text.
    knowledge = service.catalog_repository.product_knowledge()
    owners = {}
    for index, row in enumerate(knowledge["listing_facts"]):
        if row in materialized:
            owners[f"integrated:listing_facts:{index}"] = str(
                row["source_product_ids"][0])
    assert len(owners) == len(materialized)

    siblings = [
        ("9866761076", "삼성 삼탠바이미 스마트 M5 80cm(32인치)IPTV 모니터 화이트+스탠드"),
        ("9775146473", "삼성 삼탠바이미 32인치(80cm) M5 스마트 모니터 IPTV+2in1 이동식 거치대"),
        ("11554294315", "삼성 삼탠바이미 43인치(107cm) 4K UHD 무빙 스마트 비즈니스TV 거치대"),
    ]
    for listing, title in siblings:
        result = _ask(service, listing, title,
                      "제조국이랑 AS 연락처랑 설치비 알려주세요")
        for fact in result.safe_facts:
            owner = owners.get(str(fact.canonical_fact_id))
            if owner is None:
                continue                  # not one of the rows added here
            assert owner == listing, (
                listing, fact.field_key, owner, fact.canonical_fact_id)


def test_a_listing_fact_does_not_travel_to_an_explicitly_named_model(service):
    """Cross-product resolution hands the named model its own MODEL facts.

    This listing's origin and contact are about this listing. A customer asking
    about 43BEH while reading it must not be told this listing's contact as
    though it were that model's.
    """

    result = _ask(service, CASE_LISTING, CASE_TITLE,
                  "43BEH 제조국이랑 AS 연락처 알려주세요")

    assert result.resolved_pk_targets == ("43BEH",)
    assert not [
        fact for fact in result.safe_facts
        if fact.applies_to_product_id == CASE_LISTING
    ]


def test_a_genuine_conflict_was_not_materialized(service):
    """Where a listing stated two different contacts, nothing was written.

    10914735269 records "삼성전자 1588-3366 / 스마트마운트 고객센터 / 02-706-2678"
    and "삼성전자서비스센터 / 1588-3366". Those name different sets of contacts,
    so folding them would invent a fact neither source states.
    """

    knowledge = service.catalog_repository.product_knowledge()
    added = [
        row for row in knowledge["listing_facts"]
        if isinstance(row, dict)
        and str(row.get("notes") or "").startswith("CONTEXT_SEPARATED_FROM_MODEL")
        and row["field"] == "as_contact"
        and "10914735269" in [str(v) for v in row["source_product_ids"]]
    ]
    assert added == []
