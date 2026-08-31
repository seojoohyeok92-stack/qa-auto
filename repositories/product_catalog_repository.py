"""Read-only runtime access to the operator-maintained product catalog.

The catalog deliberately performs only exact, normalised model matching.  A
catalog miss (or more than one plausible model) is an UNKNOWN, never an
invitation to borrow a neighbouring model's specification.
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


@dataclass(frozen=True)
class CatalogMatch:
    model_key: str | None
    record: dict[str, Any] | None
    reason: str | None = None


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
            if len(normalized) >= 5 and normalized in haystack
        }
        if not candidates:
            for alias, target in aliases.items():
                normalized = normalize_model(alias)
                if len(normalized) < 5 or normalized not in haystack:
                    continue
                target_key = str(target)
                if target_key in catalog:
                    candidates.add(target_key)
        if len(candidates) != 1:
            return CatalogMatch(
                None, None,
                "PRODUCT_CATALOG_AMBIGUOUS" if candidates else "PRODUCT_CATALOG_MODEL_NOT_FOUND",
            )
        key = next(iter(candidates))
        return CatalogMatch(key, dict(catalog[key]))
