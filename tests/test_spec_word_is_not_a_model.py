"""A specification word is not a model the catalogue is missing.

"DCI-P3" and "HDR10" are letters and digits with a hyphen, which is also the
shape of a model code. The catalogue's token scan therefore read them as a
named model, found no such model, and the inquiry failed closed with no
product evidence at all.

Measured before the change, on the catalog of 2026-09-27: "dci-p3 색영역" and
"hdr10" returned nothing, while "dci p3" and "hdr 10" -- the same questions --
returned the fact, because only the hyphenated spellings satisfy the shape.
Three of the 214 registry wordings were affected.

The rule is narrow on purpose, and these tests are mostly about the half that
must not move: the catalogue is asked first, a code it resolves is a model
whatever else it describes, and an unknown code still fails closed.
"""
from __future__ import annotations

import pytest

from repositories.product_catalog_repository import ProductCatalogRepository
from services.product_knowledge_service import ProductKnowledgeService
from services.spec_attributes import (
    SPEC_VOCABULARY,
    names_a_specification,
)

LISTING = "S49FG910"


@pytest.fixture(scope="module")
def service() -> ProductKnowledgeService:
    return ProductKnowledgeService(ProductCatalogRepository())


def resolve(service, question, listing=LISTING):
    return service._resolve_inquiry_targets(
        question, listing_model=listing, listing_id="")


def facts(service, question, listing=LISTING):
    return service.facts_for_inquiry(
        product_id=None, questions=[question], question=question,
        model_code=listing, product_name=f"삼성 {listing}", option_name="",
        include_all_catalog_fields=False,
    )


# -- the questions that returned nothing ------------------------------------


@pytest.mark.parametrize("question, field_key", [
    ("dci p3 커버리지는요?", "color_gamut_dci_p3"),
    ("dci-p3 색영역", "color_gamut_dci_p3"),
    ("DCI-P3 몇 퍼센트인가요?", "color_gamut_dci_p3"),
    ("hdr 10 지원하나요?", "hdr_spec"),
    ("HDR10", "hdr_spec"),
    ("HDR10+ 지원하나요?", "hdr_spec"),
])
def test_a_specification_word_names_no_model_and_the_fact_arrives(
    service, question, field_key,
):
    resolution = resolve(service, question)
    assert resolution.inquiry_target_models == (), question
    assert resolution.targets, question
    result = facts(service, question)
    found = [item for item in result.safe_facts
             if item.field_key == field_key]
    assert found, (question, [f.field_key for f in result.safe_facts])
    assert str(found[0].value).strip()


# -- the half that must not move --------------------------------------------


@pytest.mark.parametrize("token, listing", [
    ("LS32DM501EKXKR", "S32DM501"),   # full code, alias-backed
    ("S49CG954E", "S49CG954"),        # trailing letter
    ("43BEF", "LH43BEFHLGFXKR"),      # short form
    ("C27F593", "C27F593"),           # prefix difference
    ("27TQ600SW", "27TQ600SW"),       # trailing letter
    ("XL2586X", "XL2586X"),           # digits and letters, no hyphen
    ("S32BM80P", "S32BM80P"),
])
def test_a_real_model_is_still_detected(service, token, listing):
    resolution = resolve(service, f"{token} 하고 차이가 뭐예요?", listing)
    assert resolution.inquiry_target_models == (token.upper(),)
    assert resolution.reason == "INQUIRY_EXPLICIT_MODEL"
    assert len(resolution.targets) == 1


@pytest.mark.parametrize("token", ["ZZ9999QQ", "LX99ZZ9", "NOTAMODEL7"])
def test_a_model_the_catalogue_does_not_know_still_fails_closed(
    service, token,
):
    """The protection this gate exists for is untouched."""

    resolution = resolve(service, f"{token} 스펙 알려주세요", "S32BM80P")
    assert resolution.reason == "UNRESOLVED_EXPLICIT_MODEL"
    assert resolution.targets == ()


def test_a_hyphenated_accessory_code_still_fails_closed(service):
    """"WMN-B05FFXKR" is a bracket, not a display, and is not in the catalog."""

    resolution = resolve(
        service, "WMN-B05FFXKR 스펙 알려주세요", "LH43BEFHLGFXKR")
    assert resolution.reason == "UNRESOLVED_EXPLICIT_MODEL"
    assert resolution.targets == ()


# -- the rule's source of truth ---------------------------------------------


def test_the_vocabulary_comes_from_the_registry_and_stays_small():
    """Not a stopword list: every word is one some attribute is named by.

    It grew with 2-b and again with 2-c, and only through the registry:
    "HDMI 2.1", "DP 1.4" and "10bit" are the wording of
    ``hdmi_version_spec``, ``displayport_version_spec`` and
    ``color_bit_depth``, and "3.5mm" is how a customer asks
    ``headphone_out`` for the socket. Checked against the catalogue by the
    test below: no model key and no alias normalises to any of these shapes,
    so no real model becomes unreachable by being mistaken for a spec word.
    """

    assert SPEC_VOCABULARY == {
        "DCIP3", "HDR10", "HDR10+",
        "HDMI20", "HDMI21", "DP14", "DP21", "8BIT", "10BIT", "35MM",
    }


def test_no_catalogue_model_shares_a_shape_with_a_specification_word():
    """The guard above, measured rather than assumed."""

    from repositories.product_catalog_repository import (
        ProductCatalogRepository, normalize_model,
    )

    loaded = ProductCatalogRepository().catalog()
    shapes = set(SPEC_VOCABULARY)

    def shape(text):
        return normalize_model(text).upper().replace("-", "")

    assert [key for key in loaded["catalog"] if shape(key) in shapes] == []
    assert [alias for alias in (loaded.get("aliases") or {})
            if shape(alias) in shapes] == []


@pytest.mark.parametrize("token", [
    "LS32DM501EKXKR", "S49CG954E", "43BEF", "WMN-B05FFXKR", "C27F593",
    "27TQ600SW", "XL2586X", "S32BM80P", "LH43BEDHLGFXKR", "DM500", "DM501",
])
def test_no_model_code_is_mistaken_for_a_specification_word(token):
    assert names_a_specification(token) is False


@pytest.mark.parametrize("token", ["DCI-P3", "dci-p3", "HDR10", "HDR10+"])
def test_the_specification_words_are_recognised(token):
    assert names_a_specification(token) is True


def test_no_catalog_model_is_shadowed_by_the_vocabulary():
    """Whole-corpus: the rule may not hide a model the catalogue holds."""

    repository = ProductCatalogRepository()
    catalog = repository.catalog()["catalog"]
    shadowed = [
        key for key, record in catalog.items()
        if isinstance(record, dict)
        and (names_a_specification(key)
             or names_a_specification(record.get("model")))
    ]
    assert shadowed == []
