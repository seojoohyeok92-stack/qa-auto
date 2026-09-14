"""Persistence for account-scoped Coupang option-to-model mappings."""

from __future__ import annotations

import json
from typing import Any

from repositories.database import Database


CONFIRMED = "CONFIRMED"
NEEDS_REVIEW = "NEEDS_REVIEW"
AUTO_EXACT = "AUTO_EXACT"
AUTO_ALIAS = "AUTO_ALIAS"
MANUAL = "MANUAL"


class CoupangProductMappingRepository:
    """Store mappings by the account-scoped Coupang option identity only."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _row(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        try:
            result["raw_model_candidates"] = json.loads(
                result.pop("raw_model_candidates_json") or "[]"
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            result["raw_model_candidates"] = []
        return result

    @staticmethod
    def _required(value: object, name: str) -> str:
        result = str(value or "").strip()
        if not result:
            raise ValueError(f"{name} is required")
        return result

    def get(self, *, account_code: object, vendor_item_id: object) -> dict[str, Any] | None:
        account = self._required(account_code, "account_code")
        vendor_item = self._required(vendor_item_id, "vendor_item_id")
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM coupang_product_mappings
                WHERE account_code=? AND vendor_item_id=?
                """,
                (account, vendor_item),
            ).fetchone()
        return self._row(row)

    def get_confirmed(
        self, *, account_code: object, vendor_item_id: object
    ) -> dict[str, Any] | None:
        row = self.get(account_code=account_code, vendor_item_id=vendor_item_id)
        return row if row and row.get("mapping_status") == CONFIRMED else None

    def upsert(
        self,
        *,
        account_code: object,
        vendor_item_id: object,
        seller_product_id: object | None = None,
        seller_product_item_id: object | None = None,
        product_id: object | None = None,
        canonical_model: object | None = None,
        mapping_source: str | None = None,
        mapping_status: str = NEEDS_REVIEW,
        model_evidence_field: object | None = None,
        model_evidence_value: object | None = None,
        raw_model_candidates: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        account = self._required(account_code, "account_code")
        vendor_item = self._required(vendor_item_id, "vendor_item_id")
        source = str(mapping_source or "").strip().upper() or None
        status = str(mapping_status or "").strip().upper()
        if source not in {None, AUTO_EXACT, AUTO_ALIAS, MANUAL}:
            raise ValueError(f"Invalid mapping_source: {mapping_source}")
        if status not in {CONFIRMED, NEEDS_REVIEW}:
            raise ValueError(f"Invalid mapping_status: {mapping_status}")
        model = str(canonical_model or "").strip() or None
        if status == CONFIRMED and not model:
            raise ValueError("canonical_model is required for CONFIRMED mapping")
        values = (
            account,
            vendor_item,
            str(seller_product_id or "").strip() or None,
            str(seller_product_item_id or "").strip() or None,
            str(product_id or "").strip() or None,
            model,
            source,
            status,
            str(model_evidence_field or "").strip() or None,
            str(model_evidence_value or "").strip() or None,
            json.dumps(raw_model_candidates or [], ensure_ascii=False, sort_keys=True),
        )
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO coupang_product_mappings(
                    account_code, vendor_item_id, seller_product_id,
                    seller_product_item_id, product_id, canonical_model,
                    mapping_source, mapping_status, model_evidence_field,
                    model_evidence_value, raw_model_candidates_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_code, vendor_item_id) DO UPDATE SET
                    seller_product_id=excluded.seller_product_id,
                    seller_product_item_id=excluded.seller_product_item_id,
                    product_id=excluded.product_id,
                    canonical_model=excluded.canonical_model,
                    mapping_source=excluded.mapping_source,
                    mapping_status=excluded.mapping_status,
                    model_evidence_field=excluded.model_evidence_field,
                    model_evidence_value=excluded.model_evidence_value,
                    raw_model_candidates_json=excluded.raw_model_candidates_json,
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                values,
            )
            row = connection.execute(
                """
                SELECT * FROM coupang_product_mappings
                WHERE account_code=? AND vendor_item_id=?
                """,
                (account, vendor_item),
            ).fetchone()
        result = self._row(row)
        assert result is not None
        return result
