"""Filesystem cutover contracts for the single Product Knowledge JSON source."""
from __future__ import annotations

import json
from pathlib import Path

from repositories.product_catalog_repository import (
    DEFAULT_PRODUCT_CATALOG_PATH,
    ProductCatalogRepository,
)


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
ARCHIVE = ROOT / "archive" / "data_cleanup_20260913"


def test_default_catalog_is_the_final_single_filename() -> None:
    assert DEFAULT_PRODUCT_CATALOG_PATH == DATA / "model_data_with_color.json"
    assert DEFAULT_PRODUCT_CATALOG_PATH.is_file()
    assert not (DATA / "model_data_with_color_product_knowledge_final.json").exists()


def test_cutover_catalog_keeps_catalog_and_product_knowledge_sections() -> None:
    value = json.loads(DEFAULT_PRODUCT_CATALOG_PATH.read_text(encoding="utf-8"))
    assert isinstance(value["MODEL_CATALOG"], dict)
    assert isinstance(value["MODEL_ALIASES"], dict)
    assert isinstance(value["PRODUCT_KNOWLEDGE"], dict)
    assert ProductCatalogRepository().product_knowledge() == value["PRODUCT_KNOWLEDGE"]


def test_archive_is_not_an_implicit_runtime_catalog_source() -> None:
    archived_old = ARCHIVE / "legacy_catalog" / "model_data_with_color.json"
    assert archived_old.is_file()
    assert ProductCatalogRepository().path != archived_old
    assert "archive" not in str(ProductCatalogRepository().path).lower()
