"""Resolve one Coupang option to a base canonical model without touching inquiry flow."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from api.coupang_read_client import CoupangReadClient
from repositories.coupang_product_mapping_repository import (
    AMBIGUOUS,
    AUTO_ALIAS,
    AUTO_EXACT,
    CONFIRMED,
    EXACT_MODEL,
    FAMILY_ONLY,
    MANUAL,
    MANUAL_CONFIRMED,
    MANUAL_REQUIRED,
    NEEDS_REVIEW,
    NO_MODEL_EVIDENCE,
    UNRESOLVED,
    CoupangProductMappingRepository,
)
from repositories.product_catalog_repository import (
    ProductCatalogRepository,
    canonical_model_identity,
)

# A token that could be naming a model: letters and digits together, long
# enough not to be a size or a quantity. "M50D", "D400", "32DM501" qualify;
# "internal", "sku", "32", "80CM" do not.
#
# Deliberately a shape test and not a vocabulary. The families that appear in
# Coupang option text -- M50D, M50F, D400, G50D -- are listing names the
# catalog does not hold as models, so a vocabulary built from the catalog
# would miss exactly the cases this has to catch. Reading the shape instead
# risks calling an unrelated vendor SKU a family, and that error is the safe
# direction: FAMILY_ONLY asks a person, which is what an unreadable SKU needs
# anyway.
_MODEL_SHAPED_TOKEN = re.compile(r"(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*\d)[A-Z0-9]{3,}")
# Sizes and units, which are model-shaped by the rule above and are not models.
_SIZE_LIKE = re.compile(r"^\d+(CM|MM|INCH|HZ|W|K|ML|G|KG)?$")


@dataclass(frozen=True)
class CoupangProductMappingResult:
    mapping: dict[str, Any]
    reused: bool
    reason: str | None = None
    matching_item_count: int = 0
    bundle_detected: bool = False
    model_evidence_class: str | None = None
    resolution: str | None = None


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
            # Zero or several options behind one vendorItemId. Two different
            # models may be among them, so this is an ambiguity about which
            # option was asked about, not an absence of evidence.
            mapping = self._save_review(
                vendor_item_id=vendor_item,
                data=data,
                reason="VENDOR_ITEM_NOT_UNIQUE",
                evidence_class=AMBIGUOUS,
                resolution=MANUAL_REQUIRED,
            )
            return CoupangProductMappingResult(
                mapping, reused=False, reason="VENDOR_ITEM_NOT_UNIQUE",
                matching_item_count=len(matches),
                model_evidence_class=AMBIGUOUS, resolution=MANUAL_REQUIRED,
            )

        item = matches[0]
        candidates = self._exact_candidates(item)
        identities = {entry["canonical_model"] for entry in candidates}
        bundle = item.get("bundleInfo") not in (None, {}, [])
        evidence_class = self._classify(item, identities)

        if len(identities) != 1 or bundle:
            if len(identities) > 1:
                reason = "MODEL_EVIDENCE_CONFLICT"
            elif bundle:
                # A bundle listing sells more than the base model, so even an
                # exact modelNo does not say what this option delivers. The
                # evidence class still records what the text was.
                reason = "BUNDLE_REQUIRES_MANUAL"
            elif evidence_class == FAMILY_ONLY:
                reason = "MODEL_EVIDENCE_FAMILY_ONLY"
            else:
                reason = "MODEL_EVIDENCE_NOT_EXACT"
            # Nothing to finish it from is the only UNRESOLVED case. Everything
            # else names something a person can act on.
            resolution = (
                UNRESOLVED if evidence_class == NO_MODEL_EVIDENCE and not bundle
                else MANUAL_REQUIRED
            )
            mapping = self._save_review(
                vendor_item_id=vendor_item,
                data=data,
                item=item,
                candidates=candidates,
                reason=reason,
                evidence_class=evidence_class,
                resolution=resolution,
            )
            return CoupangProductMappingResult(
                mapping, reused=False, reason=reason,
                matching_item_count=1, bundle_detected=bundle,
                model_evidence_class=evidence_class, resolution=resolution,
            )

        canonical = next(iter(identities))
        evidence = candidates[0]
        # Which route reached the model. An evidence token the notation rules
        # can read on their own is AUTO_EXACT; one that only an explicit
        # model-code alias could resolve is AUTO_ALIAS. ``BE50D`` is the second
        # kind -- notation alone leaves it as ``BE50D`` and only the alias
        # table knows it is ``50BED`` -- while ``LS32DM501EKXKR`` is the first,
        # because stripping the vendor prefix and the region suffix is a rule.
        resolution = AUTO_EXACT if all(
            not entry["alias_required"] for entry in candidates
        ) else AUTO_ALIAS
        mapping = self.repository.upsert(
            account_code=self.account_code,
            vendor_item_id=vendor_item,
            seller_product_id=data.get("sellerProductId"),
            seller_product_item_id=item.get("sellerProductItemId"),
            product_id=data.get("productId"),
            canonical_model=canonical,
            mapping_source=resolution,
            mapping_status=CONFIRMED,
            model_evidence_field=evidence["field"],
            model_evidence_value=evidence["value"],
            raw_model_candidates=candidates,
            model_evidence_class=EXACT_MODEL,
            resolution=resolution,
        )
        return CoupangProductMappingResult(
            mapping, reused=False, matching_item_count=1, bundle_detected=bundle,
            model_evidence_class=EXACT_MODEL, resolution=resolution,
        )

    def _classify(
        self, item: dict[str, Any], identities: set[str]
    ) -> str:
        """What the option's text amounts to, before anything is decided."""

        if len(identities) > 1:
            return AMBIGUOUS
        if len(identities) == 1:
            return EXACT_MODEL
        # No model resolved. Does the text name a family, or nothing at all?
        for text in self._evidence_texts(item):
            for token in _MODEL_SHAPED_TOKEN.findall(
                "".join(ch if ch.isalnum() else " " for ch in text.upper())
            ):
                if not _SIZE_LIKE.fullmatch(token):
                    return FAMILY_ONLY
        return NO_MODEL_EVIDENCE

    def _evidence_texts(self, item: dict[str, Any]) -> list[str]:
        """Every field a model or a family could be stated in.

        ``itemName`` is read for classification only and never for identity:
        an option title may name a family, and naming a family is what
        FAMILY_ONLY records.
        """

        return [
            str(item.get("modelNo") or ""),
            str(item.get("externalVendorSku") or ""),
            str(item.get("itemName") or ""),
            *self._attribute_values(item.get("attributes")),
        ]

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
        # Carry the automatic classification forward. The person resolved the
        # mapping, but what the option's own text amounted to -- FAMILY_ONLY,
        # AMBIGUOUS -- is still the reason this needed a person, and dropping
        # it would lose the only record of that.
        previous = self.repository.get(
            account_code=self.account_code, vendor_item_id=vendor_item_id
        ) or {}
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
            model_evidence_class=previous.get("model_evidence_class"),
            resolution=MANUAL_CONFIRMED,
        )

    def _save_review(
        self,
        *,
        vendor_item_id: str,
        data: dict[str, Any],
        item: dict[str, Any] | None = None,
        candidates: list[dict[str, str]] | None = None,
        reason: str,
        evidence_class: str,
        resolution: str,
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
            model_evidence_class=evidence_class,
            resolution=resolution,
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
                    # Would the notation rules alone have reached the same
                    # model? If not, an explicit alias is what identified it,
                    # and the mapping is AUTO_ALIAS rather than AUTO_EXACT.
                    found.append({
                        "field": field,
                        "value": raw,
                        "canonical_model": canonical,
                        "alias_required": canonical_model_identity(
                            raw, aliases=None) != canonical,
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
