"""The BE-H business-display line has catalogue records of its own.

Separating BE43H-H from BE43D-H left the BEH line with no catalogue key, so a
listing that named it fail-closed to NOT_FOUND. The answer to that is not an
alias back onto the neighbouring generation -- it is the record the catalogue
was missing.

Every record here is keyed on the exact model code its own Samsung spec sheet
states, one sheet per size, all six present in the v8.2 Product Knowledge
source set:

    LH43BEHHLGFXKR_spec.jpg   LH50BEHHLGFXKR_spec.jpg   LH55BEHHLGFXKR_spec.jpg
    LH65BEHHLGFXKR_spec.jpg   LH75BEHHLGFXKR_spec.jpg   LH85BEHHLGFXKR_spec.jpg

The key follows the line's own convention, ``LH{size}BE{generation}H`` -- the
same shape as BEAH, BECH, BEDH, BEFH and BETH, with H as the generation. No
alias row was added: the notations reduce to the record structurally.

What is pinned is that the line is reachable, that it is still not BED, and
that the two never share evidence.
"""

from __future__ import annotations

import pytest

from repositories.product_catalog_repository import (
    ProductCatalogRepository,
    canonical_model_identity,
)
from services.product_knowledge_service import ProductKnowledgeService

SIZES = ["43", "50", "55", "65", "75", "85"]


def _repository() -> ProductCatalogRepository:
    return ProductCatalogRepository()


def _aliases():
    return _repository().catalog()["aliases"]


# --- the records exist, and say what their source says --------------------------

@pytest.mark.parametrize("size", SIZES)
def test_the_be_h_line_has_a_catalogue_record(size):
    catalog = _repository().catalog()["catalog"]
    record = catalog.get(f"LH{size}BEHH")

    assert record is not None
    assert record["model"] == f"LH{size}BEHHLGFXKR"
    assert record["size_inch"] == f"{size}인치"
    assert record["resolution"] == "4K UHD"
    assert record["brand"] == "삼성"


@pytest.mark.parametrize("size", SIZES)
def test_a_field_the_sheet_does_not_state_is_not_borrowed(size):
    """The BEH sheets state no refresh rate and no VESA size.

    A neighbouring generation's 60Hz/200x200 would read as this model's
    measured specification, so the field stays empty until a source states it.
    """

    record = _repository().catalog()["catalog"][f"LH{size}BEHH"]

    assert record["hz"] is None
    assert record["vesa"] is None


# --- every notation reaches it --------------------------------------------------

@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize(
    "notation", ["{prefix}HHLGFXKR", "{prefix}HH", "{prefix}H", "{prefix}-H"]
)
def test_every_be_h_notation_resolves_to_its_own_record(size, notation):
    code = notation.format(prefix=f"LH{size}BE")
    match = _repository().match(model_code=code)

    assert match.model_key == f"LH{size}BEHH", (code, match.status)
    assert match.record is not None
    assert match.status in {"EXACT", "UNIQUE_MATCH"}


@pytest.mark.parametrize(
    "title",
    [
        "삼성 107.9cm(43인치) UHD 4K 1등급 비즈니스 TV LH43BEHHLGFXKR 스탠드형",
        "삼성 107.9cm(43인치) UHD 4K 1등급 비즈니스 TV LH43BEHHLGFXKR 벽걸이형",
        "삼성 4K UHD 스마트 비즈니스 TV LH43BEHHLGFXKR 1등급 107.9cm(43인치), 스탠드",
    ],
)
def test_a_real_be43h_listing_title_reaches_the_be43h_record(title):
    match = _repository().match(product_name=title)

    assert match.model_key == "LH43BEHH", (title, match.status)
    assert match.model_key != "LH43BEDH"


# --- and none of them reaches the neighbouring generation -----------------------

@pytest.mark.parametrize("size", SIZES)
def test_be_h_and_be_d_are_two_identities(size):
    aliases = _aliases()
    catalog = _repository().catalog()["catalog"]
    if f"LH{size}BEDH" not in catalog:
        pytest.skip(f"no BED record at {size} inches")

    assert canonical_model_identity(f"LH{size}BEHH", aliases=aliases) == f"{size}BEH"
    assert canonical_model_identity(f"LH{size}BEDH", aliases=aliases) == f"{size}BED"


@pytest.mark.parametrize(
    "code", ["LH43BEDH", "LH43BED-H", "43BEDH", "43BED", "LH43BEDHLGFXKR"]
)
def test_be43d_still_resolves_to_its_own_record(code):
    assert _repository().match(model_code=code).model_key == "LH43BEDH"


def test_the_line_was_completed_without_a_single_alias_row():
    """The notations reduce to the record; nothing had to be declared.

    An alias that dropped the generation letter is what caused the original
    collapse, so the line is reachable by structure alone.
    """

    aliases = _aliases()
    named = {
        key: target for key, target in aliases.items()
        if "BEH" in str(key).upper().replace("-", "")
    }
    assert not named, named


# --- the evidence follows the identity ------------------------------------------

def test_be43h_and_be43d_never_share_product_knowledge():
    service = ProductKnowledgeService(_repository())
    question = "해상도와 스피커, HDMI 단자 알려주세요"

    beh = service.facts_for_inquiry(
        product_id="", product_name="LH43BEHHLGFXKR", model_code="LH43BEHHLGFXKR",
        question=question, include_all_catalog_fields=True,
    )
    bed = service.facts_for_inquiry(
        product_id="", product_name="LH43BEDH", model_code="LH43BEDH",
        question=question, include_all_catalog_fields=True,
    )

    assert beh.matched and bed.matched
    beh_models = {fact.model_code for fact in beh.safe_facts}
    bed_models = {fact.model_code for fact in bed.safe_facts}

    assert beh_models and bed_models
    assert beh_models.isdisjoint(bed_models), (beh_models, bed_models)
    assert all("BEH" in str(model).upper() for model in beh_models)
    assert not any("BEHH" in str(model).upper() for model in bed_models)


def test_adding_the_line_introduced_no_identity_collision():
    """Each new record is the only catalogue key with its identity."""

    repository = _repository()
    catalog = repository.catalog()["catalog"]
    aliases = repository.catalog()["aliases"]

    shared: dict[str, set[str]] = {}
    for key in catalog:
        identity = canonical_model_identity(key, aliases=aliases)
        if identity:
            shared.setdefault(identity, set()).add(key)

    for size in SIZES:
        identity = canonical_model_identity(f"LH{size}BEHH", aliases=aliases)
        assert shared[identity] == {f"LH{size}BEHH"}, shared[identity]
