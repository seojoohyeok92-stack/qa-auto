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

# What the option's own text amounted to, judged before anything is decided.
#
# EXACT_MODEL   a single model code this catalog knows, however it was written.
# FAMILY_ONLY   a series or family is named and no model code resolves from it:
#               "M50D 32", "D400 22/24". A size may be present and may even be
#               unique; naming the family is still not naming the model.
# AMBIGUOUS     two or more different models are stated, or the vendorItemId
#               does not identify exactly one option.
# NO_MODEL_EVIDENCE  nothing model-shaped at all.
EXACT_MODEL = "EXACT_MODEL"
FAMILY_ONLY = "FAMILY_ONLY"
AMBIGUOUS = "AMBIGUOUS"
NO_MODEL_EVIDENCE = "NO_MODEL_EVIDENCE"
MODEL_EVIDENCE_CLASSES = frozenset({
    EXACT_MODEL, FAMILY_ONLY, AMBIGUOUS, NO_MODEL_EVIDENCE,
})

# What was done about it. Separate from the class because the same class does
# not always resolve the same way.
#
# AUTO_EXACT        the evidence states the model in a notation the rules read.
# AUTO_ALIAS        only an explicit model-code alias could reach the model.
# MANUAL_REQUIRED   a person can finish this, and only a person may.
# UNRESOLVED        there is nothing to finish it from.
# MANUAL_CONFIRMED  a person did finish it.
MANUAL_REQUIRED = "MANUAL_REQUIRED"
UNRESOLVED = "UNRESOLVED"
MANUAL_CONFIRMED = "MANUAL_CONFIRMED"
RESOLUTIONS = frozenset({
    AUTO_EXACT, AUTO_ALIAS, MANUAL_REQUIRED, UNRESOLVED, MANUAL_CONFIRMED,
})

# Which resolutions may carry a confirmed model. Kept here rather than at the
# call site so a new resolution cannot quietly become auto-confirmable.
CONFIRMING_RESOLUTIONS = frozenset({AUTO_EXACT, AUTO_ALIAS, MANUAL_CONFIRMED})


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
        model_evidence_class: object | None = None,
        resolution: object | None = None,
    ) -> dict[str, Any]:
        account = self._required(account_code, "account_code")
        vendor_item = self._required(vendor_item_id, "vendor_item_id")
        source = str(mapping_source or "").strip().upper() or None
        status = str(mapping_status or "").strip().upper()
        evidence_class = str(model_evidence_class or "").strip().upper() or None
        decided = str(resolution or "").strip().upper() or None
        if source not in {None, AUTO_EXACT, AUTO_ALIAS, MANUAL}:
            raise ValueError(f"Invalid mapping_source: {mapping_source}")
        if status not in {CONFIRMED, NEEDS_REVIEW}:
            raise ValueError(f"Invalid mapping_status: {mapping_status}")
        # Fail closed on both new fields. An unknown value reaching the column
        # would be indistinguishable from a classification nobody wrote, and
        # the whole point of the pair is that the outcome is readable.
        if evidence_class is not None and evidence_class not in MODEL_EVIDENCE_CLASSES:
            raise ValueError(
                f"Invalid model_evidence_class: {model_evidence_class}")
        if decided is not None and decided not in RESOLUTIONS:
            raise ValueError(f"Invalid resolution: {resolution}")
        model = str(canonical_model or "").strip() or None
        if status == CONFIRMED and not model:
            raise ValueError("canonical_model is required for CONFIRMED mapping")
        # A resolution that is not a confirming one must not arrive CONFIRMED,
        # and a confirming one must not arrive as a review. Without this the
        # two fields could disagree about the same row and a reader would have
        # to guess which one decided.
        if decided is not None:
            if status == CONFIRMED and decided not in CONFIRMING_RESOLUTIONS:
                raise ValueError(
                    f"resolution {decided} cannot be CONFIRMED")
            if status == NEEDS_REVIEW and decided in CONFIRMING_RESOLUTIONS:
                raise ValueError(
                    f"resolution {decided} cannot be NEEDS_REVIEW")
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
            evidence_class,
            decided,
        )
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO coupang_product_mappings(
                    account_code, vendor_item_id, seller_product_id,
                    seller_product_item_id, product_id, canonical_model,
                    mapping_source, mapping_status, model_evidence_field,
                    model_evidence_value, raw_model_candidates_json,
                    model_evidence_class, resolution
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    model_evidence_class=excluded.model_evidence_class,
                    resolution=excluded.resolution,
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
