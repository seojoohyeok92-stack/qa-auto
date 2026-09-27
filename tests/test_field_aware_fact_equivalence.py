"""Two readings of one fact are one fact; two facts are still two.

The conflict resolver compared normalised strings. That is right for a number and
wrong for everything else: "삼성 서비스센터 : 1588-3366" and "삼성전자서비스센터 /
1588-3366" are one telephone number written twice, and comparing the text made
them contradict each other, so both were withheld. Measured after the review
promotions, 75 of 159 rows died that way -- every one of them a spelling
difference, none of them a disagreement.

So equivalence is decided per field family, each rule deterministic and each
meaning-preserving. The danger of any such rule is that it merges too much, and
most of what follows is about the other direction: a different number, a
different company, a different country, a different word and a max-versus-typical
reading must all still conflict.

Three more things are pinned here, all found by measuring rather than by reading
the code: six model codes that their own listings declare but the catalogue never
held, so 31 facts could not be reached by any model-keyed lookup; dimensions that
differ only in printed precision; and the same fact arriving twice because the
model record and the listing record both state it.
"""

from __future__ import annotations

import pytest

from repositories.product_catalog_repository import (
    ProductCatalogRepository,
    canonical_model_identity,
)
from services.product_fact_guard import extract_model_code
from services.product_knowledge_service import (
    ProductFact,
    ProductKnowledgeService,
    _canonical_fact_value,
    _rounding_representative,
    select_prompt_facts,
)

ADDED_MODELS = (
    "LS32FG500EKXKR", "LS27HG806EFXKR", "LC34G55TWWKXKR",
    "LF24T450FQKXKR", "LS24F322GAKXKR", "LS27FG700EKXKR",
)


@pytest.fixture(scope="module")
def service():
    return ProductKnowledgeService(ProductCatalogRepository())


def _fact(field, value, *, model="LS32DM501EKXKR", scope="EXACT_MODEL",
          fact_id="x"):
    return ProductFact(
        product_id="", listing_id="", model_code=model, field_key=field,
        value=value, raw_value=value, unit=None, scope=scope, scope_key="",
        component_scope="", volatility="", verification_status="",
        resolution_status="", lifecycle_status="", canonical_fact_id=fact_id,
        value_id=None, provenance=(), safe_for_answer=True,
    )


def _same(field, a, b):
    return _canonical_fact_value(_fact(field, a)) == _canonical_fact_value(
        _fact(field, b))


# --- what counts as the same fact ------------------------------------------

@pytest.mark.parametrize("a,b", [
    ("삼성 서비스센터 : 1588-3366", "삼성전자서비스센터 / 1588-3366"),
    ("삼성전자서비스: 1588-3366", "삼성 서비스센터 1588-3366"),
    ("삼성전자서비스센터 / 1588-3366", "삼성전자서비스: 1588-3366 (1588-3366)"),
])
def test_one_number_written_differently_is_one_contact(a, b):
    assert _same("as_contact", a, b)


def test_two_contacts_are_not_one_contact():
    """The maker's line plus a stand vendor's line is a different answer from
    the maker's line alone, however similar the wording."""

    assert not _same(
        "as_contact",
        "삼성전자 1588-3366 / 스마트마운트 고객센터 / 02-706-2678",
        "삼성전자서비스센터 / 1588-3366",
    )


@pytest.mark.parametrize("a,b", [
    ("삼성전자", "삼성전자㈜"), ("삼성전자", "삼성전자(주)"),
    ("삼성전자 ", "삼성전자주식회사"),
])
def test_a_legal_suffix_is_not_a_different_company(a, b):
    assert _same("manufacturer", a, b)


def test_a_different_company_is_a_different_company():
    assert not _same("manufacturer", "삼성전자", "엘지전자")


@pytest.mark.parametrize("a,b", [
    ("2024-04", "2024년 4월"), ("2024-04", "2024.04"), ("2024.04", "2024년 4월"),
])
def test_one_month_written_differently_is_one_date(a, b):
    assert _same("release_ym", a, b)


@pytest.mark.parametrize("a,b", [
    ("2024-04", "2025-04"), ("2024-04", "2024-11"),
])
def test_a_different_month_or_year_still_conflicts(a, b):
    assert not _same("release_ym", a, b)


def test_country_order_does_not_change_the_origin():
    assert _same(
        "origin_country",
        "한국 (베트남, 중국, 태국, 멕시코, 헝가리, 슬로바키아)",
        "한국 (중국, 멕시코, 베트남, 슬로바키아, 헝가리, 태국)",
    )


def test_a_different_country_of_record_still_conflicts():
    assert not _same(
        "origin_country", "중국산((주)삼성전자)", "한국 (베트남, 중국, 태국)")


def test_punctuation_in_a_policy_is_not_a_different_policy():
    assert _same(
        "warranty_policy",
        "결함. 하자 등에 따른 소비자 피해에 대해서는",
        "결함·하자 등에 따른 소비자피해에 대해서는",
    )


def test_a_one_character_word_difference_is_never_folded():
    """"결함" and "결합" are one edit apart and are different words.

    A rule loose enough to merge them would be loose enough to merge a misread
    into the record as though it were agreement.
    """

    assert not _same("warranty_policy", "결함·하자 등에", "결합·하자 등에")


@pytest.mark.parametrize("a,b", [
    ("MAX 60W", "31.0 W"), ("Max 59W", "32W(Typical)"),
])
def test_a_maximum_is_not_a_typical_reading(a, b):
    """One field holding two measurement semantics is a schema problem.

    Folding them would answer "how much power does it draw" with whichever was
    written first. They stay in conflict until the field is split.
    """

    assert not _same("power_consumption_labelled", a, b)


# --- dimensions that differ only in printed precision ----------------------

def test_a_rounded_dimension_is_the_same_measurement():
    group = [
        _fact("dimensions_labelled",
              "스탠드포함(가로X높이X깊이) 716.1 X 517.0 X 193.5mm", fact_id="a"),
        _fact("dimensions_labelled", "716x517x194mm", fact_id="b"),
    ]

    representative = _rounding_representative(group)

    assert representative is not None
    # The precise reading represents the group; the rounded one corroborates.
    assert "193.5" in str(representative.value)


def test_a_different_condition_is_never_a_rounding_variant():
    """A with-stand depth and a without-stand depth are different facts, and
    they would differ by far more than a rounding step anyway."""

    group = [
        _fact("dimensions_labelled", "스탠드포함 716.1 X 517.0 X 193.5mm",
              fact_id="a"),
        _fact("dimensions_labelled", "스탠드미포함 716.1 X 517.0 X 59.7mm",
              fact_id="b"),
    ]

    assert _rounding_representative(group) is None


def test_a_value_outside_rounding_range_is_a_conflict():
    group = [
        _fact("dimensions_labelled", "716.1 X 517.0 X 59.7mm", fact_id="a"),
        _fact("dimensions_labelled", "716.1 X 517.0 X 76.3mm", fact_id="b"),
    ]

    assert _rounding_representative(group) is None


# --- the six models their own listings declare -----------------------------

@pytest.mark.parametrize("model", ADDED_MODELS)
def test_each_added_model_resolves_to_itself(service, model):
    match = service.catalog_repository.match(model_code=model)

    assert match.model_key == model
    assert match.status in ("EXACT", "UNIQUE_MATCH")


@pytest.mark.parametrize("model", ADDED_MODELS)
def test_no_added_model_collapses_into_another(service, model):
    """A catalogue entry that shared an identity with an existing model would
    hand one product's specification to another."""

    loaded = service.catalog_repository.catalog()
    aliases = loaded["aliases"]
    identity = canonical_model_identity(model, aliases=aliases)
    sharing = [
        key for key in loaded["normalized_catalog"].values()
        if key != model
        and canonical_model_identity(key, aliases=aliases) == identity
    ]
    assert sharing == [], (model, identity, sharing)


@pytest.mark.parametrize("written,expected", [
    ("LS32FG500", "LS32FG500EKXKR"),
    ("S32FG500", "LS32FG500EKXKR"),
    ("32FG500", "LS32FG500EKXKR"),
    ("LS27FG700", "LS27FG700EKXKR"),
])
def test_the_short_forms_customers_write_reach_the_added_model(
    service, written, expected,
):
    match = service.catalog_repository.match(model_code=written)

    assert match.model_key == expected, (written, match.status, match.model_key)


def test_an_added_model_reaches_its_own_facts(service):
    result = service.facts_for_inquiry(
        product_id="", question="제조사랑 출시 연식 알려주세요",
        model_code="LS32FG500EKXKR", product_name="LS32FG500EKXKR",
        include_all_catalog_fields=True,
    )

    assert result.matched
    fields = {str(fact.field_key) for fact in result.safe_facts}
    assert {"manufacturer", "release_ym"} & fields, sorted(fields)[:20]


# --- one meaning, one entry in the prompt ----------------------------------

LISTING = "10843799303"
TITLE = "삼성 삼탠바이미 스마트 모니터 M5 27인치(68cm) IPTV 화이트+이동식 거치대"
QUESTION = "27인치 모니터 생산지가 어디인가요? 중국이나 베트남일거같은데"


def test_one_origin_fact_reaches_the_prompt(service):
    """The model record and the listing record both state this origin.

    Both are correct and both are this product's, so carrying both is not
    contamination -- it is the same fact twice, in two spellings, for the model
    to choose between.
    """

    result = service.facts_for_inquiry(
        product_id=LISTING, question=QUESTION,
        model_code=extract_model_code(TITLE), product_name=TITLE,
        include_all_catalog_fields=True,
    )
    retrieved = [f for f in result.safe_facts
                 if str(f.field_key) == "origin_country"]
    assert len(retrieved) > 1, "expected the duplicate to exist before dedupe"

    kept, _facts, report = select_prompt_facts(
        result.safe_facts, requested_fields=result.requested_fields,
        topics=result.topics, question=QUESTION,
    )
    in_prompt = [f for f in kept if str(f.field_key) == "origin_country"]

    assert len(in_prompt) == 1
    # The page the customer is reading states it, so that copy represents.
    assert in_prompt[0].scope == "LISTING"
    assert any(item.get("reason") == "SEMANTIC_DUPLICATE_ACROSS_SCOPE"
               for item in report.get("dropped", []))


def test_the_dropped_copy_is_still_accounted_for(service):
    """Merging evidence must not lose where the other copy came from."""

    result = service.facts_for_inquiry(
        product_id=LISTING, question=QUESTION,
        model_code=extract_model_code(TITLE), product_name=TITLE,
        include_all_catalog_fields=True,
    )
    _kept, _facts, report = select_prompt_facts(
        result.safe_facts, requested_fields=result.requested_fields,
        topics=result.topics, question=QUESTION,
    )

    merged = [item for item in report.get("dropped", [])
              if item.get("reason") == "SEMANTIC_DUPLICATE_ACROSS_SCOPE"]
    for item in merged:
        assert item.get("kept_instead")
        assert item.get("kept_scope")


def test_dedupe_never_merges_two_models(service):
    """A comparison must keep both sides.

    The grouping is by canonical model identity, so two products can hold the
    same value for the same field and both survive.
    """

    bed = "삼성 107.9cm(43인치) 비즈니스TV 4K UHD 1등급 LH43BEDHLGFXKR 스탠드형"
    result = service.facts_for_inquiry(
        product_id="", question="43BEH랑 43BEF 차이가 뭐예요?",
        model_code=extract_model_code(bed), product_name=bed,
        include_all_catalog_fields=True,
    )
    kept, _facts, _report = select_prompt_facts(
        result.safe_facts, requested_fields=result.requested_fields,
        topics=result.topics, question="43BEH랑 43BEF 차이가 뭐예요?",
    )

    identities = {
        canonical_model_identity(fact.model_code) for fact in kept
        if fact.model_code
    }
    assert {"43BEH", "43BEF"} <= identities, identities


def test_a_cross_model_target_still_gets_no_listing_evidence(service):
    """Preferring the listing copy must not survive into another model's answer."""

    result = service.facts_for_inquiry(
        product_id=LISTING, question="43BEH 제조국이랑 AS 연락처 알려주세요",
        model_code=extract_model_code(TITLE), product_name=TITLE,
        include_all_catalog_fields=True,
    )

    assert result.resolved_pk_targets == ("43BEH",)
    assert not [fact for fact in result.safe_facts
                if fact.applies_to_product_id == LISTING]
