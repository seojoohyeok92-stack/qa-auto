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


# --- the 43-inch BE equivalence -------------------------------------------------
#
# 43BED, 43BEH and 43BEDH differ by model year, not by specification, so for
# answering they name one product.  This is operator-confirmed for 43 inches
# only and is recorded where every other model-code equivalence in this
# catalogue is recorded -- MODEL_ALIASES -- so no code knows about it.
#
# Before it was recorded the two halves of the product were unreachable from
# each other: the catalogue record lived under LH43BEDH and its Product
# Knowledge under LH43BEHHLGFXKR, which was not a catalogue key at all.

BE43_FORMS = [
    "LH43BEHHLGFXKR", "LH43BEHH", "LH43BE-H",
    "LH43BEDH", "LH43BED-H", "43BEDH", "43BEH", "43BED",
]


@pytest.mark.parametrize("raw", BE43_FORMS)
def test_every_43be_notation_resolves_to_one_catalog_record(raw):
    match = ProductCatalogRepository(CATALOG).match(model_code=raw)
    assert match.model_key == "LH43BEDH"
    assert match.record is not None
    assert match.status in {"EXACT", "UNIQUE_MATCH"}


@pytest.mark.parametrize("raw", BE43_FORMS)
def test_every_43be_notation_shares_one_identity(raw):
    aliases = ProductCatalogRepository(CATALOG).catalog()["aliases"]
    assert canonical_model_identity(raw, aliases=aliases) == "LH43BEDH"


def test_the_confirmed_mapping_reaches_the_shared_product_knowledge():
    """The raw CONFIRMED model and the representative read the same facts.

    The operator's mapping still says LH43BEHHLGFXKR -- that provenance is not
    rewritten.  What changed is that the representative the catalogue uses now
    reaches the same evidence instead of none.
    """

    service = _service()
    facts = {}
    for raw in ("LH43BEHHLGFXKR", "LH43BEDH"):
        result = service.facts_for_inquiry(
            product_id="identity-probe-43be",
            product_name=raw,
            model_code=raw,
            question="VESA HDMI USB speaker resolution",
            include_all_catalog_fields=True,
        )
        assert result.matched is True
        facts[raw] = {(f.field_key, str(f.value)) for f in result.safe_facts}
    assert facts["LH43BEDH"]
    assert facts["LH43BEDH"] == facts["LH43BEHHLGFXKR"]


def test_the_equivalence_does_not_generalise_to_other_sizes_or_lines():
    """Only 43 inches was confirmed; nothing else may have been pulled in."""

    repository = ProductCatalogRepository(CATALOG)
    aliases = repository.catalog()["aliases"]
    # Other 43-inch BE lines keep their own records.
    for other in ("LH43BEAH", "LH43BECH", "LH43BEFH"):
        assert repository.match(model_code=other).model_key == other
        assert canonical_model_identity(other, aliases=aliases) == other
    # Other sizes keep theirs, including their own D/C/F distinctions.
    for other in ("LH50BEDH", "LH50BECH", "LH50BEFH", "LH85BEFH"):
        assert repository.match(model_code=other).model_key == other
        assert canonical_model_identity(other, aliases=aliases) == other
    assert canonical_model_identity("50BEH", aliases=aliases) != "LH50BEDH"


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
