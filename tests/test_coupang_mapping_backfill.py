"""The legacy backfill: it classifies from stored data and touches nothing else.

The tool exists because the rows written before ``resolution`` and
``model_evidence_class`` all read NEEDS_REVIEW with the reason squeezed into
``model_evidence_field``, and because every field the classifier needs is
already in ``coupang_catalog_options`` -- so they can be reclassified without
asking Coupang anything.

What these tests pin is not the classification, which belongs to
``CoupangProductMappingService`` and is tested there. It is the four properties
the backfill itself has to get right:

* it selects exactly the legacy rows and leaves every other row byte-identical;
* it cannot reach Coupang, in either mode;
* without ``--apply`` the database does not change at all;
* running it twice does nothing the second time, which is what makes it
  safe to re-run after an interrupted pass.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from config import COUPANG_OJE_NS, COUPANG_OJE_PLUS
from repositories.coupang_product_mapping_repository import (
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
from repositories.database import Database
from scripts import backfill_coupang_mapping_resolution as backfill


def _catalog(tmp_path: Path):
    from repositories.product_catalog_repository import ProductCatalogRepository

    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({
        "MODEL_CATALOG": {
            "S32DM501": {"model": "S32DM501"},
            "S32DM500": {"model": "S32DM500"},
        },
        "MODEL_ALIASES": {"LS32DM501EKXKR": "S32DM501"},
        "PRODUCT_KNOWLEDGE": {"model_facts": []},
    }), encoding="utf-8")
    return ProductCatalogRepository(path)


@pytest.fixture(autouse=True)
def _catalog_for_the_backfill(tmp_path, monkeypatch):
    """The tool builds its own services, so the catalog is patched at source."""

    import services.coupang_product_mapping_service as service_module

    monkeypatch.setattr(
        service_module, "ProductCatalogRepository",
        lambda *a, **k: _catalog(tmp_path))


def _database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "backfill.db")
    database.initialize()
    return database


def _option(connection, account, vendor_item, seller_product, **fields):
    connection.execute(
        "INSERT INTO coupang_catalog_options(account_code, vendor_item_id,"
        " seller_product_id, seller_product_item_id, item_name,"
        " external_vendor_sku, model_no, attributes_json, bundle_info_json)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (account, str(vendor_item), str(seller_product),
         str(int(vendor_item) + 100), fields.get("item_name", "판매 옵션"),
         fields.get("sku", ""), fields.get("model_no", ""),
         json.dumps(fields.get("attributes", [])),
         json.dumps(fields.get("bundle", {}))))


def _product(connection, account, seller_product, product_id="20001"):
    connection.execute(
        "INSERT INTO coupang_catalog_products(account_code, seller_product_id,"
        " product_id, seller_product_name, display_product_name,"
        " general_product_name, status_name, raw_status)"
        " VALUES (?, ?, ?, '상품', '상품', '상품', '승인완료', 'APPROVED')",
        (account, str(seller_product), str(product_id)))


def _legacy(connection, account, vendor_item, reason="MODEL_EVIDENCE_NOT_EXACT"):
    """A row as it was written before resolution/model_evidence_class existed."""

    connection.execute(
        "INSERT INTO coupang_product_mappings(account_code, vendor_item_id,"
        " mapping_status, model_evidence_field) VALUES (?, ?, 'NEEDS_REVIEW', ?)",
        (account, str(vendor_item), reason))


def seeded(tmp_path: Path) -> Database:
    """Three legacy rows, one of each outcome, plus two protected rows."""

    database = _database(tmp_path)
    with database.transaction() as connection:
        _product(connection, COUPANG_OJE_NS, 10001)
        # exact -> promoted
        _option(connection, COUPANG_OJE_NS, 1, 10001,
                model_no="LS32DM501EKXKR")
        _legacy(connection, COUPANG_OJE_NS, 1)
        # family -> stays a review
        _option(connection, COUPANG_OJE_NS, 2, 10001,
                item_name="M50D 32인치 모니터")
        _legacy(connection, COUPANG_OJE_NS, 2)
        # nothing model-shaped -> unresolved
        _option(connection, COUPANG_OJE_NS, 3, 10001,
                sku="internal-sku", item_name="단일상품")
        _legacy(connection, COUPANG_OJE_NS, 3)
    repository = CoupangProductMappingRepository(database)
    # Two rows the tool must never select or alter.
    repository.upsert(
        account_code=COUPANG_OJE_NS, vendor_item_id=900,
        canonical_model="32DM500", mapping_source=AUTO_EXACT,
        mapping_status=CONFIRMED, resolution=AUTO_EXACT,
        model_evidence_class=EXACT_MODEL)
    repository.upsert(
        account_code=COUPANG_OJE_PLUS, vendor_item_id=901,
        canonical_model="32DM501", mapping_source=MANUAL,
        mapping_status=CONFIRMED, resolution=MANUAL_CONFIRMED)
    return database


def rows(database: Database) -> dict[tuple[str, str], dict]:
    with database.connection() as connection:
        return {
            (row["account_code"], row["vendor_item_id"]): dict(row)
            for row in connection.execute(
                "SELECT * FROM coupang_product_mappings")
        }


# --- the classification lands where the dry run said it would -------------

def test_each_outcome_is_written_once_applied(tmp_path: Path) -> None:
    database = seeded(tmp_path)
    report = backfill.classify(database, apply=True)

    assert report["selected"] == 3
    assert report["reclassified"] == 3
    assert report["unclassifiable"] == 0
    assert report["resolutions"] == {
        AUTO_EXACT: 1, MANUAL_REQUIRED: 1, UNRESOLVED: 1}
    assert report["model_evidence_classes"] == {
        EXACT_MODEL: 1, FAMILY_ONLY: 1, NO_MODEL_EVIDENCE: 1}

    stored = rows(database)
    exact = stored[(COUPANG_OJE_NS, "1")]
    assert exact["mapping_status"] == CONFIRMED
    assert exact["mapping_source"] == AUTO_EXACT
    assert exact["resolution"] == AUTO_EXACT
    assert exact["model_evidence_class"] == EXACT_MODEL
    assert exact["canonical_model"] == "32DM501"

    family = stored[(COUPANG_OJE_NS, "2")]
    assert family["mapping_status"] == NEEDS_REVIEW
    assert family["resolution"] == MANUAL_REQUIRED
    assert family["model_evidence_class"] == FAMILY_ONLY
    assert family["canonical_model"] is None
    assert family["mapping_source"] is None

    unresolved = stored[(COUPANG_OJE_NS, "3")]
    assert unresolved["resolution"] == UNRESOLVED
    assert unresolved["model_evidence_class"] == NO_MODEL_EVIDENCE
    assert unresolved["canonical_model"] is None


def test_the_default_run_writes_nothing(tmp_path: Path) -> None:
    """``--apply`` is the only thing that may change a row."""

    database = seeded(tmp_path)
    before = rows(database)
    report = backfill.classify(database, apply=False)

    assert report["applied"] is False
    assert report["selected"] == 3
    assert report["writes_captured"] == 3
    assert rows(database) == before
    assert report["legacy_remaining"] == 3


# --- the rows it must not touch -------------------------------------------

def test_confirmed_and_sourced_rows_are_not_selected_or_changed(
        tmp_path: Path) -> None:
    database = seeded(tmp_path)
    before = rows(database)
    backfill.classify(database, apply=True)
    after = rows(database)

    for key in ((COUPANG_OJE_NS, "900"), (COUPANG_OJE_PLUS, "901")):
        assert after[key] == before[key], key
    assert backfill.classify(database, apply=False)["protected_unchanged"]


def test_a_previously_confirmed_row_that_moved_is_reported(
        tmp_path: Path) -> None:
    """The guard has to fail when it should, not only pass when it should.

    Asserted by moving a protected row behind the tool's back between the two
    fingerprints, which is the only way to reach the failure branch without
    breaking the tool itself.
    """

    database = seeded(tmp_path)
    with database.connection() as connection:
        snapshot = backfill.protected_fingerprint(connection)
    CoupangProductMappingRepository(database).upsert(
        account_code=COUPANG_OJE_NS, vendor_item_id=900,
        canonical_model="32DM500", mapping_source=MANUAL,
        mapping_status=CONFIRMED, resolution=MANUAL_CONFIRMED)
    with database.connection() as connection:
        moved = backfill.protected_fingerprint(connection)

    survived, damaged = backfill._unchanged(snapshot, moved)
    assert survived is False
    assert damaged and "900" in damaged[0]


def test_classify_reports_a_damaged_protected_row(tmp_path: Path, monkeypatch) -> None:
    """The comparison has to reach the report, not just exist.

    ``_unchanged`` returning False is worth nothing if ``classify`` does not
    read it: hardcoding ``survived = True`` left every other test in this file
    green. So the two fingerprints are made to disagree and the report must
    say so.
    """

    database = seeded(tmp_path)
    real = backfill.protected_fingerprint
    calls = {"n": 0}

    def shifting(connection):
        calls["n"] += 1
        snapshot = real(connection)
        if calls["n"] > 1 and snapshot:
            key = sorted(snapshot)[0]
            snapshot[key] = ("SOMETHING-ELSE", MANUAL, CONFIRMED)
        return snapshot

    monkeypatch.setattr(backfill, "protected_fingerprint", shifting)
    report = backfill.classify(database, apply=False)

    assert report["protected_unchanged"] is False
    assert report["protected_damaged"]
    assert any("previously CONFIRMED" in problem
               for problem in backfill.verify(report, expect=None))


# --- running it twice ------------------------------------------------------

def test_a_second_run_selects_nothing(tmp_path: Path) -> None:
    """A review row keeps mapping_source NULL, so ``resolution`` is the marker."""

    database = seeded(tmp_path)
    first = backfill.classify(database, apply=True)
    assert first["selected"] == 3
    assert first["legacy_remaining"] == 0

    after_first = rows(database)
    second = backfill.classify(database, apply=True)
    assert second["selected"] == 0
    assert second["reclassified"] == 0
    assert rows(database) == after_first


# --- it cannot reach Coupang ----------------------------------------------

def test_a_row_without_a_stored_option_is_unclassifiable_not_fetched(
        tmp_path: Path) -> None:
    """The absent option must be reported, never fetched from Coupang."""

    database = _database(tmp_path)
    with database.transaction() as connection:
        _legacy(connection, COUPANG_OJE_NS, 7)
    report = backfill.classify(database, apply=True)

    assert report["selected"] == 1
    assert report["reclassified"] == 0
    assert report["unclassifiable"] == 1
    assert report["unclassifiable_detail"][0]["why"] == "NO_CATALOG_OPTION"
    assert rows(database)[(COUPANG_OJE_NS, "7")]["resolution"] is None


def test_the_read_client_refuses(tmp_path: Path) -> None:
    with pytest.raises(backfill.CoupangApiForbidden):
        backfill._RefusingClient().get_seller_product(10001)


def test_the_services_it_builds_carry_the_refusing_client(
        tmp_path: Path) -> None:
    database = seeded(tmp_path)
    services = backfill.build_services(
        [COUPANG_OJE_NS], CoupangProductMappingRepository(database))
    service = services[COUPANG_OJE_NS]
    assert isinstance(service.read_client, backfill._RefusingClient)
    # And the two catalog lookups are held rather than recomputed.
    assert service._known_models() is service._known_models()
    assert service._aliases() is service._aliases()


# --- the verification the operator reads ----------------------------------

def test_verify_passes_a_clean_apply(tmp_path: Path) -> None:
    database = seeded(tmp_path)
    report = backfill.classify(database, apply=True)
    assert backfill.verify(report, expect=3) == []


def test_verify_rejects_a_wrong_population(tmp_path: Path) -> None:
    database = seeded(tmp_path)
    report = backfill.classify(database, apply=False)
    problems = backfill.verify(report, expect=807)
    assert any("expected 807" in problem for problem in problems)


def test_verify_rejects_leftovers_after_apply(tmp_path: Path) -> None:
    database = seeded(tmp_path)
    report = backfill.classify(database, apply=True)
    report["legacy_remaining"] = 5
    assert any("remaining" in problem
               for problem in backfill.verify(report, expect=None))


def test_verify_rejects_unclassifiable_rows(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with database.transaction() as connection:
        _legacy(connection, COUPANG_OJE_NS, 7)
    report = backfill.classify(database, apply=False)
    assert any("unclassifiable" in problem
               for problem in backfill.verify(report, expect=1))


# --- the command line ------------------------------------------------------

def test_the_default_command_is_a_dry_run() -> None:
    arguments = backfill.build_parser().parse_args([])
    assert arguments.apply is False


def test_apply_is_explicit() -> None:
    assert backfill.build_parser().parse_args(["--apply"]).apply is True


def test_main_returns_non_zero_when_the_population_is_unexpected(
        tmp_path: Path, capsys) -> None:
    database = seeded(tmp_path)
    code = backfill.main([
        "--database", str(database.path), "--expect", "807"])
    assert code == 1
    assert "FAILED" in capsys.readouterr().out


def test_main_dry_run_returns_zero_and_writes_nothing(
        tmp_path: Path, capsys) -> None:
    database = seeded(tmp_path)
    before = rows(database)
    code = backfill.main(["--database", str(database.path), "--expect", "3"])
    assert code == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert rows(database) == before


def test_the_selector_requires_all_three_clauses() -> None:
    """Each clause is load-bearing; the third is what makes a re-run empty."""

    assert "mapping_source IS NULL" in backfill.LEGACY_WHERE
    assert "mapping_status = 'NEEDS_REVIEW'" in backfill.LEGACY_WHERE
    assert "resolution IS NULL" in backfill.LEGACY_WHERE
