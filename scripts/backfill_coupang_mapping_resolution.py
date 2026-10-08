"""Classify the legacy Coupang mappings once, from data already stored.

The rows written before ``resolution`` and ``model_evidence_class`` existed all
read NEEDS_REVIEW with the reason squeezed into ``model_evidence_field``; in the
live store 805 of the 807 say MODEL_EVIDENCE_NOT_EXACT, which does not separate
an option a person can finish from one that carries no model text at all.

They can be classified without asking Coupang anything, because
``coupang_catalog_options`` already stores every field the classifier reads --
``model_no``, ``external_vendor_sku``, ``item_name``, ``attributes_json``,
``bundle_info_json`` -- and ``coupang_catalog_products`` stores the product id.
So this rebuilds the ``product_data`` the service would have fetched and hands
it to the real ``CoupangProductMappingService.resolve``. The classification
rules are not restated here; if they change, this follows.

    python scripts/backfill_coupang_mapping_resolution.py            # dry run
    python scripts/backfill_coupang_mapping_resolution.py --apply
    python scripts/backfill_coupang_mapping_resolution.py --apply --expect 807

Which rows. Exactly ``mapping_source IS NULL AND mapping_status='NEEDS_REVIEW'
AND resolution IS NULL``. The third clause is what the first two cannot give:
a row this tool leaves as a review keeps ``mapping_source`` NULL and
``mapping_status`` NEEDS_REVIEW, so without it a second run would select the
same 759 rows again and the tool could never report itself finished. With it,
a second run selects nothing. On the first run the two selectors are the same
807 rows.

What it must not touch, and does not select: every CONFIRMED row, and every row
carrying AUTO_EXACT, AUTO_ALIAS or MANUAL. ``--apply`` re-counts them before and
after and refuses to report success if either number moved.

Promotion is deliberate. 48 of the 807 carry an exact model code that the
catalog knows -- they read NOT_EXACT only because the canonicaliser learned the
LH business-display notation after they were written -- and they become
CONFIRMED/AUTO_EXACT. The ``_known_models`` gate is what makes that safe: 270
rows resolve notationally and 222 of them are held back because the model is not
in the catalog, among them a listing whose modelNo is ``LH8543BEFKR`` and whose
title names both a 43-inch and an 85-inch set.

No Coupang request is possible: the service is built with a read client that
raises, and every call supplies ``product_data``.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from repositories.coupang_product_mapping_repository import (  # noqa: E402
    AUTO_ALIAS,
    AUTO_EXACT,
    CONFIRMED,
    MANUAL,
    MANUAL_REQUIRED,
    NEEDS_REVIEW,
    UNRESOLVED,
    CoupangProductMappingRepository,
)
from repositories.database import Database  # noqa: E402
from services.coupang_product_mapping_service import (  # noqa: E402
    CoupangProductMappingService,
)

#: Rows written before this tool existed. See the module note on the third
#: clause -- without it the selection is not idempotent.
LEGACY_WHERE = (
    "mapping_source IS NULL AND mapping_status = 'NEEDS_REVIEW'"
    " AND resolution IS NULL"
)
#: Rows this tool must never select, counted before and after as a guard.
PROTECTED_WHERE = (
    "mapping_status = 'CONFIRMED'"
    " OR mapping_source IN ('AUTO_EXACT', 'AUTO_ALIAS', 'MANUAL')"
)


class CoupangApiForbidden(RuntimeError):
    """Raised if anything tries to reach Coupang from this tool."""


class _RefusingClient:
    def get_seller_product(self, seller_product_id: object) -> Any:
        raise CoupangApiForbidden(
            "the backfill must not call Coupang; product_data was missing for "
            f"seller_product_id={seller_product_id}"
        )


def _json(value: object, fallback: Any) -> Any:
    try:
        parsed = json.loads(value or "")  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return fallback if parsed is None else parsed


def _item_from(row: Any) -> dict[str, Any]:
    """One stored option, in the shape ``resolve`` reads off a product."""

    item = {
        "vendorItemId": str(row["vendor_item_id"] or ""),
        "sellerProductItemId": row["seller_product_item_id"],
        "itemName": row["item_name"] or "",
        "modelNo": row["model_no"] or "",
        "externalVendorSku": row["external_vendor_sku"] or "",
        "attributes": _json(row["attributes_json"], []),
    }
    bundle = _json(row["bundle_info_json"], {})
    if bundle not in (None, {}, []):
        item["bundleInfo"] = bundle
    return item


def load_catalog(connection) -> tuple[dict, dict]:
    """Options grouped by the product they belong to, and the product ids.

    Grouped rather than fetched per row because ``resolve`` counts how many
    items in the product carry the asked-for vendorItemId. Handing it a single
    item would answer a different question than production asks.
    """

    options: dict[tuple[str, str], list[dict[str, Any]]] = (
        collections.defaultdict(list))
    for row in connection.execute(
            "SELECT account_code, seller_product_id, vendor_item_id,"
            " seller_product_item_id, item_name, external_vendor_sku, model_no,"
            " attributes_json, bundle_info_json FROM coupang_catalog_options"):
        options[(str(row["account_code"]),
                 str(row["seller_product_id"]))].append(_item_from(row))
    products = {
        (str(row["account_code"]), str(row["seller_product_id"])):
            row["product_id"]
        for row in connection.execute(
            "SELECT account_code, seller_product_id, product_id"
            " FROM coupang_catalog_products")
    }
    return options, products


def legacy_rows(connection) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(
        "SELECT m.account_code, m.vendor_item_id,"
        " m.model_evidence_field AS legacy_reason, o.seller_product_id"
        " FROM coupang_product_mappings m"
        " LEFT JOIN coupang_catalog_options o"
        "   ON o.account_code = m.account_code"
        "  AND o.vendor_item_id = m.vendor_item_id"
        f" WHERE {LEGACY_WHERE}"
        " ORDER BY m.account_code, m.vendor_item_id")]


def count(connection, where: str) -> int:
    return int(connection.execute(
        f"SELECT COUNT(*) FROM coupang_product_mappings WHERE {where}"
    ).fetchone()[0])


def protected_fingerprint(connection) -> dict[tuple[str, str], tuple]:
    """Every row this tool must not touch, keyed so it can be rechecked.

    Keyed rather than listed because the set legitimately grows: a legacy row
    promoted to CONFIRMED joins it. Comparing the whole set would read this
    tool's own 48 promotions as "a protected row changed", which it did on the
    first apply here. What must hold is narrower and is what ``_unchanged``
    checks: every row that was protected BEFORE is still present and still
    identical.
    """

    return {
        (str(row["account_code"]), str(row["vendor_item_id"])): (
            row["canonical_model"], row["mapping_source"], row["mapping_status"]
        )
        for row in connection.execute(
            "SELECT account_code, vendor_item_id, canonical_model,"
            " mapping_source, mapping_status FROM coupang_product_mappings"
            f" WHERE {PROTECTED_WHERE}")
    }


def _unchanged(before: dict, after: dict) -> tuple[bool, list[str]]:
    """Whether every previously protected row survived untouched."""

    damaged = [
        f"{account}/{vendor_item}: {values} -> {after.get((account, vendor_item))}"
        for (account, vendor_item), values in before.items()
        if after.get((account, vendor_item)) != values
    ]
    return not damaged, damaged[:10]


def build_services(accounts, repository) -> dict[str, Any]:
    """One service per account, with the two catalog lookups held.

    ``_known_models`` canonicalises all 1,598 catalog keys plus the Product
    Knowledge codes, and ``resolve`` calls it once per row: over 807 rows that
    is more than a million regex passes and the run does not finish. Both are
    pure functions of a catalog file that cannot change while this runs, so
    holding the first answer returns what the repeated calls would have
    returned. The production service is not modified -- the cache lives on
    these instances, which exist only for this run.
    """

    services: dict[str, Any] = {}
    for account in accounts:
        service = CoupangProductMappingService(
            account_code=account,
            read_client=_RefusingClient(),  # type: ignore[arg-type]
            repository=repository,
        )
        known, aliases = service._known_models(), service._aliases()
        service._known_models = lambda _value=known: _value
        service._aliases = lambda _value=aliases: _value
        services[account] = service
    return services


class _CapturingRepository(CoupangProductMappingRepository):
    """A dry run's repository: reads are real, the write is recorded only."""

    def __init__(self, database) -> None:
        super().__init__(database)
        self.captured: list[dict[str, Any]] = []

    def upsert(self, **kwargs: Any) -> dict[str, Any]:  # type: ignore[override]
        self.captured.append(dict(kwargs))
        return {
            "account_code": kwargs.get("account_code"),
            "vendor_item_id": str(kwargs.get("vendor_item_id") or ""),
            "canonical_model": kwargs.get("canonical_model"),
            "mapping_source": kwargs.get("mapping_source"),
            "mapping_status": kwargs.get("mapping_status", NEEDS_REVIEW),
            "model_evidence_field": kwargs.get("model_evidence_field"),
            "model_evidence_value": kwargs.get("model_evidence_value"),
            "model_evidence_class": kwargs.get("model_evidence_class"),
            "resolution": kwargs.get("resolution"),
            "raw_model_candidates": kwargs.get("raw_model_candidates") or [],
        }


def classify(database: Database, *, apply: bool) -> dict[str, Any]:
    """Reclassify every legacy row. Writes only when ``apply`` is true."""

    with database.connection() as connection:
        rows = legacy_rows(connection)
        options, products = load_catalog(connection)
        protected_before = protected_fingerprint(connection)

    repository = (CoupangProductMappingRepository(database) if apply
                  else _CapturingRepository(database))
    services = build_services(
        sorted({str(row["account_code"]) for row in rows}), repository)

    resolutions: collections.Counter = collections.Counter()
    classes: collections.Counter = collections.Counter()
    statuses: collections.Counter = collections.Counter()
    unclassifiable: list[dict[str, Any]] = []
    per_account: dict[str, collections.Counter] = collections.defaultdict(
        collections.Counter)

    for row in rows:
        account = str(row["account_code"])
        vendor_item = str(row["vendor_item_id"])
        seller_product = row["seller_product_id"]
        items = (options.get((account, str(seller_product)))
                 if seller_product is not None else None)
        if not items:
            unclassifiable.append({
                "account_code": account, "vendor_item_id": vendor_item,
                "why": "NO_CATALOG_OPTION"})
            continue
        try:
            result = services[account].resolve(
                seller_product_id=seller_product,
                vendor_item_id=vendor_item,
                product_data={
                    "sellerProductId": seller_product,
                    "productId": products.get((account, str(seller_product))),
                    "items": items,
                })
        except CoupangApiForbidden:
            raise
        except Exception as error:  # noqa: BLE001 - recorded, the run continues
            unclassifiable.append({
                "account_code": account, "vendor_item_id": vendor_item,
                "why": f"{type(error).__name__}: {error}"})
            continue
        if result.reused:
            # Only a CONFIRMED row is reused, and the selection excludes those.
            unclassifiable.append({
                "account_code": account, "vendor_item_id": vendor_item,
                "why": "UNEXPECTEDLY_ALREADY_CONFIRMED"})
            continue
        resolutions[result.resolution or "NONE"] += 1
        classes[result.model_evidence_class or "NONE"] += 1
        statuses[str(result.mapping.get("mapping_status") or "")] += 1
        per_account[account][result.resolution or "NONE"] += 1

    with database.connection() as connection:
        protected_after = protected_fingerprint(connection)
        remaining = count(connection, LEGACY_WHERE)
    survived, damaged = _unchanged(protected_before, protected_after)

    return {
        "protected_damaged": damaged,
        "applied": bool(apply),
        "selected": len(rows),
        "reclassified": sum(resolutions.values()),
        "unclassifiable": len(unclassifiable),
        "total_accounted": sum(resolutions.values()) + len(unclassifiable),
        "resolutions": dict(resolutions),
        "model_evidence_classes": dict(classes),
        "mapping_status": dict(statuses),
        "per_account": {k: dict(v) for k, v in per_account.items()},
        "unclassifiable_detail": unclassifiable[:20],
        "legacy_remaining": remaining,
        "protected_before": len(protected_before),
        "protected_after": len(protected_after),
        "protected_unchanged": survived,
        "protected_promoted_by_this_run": sorted(
            set(protected_after) - set(protected_before)).__len__(),
        "writes_captured": (len(repository.captured)
                            if isinstance(repository, _CapturingRepository)
                            else None),
    }


def verify(report: dict[str, Any], *, expect: int | None) -> list[str]:
    """Everything that must hold afterwards. Empty means it all did."""

    problems: list[str] = []
    if report["total_accounted"] != report["selected"]:
        problems.append(
            f"accounted {report['total_accounted']} != selected "
            f"{report['selected']}")
    if expect is not None and report["selected"] != expect:
        problems.append(f"selected {report['selected']} != expected {expect}")
    if report["unclassifiable"]:
        problems.append(f"unclassifiable {report['unclassifiable']} != 0")
    if not report["protected_unchanged"]:
        problems.append(
            "a previously CONFIRMED or already-sourced row changed: "
            + "; ".join(report.get("protected_damaged") or []))
    confirmed = report["resolutions"].get(AUTO_EXACT, 0) + report[
        "resolutions"].get(AUTO_ALIAS, 0)
    if report["mapping_status"].get(CONFIRMED, 0) != confirmed:
        problems.append(
            f"CONFIRMED {report['mapping_status'].get(CONFIRMED, 0)} does not "
            f"match AUTO_EXACT+AUTO_ALIAS {confirmed}")
    if report["applied"] and report["legacy_remaining"]:
        problems.append(
            f"legacy rows remaining after apply: {report['legacy_remaining']}")
    return problems


def render(report: dict[str, Any]) -> None:
    mode = "APPLY" if report["applied"] else "DRY RUN (nothing written)"
    print(f"mode                 : {mode}")
    print(f"selected             : {report['selected']}")
    print(f"reclassified         : {report['reclassified']}")
    print(f"unclassifiable       : {report['unclassifiable']}")
    print(f"total accounted      : {report['total_accounted']}")
    print()
    print("RESOLUTION")
    for key in (AUTO_EXACT, AUTO_ALIAS, MANUAL_REQUIRED, UNRESOLVED):
        print(f"  {key:<18}{report['resolutions'].get(key, 0)}")
    for key, value in sorted(report["resolutions"].items()):
        if key not in {AUTO_EXACT, AUTO_ALIAS, MANUAL_REQUIRED, UNRESOLVED}:
            print(f"  {key:<18}{value}   (unexpected)")
    print()
    print("MODEL EVIDENCE CLASS")
    for key, value in sorted(report["model_evidence_classes"].items()):
        print(f"  {key:<18}{value}")
    print()
    print(f"mapping_status       : {report['mapping_status']}")
    print(f"per account          : {report['per_account']}")
    print(f"legacy remaining     : {report['legacy_remaining']}")
    print(f"protected rows       : {report['protected_before']} -> "
          f"{report['protected_after']} "
          f"(+{report['protected_promoted_by_this_run']} promoted by this run)"
          f", pre-existing unchanged={report['protected_unchanged']}")
    if report["writes_captured"] is not None:
        print(f"writes performed     : 0 (captured "
              f"{report['writes_captured']})")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Classify legacy Coupang mappings from stored catalog data.")
    parser.add_argument(
        "--apply", action="store_true",
        help="write the classifications; without it nothing is written")
    parser.add_argument(
        "--database", type=Path, default=None,
        help="database file; defaults to the configured one")
    parser.add_argument(
        "--expect", type=int, default=None,
        help="fail unless exactly this many legacy rows are selected")
    parser.add_argument(
        "--report", type=Path, default=None,
        help="write the full report as JSON to this path")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    database = (Database(arguments.database) if arguments.database is not None
                else Database())
    report = classify(database, apply=arguments.apply)
    render(report)
    problems = verify(report, expect=arguments.expect)
    if arguments.report is not None:
        arguments.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    if problems:
        for problem in problems:
            print(f"FAILED: {problem}")
        return 1
    print("OK" if arguments.apply else "OK (dry run; re-run with --apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
