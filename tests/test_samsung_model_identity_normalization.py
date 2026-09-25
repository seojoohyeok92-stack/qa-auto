"""Focused regression coverage for deterministic Samsung display model identity."""
from __future__ import annotations

from pathlib import Path

import pytest

from repositories.product_catalog_repository import (
    ProductCatalogRepository,
    canonical_model_identity,
)
from services.learning_compatibility_service import (
    LearningCompatibilityService,
    extract_product_identity,
)
from services.product_knowledge_service import ProductKnowledgeService


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "data" / "model_data_with_color.json"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("LS32DM501EKXKR", "32DM501"),
        ("S32DM501", "32DM501"),
        ("LS32DM501", "32DM501"),
        ("32DM501EKXKR", "32DM501"),
        ("32DM501", "32DM501"),
        ("LS32DM500EKXKR", "32DM500"),
        ("S32DM500", "32DM500"),
        ("LS32DM500", "32DM500"),
        ("32DM500EKXKR", "32DM500"),
        ("32DM500", "32DM500"),
        ("LS22D400GAKXKR", "22D400"),
        ("LS24D400GAKXKR", "24D400"),
        ("LS27BG400EKXKR", "27BG400"),
        ("LS27HG400EKXKR", "27HG400"),
        ("LS27FM501EKXKR", "27FM501"),
        ("LS32FG500EKXKR", "32FG500"),
        ("LS32DG300", "32DG300"),
        ("LS49DG930SKXKR", "49DG930"),
    ],
)
def test_samsung_display_code_notations_share_a_core_identity(raw, expected):
    assert canonical_model_identity(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "M50D 32\"",
        "D400 O(22,24)",
        "M5",
        "LS32DM501EKXKR + FMS101W",
        "LS32DM501EKXKR / VI201S",
    ],
)
def test_marketing_and_bundle_text_never_becomes_a_model_identity(raw):
    assert canonical_model_identity(raw) is None


def test_neighbouring_models_do_not_collapse():
    assert canonical_model_identity("32DM500") != canonical_model_identity("32DM501")
    assert canonical_model_identity("27FM500") != canonical_model_identity("27FM501")
    assert canonical_model_identity("22D400") != canonical_model_identity("24D400")


def _service() -> ProductKnowledgeService:
    return ProductKnowledgeService(ProductCatalogRepository(CATALOG))


@pytest.mark.parametrize(
    "raw",
    ["LS32DM501EKXKR", "S32DM501", "LS32DM501", "32DM501EKXKR", "32DM501"],
)
def test_catalog_and_product_knowledge_retrieve_the_dm501_canonical_group(raw):
    result = _service().facts_for_inquiry(
        product_id="identity-probe",
        product_name=raw,
        model_code=raw,
        question="refresh rate VESA HDMI USB",
        include_all_catalog_fields=True,
    )
    assert result.matched is True
    source_models = {fact.model_code for fact in result.safe_facts}
    assert {"S32DM501", "LS32DM501EKXKR"} <= source_models
    assert all("+" not in str(model) and "/" not in str(model) for model in source_models)


def test_bare_core_finds_a_deterministic_catalog_representative():
    match = ProductCatalogRepository(CATALOG).match(
        product_name="32DM501", model_code="32DM501"
    )
    assert match.model_key == "S32DM501"
    assert match.status == "UNIQUE_MATCH"


def test_learning_model_scope_compares_canonical_samsung_identities():
    current = extract_product_identity(model_code="32DM501")
    candidate = extract_product_identity(model_code="LS32DM501EKXKR")
    assert current.model_code == candidate.model_code == "32DM501"
    decision = LearningCompatibilityService().evaluate(
        current_question="VESA specification",
        current_product=current,
        candidate_question="VESA specification",
        candidate_answer="VESA information",
        candidate_product=candidate,
        candidate_metadata={"product_scope": "MODEL"},
        query_is_product_fact=True,
    )
    assert decision.product_match == "EXACT_MODEL"


# --- the 43-inch BE line: two models, not one -----------------------------------
#
# On 2026-09-18 an operator decision about specifications -- "43BED and 43BEH
# differ by model year, the panels are the same" -- was recorded as six
# MODEL_ALIASES rows, because MODEL_ALIASES is the only place this catalogue
# can say that two codes mean one product.  It is also what canonicalization
# reads, so the claim arrived as identity: BE43H-H and BE43D-H became one
# canonical model, and no caller downstream could tell them apart again.
#
# They are two models.  The alias rows stay where the operator put them and
# still name a catalogue record for a listing that mentions them; what they no
# longer do is rewrite one model's identity into the other's.

BE43H_FORMS = ["LH43BEHHLGFXKR", "LH43BEHH", "LH43BEH", "LH43BE-H"]
BE43D_FORMS = ["LH43BEDHLGFXKR", "LH43BEDH", "LH43BED-H", "43BEDH", "43BED"]


def _aliases():
    return ProductCatalogRepository(CATALOG).catalog()["aliases"]


@pytest.mark.parametrize("raw", BE43D_FORMS)
def test_every_be43d_notation_resolves_to_the_be43d_record(raw):
    match = ProductCatalogRepository(CATALOG).match(model_code=raw)
    assert match.model_key == "LH43BEDH"
    assert match.record is not None
    assert match.status in {"EXACT", "UNIQUE_MATCH"}


@pytest.mark.parametrize("raw", BE43H_FORMS)
def test_a_be43h_code_reaches_the_be43h_record_not_be43d(raw):
    """BE43H-H has a catalogue key of its own; BE43D's is not a stand-in."""

    match = ProductCatalogRepository(CATALOG).match(model_code=raw)
    assert match.model_key == "LH43BEHH"
    assert match.model_key != "LH43BEDH"
    assert match.status in {"EXACT", "UNIQUE_MATCH"}


@pytest.mark.parametrize("raw", BE43H_FORMS)
def test_every_be43h_notation_shares_one_identity(raw):
    assert canonical_model_identity(raw, aliases=_aliases()) == "43BEH"


@pytest.mark.parametrize("raw", BE43D_FORMS)
def test_every_be43d_notation_shares_one_identity(raw):
    assert canonical_model_identity(raw, aliases=_aliases()) == "43BED"


@pytest.mark.parametrize("beh", BE43H_FORMS)
@pytest.mark.parametrize("bed", BE43D_FORMS)
def test_no_be43h_notation_is_any_be43d_notation(beh, bed):
    aliases = _aliases()
    assert canonical_model_identity(beh, aliases=aliases) != canonical_model_identity(
        bed, aliases=aliases
    )


def test_a_listing_naming_a_be43h_code_reaches_the_be43h_record():
    """The listing path was the last one that still crossed the two models.

    A BE43H title used to reach the BE43D record two ways: through the four
    alias rows that named the code outright, and -- because
    ``normalize_model`` strips Hangul -- through the size-phrase row
    "삼성 107.9cm(43인치)", which reduces to 1079CM43 and sits inside every
    43-inch title. The rows are gone and the lookup now reads the code the
    title states, so neither route is open.

    BE43H then had no record at all; it has one now, and the title finds it.
    """

    repository = ProductCatalogRepository(CATALOG)
    for kwargs in (
        {"product_name": "LH43BEHHLGFXKR", "model_code": "LH43BEHHLGFXKR"},
        {"product_name": "삼성 107.9cm(43인치) LH43BEHHLGFXKR"},
    ):
        match = repository.match(**kwargs)
        assert match.model_key == "LH43BEHH", kwargs
        assert match.model_key != "LH43BEDH"

    aliases = repository.catalog()["aliases"]
    assert canonical_model_identity("LH43BEHHLGFXKR", aliases=aliases) != (
        canonical_model_identity("LH43BEDH", aliases=aliases)
    )


def test_a_be43h_listing_does_not_borrow_be43d_product_knowledge():
    """The evidence follows the record, so each model reads its own.

    Both models are catalogued and both have evidence. What must never happen
    is one reading the other's: answering from a neighbouring model's
    specification is the failure this whole separation prevents.
    """

    service = _service()
    question = "VESA HDMI USB speaker resolution"

    beh = service.facts_for_inquiry(
        product_id="identity-probe-43beh",
        product_name="삼성 107.9cm(43인치) LH43BEHHLGFXKR",
        model_code="LH43BEHHLGFXKR",
        question=question,
        include_all_catalog_fields=True,
    )
    bed = service.facts_for_inquiry(
        product_id="identity-probe-43bed",
        product_name="LH43BEDH",
        model_code="LH43BEDH",
        question=question,
        include_all_catalog_fields=True,
    )

    assert beh.matched is True and beh.safe_facts
    assert bed.matched is True and bed.safe_facts

    beh_models = {fact.model_code for fact in beh.safe_facts}
    bed_models = {fact.model_code for fact in bed.safe_facts}
    assert beh_models.isdisjoint(bed_models), (beh_models, bed_models)
    assert not any("BEHH" in str(model).upper() for model in bed_models)


def test_every_generation_and_size_in_the_be_line_stays_its_own_model():
    """Size and generation are identity; the -H variant marker is notation."""

    repository = ProductCatalogRepository(CATALOG)
    aliases = repository.catalog()["aliases"]

    expected = {
        "LH43BEAH": "43BEA", "LH43BECH": "43BEC",
        "LH43BEDH": "43BED", "LH43BEFH": "43BEF",
        "LH50BECH": "50BEC", "LH50BEDH": "50BED", "LH50BEFH": "50BEF",
        "LH85BEFH": "85BEF",
    }
    for key, core in expected.items():
        assert repository.match(model_code=key).model_key == key
        assert canonical_model_identity(key, aliases=aliases) == core
    assert len(set(expected.values())) == len(expected)

    # A 50-inch BE-H is a 50-inch BE-H, not the 43-inch one and not 50BED.
    assert canonical_model_identity("50BEH", aliases=aliases) == "50BEH"
    assert canonical_model_identity("LH50BEHHLGFXKR", aliases=aliases) == "50BEH"
    assert canonical_model_identity("43BEH", aliases=aliases) == "43BEH"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("LS32DM501EKXKR", "32DM501"),
        ("S32DM501", "32DM501"),
        ("32DM501", "32DM501"),
        ("LS32DM500EKXKR", "32DM500"),
        ("LS27FM501EKXKR", "27FM501"),
        ("LS49CG954EKXKR", "49CG954"),
        ("S43BM702UK", "43BM702"),
    ],
)
def test_the_43be_aliases_leave_the_samsung_display_rule_alone(raw, expected):
    """The catalogue-wide identity rule is unchanged by the new entries."""

    aliases = ProductCatalogRepository(CATALOG).catalog()["aliases"]
    assert canonical_model_identity(raw, aliases=aliases) == expected
