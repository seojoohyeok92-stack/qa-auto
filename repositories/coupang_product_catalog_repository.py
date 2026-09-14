"""Persistence for Coupang registered products and their option hierarchy."""
from __future__ import annotations
import json
from typing import Any
from repositories.database import Database

class CoupangProductCatalogRepository:
    def __init__(self, database: Database) -> None: self.database = database
    @staticmethod
    def _text(value: object | None) -> str | None:
        value = str(value or "").strip()
        return value or None
    def upsert_product(self, *, account_code: object, data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        account=self._text(account_code); seller=self._text(data.get("sellerProductId"))
        if not account or not seller: raise ValueError("account_code and sellerProductId are required")
        values=(account,seller,self._text(data.get("productId")),self._text(data.get("sellerProductName")),self._text(data.get("displayProductName")),self._text(data.get("generalProductName")),self._text(data.get("statusName")),self._text(data.get("status")))
        with self.database.transaction() as c:
            existed=c.execute("SELECT 1 FROM coupang_catalog_products WHERE account_code=? AND seller_product_id=?",(account,seller)).fetchone() is not None
            c.execute("""INSERT INTO coupang_catalog_products(account_code,seller_product_id,product_id,seller_product_name,display_product_name,general_product_name,status_name,raw_status) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(account_code,seller_product_id) DO UPDATE SET product_id=excluded.product_id,seller_product_name=excluded.seller_product_name,display_product_name=excluded.display_product_name,general_product_name=excluded.general_product_name,status_name=excluded.status_name,raw_status=excluded.raw_status,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')""",values)
            row=dict(c.execute("SELECT * FROM coupang_catalog_products WHERE account_code=? AND seller_product_id=?",(account,seller)).fetchone())
        return row, not existed
    def upsert_option(self, *, account_code: object, seller_product_id: object, item: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        account=self._text(account_code); seller=self._text(seller_product_id); vendor=self._text(item.get("vendorItemId"))
        if not account or not seller or not vendor: raise ValueError("account_code, sellerProductId and vendorItemId are required")
        values=(account,vendor,seller,self._text(item.get("sellerProductItemId")),self._text(item.get("itemName")),self._text(item.get("externalVendorSku")),self._text(item.get("modelNo")),json.dumps(item.get("attributes") or [],ensure_ascii=False,sort_keys=True),json.dumps(item.get("bundleInfo") or {},ensure_ascii=False,sort_keys=True))
        with self.database.transaction() as c:
            existed=c.execute("SELECT 1 FROM coupang_catalog_options WHERE account_code=? AND vendor_item_id=?",(account,vendor)).fetchone() is not None
            c.execute("""INSERT INTO coupang_catalog_options(account_code,vendor_item_id,seller_product_id,seller_product_item_id,item_name,external_vendor_sku,model_no,attributes_json,bundle_info_json) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(account_code,vendor_item_id) DO UPDATE SET seller_product_id=excluded.seller_product_id,seller_product_item_id=excluded.seller_product_item_id,item_name=excluded.item_name,external_vendor_sku=excluded.external_vendor_sku,model_no=excluded.model_no,attributes_json=excluded.attributes_json,bundle_info_json=excluded.bundle_info_json,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')""",values)
            row=dict(c.execute("SELECT * FROM coupang_catalog_options WHERE account_code=? AND vendor_item_id=?",(account,vendor)).fetchone())
        return row, not existed
