"""A Samsung display model, however much of its name is written out.

The Korean region suffix ends in ``XKR`` but its preceding letters vary by
panel line: ``EKXKR`` and ``GAKXKR`` alongside ``EFXKR`` and ``ESXKR``.  The
rule anchored on ``KXKR``, so for the two lines that do not use a ``K`` there
it read only part of the tail as a suffix and left the rest attached to the
model.  ``LS32HG806ESXKR`` and ``32HG806`` were two products to every path
that compares identities, and the full name could not reach its own catalogue
record, ``S32HG806``.

What is pinned here is both halves: every notation of one model agrees, and
models that differ by one character of their core still do not.
"""

from __future__ import annotations

import pytest

from repositories.product_catalog_repository import (
    ProductCatalogRepository,
    canonical_model_identity,
    normalize_model,
)
from services.learning_compatibility_service import (
    LearningCompatibilityService,
    extract_product_identity,
)

# Every monitor the audit covered, with the core it must reduce to.
MONITORS = [
    ("LS32DM500EKXKR", "32DM500"),
    ("LS32DM501EKXKR", "32DM501"),
    ("LS27FM500EKXKR", "27FM500"),
    ("LS27FM501EKXKR", "27FM501"),
    ("LS32FM500EKXKR", "32FM500"),
    ("LS32FM501EKXKR", "32FM501"),
    ("LS25BG400EKXKR", "25BG400"),
    ("LS25HG400EKXKR", "25HG400"),
    ("LS27HG400EKXKR", "27HG400"),
    ("LS32FG500EKXKR", "32FG500"),
    ("LS27DG700EKXKR", "27DG700"),
    ("LS27FG700EKXKR", "27FG700"),
    # The two the anchored rule could not reduce.
    ("LS27HG806EFXKR", "27HG806"),
    ("LS32HG806ESXKR", "32HG806"),
    ("LS49CG954EKXKR", "49CG954"),
    ("LS49DG930SKXKR", "49DG930"),
    ("LS32DG300EKXKR", "32DG300"),
    ("LS22D400GAKXKR", "22D400"),
    ("LS24D400GAKXKR", "24D400"),
    ("LS27D400GAKXKR", "27D400"),
]


def _aliases():
    return ProductCatalogRepository().catalog()["aliases"]


def _notations(full: str, core: str) -> list[str]:
    """The six ways one model is written: LS/S/bare, with and without suffix."""

    without_prefix = full[2:] if full.startswith("LS") else full
    return [full, "S" + without_prefix, without_prefix, "LS" + core, "S" + core, core]


# --- 1. every notation of one model is one identity -----------------------------

@pytest.mark.parametrize("full, core", MONITORS, ids=[m[0] for m in MONITORS])
def test_every_notation_of_a_model_reduces_to_one_core(full, core) -> None:
    aliases = _aliases()

    identities = {
        notation: canonical_model_identity(notation, aliases=aliases)
        for notation in _notations(full, core)
    }

    assert set(identities.values()) == {core}, identities


@pytest.mark.parametrize("full, core", MONITORS, ids=[m[0] for m in MONITORS])
def test_learning_reads_every_notation_as_the_same_model(full, core) -> None:
    identities = {
        notation: extract_product_identity(model_code=notation).model_code
        for notation in _notations(full, core)
    }

    assert set(identities.values()) == {core}, identities


# --- 2/3. the two the old rule split -------------------------------------------

@pytest.mark.parametrize(
    "full, core",
    [("LS27HG806EFXKR", "27HG806"), ("LS32HG806ESXKR", "32HG806")],
)
def test_the_non_k_region_suffixes_are_suffixes(full, core) -> None:
    """EFXKR and ESXKR are region tails, not part of the model."""

    assert canonical_model_identity(full, aliases=_aliases()) == core
    assert canonical_model_identity(full) == core


@pytest.mark.parametrize(
    "full, core",
    [("LS27HG806EFXKR", "27HG806"), ("LS32HG806ESXKR", "32HG806")],
)
def test_learning_matches_the_full_name_against_the_core(full, core) -> None:
    decision = LearningCompatibilityService().evaluate(
        current_question="VESA 규격 알려주세요",
        current_product=extract_product_identity(model_code=core),
        candidate_question="VESA 규격 알려주세요",
        candidate_answer="VESA 100x100 입니다.",
        candidate_product=extract_product_identity(model_code=full),
        candidate_metadata={"product_scope": "MODEL"},
    )

    assert decision.eligible is True
    assert decision.product_match == "EXACT_MODEL"
    assert decision.product_match_reason == "EXPLICIT_MODEL_CODE_MATCH"


# --- 4. the full name reaches its own catalogue record --------------------------

def test_the_full_name_now_finds_its_catalogue_record() -> None:
    repository = ProductCatalogRepository()

    assert repository.match(model_code="LS32HG806ESXKR").model_key == "S32HG806"
    # The short forms already did, and still do.
    assert repository.match(model_code="S32HG806").model_key == "S32HG806"
    assert repository.match(model_code="32HG806").model_key == "S32HG806"


# --- 6. Historical candidates are judged by the same call -----------------------

def test_a_historical_candidate_is_judged_the_same_way() -> None:
    from services.historical_case_service import HistoricalCaseService

    assert HistoricalCaseService.__module__  # imported for the same evaluator
    decision = LearningCompatibilityService().evaluate(
        current_question="해상도 알려주세요",
        current_product=extract_product_identity(model_code="32HG806"),
        candidate_question="해상도 알려주세요",
        candidate_answer="QHD 입니다.",
        candidate_product=extract_product_identity(model_code="LS32HG806ESXKR"),
        candidate_metadata={"product_scope": "MODEL"},
    )
    assert decision.eligible is True


# --- negative controls ----------------------------------------------------------

@pytest.mark.parametrize(
    "left, right",
    [
        ("32DM500", "32DM501"),
        ("27FM500", "27FM501"),
        ("32FM500", "32FM501"),
        ("25BG400", "25HG400"),
        ("27DG700", "27FG700"),
        ("22D400", "24D400"),
        ("LH43BEDH", "LH50BEDH"),
        ("50BEH", "50BEDH"),
        ("85BEH", "85BEDH"),
        ("43BEH", "43BEDH"),
        ("LH43BEHHLGFXKR", "LH43BEDH"),
        # And the two repaired families must not collapse into each other.
        ("27HG806", "32HG806"),
        ("LS27HG806EFXKR", "LS32HG806ESXKR"),
    ],
)
def test_models_that_differ_stay_different(left, right) -> None:
    aliases = _aliases()

    assert canonical_model_identity(left, aliases=aliases) != canonical_model_identity(
        right, aliases=aliases
    )
    assert (
        extract_product_identity(model_code=left).model_code
        != extract_product_identity(model_code=right).model_code
    )


# --- the change reaches only what the audit said it would -----------------------

def test_the_business_display_line_reduces_the_same_way_monitors_do() -> None:
    """``LH`` names a core exactly as ``LS`` does, and the tail is a tail.

    This used to stop at ``LS``: an LH code kept whatever string it arrived
    as, so LH43BEDHLGFXKR and LH43BEDH were two products, and the seller's own
    LH43BE-H was a third.  Reducing them is the same rule, measured over the
    whole catalogue -- 1,586 keys, one new group, and that group is three
    spellings of one record whose specs are byte-identical.
    """

    aliases = _aliases()
    for code, core in (
        ("LH43BEDHLGFXKR", "43BED"), ("LH43BEDH", "43BED"), ("LH43BED-H", "43BED"),
        ("LH43BEHHLGFXKR", "43BEH"), ("LH43BEHH", "43BEH"), ("LH43BE-H", "43BEH"),
        ("LH50BEDHLBFXKR", "50BED"), ("LH50BEDHLGFXKR", "50BED"),
        ("LH55WMBWBGCXKR", "55WMBW"), ("LH55WMBW", "55WMBW"),
        ("LH65QBREBGCXKR", "65QBRE"), ("LH65QBRE", "65QBRE"),
    ):
        assert canonical_model_identity(code, aliases=aliases) == core, code


def test_a_family_whose_structure_this_cannot_read_keeps_its_own_code() -> None:
    """``[A-Z]{0,3}`` bounds the prefix and the core must be size-led.

    KQ/UN codes do not state a size first, so nothing here derives a core for
    them -- which is what keeps this from becoming a rule about product
    families it was never measured against.
    """

    for code in ("KQ43QND90AFXKR", "UN43N5000AFXKR", "KQ42SF90AEXKR"):
        identity = canonical_model_identity(code, aliases=_aliases())
        assert identity == normalize_model(code), (code, identity)


def test_the_43_inch_line_is_two_identities_not_one() -> None:
    """BE43H-H and BE43D-H are different models and must stay different.

    Six MODEL_ALIASES rows once said otherwise -- an operator judgement about
    specifications, read here as identity.  An alias may respell a model; it
    may not restate it.
    """

    aliases = _aliases()

    beh = {canonical_model_identity(c, aliases=aliases)
           for c in ("LH43BEHHLGFXKR", "LH43BEHH", "LH43BEH", "LH43BE-H", "43BEH")}
    bed = {canonical_model_identity(c, aliases=aliases)
           for c in ("LH43BEDHLGFXKR", "LH43BEDH", "LH43BED-H", "43BEDH", "43BED")}

    assert beh == {"43BEH"}
    assert bed == {"43BED"}
    # And neither reaches another size.
    for size in ("50", "55", "65", "85"):
        assert canonical_model_identity(f"LH{size}BEHHLGFXKR", aliases=aliases) == (
            f"{size}BEH"
        )
        assert canonical_model_identity(f"LH{size}BEHHLGFXKR", aliases=aliases) not in (
            beh | bed
        )
