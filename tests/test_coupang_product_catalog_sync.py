from __future__ import annotations
from pathlib import Path
from typing import Any
from repositories.database import Database
from repositories.coupang_product_catalog_repository import CoupangProductCatalogRepository
from repositories.coupang_product_mapping_repository import CoupangProductMappingRepository
from repositories.product_catalog_repository import ProductCatalogRepository
from services.coupang_product_mapping_service import CoupangProductMappingService
from services.coupang_product_catalog_sync_service import CoupangProductCatalogSyncService

class Client:
    def __init__(self): self.calls=[]
    def list_seller_products(self, *, next_token=None, **kwargs):
        self.calls.append(("list",next_token))
        return ({"data":[{"sellerProductId": "1", "sellerProductName":"M50D 32"}],"nextToken":"2"} if next_token is None else {"data":[{"sellerProductId":"1"},{"sellerProductId":"2"}],"nextToken":""})
    def get_seller_product(self, seller_product_id):
        self.calls.append(("get",str(seller_product_id)))
        models={"1":"LS25HG400EKXKR","2":"M50F 27"}
        model=models[str(seller_product_id)]
        return {"data":{"sellerProductId":str(seller_product_id),"productId":f"p{seller_product_id}","displayProductName":f"product {seller_product_id}","items":[{"vendorItemId":f"v{seller_product_id}","sellerProductItemId":f"i{seller_product_id}","itemName":model,"modelNo":model,"externalVendorSku":model,"attributes":[{"attributeValueName":model}],"bundleInfo":{} }]}}
    def get_vendor_item_inventory(self, vendor_item_id): return {"data":{"onSale":True}}

def test_catalog_sync_preserves_all_products_and_options(tmp_path: Path):
    db=Database(tmp_path/"catalog.db"); db.initialize()
    path=tmp_path/"model.json"; path.write_text('{"MODEL_CATALOG":{},"MODEL_ALIASES":{},"PRODUCT_KNOWLEDGE":{"model_facts":{}}}', encoding="utf-8")
    catalog=ProductCatalogRepository(path)
    # Empty catalog makes both model decisions review-only but must not exclude catalog rows.
    client=Client(); mappings=CoupangProductMappingRepository(db)
    mapping=CoupangProductMappingService(account_code="OJE_NS",read_client=client,repository=mappings,catalog_repository=catalog)
    service=CoupangProductCatalogSyncService(account_code="OJE_NS",read_client=client,catalog_repository=CoupangProductCatalogRepository(db),mapping_service=mapping)
    result=service.sync_account(max_per_page=2)
    assert (result.products_seen,result.options_seen,result.needs_review)==(2,2,2)
    assert [x for x in client.calls if x[0]=="list"] == [("list",None),("list","2")]
    with db.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM coupang_catalog_products").fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM coupang_catalog_options").fetchone()[0] == 2
    second=service.sync_account(max_per_page=2)
    assert (second.products_new,second.options_new)==(0,0)

def test_catalog_repository_isolates_same_vendor_item_by_account(tmp_path: Path):
    db=Database(tmp_path/"catalog.db"); db.initialize(); repo=CoupangProductCatalogRepository(db)
    item={"vendorItemId":"same","itemName":"x"}
    repo.upsert_option(account_code="OJE_NS",seller_product_id="one",item=item)
    repo.upsert_option(account_code="OJE_PLUS",seller_product_id="two",item=item)
    with db.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM coupang_catalog_options").fetchone()[0] == 2


class ApprovedClient:
    def __init__(self, product_ids: list[str]) -> None:
        self.product_ids = product_ids
        self.statuses: list[str | None] = []

    def list_seller_products(self, *, status=None, **kwargs):
        self.statuses.append(status)
        return {
            "data": [
                {
                    "sellerProductId": product_id,
                    "sellerProductName": f"product {product_id}",
                    "statusName": "승인완료",
                }
                for product_id in self.product_ids
            ],
            "nextToken": "",
        }

    def get_seller_product(self, seller_product_id):
        product_id = str(seller_product_id)
        return {
            "data": {
                "sellerProductId": product_id,
                "statusName": "승인완료",
                "items": [{
                    "vendorItemId": f"v-{product_id}",
                    "sellerProductItemId": f"i-{product_id}",
                    "itemName": "M50F 27",
                }],
            }
        }

    def get_vendor_item_inventory(self, vendor_item_id):
        return {"data": {"onSale": True}}


def _approved_sync_service(db: Database, client: ApprovedClient, account_code: str = "OJE_NS"):
    catalog = CoupangProductCatalogRepository(db)
    mappings = CoupangProductMappingRepository(db)
    mapping = CoupangProductMappingService(
        account_code=account_code, read_client=client, repository=mappings,
    )
    return CoupangProductCatalogSyncService(
        account_code=account_code, read_client=client,
        catalog_repository=catalog, mapping_service=mapping,
    )


def test_approved_sync_hides_products_absent_from_next_successful_sync(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "catalog.db")
    db.initialize()
    catalog = CoupangProductCatalogRepository(db)
    catalog.upsert_product(
        account_code="OJE_NS",
        data={"sellerProductId": "old", "sellerProductName": "old"},
    )
    CoupangProductMappingRepository(db).upsert(
        account_code="OJE_NS", vendor_item_id="old-option",
        canonical_model="32DM501", mapping_source="MANUAL",
        mapping_status="CONFIRMED",
    )

    client = ApprovedClient(["current-a", "current-b"])
    result = _approved_sync_service(db, client).sync_account()

    assert result.errors == []
    assert client.statuses == ["APPROVED"]
    active = catalog.grouped_products(account_code="OJE_NS")
    assert {row["seller_product_id"] for row in active} == {"current-a", "current-b"}
    with db.connection() as connection:
        old = connection.execute(
            "SELECT is_active FROM coupang_catalog_products "
            "WHERE account_code='OJE_NS' AND seller_product_id='old'"
        ).fetchone()
    assert old["is_active"] == 0
    assert CoupangProductMappingRepository(db).get(
        account_code="OJE_NS", vendor_item_id="old-option"
    )["canonical_model"] == "32DM501"


def test_grouped_products_never_mix_accounts(tmp_path: Path) -> None:
    db = Database(tmp_path / "catalog.db")
    db.initialize()
    catalog = CoupangProductCatalogRepository(db)
    for account, seller, vendor in (
        ("OJE_NS", "same-seller", "same-vendor"),
        ("OJE_PLUS", "same-seller", "same-vendor"),
    ):
        catalog.upsert_product(account_code=account, data={"sellerProductId": seller})
        catalog.upsert_option(
            account_code=account, seller_product_id=seller,
            item={"vendorItemId": vendor, "itemName": account},
        )
        catalog.set_product_active(
            account_code=account, seller_product_id=seller, is_active=True,
        )

    assert [row["account_code"] for row in catalog.grouped_products(account_code="OJE_NS")] == ["OJE_NS"]
    assert [row["account_code"] for row in catalog.grouped_products(account_code="OJE_PLUS")] == ["OJE_PLUS"]


class SaleStateClient(ApprovedClient):
    def __init__(self, states: dict[str, list[bool | Exception]]) -> None:
        super().__init__(list(states))
        self.states = states

    def get_seller_product(self, seller_product_id):
        product_id = str(seller_product_id)
        return {"data": {"sellerProductId": product_id, "items": [
            {"vendorItemId": f"{product_id}-{index}", "itemName": "option"}
            for index, _ in enumerate(self.states[product_id])
        ]}}

    def get_vendor_item_inventory(self, vendor_item_id):
        product_id, index = str(vendor_item_id).rsplit("-", 1)
        value = self.states[product_id][int(index)]
        if isinstance(value, Exception):
            raise value
        return {"data": {"onSale": value}}


def test_active_requires_any_option_on_sale_and_preserves_inactive_mapping(tmp_path: Path) -> None:
    db = Database(tmp_path / "catalog.db"); db.initialize()
    service = _approved_sync_service(db, SaleStateClient({"active": [False, True], "inactive": [False, False]}))
    assert service.sync_account().errors == []
    catalog = CoupangProductCatalogRepository(db)
    assert {row["seller_product_id"] for row in catalog.grouped_products(account_code="OJE_NS")} == {"active"}
    with db.connection() as c:
        states = dict(c.execute("SELECT seller_product_id, is_active FROM coupang_catalog_products"))
    assert states == {"active": 1, "inactive": 0}


def test_inventory_failure_does_not_change_existing_active_state(tmp_path: Path) -> None:
    db = Database(tmp_path / "catalog.db"); db.initialize(); catalog = CoupangProductCatalogRepository(db)
    catalog.upsert_product(account_code="OJE_NS", data={"sellerProductId": "safe"})
    catalog.set_product_active(account_code="OJE_NS", seller_product_id="safe", is_active=True)
    service = _approved_sync_service(db, SaleStateClient({"safe": [RuntimeError("network")]}))
    assert service.sync_account().errors
    with db.connection() as c:
        assert c.execute("SELECT is_active FROM coupang_catalog_products WHERE seller_product_id='safe'").fetchone()[0] == 1


def test_sale_state_sync_is_account_scoped(tmp_path: Path) -> None:
    db = Database(tmp_path / "catalog.db"); db.initialize()
    _approved_sync_service(db, SaleStateClient({"same": [True]}), "OJE_NS").sync_account()
    _approved_sync_service(db, SaleStateClient({"same": [False]}), "OJE_PLUS").sync_account()
    catalog = CoupangProductCatalogRepository(db)
    assert [row["account_code"] for row in catalog.grouped_products(account_code="OJE_NS")] == ["OJE_NS"]
    assert catalog.grouped_products(account_code="OJE_PLUS") == []
