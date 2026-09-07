"""Read-only runtime access to the operator-maintained product catalog.

Identification is deliberately deterministic and fail-closed. Borrowing a
neighbouring model's specification is worse than saying nothing, so a lookup
that cannot name one model never picks one.

What changed is how much of the listing the lookup is allowed to read before it
gives up. It used to require a whole catalog key to appear inside the product
name -- "LH50BEDH" inside "삼성 삼탠바이미 50인치(125cm) 4K UHD 무빙 스마트
비즈니스TV 거치대 화이트", which normalises to "50125CM4KUHDTV" and contains no
model code at all. Measured over this store, 1,584 of 3,051 inquiries (52%) name
a product whose title carries no full catalog key, so the majority of listings
could never reach their own catalogued specification.

So the outcome is now graded rather than binary:

    EXACT         the listing (or an explicit model_code) names a catalog key
    UNIQUE_MATCH  a model-code-shaped token in the listing narrows to exactly one
    AMBIGUOUS     it narrows to several -- the candidates are reported, unchosen
    NOT_FOUND     nothing in the listing looks like a catalogued model

Only EXACT and UNIQUE_MATCH carry a record. AMBIGUOUS reports candidates so a
reader downstream can see them as candidates; it never elects one, because
which of three 43-inch panels a listing means is not something a substring can
settle.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


DEFAULT_PRODUCT_CATALOG_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "model_data_with_color.json"
)


def normalize_model(value: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


EXACT = "EXACT"
UNIQUE_MATCH = "UNIQUE_MATCH"
AMBIGUOUS = "AMBIGUOUS"
NOT_FOUND = "NOT_FOUND"
UNAVAILABLE = "UNAVAILABLE"

# The shortest run of a catalog key that may stand in for the whole key.
# Five characters is what the existing whole-key rule already required of a
# match, kept identical so this cannot start recognising something the old
# rule would have called too generic.
MIN_IDENTIFYING_LENGTH = 5

# A token that looks like a model code: letters and digits together. Purely
# structural -- it knows nothing about any product, brand or line, and is what
# separates "LH43B" from "50" or "UHD".
_MODEL_TOKEN = re.compile(r"(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*[0-9])[A-Z0-9]{4,}")

# Units that make a token a measurement rather than a model code. A listing
# says "107.9CM" and "43인치"; neither identifies a model, and both would
# otherwise satisfy the letters-and-digits shape above.
_MEASUREMENT_SUFFIX = ("CM", "MM", "KG", "INCH", "HZ", "W", "K")


@dataclass(frozen=True)
class CatalogMatch:
    model_key: str | None
    record: dict[str, Any] | None
    reason: str | None = None
    # How the identification ended. ``EXACT``/``UNIQUE_MATCH`` are the only
    # states that carry a record; the rest carry ``candidates`` at most.
    status: str = EXACT
    # (model_key, record) pairs an AMBIGUOUS lookup narrowed to. Reported so a
    # caller can show them as candidates, never so it can pick one.
    candidates: tuple[tuple[str, dict[str, Any]], ...] = ()


@lru_cache(maxsize=4)
def _load_catalog(path_text: str, mtime_ns: int, size: int) -> dict[str, Any]:
    del mtime_ns, size
    value = json.loads(Path(path_text).read_text(encoding="utf-8"))
    catalog = value.get("MODEL_CATALOG") if isinstance(value, dict) else None
    aliases = value.get("MODEL_ALIASES") if isinstance(value, dict) else None
    if not isinstance(catalog, dict) or not isinstance(aliases, dict):
        raise ValueError("PRODUCT_CATALOG_INVALID")
    return {
        "catalog": catalog,
        "aliases": aliases,
        "normalized_catalog": {
            normalize_model(key): str(key) for key in catalog
            if normalize_model(key)
        },
    }


class ProductCatalogRepository:
    """Cached JSON catalog lookup with fail-closed model identification."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or DEFAULT_PRODUCT_CATALOG_PATH).resolve()

    def catalog(self) -> dict[str, Any]:
        stat = self.path.stat()
        return _load_catalog(str(self.path), stat.st_mtime_ns, stat.st_size)

    def match(
        self, *, product_name: object = "", option_name: object = "",
        model_code: object = None,
    ) -> CatalogMatch:
        try:
            loaded = self.catalog()
        except (OSError, ValueError, json.JSONDecodeError):
            return CatalogMatch(None, None, "PRODUCT_CATALOG_UNAVAILABLE")
        catalog: dict[str, Any] = loaded["catalog"]
        aliases: dict[str, Any] = loaded["aliases"]
        normalized_catalog: dict[str, str] = loaded["normalized_catalog"]
        explicit = normalize_model(model_code)
        if explicit in normalized_catalog:
            key = normalized_catalog[explicit]
            return CatalogMatch(key, dict(catalog[key]))

        haystack = normalize_model(f"{product_name} {option_name}")
        # A model code must be materially specific.  This avoids matching a
        # bare size or a product family token to one arbitrary variant.
        candidates = {
            key for normalized, key in normalized_catalog.items()
            if len(normalized) >= MIN_IDENTIFYING_LENGTH and normalized in haystack
        }
        if not candidates:
            for alias, target in aliases.items():
                normalized = normalize_model(alias)
                if len(normalized) < MIN_IDENTIFYING_LENGTH or normalized not in haystack:
                    continue
                target_key = str(target)
                if target_key in catalog:
                    candidates.add(target_key)
        if len(candidates) == 1:
            key = next(iter(candidates))
            return CatalogMatch(key, dict(catalog[key]), status=EXACT)
        if candidates:
            return self._ambiguous(catalog, candidates)

        # Nothing in the listing spells a catalog key out in full. Many titles
        # carry a truncated one -- "LH43B" for LH43BEDHLGFXKR -- and the whole
        # store's 43-inch business TVs share that stem, so this narrows and
        # then reports rather than choosing.
        partial = self._partial_key_candidates(normalized_catalog, haystack)
        if len(partial) == 1:
            key = next(iter(partial))
            return CatalogMatch(key, dict(catalog[key]), status=UNIQUE_MATCH)
        if partial:
            narrowed = self._narrow_by_size(catalog, partial, product_name, option_name)
            if len(narrowed) == 1:
                key = next(iter(narrowed))
                return CatalogMatch(key, dict(catalog[key]), status=UNIQUE_MATCH)
            return self._ambiguous(catalog, narrowed or partial)
        return CatalogMatch(
            None, None, "PRODUCT_CATALOG_MODEL_NOT_FOUND", status=NOT_FOUND,
        )

    @staticmethod
    def _ambiguous(
        catalog: dict[str, Any], keys: set[str]
    ) -> CatalogMatch:
        """Report every model the listing could mean, and elect none."""

        ordered = sorted(keys)
        return CatalogMatch(
            None, None, "PRODUCT_CATALOG_AMBIGUOUS", status=AMBIGUOUS,
            candidates=tuple(
                (key, dict(catalog[key])) for key in ordered if key in catalog
            ),
        )

    @staticmethod
    def _model_tokens(haystack: str) -> set[str]:
        """Model-code-shaped tokens in a normalised listing title.

        Structural only: a run of letters and digits containing at least one of
        each. Measurements are dropped because "107.9CM" and "300HZ" satisfy
        that shape and identify nothing.
        """

        found = set()
        for match in _MODEL_TOKEN.finditer(haystack):
            token = match.group()
            if len(token) < MIN_IDENTIFYING_LENGTH:
                continue
            if token.endswith(_MEASUREMENT_SUFFIX):
                continue
            found.add(token)
        return found

    @classmethod
    def _partial_key_candidates(
        cls, normalized_catalog: dict[str, str], haystack: str
    ) -> set[str]:
        """Catalog keys a truncated model code in the listing could name.

        A key qualifies when one of the listing's model-shaped tokens is a
        prefix of it. Prefix, not substring: "BE85F" naming BE85FLGF... is a
        title that shortened the code, whereas an arbitrary interior match
        would let one model's digits vouch for another's.
        """

        tokens = cls._model_tokens(haystack)
        if not tokens:
            return set()
        return {
            key
            for normalized, key in normalized_catalog.items()
            if len(normalized) >= MIN_IDENTIFYING_LENGTH
            and any(normalized.startswith(token) for token in tokens)
        }

    @staticmethod
    def _narrow_by_size(
        catalog: dict[str, Any], keys: set[str],
        product_name: object, option_name: object,
    ) -> set[str]:
        """Keep only the candidates whose catalogued size the listing states.

        The listing writes the size and so does the catalog, so this compares
        two recorded values rather than guessing. It can only ever shrink the
        candidate set, and a size the catalog does not record leaves it alone.
        """

        text = f"{product_name} {option_name}"
        stated = {
            int(value)
            for value in re.findall(r"(\d{2})\s*인치", text)
        }
        if not stated:
            return set()
        narrowed = set()
        for key in keys:
            record = catalog.get(key) or {}
            sizes = {
                int(value)
                for value in re.findall(r"(\d{2})", str(record.get("size_inch") or ""))
            }
            if sizes & stated:
                narrowed.add(key)
        return narrowed
