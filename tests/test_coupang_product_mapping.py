from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from config import (
    COUPANG_OJE_NS,
    COUPANG_OJE_PLUS,
    get_coupang_account,
    get_coupang_accounts,
)
from repositories.coupang_product_mapping_repository import (
    AUTO_EXACT,
    CONFIRMED,
    MANUAL,
    NEEDS_REVIEW,
    CoupangProductMappingRepository,
)
from repositories.database import Database
from repositories.product_catalog_repository import ProductCatalogRepository
from services.coupang_product_mapping_service import CoupangProductMappingService


class FakeProductClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls = 0

    def get_seller_product(self, seller_product_id: object) -> dict[str, Any]:
        self.calls += 1
        return self.payload


def product(*items: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": "SUCCESS",
        "data": {
            "sellerProductId": 10001,
            "productId": 20001,
            "items": list(items),
        },
    }


def item(vendor_item_id: int, *, model_no: str = "", sku: str = "", attributes: list[dict[str, str]] | None = None, bundle: object = None) -> dict[str, Any]:
    result = {
        "vendorItemId": vendor_item_id,
        "sellerProductItemId": vendor_item_id + 100,
        "itemName": "판매 옵션",
        "modelNo": model_no,
        "externalVendorSku": sku,
        "attributes": attributes or [],
    }
    if bundle is not None:
        result["bundleInfo"] = bundle
    return result


def catalog(tmp_path: Path) -> ProductCatalogRepository:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({
        "MODEL_CATALOG": {
            "S25HG400": {"model": "S25HG400"},
            "S32DM501": {"model": "S32DM501"},
            "S32DM500": {"model": "S32DM500"},
            "S22D400": {"model": "S22D400"},
            "S24D400": {"model": "S24D400"},
        },
        "MODEL_ALIASES": {
            "LS25HG400EKXKR": "S25HG400",
            "LS32DM501EKXKR": "S32DM501",
            "LS32DM500EKXKR": "S32DM500",
            "LS22D400GAKXKR": "S22D400",
            "LS24D400GAKXKR": "S24D400",
        },
        "PRODUCT_KNOWLEDGE": {"model_facts": []},
    }), encoding="utf-8")
    return ProductCatalogRepository(path)


def service(tmp_path: Path, client: FakeProductClient, account: str = COUPANG_OJE_NS) -> tuple[CoupangProductMappingService, CoupangProductMappingRepository]:
    database = Database(tmp_path / "mapping.db")
    database.initialize()
    repository = CoupangProductMappingRepository(database)
    return (
        CoupangProductMappingService(
            account_code=account,
            read_client=client,  # type: ignore[arg-type]
            repository=repository,
            catalog_repository=catalog(tmp_path),
        ),
        repository,
    )


def test_multi_account_configuration_preserves_existing_env_and_rejects_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("COUPANG_ACCESS_KEY", "COUPANG_SECRET_KEY", "COUPANG_VENDOR_ID", "COUPANG_2_ACCESS_KEY", "COUPANG_2_SECRET_KEY", "COUPANG_2_VENDOR_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COUPANG_ACCESS_KEY", "ns-access")
    monkeypatch.setenv("COUPANG_SECRET_KEY", "ns-secret")
    monkeypatch.setenv("COUPANG_VENDOR_ID", "A00000001")
    assert [account.account_code for account in get_coupang_accounts()] == [COUPANG_OJE_NS]
    monkeypatch.setenv("COUPANG_2_ACCESS_KEY", "plus-access")
    monkeypatch.setenv("COUPANG_2_SECRET_KEY", "plus-secret")
    monkeypatch.setenv("COUPANG_2_VENDOR_ID", "A00000002")
    assert [account.account_code for account in get_coupang_accounts()] == [COUPANG_OJE_NS, COUPANG_OJE_PLUS]
    monkeypatch.delenv("COUPANG_2_SECRET_KEY")
    assert [account.account_code for account in get_coupang_accounts()] == [COUPANG_OJE_NS]
    with pytest.raises(ValueError, match="COUPANG_2_SECRET_KEY"):
        get_coupang_account(COUPANG_OJE_PLUS)


def test_auto_exact_unique_vendor_item_and_existing_mapping_reuse(tmp_path: Path) -> None:
    client = FakeProductClient(product(
        item(1, model_no="LS32DM500EKXKR"),
        item(2, model_no="LS25HG400EKXKR", sku="25HG400", attributes=[{"attributeValueName": "S25HG400"}]),
        item(3, model_no="LS32DM501EKXKR"),
        item(4, model_no="LS24D400GAKXKR"),
    ))
    mapping_service, repository = service(tmp_path, client)
    result = mapping_service.resolve(seller_product_id=10001, vendor_item_id=2)
    assert result.mapping["mapping_source"] == AUTO_EXACT
    assert result.mapping["mapping_status"] == CONFIRMED
    assert result.mapping["canonical_model"] == "25HG400"
    assert result.matching_item_count == 1
    assert client.calls == 1
    reused = mapping_service.resolve(seller_product_id=10001, vendor_item_id=2)
    assert reused.reused is True
    assert reused.mapping["canonical_model"] == "25HG400"
    assert client.calls == 1
    assert repository.get_confirmed(account_code=COUPANG_OJE_NS, vendor_item_id=2) is not None


@pytest.mark.parametrize("items, vendor_item_id, reason", [
    ([], 1, "VENDOR_ITEM_NOT_UNIQUE"),
    ([item(1, model_no="LS32DM500EKXKR"), item(1, model_no="LS32DM501EKXKR")], 1, "VENDOR_ITEM_NOT_UNIQUE"),
    ([item(1, model_no="32DM500", sku="32DM501")], 1, "MODEL_EVIDENCE_CONFLICT"),
    ([item(1, model_no="", sku="internal-sku", attributes=[{"attributeValueName": "M50D 32\""}])], 1, "MODEL_EVIDENCE_NOT_EXACT"),
])
def test_ambiguous_or_conflicting_evidence_stays_review(tmp_path: Path, items: list[dict[str, Any]], vendor_item_id: int, reason: str) -> None:
    mapping_service, _ = service(tmp_path, FakeProductClient(product(*items)))
    result = mapping_service.resolve(seller_product_id=10001, vendor_item_id=vendor_item_id)
    assert result.reason == reason
    assert result.mapping["mapping_status"] == NEEDS_REVIEW
    assert result.mapping["canonical_model"] is None
    assert result.mapping["mapping_source"] is None


def test_d400_options_are_independent_and_account_scoped(tmp_path: Path) -> None:
    client = FakeProductClient(product(
        item(12345, model_no="LS22D400GAKXKR"),
        item(12346, model_no="LS24D400GAKXKR"),
    ))
    ns, repository = service(tmp_path, client, COUPANG_OJE_NS)
    first = ns.resolve(seller_product_id=10001, vendor_item_id=12345)
    second = ns.resolve(seller_product_id=10001, vendor_item_id=12346)
    assert first.mapping["canonical_model"] == "22D400"
    assert second.mapping["canonical_model"] == "24D400"
    plus = CoupangProductMappingService(
        account_code=COUPANG_OJE_PLUS, read_client=client,  # type: ignore[arg-type]
        repository=repository, catalog_repository=catalog(tmp_path),
    )
    manual = plus.save_manual_mapping(vendor_item_id=12345, canonical_model="32DM501")
    assert manual["mapping_source"] == MANUAL
    assert repository.get(account_code=COUPANG_OJE_NS, vendor_item_id=12345)["canonical_model"] == "22D400"
    assert repository.get(account_code=COUPANG_OJE_PLUS, vendor_item_id=12345)["canonical_model"] == "32DM501"


def test_bundle_item_keeps_only_base_model_mapping(tmp_path: Path) -> None:
    mapping_service, _ = service(tmp_path, FakeProductClient(product(
        item(7, model_no="LS32DM501EKXKR", bundle={"bundleType": "AB"}),
    )))
    result = mapping_service.resolve(seller_product_id=10001, vendor_item_id=7)
    assert result.mapping["canonical_model"] == "32DM501"
    assert result.bundle_detected is True
