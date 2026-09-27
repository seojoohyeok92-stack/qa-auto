"""A question has to name the field the record actually stores.

``FIELD_TOPICS`` grew against an ontology the collected records do not use.
Measured against the merged file: Product Knowledge holds 506 distinct field
names, the router aims at 90, 465 stored names have no keyword route at all, and
38 of the names the router does aim at exist nowhere in the data. So a question
could match a topic, produce a field list, and still name nothing the record
contains.

Two failures make that concrete. "as는 몇년인가요" matched no topic whatsoever, so
the lookup returned ``NO_PRODUCT_CATALOG_TOPIC`` before reading anything, while
``as_phone`` and ``as_guide`` sat on that listing with the service number in
them. And "이 제품 베사홀 규격이 어떻게 되나요?" asked for ``vesa_mm`` while the
record stored ``vesa``. Over the 102 product questions whose drafts record what
reached the prompt, 34 asked for something the record held and got nothing.

What is pinned here is the resolution, not a list of phrasings: the expansion
matches family fragments against the vocabulary the loaded file actually has, so
it can never invent a field, and a stored name that gains or loses a suffix
keeps working without a code change.
"""

from __future__ import annotations

import pytest

from repositories.product_catalog_repository import ProductCatalogRepository
from services.product_knowledge_service import (
    _ATTRIBUTE_FAMILIES,
    ProductKnowledgeService,
    fields_for_question,
)


@pytest.fixture(scope="module")
def service():
    return ProductKnowledgeService(ProductCatalogRepository())


@pytest.fixture(scope="module")
def stored(service):
    return service._stored_field_names()


def _expanded(service, question):
    before, _topics = fields_for_question(question)
    return before, service._expand_to_stored_fields(question, before)


# --- the expansion can only ever name fields the data has -------------------

def test_every_family_target_exists_in_the_record(stored):
    """A family that names a field the file does not hold is a dead rule.

    This is the guard against the defect being reintroduced: the router's own
    38 dead targets are exactly what made a matched topic produce nothing.
    """

    lowered = {name.lower() for name in stored}
    for family, _keywords, targets in _ATTRIBUTE_FAMILIES:
        reached = [
            fragment for fragment in targets
            if any(fragment in name for name in lowered)
        ]
        assert reached, (family, targets)


def test_expansion_never_invents_a_field(service, stored):
    _before, after = _expanded(service, "무게랑 베사 규격이랑 제조국 알려주세요")

    invented = [
        field for field in after
        if field not in stored and field not in fields_for_question(
            "무게랑 베사 규격이랑 제조국 알려주세요")[0]
    ]
    assert invented == []


def test_a_question_about_nothing_stored_expands_to_nothing(service):
    before, after = _expanded(service, "안녕하세요 감사합니다")

    assert before == ()
    assert after == ()


# --- the paraphrases customers actually use --------------------------------

@pytest.mark.parametrize("question", [
    "as는 몇년인가요",
    "서비스센터 어디예요?",
    "고장나면 어디로 문의해요?",
    "보증기간 얼마나 되나요?",
])
def test_service_and_warranty_questions_reach_the_stored_contact(
    service, question,
):
    """Four phrasings, none of which reached any field before."""

    _before, after = _expanded(service, question)

    assert {"as_contact", "as_phone", "as_guide"} & set(after), sorted(after)


@pytest.mark.parametrize("question", [
    "제조국 어디예요?",
    "원산지 어디예요?",
    "어디서 만들어요?",
    "생산 국가는?",
    "27인치 모니터 생산지가 어디인가요?",
])
def test_origin_questions_reach_the_stored_origin_field(service, question):
    """The router aimed at ``country_of_origin``; the record mostly stores
    ``origin_country``. Both are asked for now."""

    _before, after = _expanded(service, question)

    assert "origin_country" in after, sorted(after)


@pytest.mark.parametrize("question,expected", [
    ("무게가 얼마예요?", "package_weight"),
    ("몇 kg인가요?", "package_weight"),
    ("제품 중량 알려주세요", "product_weight"),
    ("베사 몇이에요?", "vesa"),
    ("벽걸이 규격?", "vesa"),
    ("블루투스 돼요?", "bluetooth"),
    ("무선 이어폰 연결 가능?", "bluetooth"),
    ("배송비 무료인가요?", "delivery_fee_type"),
    ("배송비 얼마예요?", "delivery_fee_type"),
    ("소비전력 얼마예요?", "power_consumption_labelled"),
    ("전기 얼마나 먹나요?", "power_consumption_labelled"),
    ("응답속도 얼마예요?", "response_time"),
])
def test_paraphrases_reach_the_stored_name(service, question, expected):
    _before, after = _expanded(service, question)

    assert expected in after, (question, sorted(after)[:12])


def test_the_expansion_is_additive(service):
    """The model catalogue answers to the router's own names, so they stay."""

    before, after = _expanded(service, "이 제품 무게가 몇 kg인가요?")

    assert set(before) <= set(after)
    assert len(after) > len(before)


# --- P2: a listing that names no model still reaches its own record --------

BUNDLE_LISTING = "삼성 삼탠바이미 스마트 모니터 M5 27인치(68cm) IPTV 화이트+이동식 거치대"
BUNDLE_LISTING_ID = "10843799303"


def test_a_bundle_listing_resolves_through_its_own_api_model(service):
    """The listing sells a set, so its API model name is a compound.

    ``_integrated_exact_model_for_listing`` refuses a compound, and for this
    store most listings are compounds -- the monitor ships with a stand. Every
    ``model_facts`` row is keyed by model, so those listings reached none of
    their own specification: 1,076 of 3,419 real inquiries sat on one.
    """

    assert service._integrated_exact_model_for_listing(BUNDLE_LISTING_ID) is None
    assert service._listing_declared_base_model(BUNDLE_LISTING_ID) is not None


def test_the_listing_reaches_its_model_facts(service):
    from services.product_fact_guard import extract_model_code

    result = service.facts_for_inquiry(
        product_id=BUNDLE_LISTING_ID,
        question="이 제품 무게랑 해상도 알려주세요",
        model_code=extract_model_code(BUNDLE_LISTING),
        product_name=BUNDLE_LISTING,
        include_all_catalog_fields=True,
    )

    assert result.matched
    assert result.identity_status == "LISTING_EXACT_API_MODEL"
    assert result.listing_model
    assert len(result.safe_facts) > 20


def test_a_listing_declaring_two_models_stays_unresolved(service):
    """Ambiguity is not resolved by picking one.

    A listing whose own API record names two products cannot be reduced to one
    of them, and doing so is how another product's specification would be
    presented as this one's.
    """

    from unittest.mock import patch

    knowledge = dict(service.catalog_repository.product_knowledge())
    knowledge["model_facts"] = [
        {
            "field": "model_code", "subject": "MAIN_PRODUCT",
            "scope": "EXACT_MODEL", "value": value,
            "provenance": [{"source_type": "API", "source_product_id": "PID"}],
        }
        for value in ("LS27FM501EKXKR+이동식 스탠드", "LS32FM501EKXKR+이동식 스탠드")
    ]
    with patch.object(service.catalog_repository, "product_knowledge",
                      return_value=knowledge):
        assert service._listing_declared_base_model("PID") is None


def test_only_the_listings_own_api_row_counts(service):
    """A page image may show several models; the API row is the listing's own
    declaration. Only that anchors the identity."""

    from unittest.mock import patch

    knowledge = dict(service.catalog_repository.product_knowledge())
    knowledge["model_facts"] = [{
        "field": "model_code", "subject": "MAIN_PRODUCT", "scope": "EXACT_MODEL",
        "value": "LS27FM501EKXKR",
        "provenance": [{"source_type": "IMAGE_VISION", "source_product_id": "PID"}],
    }]
    with patch.object(service.catalog_repository, "product_knowledge",
                      return_value=knowledge):
        assert service._listing_declared_base_model("PID") is None


def test_no_listing_id_resolves_to_nothing(service):
    assert service._listing_declared_base_model("") is None
    assert service._listing_declared_base_model(None) is None


# --- listing evidence stays with its own listing ---------------------------

def test_a_cross_model_target_gets_no_listing_scoped_evidence(service):
    """P2 identifies *this* listing. It must not hand this listing's own rows
    to a model the customer named instead."""

    from services.product_fact_guard import extract_model_code

    result = service.facts_for_inquiry(
        product_id=BUNDLE_LISTING_ID,
        question="43BEH 무게가 얼마예요?",
        model_code=extract_model_code(BUNDLE_LISTING),
        product_name=BUNDLE_LISTING,
        include_all_catalog_fields=True,
    )

    assert result.resolved_pk_targets == ("43BEH",)
    assert not [
        fact for fact in result.safe_facts
        if fact.applies_to_product_id == BUNDLE_LISTING_ID
    ]
