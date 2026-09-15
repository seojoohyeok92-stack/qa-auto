"""Resolve one Coupang option to a base canonical model without touching inquiry flow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from api.coupang_read_client import CoupangReadClient
from repositories.coupang_product_mapping_repository import (
    AUTO_EXACT,
    CONFIRMED,
    MANUAL,
    NEEDS_REVIEW,
    CoupangProductMappingRepository,
)
from repositories.product_catalog_repository import (
    ProductCatalogRepository,
    canonical_model_identity,
)


@dataclass(frozen=True)
class CoupangProductMappingResult:
    mapping: dict[str, Any]
    reused: bool
    reason: str | None = None
    matching_item_count: int = 0
    bundle_detected: bool = False


class CoupangProductMappingService:
    """Map an account-scoped vendorItemId only when product evidence is exact."""

    def __init__(
        self,
        *,
        account_code: str,
        read_client: CoupangReadClient,
        repository: CoupangProductMappingRepository,
        catalog_repository: ProductCatalogRepository | None = None,
    ) -> None:
        self.account_code = str(account_code or "").strip().upper()
        if not self.account_code:
            raise ValueError("account_code is required")
        self.read_client = read_client
        self.repository = repository
        self.catalog_repository = catalog_repository or ProductCatalogRepository()

    def resolve(
        self,
        *,
        seller_product_id: object,
        vendor_item_id: object,
        product_data: dict[str, Any] | None = None,
    ) -> CoupangProductMappingResult:
        vendor_item = str(vendor_item_id or "").strip()
        existing = self.repository.get_confirmed(
            account_code=self.account_code, vendor_item_id=vendor_item
        )
        if existing is not None:
            return CoupangProductMappingResult(existing, reused=True)

        if product_data is None:
            product = self.read_client.get_seller_product(seller_product_id)
            data = product.get("data") if isinstance(product.get("data"), dict) else {}
        else:
            data = product_data
        items = data.get("items") if isinstance(data.get("items"), list) else []
        matches = [
            item for item in items
            if isinstance(item, dict) and str(item.get("vendorItemId") or "") == vendor_item
        ]
        if len(matches) != 1:
            mapping = self._save_review(
                vendor_item_id=vendor_item,
                data=data,
                reason="VENDOR_ITEM_NOT_UNIQUE",
            )
            return CoupangProductMappingResult(
                mapping, reused=False, reason="VENDOR_ITEM_NOT_UNIQUE",
                matching_item_count=len(matches),
            )

        item = matches[0]
        candidates = self._exact_candidates(item)
        identities = {entry["canonical_model"] for entry in candidates}
        bundle = item.get("bundleInfo") not in (None, {}, [])
        if len(identities) != 1:
            reason = "MODEL_EVIDENCE_CONFLICT" if len(identities) > 1 else "MODEL_EVIDENCE_NOT_EXACT"
            mapping = self._save_review(
                vendor_item_id=vendor_item,
                data=data,
                item=item,
                candidates=candidates,
                reason=reason,
            )
            return CoupangProductMappingResult(
                mapping, reused=False, reason=reason,
                matching_item_count=1, bundle_detected=bundle,
            )

        canonical = next(iter(identities))
        evidence = candidates[0]
        mapping = self.repository.upsert(
            account_code=self.account_code,
            vendor_item_id=vendor_item,
            seller_product_id=data.get("sellerProductId"),
            seller_product_item_id=item.get("sellerProductItemId"),
            product_id=data.get("productId"),
            canonical_model=canonical,
            mapping_source=AUTO_EXACT,
            mapping_status=CONFIRMED,
            model_evidence_field=evidence["field"],
            model_evidence_value=evidence["value"],
            raw_model_candidates=candidates,
        )
        return CoupangProductMappingResult(
            mapping, reused=False, matching_item_count=1, bundle_detected=bundle
        )

    def save_manual_mapping(
        self,
        *,
        vendor_item_id: object,
        canonical_model: object,
        seller_product_id: object | None = None,
        seller_product_item_id: object | None = None,
        product_id: object | None = None,
    ) -> dict[str, Any]:
        canonical = canonical_model_identity(
            canonical_model, aliases=self._aliases()
        )
        if not canonical or canonical not in self._known_models():
            raise ValueError("canonical_model must be a known exact catalog or Product Knowledge model")
        return self.repository.upsert(
            account_code=self.account_code,
            vendor_item_id=vendor_item_id,
            seller_product_id=seller_product_id,
            seller_product_item_id=seller_product_item_id,
            product_id=product_id,
            canonical_model=canonical,
            mapping_source=MANUAL,
            mapping_status=CONFIRMED,
            model_evidence_field="MANUAL",
            model_evidence_value=str(canonical_model),
        )

    def _save_review(
        self,
        *,
        vendor_item_id: str,
        data: dict[str, Any],
        item: dict[str, Any] | None = None,
        candidates: list[dict[str, str]] | None = None,
        reason: str,
    ) -> dict[str, Any]:
        return self.repository.upsert(
            account_code=self.account_code,
            vendor_item_id=vendor_item_id,
            seller_product_id=data.get("sellerProductId"),
            seller_product_item_id=(item or {}).get("sellerProductItemId"),
            product_id=data.get("productId"),
            mapping_status=NEEDS_REVIEW,
            model_evidence_field=reason,
            raw_model_candidates=candidates or [],
        )

    def _aliases(self) -> dict[str, Any]:
        return dict(self.catalog_repository.catalog().get("aliases") or {})

    def _known_models(self) -> set[str]:
        aliases = self._aliases()
        catalog = self.catalog_repository.catalog()
        keys: Iterable[object] = list(catalog.get("catalog", {}))
        knowledge = self.catalog_repository.product_knowledge()
        keys = [*keys, *(row.get("model_code") for row in knowledge.get("model_facts", []) if isinstance(row, dict))]
        return {
            canonical for key in keys
            if (canonical := canonical_model_identity(key, aliases=aliases))
        }

    def _exact_candidates(self, item: dict[str, Any]) -> list[dict[str, str]]:
        aliases = self._aliases()
        known = self._known_models()
        found: list[dict[str, str]] = []
        for field, values in (
            ("modelNo", [item.get("modelNo")]),
            ("externalVendorSku", [item.get("externalVendorSku")]),
            ("attributes", self._attribute_values(item.get("attributes"))),
        ):
            for value in values:
                raw = str(value or "").strip()
                canonical = canonical_model_identity(raw, aliases=aliases)
                if raw and canonical and canonical in known:
                    found.append({
                        "field": field,
                        "value": raw,
                        "canonical_model": canonical,
                    })
        return found

    @staticmethod
    def _attribute_values(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        allowed = {"모델", "모델명", "모델번호", "모델코드", "model", "modelno", "modelnumber", "modelcode"}
        result: list[str] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            name = str(item.get("attributeTypeName") or item.get("attributeName") or "")
            normalized = "".join(name.lower().split()).replace("_", "")
            if normalized in allowed:
                result.append(str(item.get("attributeValueName") or ""))
        return result
