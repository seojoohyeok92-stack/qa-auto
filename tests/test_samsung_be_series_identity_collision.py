"""BE43H and BE43D are two products, and canonicalization must keep them apart.

``MODEL_ALIASES`` is where this catalogue records that two model codes are the
same product.  It is also the input ``canonical_model_identity`` trusts to
rewrite one code into another.  Nothing checked that such a rewrite preserves
the model, so on 2026-09-18 an operator decision about *specifications* --
"43BED and 43BEH differ by model year, the panels are the same" -- was written
as six alias rows and silently became a decision about *identity*:

    LH43BEHHLGFXKR -> LH43BEDH
    LH43BEHH       -> LH43BEDH
    LH43BE-H       -> LH43BEDH

BE43H-H (``LH43BEHH...``) and BE43D-H (``LH43BEDH``) are different models.
Once they share a canonical identity, every path that compares identities --
Learning scope, Product Knowledge retrieval, Coupang mapping -- treats one
product's evidence as the other's, and no caller can tell them apart again.

What is pinned here is both halves: the BE43H notations agree with each other,
they do not agree with BE43D, and the notation aliases that really are
notation (``LS32DM501EKXKR`` -> ``32DM501``) keep working.
"""

from __future__ import annotations

import re

import pytest

from repositories.product_catalog_repository import (
    ProductCatalogRepository,
    canonical_model_identity,
)

# Every way this store writes the 43-inch BE43H-H, and the BE43D-H it must not
# become.  ``LH43BE-H`` is the seller's own listing notation for BE43H-H.
BE43H_FORMS = ["LH43BEHHLGFXKR", "LH43BEH", "LH43BE-H", "LH43BEHH"]
BE43D_FORMS = ["LH43BEDHLGFXKR", "LH43BEDH", "LH43BED-H", "43BEDH", "43BED"]


def _aliases():
    return ProductCatalogRepository().catalog()["aliases"]


def test_every_be43h_notation_is_one_identity():
    aliases = _aliases()
    identities = {raw: canonical_model_identity(raw, aliases=aliases) for raw in BE43H_FORMS}
    assert len(set(identities.values())) == 1, identities
    assert None not in set(identities.values()), identities


def test_every_be43d_notation_is_one_identity():
    aliases = _aliases()
    identities = {raw: canonical_model_identity(raw, aliases=aliases) for raw in BE43D_FORMS}
    assert len(set(identities.values())) == 1, identities


@pytest.mark.parametrize("beh", BE43H_FORMS)
@pytest.mark.parametrize("bed", BE43D_FORMS)
def test_be43h_never_canonicalizes_onto_be43d(beh, bed):
    aliases = _aliases()
    assert canonical_model_identity(beh, aliases=aliases) != canonical_model_identity(
        bed, aliases=aliases
    )


def test_an_alias_may_not_restate_the_model():
    """A code-shaped alias whose target is a different model is not an identity."""
    aliases = {"LH43BEHH": "LH43BEDH"}
    assert canonical_model_identity("LH43BEHH", aliases=aliases) != canonical_model_identity(
        "LH43BEDH", aliases=aliases
    )


# --- the aliases that really are notation keep working --------------------------

NOTATION_GROUPS = {
    "32DM501": ["LS32DM501EKXKR", "S32DM501", "LS32DM501", "32DM501EKXKR", "32DM501"],
    "25BG400": ["LS25BG400EKXKR", "S25BG400", "25BG400"],
    "49DG930": ["S49DG930", "LS49DG930SKXKR", "49DG930"],
    "22D400": ["S22D400", "LS22D400GAKXKR", "22D400"],
    "24D400": ["S24D400", "LS24D400GAKXKR", "24D400"],
    "27D400": ["S27D400", "LS27D400GAKXKR", "27D400"],
}


@pytest.mark.parametrize("core, forms", sorted(NOTATION_GROUPS.items()))
def test_a_notation_alias_still_reduces_to_its_core(core, forms):
    aliases = _aliases()
    identities = {f: canonical_model_identity(f, aliases=aliases) for f in forms}
    assert set(identities.values()) == {core}, identities


def test_the_three_d400_sizes_are_three_models():
    aliases = _aliases()
    cores = {size: canonical_model_identity(f"{size}D400", aliases=aliases)
             for size in ("22", "24", "27")}
    assert len(set(cores.values())) == 3, cores


# --- the invariant, over the whole catalogue ------------------------------------

def _be_parts(token: str) -> tuple[str, str] | None:
    """(size, generation) for a BE-line code, whichever way it is written."""

    match = re.fullmatch(
        r"(?:LH)?(\d{2})BE([A-Z])H?(?:[A-Z]{0,3}XKR)?",
        re.sub(r"[^A-Z0-9]", "", token.upper()),
    )
    return (match.group(1), match.group(2)) if match else None


def test_no_canonical_identity_covers_two_models_of_the_be_line():
    """Size and generation are identity; nothing may merge across either.

    Run over every catalogue key plus every alias key and target, so a future
    alias row that restates a model fails here rather than in production.
    """

    repository = ProductCatalogRepository()
    loaded = repository.catalog()
    aliases = loaded["aliases"]
    probes = (
        {str(k) for k in loaded["catalog"]}
        | {str(k) for k in aliases}
        | {str(v) for v in aliases.values()}
    )

    by_identity: dict[str, set[tuple[str, str]]] = {}
    for probe in probes:
        parts = _be_parts(probe)
        if parts is None:
            continue
        identity = canonical_model_identity(probe, aliases=aliases)
        assert identity is not None, probe
        by_identity.setdefault(identity, set()).add(parts)

    merged = {i: p for i, p in by_identity.items() if len(p) > 1}
    assert not merged, merged
    # And the line really is covered, rather than the probe finding nothing.
    assert len(by_identity) >= 25
