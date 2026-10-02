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


def test_archive_is_not_an_implicit_runtime_catalog_source(
    tmp_path, monkeypatch
) -> None:
    """An archived catalog copy can never become the runtime source.

    The mechanism is that there is no mechanism.  ``DEFAULT_PRODUCT_CATALOG_PATH``
    is derived from this package's own location -- ``Path(__file__).resolve()
    .parents[1] / "data" / "model_data_with_color.json"`` -- and the constructor
    is ``Path(path or DEFAULT).resolve()``.  No search, no glob, no directory
    scan, no environment override.  A catalog file anywhere else is therefore
    not a candidate, and the archived copy is reachable only by naming it.

    This was previously checked by asserting that a file under ``archive/``
    existed on the developer's machine, so the test passed only where that
    untracked folder happened to be present and said nothing on a clean clone.
    The archive-like candidate is now built here instead, so the claim is
    exercised rather than assumed -- and the process runs with its working
    directory *inside* that tree, which is what would expose a cwd-relative
    resolution.
    """

    archived = (
        tmp_path / "archive" / "data_cleanup_20260913" / "legacy_catalog"
        / "model_data_with_color.json"
    )
    archived.parent.mkdir(parents=True)
    archived.write_text(
        json.dumps(
            {
                "MODEL_CATALOG": {"ARCHIVED-ONLY": {"model": "ARCHIVED-ONLY"}},
                "MODEL_ALIASES": {},
                "PRODUCT_KNOWLEDGE": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    repository = ProductCatalogRepository()
    assert repository.path == DEFAULT_PRODUCT_CATALOG_PATH
    assert repository.path != archived
    assert "archive" not in str(repository.path).lower()
    # The archived file's own content never reaches the runtime catalog, even
    # with the process sitting right on top of it.  ``catalog()`` renames the
    # JSON's MODEL_CATALOG to "catalog" as it loads.
    assert "ARCHIVED-ONLY" not in repository.catalog()["catalog"]

    # And the protection is not a blocklist: an explicitly named archive path
    # is honoured. Nothing in production ever supplies one, which is the whole
    # of the guarantee.
    assert ProductCatalogRepository(archived).path == archived
