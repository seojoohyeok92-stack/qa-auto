from __future__ import annotations
from pathlib import Path
from typing import Any
from config import (
    COUPANG_OJE_NS,
    COUPANG_OJE_PLUS,
    OJE_PLUS_TOP_SELLER_PRODUCT_IDS,
    get_coupang_catalog_sync_scope,
)
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


class TopScopeClient:
    def __init__(self, product_ids: tuple[str, ...], states: dict[str, list[bool | Exception]]) -> None:
        self.product_ids = product_ids
        self.states = states
        self.list_calls = 0
        self.product_calls: list[str] = []
        self.inventory_calls: list[str] = []

    def list_seller_products(self, **kwargs):
        self.list_calls += 1
        raise AssertionError("direct OJE_PLUS scope must not list all seller products")

    def get_seller_product(self, seller_product_id):
        product_id = str(seller_product_id)
        self.product_calls.append(product_id)
        if product_id not in self.product_ids:
            raise AssertionError(f"out-of-scope Product GET: {product_id}")
        return {"data": {"sellerProductId": product_id, "productId": f"p-{product_id}", "status": "APPROVED", "items": [
            {
                "vendorItemId": f"{product_id}-{index}",
                "sellerProductItemId": f"item-{product_id}-{index}",
                "itemName": "option",
                "modelNo": "LS25HG400EKXKR",
            }
            for index, _ in enumerate(self.states[product_id])
        ]}}

    def get_vendor_item_inventory(self, vendor_item_id):
        self.inventory_calls.append(str(vendor_item_id))
        product_id, index = str(vendor_item_id).rsplit("-", 1)
        result = self.states[product_id][int(index)]
        if isinstance(result, Exception):
            raise result
        return {"data": {"onSale": result}}


def _top_scope_service(db: Database, client: TopScopeClient, *, account_code: str = COUPANG_OJE_PLUS,
                       scope: tuple[str, ...] = OJE_PLUS_TOP_SELLER_PRODUCT_IDS):
    catalog = CoupangProductCatalogRepository(db)
    mappings = CoupangProductMappingRepository(db)
    mapping = CoupangProductMappingService(
        account_code=account_code, read_client=client, repository=mappings,
    )
    return CoupangProductCatalogSyncService(
        account_code=account_code, read_client=client, catalog_repository=catalog,
        mapping_service=mapping, seller_product_ids=scope,
    )


def test_oje_plus_top20_direct_scope_reuses_detail_and_reports_progress(tmp_path: Path) -> None:
    db = Database(tmp_path / "catalog.db"); db.initialize()
    states = {
        product_id: ([True, False] if index == 0 else [False] if index == 1 else [True])
        for index, product_id in enumerate(OJE_PLUS_TOP_SELLER_PRODUCT_IDS)
    }
    client = TopScopeClient(OJE_PLUS_TOP_SELLER_PRODUCT_IDS, states)
    mappings = CoupangProductMappingRepository(db)
    mappings.upsert(
        account_code=COUPANG_OJE_PLUS,
        vendor_item_id=f"{OJE_PLUS_TOP_SELLER_PRODUCT_IDS[0]}-0",
        canonical_model="25HG400", mapping_source="MANUAL", mapping_status="CONFIRMED",
    )
    progress: list[tuple[int, int | None]] = []
    result = _top_scope_service(db, client).sync_account(
        progress_callback=lambda completed, total: progress.append((completed, total))
    )

    assert result.errors == []
    assert client.list_calls == 0
    assert client.product_calls == list(OJE_PLUS_TOP_SELLER_PRODUCT_IDS)
    assert len(client.product_calls) == 20  # no mapping-triggered duplicate Product GET
    assert len(client.inventory_calls) == sum(len(value) for value in states.values())
    assert result.confirmed_reused == 1
    assert progress == [(index, 20) for index in range(1, 21)]
    with db.connection() as connection:
        active = dict(connection.execute(
            "SELECT seller_product_id, is_active FROM coupang_catalog_products WHERE account_code=?",
            (COUPANG_OJE_PLUS,),
        ))
    assert active[OJE_PLUS_TOP_SELLER_PRODUCT_IDS[0]] == 1
    assert active[OJE_PLUS_TOP_SELLER_PRODUCT_IDS[1]] == 0


def test_oje_ns_keeps_product_list_sync_and_has_no_top20_scope(tmp_path: Path) -> None:
    db = Database(tmp_path / "catalog.db"); db.initialize()
    client = ApprovedClient(["ns-a", "ns-b"])
    assert get_coupang_catalog_sync_scope(COUPANG_OJE_NS) is None
    assert get_coupang_catalog_sync_scope(COUPANG_OJE_PLUS) == OJE_PLUS_TOP_SELLER_PRODUCT_IDS
    assert _approved_sync_service(db, client, COUPANG_OJE_NS).sync_account().errors == []
    assert client.statuses == ["APPROVED"]


def test_oje_plus_scope_hides_stale_products_and_mapping_filters_match_options(tmp_path: Path) -> None:
    db = Database(tmp_path / "catalog.db"); db.initialize()
    catalog = CoupangProductCatalogRepository(db)
    mappings = CoupangProductMappingRepository(db)
    selected = OJE_PLUS_TOP_SELLER_PRODUCT_IDS[0]
    for seller, vendor in ((selected, "complete"), (selected, "review"), ("outside", "outside")):
        catalog.upsert_product(account_code=COUPANG_OJE_PLUS, data={"sellerProductId": seller})
        catalog.upsert_option(account_code=COUPANG_OJE_PLUS, seller_product_id=seller, item={"vendorItemId": vendor})
        catalog.set_product_active(account_code=COUPANG_OJE_PLUS, seller_product_id=seller, is_active=True)
    mappings.upsert(account_code=COUPANG_OJE_PLUS, vendor_item_id="complete", canonical_model="25HG400", mapping_source="AUTO_EXACT", mapping_status="CONFIRMED")
    mappings.upsert(account_code=COUPANG_OJE_PLUS, vendor_item_id="review", mapping_status="NEEDS_REVIEW")
    mappings.upsert(account_code=COUPANG_OJE_PLUS, vendor_item_id="outside", mapping_status="NEEDS_REVIEW")

    active = catalog.grouped_products(account_code=COUPANG_OJE_PLUS, seller_product_ids=OJE_PLUS_TOP_SELLER_PRODUCT_IDS)
    assert [row["seller_product_id"] for row in active] == [selected]
    complete = catalog.grouped_products(account_code=COUPANG_OJE_PLUS, status="COMPLETE", seller_product_ids=OJE_PLUS_TOP_SELLER_PRODUCT_IDS)
    review = catalog.grouped_products(account_code=COUPANG_OJE_PLUS, status="REVIEW", seller_product_ids=OJE_PLUS_TOP_SELLER_PRODUCT_IDS)
    assert [option["vendor_item_id"] for option in complete[0]["options"]] == ["complete"]
    assert [option["vendor_item_id"] for option in review[0]["options"]] == ["review"]


def test_oje_plus_scoped_partial_inventory_failure_preserves_existing_active_state(tmp_path: Path) -> None:
    db = Database(tmp_path / "catalog.db"); db.initialize()
    seller_id = OJE_PLUS_TOP_SELLER_PRODUCT_IDS[0]
    catalog = CoupangProductCatalogRepository(db)
    catalog.upsert_product(account_code=COUPANG_OJE_PLUS, data={"sellerProductId": seller_id})
    catalog.set_product_active(account_code=COUPANG_OJE_PLUS, seller_product_id=seller_id, is_active=True)
    client = TopScopeClient((seller_id,), {seller_id: [RuntimeError("network")]})
    result = _top_scope_service(db, client, scope=(seller_id,)).sync_account()
    assert result.errors
    with db.connection() as connection:
        assert connection.execute(
            "SELECT is_active FROM coupang_catalog_products WHERE account_code=? AND seller_product_id=?",
            (COUPANG_OJE_PLUS, seller_id),
        ).fetchone()[0] == 1
