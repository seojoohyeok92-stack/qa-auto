"""A listing that says BE43H must not be answered from the BE43D record.

``MODEL_ALIASES`` carries two different kinds of row.  Most map a listing
title to the catalogue record it names -- that is the documented purpose, and
it is how a title with no model code of its own reaches a record at all.  Six
rows added on 2026-09-18 did something else: they mapped the BE43H-H codes
``LH43BEHHLGFXKR``, ``LH43BEHH``, ``LH43BE-H`` and ``43BEH`` onto
``LH43BEDH``, which is BE43D-H, a different model.

Removing those rows is not by itself enough, because ``normalize_model``
strips Hangul: the size-phrase row ``"삼성 107.9cm(43인치)"`` reduces to
``1079CM43``, a substring of every 43-inch title including a BE43H one.  So
the lookup also reads what the title says for itself.  A title that writes a
model code has named its product, even when this catalogue has no key for it,
and an alias that disagrees with that code does not speak for that title.

BE43H now has catalogue records of its own, one per size, keyed on the exact
code each Samsung spec sheet states.  A BE43H listing reaches BE43H.  What it
must never reach is BE43D, and no alias row exists to take it there.
"""

from __future__ import annotations

import pytest

from repositories.product_catalog_repository import (
    ProductCatalogRepository,
    canonical_model_identity,
)

BE43D_RECORD = "LH43BEDH"

# Every way a BE43H listing reaches the lookup.
BE43H_TITLES = [
    "삼성 107.9cm(43인치) LH43BEHHLGFXKR",
    "삼성 107.9cm(43인치) 4K UHD LED TV LH43BE-H 스마트 비즈니스TV 스탠드형",
    "LH43BEHH 43인치 비즈니스TV",
    "삼성 LH43BEHHLGFXKR 스탠드형",
]
BE43H_CODES = ["LH43BEHHLGFXKR", "LH43BEHH", "LH43BEH", "LH43BE-H", "43BEH"]


def _repository() -> ProductCatalogRepository:
    return ProductCatalogRepository()


# --- 1. a BE43H title never elects the BE43D record -----------------------------

@pytest.mark.parametrize("title", BE43H_TITLES)
def test_a_be43h_title_does_not_resolve_to_the_be43d_record(title):
    match = _repository().match(product_name=title)

    assert match.model_key != BE43D_RECORD, (title, match.status)
    # Either it reaches BE43H's own record, or -- for a title that names no
    # generation at all -- it elects nothing. Never the neighbour.
    assert match.model_key in {"LH43BEHH", None}, (title, match.status)


@pytest.mark.parametrize("title", BE43H_TITLES)
def test_a_be43h_title_does_not_elect_the_be43d_record_via_the_option(title):
    """The same holds when the code arrives in the option rather than the name."""

    match = _repository().match(product_name="삼성 비즈니스TV", option_name=title)

    assert match.model_key != BE43D_RECORD
    assert match.model_key in {"LH43BEHH", None}


# --- 2. a BE43H code alone is NOT_FOUND, not a neighbour's record ---------------

@pytest.mark.parametrize("code", BE43H_CODES)
def test_a_be43h_code_lookup_reaches_the_be43h_record(code):
    match = _repository().match(model_code=code)

    assert match.model_key == "LH43BEHH", (code, match.status)
    assert match.model_key != BE43D_RECORD
    assert match.status in {"EXACT", "UNIQUE_MATCH"}


def test_the_be43h_line_has_its_own_catalogue_key():
    """The premise of the test above, stated rather than assumed."""

    catalog = _repository().catalog()["catalog"]
    assert BE43D_RECORD in catalog
    assert "LH43BEHH" in catalog
    assert catalog["LH43BEHH"]["model"] == "LH43BEHHLGFXKR"
    assert catalog["LH43BEHH"] != catalog[BE43D_RECORD]


# --- 3. no alias row ties a BE43H notation to the BE43D record ------------------

def test_no_alias_row_maps_a_be43h_notation_onto_be43d():
    aliases = _repository().catalog()["aliases"]

    offenders = {
        key: target for key, target in aliases.items()
        if "43BEH" in str(key).upper().replace("-", "")
        and str(target).upper() == BE43D_RECORD
    }
    assert not offenders, offenders


# --- 4. BE43D itself still resolves, by title and by code -----------------------

@pytest.mark.parametrize(
    "kwargs",
    [
        {"model_code": "LH43BEDH"},
        {"model_code": "LH43BED-H"},
        {"model_code": "43BEDH"},
        {"model_code": "43BED"},
        {"model_code": "LH43BEDHLGFXKR"},
        {"product_name": "삼성 107.9cm(43인치) LH43BEDHLGFXKR"},
        {"product_name": "삼성 107.9cm(43인치) BE43D"},
        {"product_name": "삼성 107.9cm(43인치)"},
        {"product_name": "TV 107.9cm(43인치)"},
    ],
    ids=lambda kw: next(iter(kw.values())),
)
def test_be43d_still_resolves_to_its_own_record(kwargs):
    match = _repository().match(**kwargs)

    assert match.model_key == BE43D_RECORD, (kwargs, match.status)
    assert match.record is not None
    assert match.status in {"EXACT", "UNIQUE_MATCH"}


# --- 5. the identities, and the aliases that are really notation ----------------

def test_the_two_43_inch_models_keep_separate_identities():
    aliases = _repository().catalog()["aliases"]

    beh = {canonical_model_identity(code, aliases=aliases) for code in BE43H_CODES}
    bed = {canonical_model_identity(code, aliases=aliases)
           for code in ("LH43BEDHLGFXKR", "LH43BEDH", "LH43BED-H", "43BEDH", "43BED")}

    assert beh == {"43BEH"}
    assert bed == {"43BED"}


@pytest.mark.parametrize(
    "forms, core",
    [
        (["LS32DM501EKXKR", "S32DM501", "LS32DM501", "32DM501EKXKR"], "32DM501"),
        (["LS25BG400EKXKR", "S25BG400"], "25BG400"),
        (["LS49DG930SKXKR", "S49DG930"], "49DG930"),
        (["LS22D400GAKXKR", "S22D400"], "22D400"),
        (["LS24D400GAKXKR", "S24D400"], "24D400"),
        (["LS27D400GAKXKR", "S27D400"], "27D400"),
    ],
    ids=["32DM501", "25BG400", "49DG930", "22D400", "24D400", "27D400"],
)
def test_the_notation_aliases_are_untouched(forms, core):
    aliases = _repository().catalog()["aliases"]
    identities = {f: canonical_model_identity(f, aliases=aliases) for f in forms}

    assert set(identities.values()) == {core}, identities


def test_a_listing_with_no_model_code_still_reaches_its_record_by_alias():
    """Removing four rows must not take the alias mechanism with it."""

    repository = _repository()
    for title, expected in (
        ("삼성 125.7cm(50인치)", "LH50BEDH"),
        ("삼성 138.7cm(55인치)", "LH55BEDH"),
        ("삼성 163.9cm(65인치)", "LH65BEDH"),
    ):
        match = repository.match(product_name=title)
        assert match.model_key == expected, (title, match.model_key, match.status)
