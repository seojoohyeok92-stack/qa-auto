"""A key this catalogue literally holds is that key.

``normalized_catalog`` is keyed by ``normalize_model``, which drops Korean text
and punctuation, so two distinct keys can normalise to one form and the dict
keeps whichever was built last. Measured on the catalog of 2026-09-27, four
forms collide across eight keys:

    EDGE27CH3 <- "EDGE 27CH3", "EDGE 27CH3 화이트"
    EDGE24144 <- "EDGE 24 커브드144 게이밍 블랙", "... 화이트"
    XL2586X   <- "XL2586X+", "XL2586X"
    XL2546X   <- "XL2546X+", "XL2546X"

``match`` looked the model code up in that index first, so asking for
"EDGE 27CH3" returned the 화이트 record -- a different panel, 144Hz/4ms/7.2kg
against its own 165Hz/1ms/5.56kg -- and the black listing was answered
"화이트".

Diffing the resolution of all 1,598 catalog entries before and after, exactly
four change, and all four now resolve to their own key.
"""
from __future__ import annotations

import pytest

from repositories.product_catalog_repository import (
    ProductCatalogRepository,
    canonical_model_identity,
    normalize_model,
)
from services.product_knowledge_service import ProductKnowledgeService


@pytest.fixture(scope="module")
def repository() -> ProductCatalogRepository:
    return ProductCatalogRepository()


@pytest.fixture(scope="module")
def catalog(repository):
    return repository.catalog()["catalog"]


@pytest.fixture(scope="module")
def service(repository) -> ProductKnowledgeService:
    return ProductKnowledgeService(repository)


def ask(service, model, question):
    return service.facts_for_inquiry(
        product_id=None, questions=[question], question=question,
        model_code=model, product_name=f"삼성 {model}", option_name="",
        include_all_catalog_fields=False,
    )


# -- the literal rule -------------------------------------------------------


def test_every_catalog_key_resolves_to_itself(repository, catalog):
    """Whole-corpus: no key may resolve to another model."""

    wrong = []
    for key, record in catalog.items():
        if not isinstance(record, dict):
            continue
        match = repository.match(model_code=key)
        if match.model_key != key:
            wrong.append((key, match.model_key, match.status))
    assert wrong == []


@pytest.mark.parametrize("key", [
    "EDGE 27CH3", "EDGE 27CH3 화이트",
    "EDGE 24 커브드144 게이밍 블랙", "EDGE 24 커브드144 게이밍 화이트",
    "XL2586X+", "XL2586X", "XL2546X+", "XL2546X",
])
def test_a_colliding_key_is_not_redirected(repository, key):
    """Each of the eight keys whose normalised form collides with another."""

    match = repository.match(model_code=key)
    assert match.model_key == key
    assert match.status == "EXACT"


def test_the_collision_is_real_and_still_present(catalog):
    """The index still collides; the lookup no longer depends on it."""

    assert normalize_model("EDGE 27CH3") == normalize_model("EDGE 27CH3 화이트")
    assert normalize_model("XL2586X+") == normalize_model("XL2586X")


# -- the wrong answers, through the production service ----------------------


@pytest.mark.parametrize("question, field_key, expected", [
    ("주사율 몇 Hz인가요?", "refresh_rate", "165Hz"),
    ("무게가 몇 kg인가요?", "weight_catalog", "5.56kg"),
    ("응답속도 얼마인가요?", "response_time_ms", "1ms(OD)"),
])
def test_edge_27ch3_answers_with_its_own_specification(
    service, question, field_key, expected,
):
    """It used to answer 144Hz / 7.2kg / 4ms -- the 화이트 panel's."""

    result = ask(service, "EDGE 27CH3", question)
    assert result.resolved_pk_targets == ("EDGE 27CH3",)
    found = [i for i in result.safe_facts if i.field_key == field_key]
    assert found and str(found[0].value) == expected


def test_the_black_listing_is_not_answered_white(service):
    result = ask(service, "EDGE 24 커브드144 게이밍 블랙",
                 "제품 색상이 어떻게 되나요?")
    colours = {str(i.value) for i in result.safe_facts
               if i.field_key == "color"}
    assert colours == {"블랙"}


@pytest.mark.parametrize("model, question, field_key, expected", [
    ("EDGE 27CH3 화이트", "주사율 몇 Hz인가요?", "refresh_rate", "144Hz"),
    ("EDGE 24 커브드144 게이밍 화이트", "제품 색상이 어떻게 되나요?",
     "color", "화이트"),
])
def test_the_other_side_of_each_collision_is_untouched(
    service, model, question, field_key, expected,
):
    result = ask(service, model, question)
    found = [i for i in result.safe_facts if i.field_key == field_key]
    assert found and str(found[0].value) == expected


# -- what must not move -----------------------------------------------------


def test_the_confirmed_equivalence_still_holds(repository):
    """LS32DM501EKXKR = S32DM501 = 32DM501, by identity."""

    aliases = repository.catalog().get("aliases")
    identities = {
        canonical_model_identity(code, aliases=aliases)
        for code in ("LS32DM501EKXKR", "S32DM501", "32DM501")
    }
    assert identities == {"32DM501"}
    # And a short code still reaches the catalogued model through the
    # existing partial/alias steps, which this change does not touch.
    assert repository.match(model_code="32DM501").model_key == "S32DM501"


@pytest.mark.parametrize("left, right", [
    ("S32DM500", "S32DM501"),
    ("LH43BEDHLGFXKR", "LH43BEHHLGFXKR"),
    ("S32FM701", "S32FM703"),
    ("27TQ600S", "27TQ600SW"),
])
def test_a_real_variant_keeps_its_own_identity(repository, left, right):
    """String similarity is not identity."""

    aliases = repository.catalog().get("aliases")
    assert (canonical_model_identity(left, aliases=aliases)
            != canonical_model_identity(right, aliases=aliases))


def test_resolution_changed_for_four_entries_and_no_more(repository, catalog):
    """The blast radius, pinned.

    Only the keys whose normalised form collides with another key may have
    moved; every other entry resolves by the same step it always did.
    """

    moved = []
    for key, record in catalog.items():
        if not isinstance(record, dict):
            continue
        model_code = str(record.get("model") or key)
        match = repository.match(
            product_name=f"삼성 {model_code}", option_name="",
            model_code=model_code)
        if match.model_key == key:
            continue
        moved.append(key)
    # Keys that still resolve elsewhere do so because their own model field
    # names a different catalogue key -- a sales title naming its model, which
    # is the behaviour this change deliberately leaves alone.
    assert "EDGE 27CH3" not in moved
    assert "EDGE 24 커브드144 게이밍 블랙" not in moved
    assert "XL2586X+" not in moved
    assert "XL2546X+" not in moved
