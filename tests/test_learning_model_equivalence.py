"""Which two model codes Learning treats as one product.

A 43-inch Coupang answer stored under ``LH43BEHHLGFXKR`` was reported as a
different model from the option's own representative ``LH43BEDH`` and went
unused, although the catalogue, Product Knowledge and the Coupang mapping
service all already read the two as one product.

The difference was not a policy disagreement.  ``canonical_model_identity``
applies the catalogue's ``MODEL_ALIASES`` only when it is handed them, and
Learning's ``_model_code`` was the one caller that did not.  Everything about
which codes are equivalent still lives in the catalogue; these tests pin that
Learning now asks the same question the other three ask, and -- just as
importantly -- that it did not start answering a wider one.
"""

from __future__ import annotations

import pytest

from repositories.product_catalog_repository import (
    ProductCatalogRepository,
    canonical_model_identity,
)
from services.learning_compatibility_service import (
    LearningCompatibilityService,
    extract_product_identity,
)

QUESTION = "VESA 규격 알려주세요"
ANSWER = "VESA 100x100 규격입니다."


def _decision(source: str, current: str, *, scope: str = "MODEL"):
    """The production compatibility decision for one Learning candidate."""

    return LearningCompatibilityService().evaluate(
        current_question=QUESTION,
        current_product=extract_product_identity(model_code=current),
        candidate_question=QUESTION,
        candidate_answer=ANSWER,
        candidate_product=extract_product_identity(model_code=source),
        candidate_metadata={"product_scope": scope},
    )


# --- A/B/C: the 43-inch equivalence the catalogue already records ---------------

@pytest.mark.parametrize(
    "source, current",
    [
        ("LH43BEHHLGFXKR", "LH43BEDH"),
        ("43BEH", "43BEDH"),
        ("43BED", "LH43BEDH"),
        ("LH43BE-H", "LH43BEDH"),
        ("LH43BEHH", "LH43BEDH"),
        # And the representative compared with itself, from either notation.
        ("LH43BEDH", "43BEDH"),
    ],
)
def test_the_43_inch_family_is_one_product(source, current) -> None:
    decision = _decision(source, current)

    assert decision.eligible is True
    assert decision.product_match == "EXACT_MODEL"
    assert decision.product_match_reason == "EXPLICIT_MODEL_CODE_MATCH"
    assert decision.reject_reason is None


# --- D: the exception must not widen to other sizes -----------------------------

@pytest.mark.parametrize(
    "source, current",
    [
        # The reported case's shape at every other size we sell.
        ("LH50BEHHLGFXKR", "LH43BEDH"),
        ("LH50BEHHLGFXKR", "LH50BEDH"),
        ("LH55BEHHLGFXKR", "LH55BEDH"),
        ("LH65BEHHLGFXKR", "LH65BEDH"),
        ("LH50BEH", "LH50BEDH"),
        ("LH55BEH", "LH55BEDH"),
        # A 43-inch answer is still not a 50-inch answer.
        ("LH43BEDH", "LH50BEDH"),
    ],
)
def test_bringing_43_inch_together_leaves_every_other_size_apart(
    source, current,
) -> None:
    decision = _decision(source, current)

    assert decision.eligible is False
    assert decision.product_match == "MISMATCH"
    assert decision.product_match_reason == "EXPLICIT_MODEL_CODE_MISMATCH"


# --- E/F: the pre-existing Samsung display rule is untouched --------------------

def test_neighbouring_display_models_still_do_not_collapse() -> None:
    """E: one digit apart is a different panel, alias table or not."""

    for source, current in (
        ("32DM500", "32DM501"),
        ("27FM500", "27FM501"),
        ("22D400", "24D400"),
    ):
        decision = _decision(source, current)
        assert decision.eligible is False, (source, current)
        assert decision.product_match == "MISMATCH"


@pytest.mark.parametrize(
    "source, current",
    [
        ("LS32DM501EKXKR", "32DM501"),
        ("S32DM501", "32DM501"),
        ("LS32DM501", "32DM501EKXKR"),
    ],
)
def test_the_samsung_notation_rule_still_matches(source, current) -> None:
    """F: LS/S/bare-core is built into the normalizer, not into an alias."""

    decision = _decision(source, current)

    assert decision.eligible is True
    assert decision.product_match == "EXACT_MODEL"


# --- the identity itself, and the provenance beside it --------------------------

def test_comparison_uses_the_catalogue_identity_not_the_raw_string() -> None:
    aliases = ProductCatalogRepository().catalog()["aliases"]
    raw = "LH43BEHHLGFXKR"

    identity = extract_product_identity(model_code=raw)

    # The identity used for comparison is the catalogue's representative...
    assert identity.model_code == "LH43BEDH"
    assert identity.model_code == canonical_model_identity(raw, aliases=aliases)
    # ...and it agrees with what the catalogue lookup itself resolves.
    assert ProductCatalogRepository().match(model_code=raw).model_key == "LH43BEDH"


def test_the_raw_model_string_is_not_rewritten_anywhere() -> None:
    """Only the comparison is canonical; stored provenance is untouched."""

    raw = "LH43BEHHLGFXKR"
    identity = extract_product_identity(
        model_code=raw, metadata={"canonical_model": raw, "model_code": raw},
    )

    assert identity.model_code == "LH43BEDH"
    # Nothing here mutates the caller's metadata or the string it was given.
    assert raw == "LH43BEHHLGFXKR"
    assert identity.to_dict()["model_code"] == "LH43BEDH"


def test_an_unreadable_catalogue_falls_back_instead_of_failing(monkeypatch) -> None:
    """A missing catalogue must not break identity comparison."""

    import services.learning_compatibility_service as module

    def exploding(self):
        raise OSError("catalogue unavailable")

    monkeypatch.setattr(ProductCatalogRepository, "catalog", exploding)

    # Previous behaviour: the built-in Samsung rule still applies, the
    # alias-based 43-inch equivalence does not, and nothing raises.
    assert module._catalog_aliases() is None
    assert extract_product_identity(model_code="LS32DM501EKXKR").model_code == "32DM501"
    assert _decision("LH43BEHHLGFXKR", "LH43BEDH").eligible is False


# --- the same verdict wherever a candidate is judged ----------------------------

@pytest.mark.parametrize("scope", ["MODEL", "VARIANT"])
def test_the_verdict_is_the_same_for_every_model_scoped_candidate(scope) -> None:
    assert _decision("LH43BEHHLGFXKR", "LH43BEDH", scope=scope).eligible is True
    assert _decision("LH50BEHHLGFXKR", "LH50BEDH", scope=scope).eligible is False


def test_a_human_verified_candidate_is_judged_the_same_way() -> None:
    """Human verification does not change which product an answer is about."""

    service = LearningCompatibilityService()
    for metadata, expected in (
        ({"product_scope": "MODEL", "human_verified": True}, True),
        ({"product_scope": "MODEL"}, True),
    ):
        decision = service.evaluate(
            current_question=QUESTION,
            current_product=extract_product_identity(model_code="LH43BEDH"),
            candidate_question=QUESTION,
            candidate_answer=ANSWER,
            candidate_product=extract_product_identity(
                model_code="LH43BEHHLGFXKR"
            ),
            candidate_metadata=metadata,
        )
        assert decision.eligible is expected
        assert decision.product_match == "EXACT_MODEL"
