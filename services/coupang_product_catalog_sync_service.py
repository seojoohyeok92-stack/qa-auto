"""Manual, read-only Coupang seller-product catalog synchronization."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any
from api.coupang_read_client import CoupangReadClient
from repositories.coupang_product_catalog_repository import CoupangProductCatalogRepository
from services.coupang_product_mapping_service import CoupangProductMappingService

@dataclass
class CoupangCatalogSyncSummary:
    account_code: str
    products_seen: int = 0; products_new: int = 0; products_updated: int = 0
    options_seen: int = 0; options_new: int = 0; options_updated: int = 0
    auto_exact: int = 0; confirmed_reused: int = 0; needs_review: int = 0
    errors: list[str] = field(default_factory=list)

class CoupangProductCatalogSyncService:
    def __init__(self, *, account_code: str, read_client: CoupangReadClient,
                 catalog_repository: CoupangProductCatalogRepository,
                 mapping_service: CoupangProductMappingService) -> None:
        self.account_code=account_code; self.read_client=read_client
        self.catalog_repository=catalog_repository; self.mapping_service=mapping_service
    def sync_account(self, *, max_per_page: int = 100, status: str | None = None) -> CoupangCatalogSyncSummary:
        result=CoupangCatalogSyncSummary(account_code=self.account_code); token: str | None=None; seen:set[str]=set()
        while True:
            page=self.read_client.list_seller_products(next_token=token,max_per_page=max_per_page,status=status)
            rows=page.get("data")
            if not isinstance(rows,list):
                raise ValueError("Malformed Coupang seller-product list response")
            for listed in rows:
                if not isinstance(listed,dict) or not str(listed.get("sellerProductId") or "").strip():
                    result.errors.append("MALFORMED_PRODUCT_ROW"); continue
                seller_id=str(listed["sellerProductId"])
                if seller_id in seen: continue
                seen.add(seller_id); result.products_seen += 1
                try:
                    detail=self.read_client.get_seller_product(seller_id)
                    data=detail.get("data") if isinstance(detail.get("data"),dict) else {}
                    merged={**listed,**data,"sellerProductId":data.get("sellerProductId",seller_id)}
                    _, created=self.catalog_repository.upsert_product(account_code=self.account_code,data=merged)
                    result.products_new += int(created); result.products_updated += int(not created)
                    for item in data.get("items") if isinstance(data.get("items"),list) else []:
                        if not isinstance(item,dict) or not item.get("vendorItemId"): continue
                        result.options_seen += 1
                        _, created=self.catalog_repository.upsert_option(account_code=self.account_code,seller_product_id=seller_id,item=item)
                        result.options_new += int(created); result.options_updated += int(not created)
                        mapping=self.mapping_service.resolve(seller_product_id=seller_id,vendor_item_id=item["vendorItemId"])
                        status_value=mapping.mapping.get("mapping_status")
                        if mapping.reused: result.confirmed_reused += 1
                        elif status_value == "CONFIRMED": result.auto_exact += 1
                        else: result.needs_review += 1
                except Exception as exc:
                    result.errors.append(f"{seller_id}:{type(exc).__name__}")
            next_value=page.get("nextToken")
            if next_value in (None, ""): break
            token=str(next_value)
        return result
