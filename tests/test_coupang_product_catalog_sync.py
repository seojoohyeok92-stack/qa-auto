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
