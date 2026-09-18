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
from typing import Any, Mapping


DEFAULT_PRODUCT_CATALOG_PATH = (
    Path(__file__).resolve().parents[1]
    / "data" / "model_data_with_color.json"
)


def normalize_model(value: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


_MODEL_CODE_TEXT = re.compile(r"^[A-Za-z0-9-]+$")
_SAMSUNG_DISPLAY_CORE = re.compile(r"^\d{2}[A-Z]+\d[A-Z0-9]*$")
# The Korean region suffix, whose last four characters vary by panel line:
# EKXKR, GAKXKR and SKXKR alongside EFXKR and ESXKR.  Anchoring on KXKR read
# the first three as a suffix and the last two as part of the model, so
# LS32HG806ESXKR and 32HG806 were two products -- and the full name could not
# reach its own catalogue record, S32HG806.  The bounded prefix is unchanged,
# so a longer regional tail (WBGCXKR, EBGCXKR) is still not a suffix here.
_KOREAN_REGION_SUFFIX = re.compile(r"[A-Z]{0,3}XKR$")


def _model_code_aliases(aliases: Mapping[object, object] | None) -> dict[str, str]:
    """Return only explicit aliases that are themselves model-code-shaped.

    ``MODEL_ALIASES`` also intentionally contains operator-maintained listing
    titles such as colour/size descriptions.  Those remain catalog matching
    hints; they must never become model identities.
    """

    result: dict[str, str] = {}
    for alias, target in (aliases or {}).items():
        alias_text = str(alias or "").strip()
        target_text = str(target or "").strip()
        if not (_MODEL_CODE_TEXT.fullmatch(alias_text) and _MODEL_CODE_TEXT.fullmatch(target_text)):
            continue
        normalized_alias = normalize_model(alias_text)
        normalized_target = normalize_model(target_text)
        if (
            len(normalized_alias) >= MIN_IDENTIFYING_LENGTH
            and _MODEL_TOKEN.fullmatch(normalized_alias)
            and normalized_target
        ):
            result[normalized_alias] = normalized_target
    return result


def canonical_model_identity(
    raw_model: object,
    *,
    aliases: Mapping[object, object] | None = None,
) -> str | None:
    """Return a safe canonical Samsung display-model identity, if stated.

    This is a notation normalizer, not a product classifier.  It accepts only
    a single model-code token; bundle keys, listing titles, family names and
    option text return ``None``.  For the Samsung display codes present in the
    catalog, ``LS32DM501EKXKR``, ``S32DM501``, ``LS32DM501`` and
    ``32DM501EKXKR`` therefore all become ``32DM501``.  Screen size and the
    complete core remain part of the identity, so DM500/DM501 and 22D400/24D400
    cannot collapse.

    Explicit model-code aliases still participate first.  Manual aliases that
    are listing descriptions deliberately do not: resolving ``M50D 32`` to a
    colour variant would be an unsupported product inference.
    """

    text = str(raw_model or "").strip()
    if not text or not _MODEL_CODE_TEXT.fullmatch(text):
        return None
    normalized = normalize_model(text)
    if not normalized or not _MODEL_TOKEN.fullmatch(normalized):
        return None

    mapped = _model_code_aliases(aliases).get(normalized, normalized)
    # Samsung display codes in this catalog either state LS before the core,
    # state S before it, or state the core directly.  Other Samsung families
    # (LH/KQ/etc.) retain their recorded code unless an explicit alias maps
    # them; the display rule must not guess their internal structure.
    candidate = mapped
    if candidate.startswith("LS"):
        candidate = candidate[2:]
    elif candidate.startswith("S") and len(candidate) > 1 and candidate[1].isdigit():
        candidate = candidate[1:]
    candidate = _KOREAN_REGION_SUFFIX.sub("", candidate)
    if _SAMSUNG_DISPLAY_CORE.fullmatch(candidate):
        return candidate
    return mapped


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

# Where one word ends and the next begins, before normalisation removes the
# evidence. ``normalize_model`` strips every separator, so running the token
# scan over an already-normalised title collapsed the whole thing into a single
# run: "삼성 오디세이 G5 LS32FG500 80.1cm(32인치) … 180Hz" became
# "G5LS32FG500801CM32180HZ", one token, ending in HZ and therefore discarded as
# a measurement. The model code was in the title and the scan could not see it.
_WORD_SPLIT = re.compile(r"[^0-9A-Za-z]+")

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
        # Optional, integrated evidence.  Keeping it on the same cached JSON
        # object preserves the existing catalog source and does not create a
        # second runtime store.
        "product_knowledge": (
            value.get("PRODUCT_KNOWLEDGE")
            if isinstance(value.get("PRODUCT_KNOWLEDGE"), dict) else {}
        ),
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

    def product_knowledge(self) -> dict[str, Any]:
        """Integrated Product Knowledge section, or an empty mapping.

        The legacy catalog-only file remains a valid input, so this is an
        additive read rather than a new required top-level schema contract.
        """
        return dict(self.catalog().get("product_knowledge") or {})

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
        if len(candidates) == 1:
            key = next(iter(candidates))
            return CatalogMatch(key, dict(catalog[key]), status=EXACT)
        if candidates:
            return self._ambiguous(catalog, candidates)

        # What the title itself says about the model outranks an alias.
        #
        # Aliases are matched as text, and 34 of the 72 in this catalogue carry
        # no model code at all -- "삼성 107.9cm(43인치)" -> LH43BEDH. Trying
        # them before reading the title's own model token let a size phrase
        # elect a model while the title named a different one: measured over
        # the server's inquiries, 21 listings whose title said LH43BEF were
        # matched to LH43BEDH, and 96 more had an alias pick one of several
        # models the title's token narrowed to. A truncated code -- "LH43B" for
        # LH43BEDHLGFXKR -- narrows and then reports rather than choosing.
        partial = self._partial_key_candidates(
            normalized_catalog, f"{product_name} {option_name}",
        )
        if len(partial) == 1:
            key = next(iter(partial))
            return CatalogMatch(key, dict(catalog[key]), status=UNIQUE_MATCH)
        if partial:
            narrowed = self._narrow_by_size(catalog, partial, product_name, option_name)
            if len(narrowed) == 1:
                key = next(iter(narrowed))
                return CatalogMatch(key, dict(catalog[key]), status=UNIQUE_MATCH)
            return self._ambiguous(catalog, narrowed or partial)

        # Only now, with nothing in the title naming a model, may an alias
        # speak for the listing.
        alias_keys = set()
        for alias, target in aliases.items():
            normalized = normalize_model(alias)
            if len(normalized) < MIN_IDENTIFYING_LENGTH or normalized not in haystack:
                continue
            target_key = str(target)
            if target_key in catalog:
                alias_keys.add(target_key)
        if len(alias_keys) == 1:
            key = next(iter(alias_keys))
            return CatalogMatch(key, dict(catalog[key]), status=EXACT)
        if alias_keys:
            return self._ambiguous(catalog, alias_keys)

        # A bare Samsung display core (``32DM501``) is neither a catalog key
        # nor a listing-title alias, but it is an exact notation of a model
        # whose recorded keys may be ``S32DM501`` and/or
        # ``LS32DM501EKXKR``.  This fallback only accepts the shared
        # model-code normalizer's safe token form; it does not inspect a
        # marketing title or elect a family/size collection.
        canonical = canonical_model_identity(model_code, aliases=aliases)
        if canonical:
            canonical_keys = {
                key for key in catalog
                if canonical_model_identity(key, aliases=aliases) == canonical
            }
            if canonical_keys:
                # The explicit alias target, where present, is the existing
                # operator-maintained representative.  Otherwise a catalog
                # record whose own ``model`` field names one candidate is a
                # deterministic representative.  Multiple remaining records
                # stay AMBIGUOUS rather than being selected by key ordering.
                targets = {
                    str(target) for target in aliases.values()
                    if str(target) in canonical_keys
                    and canonical_model_identity(target, aliases=aliases) == canonical
                }
                if len(targets) == 1:
                    key = next(iter(targets))
                    return CatalogMatch(key, dict(catalog[key]), status=UNIQUE_MATCH)
                representatives = {
                    key for key in canonical_keys
                    if normalize_model((catalog.get(key) or {}).get("model"))
                    == normalize_model(key)
                }
                if len(representatives) == 1:
                    key = next(iter(representatives))
                    return CatalogMatch(key, dict(catalog[key]), status=UNIQUE_MATCH)
                if len(canonical_keys) == 1:
                    key = next(iter(canonical_keys))
                    return CatalogMatch(key, dict(catalog[key]), status=UNIQUE_MATCH)
                return self._ambiguous(catalog, canonical_keys)
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
    def _model_tokens(text: object) -> set[str]:
        """Model-code-shaped words in a listing title.

        Structural only: a word of letters and digits containing at least one of
        each. Measurements are dropped because "107.9CM" and "300HZ" satisfy
        that shape and identify nothing.

        Split on the original text, not the normalised one. Normalisation
        removes the separators, and the scan then sees one run spanning the
        whole title -- which is both too long to be a model code and liable to
        end in a unit from the last word, so every title failed.
        """

        found = set()
        for word in _WORD_SPLIT.split(str(text or "")):
            token = normalize_model(word)
            if len(token) < MIN_IDENTIFYING_LENGTH:
                continue
            if token.endswith(_MEASUREMENT_SUFFIX):
                continue
            if _MODEL_TOKEN.fullmatch(token):
                found.add(token)
        return found

    @classmethod
    def _partial_key_candidates(
        cls, normalized_catalog: dict[str, str], text: object
    ) -> set[str]:
        """Catalog keys a truncated model code in the listing could name.

        A key qualifies when one of the listing's model-shaped tokens is a
        prefix of it. Prefix, not substring: "BE85F" naming BE85FLGF... is a
        title that shortened the code, whereas an arbitrary interior match
        would let one model's digits vouch for another's.
        """

        tokens = cls._model_tokens(text)
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
