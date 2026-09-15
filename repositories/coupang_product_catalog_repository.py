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
    def upsert_product(
        self,
        *,
        account_code: object,
        data: dict[str, Any],
        sync_token: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        account=self._text(account_code); seller=self._text(data.get("sellerProductId"))
        if not account or not seller: raise ValueError("account_code and sellerProductId are required")
        values=(account,seller,self._text(data.get("productId")),self._text(data.get("sellerProductName")),self._text(data.get("displayProductName")),self._text(data.get("generalProductName")),self._text(data.get("statusName")),self._text(data.get("status")),self._text(sync_token))
        with self.database.transaction() as c:
            existed=c.execute("SELECT 1 FROM coupang_catalog_products WHERE account_code=? AND seller_product_id=?",(account,seller)).fetchone() is not None
            c.execute("""INSERT INTO coupang_catalog_products(account_code,seller_product_id,product_id,seller_product_name,display_product_name,general_product_name,status_name,raw_status,is_active,last_seen_sync) VALUES(?,?,?,?,?,?,?,?,1,?) ON CONFLICT(account_code,seller_product_id) DO UPDATE SET product_id=excluded.product_id,seller_product_name=excluded.seller_product_name,display_product_name=excluded.display_product_name,general_product_name=excluded.general_product_name,status_name=excluded.status_name,raw_status=excluded.raw_status,is_active=1,last_seen_sync=excluded.last_seen_sync,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')""",values)
            row=dict(c.execute("SELECT * FROM coupang_catalog_products WHERE account_code=? AND seller_product_id=?",(account,seller)).fetchone())
        return row, not existed

    def deactivate_products_not_seen(
        self, *, account_code: object, sync_token: object
    ) -> int:
        """Hide products absent from one successful account-scoped sync.

        Rows and their option/mapping provenance remain intact.  A later
        successful approved-product sync can reactivate the same row.
        """

        account = self._text(account_code)
        token = self._text(sync_token)
        if not account or not token:
            raise ValueError("account_code and sync_token are required")
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE coupang_catalog_products
                SET is_active=0, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE account_code=? AND is_active=1
                  AND (last_seen_sync IS NULL OR last_seen_sync<>?)
                """,
                (account, token),
            )
        return int(cursor.rowcount)
    def upsert_option(self, *, account_code: object, seller_product_id: object, item: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        account=self._text(account_code); seller=self._text(seller_product_id); vendor=self._text(item.get("vendorItemId"))
        if not account or not seller or not vendor: raise ValueError("account_code, sellerProductId and vendorItemId are required")
        values=(account,vendor,seller,self._text(item.get("sellerProductItemId")),self._text(item.get("itemName")),self._text(item.get("externalVendorSku")),self._text(item.get("modelNo")),json.dumps(item.get("attributes") or [],ensure_ascii=False,sort_keys=True),json.dumps(item.get("bundleInfo") or {},ensure_ascii=False,sort_keys=True))
        with self.database.transaction() as c:
            existed=c.execute("SELECT 1 FROM coupang_catalog_options WHERE account_code=? AND vendor_item_id=?",(account,vendor)).fetchone() is not None
            c.execute("""INSERT INTO coupang_catalog_options(account_code,vendor_item_id,seller_product_id,seller_product_item_id,item_name,external_vendor_sku,model_no,attributes_json,bundle_info_json) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(account_code,vendor_item_id) DO UPDATE SET seller_product_id=excluded.seller_product_id,seller_product_item_id=excluded.seller_product_item_id,item_name=excluded.item_name,external_vendor_sku=excluded.external_vendor_sku,model_no=excluded.model_no,attributes_json=excluded.attributes_json,bundle_info_json=excluded.bundle_info_json,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')""",values)
            row=dict(c.execute("SELECT * FROM coupang_catalog_options WHERE account_code=? AND vendor_item_id=?",(account,vendor)).fetchone())
        return row, not existed

    def grouped_products(self, *, account_code: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        clauses=[]; values=[]
        clauses=["p.is_active=1"]; values=[]
        if account_code and account_code != "ALL": clauses.append("p.account_code=?"); values.append(account_code)
        where=(" WHERE " + " AND ".join(clauses)) if clauses else ""
        query="""SELECT p.*, o.vendor_item_id, o.item_name, m.canonical_model, m.mapping_source, m.mapping_status
        FROM coupang_catalog_products p LEFT JOIN coupang_catalog_options o ON o.account_code=p.account_code AND o.seller_product_id=p.seller_product_id
        LEFT JOIN coupang_product_mappings m ON m.account_code=o.account_code AND m.vendor_item_id=o.vendor_item_id""" + where + " ORDER BY p.account_code,p.seller_product_id,o.vendor_item_id"
        grouped: dict[tuple[str,str],dict[str,Any]]={}
        with self.database.connection() as c: rows=c.execute(query,values).fetchall()
        for row in rows:
            r=dict(row); key=(r['account_code'],r['seller_product_id']); product=grouped.setdefault(key,{k:r[k] for k in r if k not in {'vendor_item_id','item_name','canonical_model','mapping_source','mapping_status'}}|{'options':[]})
            if r['vendor_item_id']:
                complete=r['mapping_status']=='CONFIRMED'; option={'vendor_item_id':r['vendor_item_id'],'item_name':r['item_name'],'canonical_model':r['canonical_model'],'mapping_source':r['mapping_source'],'mapping_status':r['mapping_status'],'complete':complete}
                if status in {'COMPLETE','REVIEW'} and (complete != (status=='COMPLETE')): continue
                product['options'].append(option)
        return [p for p in grouped.values() if p['options'] or status not in {'COMPLETE','REVIEW'}]
