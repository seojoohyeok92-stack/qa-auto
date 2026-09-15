from __future__ import annotations

from repositories.product_catalog_repository import (
    ProductCatalogRepository,
    canonical_model_identity,
)
from services.coupang_product_mapping_service import CoupangProductMappingService
from ui.coupang_management import MANUAL_MODEL_CODES, _models


def test_coupang_manual_model_allowlist_is_exact_and_resolvable() -> None:
    expected = {
        "LS25BG400EKXKR", "LS25HG400EKXKR", "LS27HG400EKXKR",
        "LS32FG500EKXKR", "LS32DM500EKXKR", "LS32DM501EKXKR",
        "LS27FM500EKXKR", "LS27FM501EKXKR", "LS32FM500EKXKR",
        "LS32FM501EKXKR", "LS27DG700EKXKR", "LS27FG700EKXKR",
        "LS27HG806EFXKR", "LS32HG806ESXKR", "LS49CG954EKXKR",
        "LS49DG930SKXKR", "LS32DG300EKXKR", "LS22D400GAKXKR",
        "LS24D400GAKXKR", "LS27D400GAKXKR", "LH43BEHHLGFXKR",
        "LH50BEHHLGFXKR", "LH85BEHHLGFXKR",
    }
    assert set(MANUAL_MODEL_CODES) == expected
    assert len(MANUAL_MODEL_CODES) == 23

    catalog = ProductCatalogRepository()
    aliases = catalog.catalog()["aliases"]
    known = CoupangProductMappingService(
        account_code="TEST", read_client=None, repository=None,  # type: ignore[arg-type]
        catalog_repository=catalog,
    )._known_models()
    selected = _models()

    assert set(selected) == {
        canonical_model_identity(code, aliases=aliases)
        for code in MANUAL_MODEL_CODES
    }
    assert all(model in known for model in selected)
    assert {"16B1P", "16F50P", "16F90P", "16MR70", "17MT70", "20B300"}.isdisjoint(selected)

