"""Four outcomes, four evidence classes, and nothing guessed in between.

The mapping layer already confirmed exact models and sent everything else to
review. What it could not say was *why*: in the live store 805 of the 807
review rows read MODEL_EVIDENCE_NOT_EXACT, which does not distinguish an option
whose title names a family a person can resolve from one that carries no model
text at all. It also could not say which route reached a confirmed model, so a
vendor shorthand only the alias table understands was recorded exactly like a
full model code.

So each mapping now carries two fields. ``model_evidence_class`` is what the
option's text amounted to -- EXACT_MODEL, FAMILY_ONLY, AMBIGUOUS,
NO_MODEL_EVIDENCE. ``resolution`` is what was done about it -- AUTO_EXACT,
AUTO_ALIAS, MANUAL_REQUIRED, UNRESOLVED, MANUAL_CONFIRMED. Two fields because
the same class resolves differently: an EXACT_MODEL inside a bundle listing is
still MANUAL_REQUIRED.

The rule these tests exist to protect is that a family is never resolved to a
model. "M50D 32" could be 32DM501 or 32DM500 and "D400 22/24" could be 22D400
or 24D400; both are MANUAL_REQUIRED, and a size that happens to be unique does
not make either of them automatic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from config import COUPANG_OJE_NS, COUPANG_OJE_PLUS
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
from repositories.database import Database
from repositories.product_catalog_repository import (
    ProductCatalogRepository,
    canonical_model_identity,
)
from services.coupang_product_mapping_service import CoupangProductMappingService


class FakeProductClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls = 0

    def get_seller_product(self, seller_product_id: object) -> dict[str, Any]:
        self.calls += 1
        return self.payload


def product(*items: dict[str, Any]) -> dict[str, Any]:
    return {"code": "SUCCESS", "data": {
        "sellerProductId": 10001, "productId": 20001, "items": list(items)}}


def item(
    vendor_item_id: int,
    *,
    model_no: str = "",
    sku: str = "",
    item_name: str = "판매 옵션",
    attributes: list[dict[str, str]] | None = None,
    bundle: object = None,
) -> dict[str, Any]:
    result = {
        "vendorItemId": vendor_item_id,
        "sellerProductItemId": vendor_item_id + 100,
        "itemName": item_name,
        "modelNo": model_no,
        "externalVendorSku": sku,
        "attributes": attributes or [],
    }
    if bundle is not None:
        result["bundleInfo"] = bundle
    return result


def catalog(tmp_path: Path) -> ProductCatalogRepository:
    """A catalog whose aliases cover both routes to a model.

    ``LS32DM501EKXKR`` is a notation variant the rules read without help.
    ``BE50D`` is vendor shorthand: notation alone leaves it as ``BE50D`` and
    only the alias row knows it is ``50BED``. The pair is what separates
    AUTO_EXACT from AUTO_ALIAS.
    """

    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({
        "MODEL_CATALOG": {
            "S32DM501": {"model": "S32DM501"},
            "S32DM500": {"model": "S32DM500"},
            "S27FM501": {"model": "S27FM501"},
            "S27FM500": {"model": "S27FM500"},
            "S22D400": {"model": "S22D400"},
            "S24D400": {"model": "S24D400"},
            "LH50BEDH": {"model": "LH50BEDHLGFXKR"},
        },
        "MODEL_ALIASES": {
            "LS32DM501EKXKR": "S32DM501",
            "LS32DM500EKXKR": "S32DM500",
            "LS22D400GAKXKR": "S22D400",
            "LS24D400GAKXKR": "S24D400",
            "BE50D": "LH50BEDH",
        },
        "PRODUCT_KNOWLEDGE": {"model_facts": []},
    }), encoding="utf-8")
    return ProductCatalogRepository(path)


def service(
    tmp_path: Path, client: FakeProductClient, account: str = COUPANG_OJE_NS,
) -> tuple[CoupangProductMappingService, CoupangProductMappingRepository]:
    database = Database(tmp_path / "mapping.db")
    database.initialize()
    repository = CoupangProductMappingRepository(database)
    return (
        CoupangProductMappingService(
            account_code=account,
            read_client=client,  # type: ignore[arg-type]
            repository=repository,
            catalog_repository=catalog(tmp_path),
        ),
        repository,
    )


def resolve(tmp_path: Path, *items: dict[str, Any], vendor_item_id: int = 1):
    mapping_service, repository = service(tmp_path, FakeProductClient(product(*items)))
    result = mapping_service.resolve(
        seller_product_id=10001, vendor_item_id=vendor_item_id)
    return result, repository


# --- AUTO_EXACT -----------------------------------------------------------

def test_a_full_model_code_is_auto_exact(tmp_path: Path) -> None:
    result, _ = resolve(tmp_path, item(1, model_no="LS32DM501EKXKR"))
    assert result.model_evidence_class == EXACT_MODEL
    assert result.resolution == AUTO_EXACT
    assert result.mapping["mapping_status"] == CONFIRMED
    assert result.mapping["mapping_source"] == AUTO_EXACT
    assert result.mapping["canonical_model"] == "32DM501"


@pytest.mark.parametrize("stated", ["LS32DM501EKXKR", "S32DM501", "32DM501"])
def test_every_notation_of_one_model_is_auto_exact_and_agrees(
        tmp_path: Path, stated: str) -> None:
    """The canonicalization rule, exercised through the mapping layer."""

    result, _ = resolve(tmp_path, item(1, model_no=stated))
    assert result.resolution == AUTO_EXACT
    assert result.mapping["canonical_model"] == "32DM501"


# --- AUTO_ALIAS -----------------------------------------------------------

def test_vendor_shorthand_only_the_alias_table_knows_is_auto_alias(
        tmp_path: Path) -> None:
    """``BE50D`` is not a notation of ``50BED``; the alias row is the evidence."""

    result, _ = resolve(tmp_path, item(1, model_no="BE50D"))
    assert result.model_evidence_class == EXACT_MODEL
    assert result.resolution == AUTO_ALIAS
    assert result.mapping["mapping_source"] == AUTO_ALIAS
    assert result.mapping["mapping_status"] == CONFIRMED
    assert result.mapping["canonical_model"] == "50BED"


def test_the_two_automatic_routes_are_told_apart_not_merged(tmp_path: Path) -> None:
    """Both confirm, and the record says which one did it."""

    exact, repository = resolve(tmp_path, item(1, model_no="LS32DM501EKXKR"))
    mapping_service = CoupangProductMappingService(
        account_code=COUPANG_OJE_NS,
        read_client=FakeProductClient(product(item(2, model_no="BE50D"))),  # type: ignore[arg-type]
        repository=repository, catalog_repository=catalog(tmp_path))
    alias = mapping_service.resolve(seller_product_id=10001, vendor_item_id=2)
    assert (exact.resolution, alias.resolution) == (AUTO_EXACT, AUTO_ALIAS)
    assert exact.mapping["canonical_model"] != alias.mapping["canonical_model"]


# --- FAMILY_ONLY -> MANUAL_REQUIRED --------------------------------------

@pytest.mark.parametrize("stated_in, text", [
    ("item_name", "M50D 32형 삼탠바이미"),
    ("item_name", "D400 22/24 모니터"),
    ("item_name", "M50F 27 스마트모니터"),
    ("model_no", "M50D"),
    ("sku", "G50D"),
])
def test_a_family_is_never_resolved_to_a_model(
        tmp_path: Path, stated_in: str, text: str) -> None:
    """Two models answer to each of these, so a person decides which."""

    result, _ = resolve(tmp_path, item(1, **{stated_in: text}))
    assert result.model_evidence_class == FAMILY_ONLY
    assert result.resolution == MANUAL_REQUIRED
    assert result.mapping["mapping_status"] == NEEDS_REVIEW
    assert result.mapping["canonical_model"] is None
    assert result.mapping["mapping_source"] is None


def test_a_unique_size_does_not_make_a_family_automatic(tmp_path: Path) -> None:
    """"M50D 32" narrows to one size and still names two models."""

    result, _ = resolve(tmp_path, item(1, item_name="M50D 32인치"))
    assert result.resolution == MANUAL_REQUIRED
    assert result.mapping["canonical_model"] is None


# --- AMBIGUOUS -> MANUAL_REQUIRED ----------------------------------------

def test_two_stated_models_are_ambiguous(tmp_path: Path) -> None:
    result, _ = resolve(tmp_path, item(1, model_no="32DM500", sku="32DM501"))
    assert result.model_evidence_class == AMBIGUOUS
    assert result.resolution == MANUAL_REQUIRED
    assert result.reason == "MODEL_EVIDENCE_CONFLICT"
    assert result.mapping["canonical_model"] is None


def test_a_model_designated_attribute_that_disagrees_is_ambiguous(
        tmp_path: Path) -> None:
    result, _ = resolve(tmp_path, item(1, model_no="32DM501", attributes=[
        {"attributeTypeName": "모델명", "attributeValueName": "32DM500"}]))
    assert result.model_evidence_class == AMBIGUOUS
    assert result.resolution == MANUAL_REQUIRED


def test_a_vendor_item_that_is_not_one_option_is_ambiguous(tmp_path: Path) -> None:
    result, _ = resolve(
        tmp_path,
        item(1, model_no="LS32DM500EKXKR"), item(1, model_no="LS32DM501EKXKR"))
    assert result.reason == "VENDOR_ITEM_NOT_UNIQUE"
    assert result.model_evidence_class == AMBIGUOUS
    assert result.resolution == MANUAL_REQUIRED
    assert result.matching_item_count == 2


# --- NO_MODEL_EVIDENCE -> UNRESOLVED -------------------------------------

def test_no_model_text_at_all_is_unresolved(tmp_path: Path) -> None:
    """The one outcome a person cannot finish, so it is not asked of them."""

    result, _ = resolve(tmp_path, item(1, sku="internal-sku", item_name="판매 옵션"))
    assert result.model_evidence_class == NO_MODEL_EVIDENCE
    assert result.resolution == UNRESOLVED
    assert result.mapping["mapping_status"] == NEEDS_REVIEW
    assert result.mapping["canonical_model"] is None


def test_an_empty_option_is_unresolved(tmp_path: Path) -> None:
    result, _ = resolve(tmp_path, item(1, item_name=""))
    assert result.model_evidence_class == NO_MODEL_EVIDENCE
    assert result.resolution == UNRESOLVED


def test_spec_noise_is_not_mistaken_for_a_family(tmp_path: Path) -> None:
    """Sizes, refresh rates and brightness are model-shaped and are not models."""

    result, _ = resolve(tmp_path, item(1, item_name="60HZ 81CM 350", attributes=[
        {"attributeTypeName": "주사율", "attributeValueName": "60HZ"},
        {"attributeTypeName": "화면크기", "attributeValueName": "81CM"}]))
    assert result.model_evidence_class == NO_MODEL_EVIDENCE
    assert result.resolution == UNRESOLVED


# --- MANUAL_CONFIRMED ----------------------------------------------------

def test_a_manual_mapping_records_why_it_needed_a_person(tmp_path: Path) -> None:
    """The class that sent it to review survives the human decision."""

    mapping_service, repository = service(
        tmp_path, FakeProductClient(product(item(1, item_name="M50D 32인치"))))
    review = mapping_service.resolve(seller_product_id=10001, vendor_item_id=1)
    assert review.model_evidence_class == FAMILY_ONLY

    saved = mapping_service.save_manual_mapping(
        vendor_item_id=1, canonical_model="32DM501")
    assert saved["mapping_status"] == CONFIRMED
    assert saved["mapping_source"] == MANUAL
    assert saved["resolution"] == MANUAL_CONFIRMED
    assert saved["model_evidence_class"] == FAMILY_ONLY
    assert saved["canonical_model"] == "32DM501"
    assert repository.get_confirmed(
        account_code=COUPANG_OJE_NS, vendor_item_id=1) is not None


def test_a_manual_mapping_still_refuses_an_unknown_model(tmp_path: Path) -> None:
    mapping_service, _ = service(tmp_path, FakeProductClient(product(item(1))))
    with pytest.raises(ValueError, match="known exact catalog"):
        mapping_service.save_manual_mapping(
            vendor_item_id=1, canonical_model="M50D 32")


# --- options must not collapse into one model ----------------------------

def test_sibling_sizes_stay_separate_models(tmp_path: Path) -> None:
    """22D400 and 24D400 are one family and two products."""

    mapping_service, repository = service(tmp_path, FakeProductClient(product(
        item(12345, model_no="LS22D400GAKXKR"),
        item(12346, model_no="LS24D400GAKXKR"))))
    first = mapping_service.resolve(seller_product_id=10001, vendor_item_id=12345)
    second = mapping_service.resolve(seller_product_id=10001, vendor_item_id=12346)
    assert first.mapping["canonical_model"] == "22D400"
    assert second.mapping["canonical_model"] == "24D400"
    assert first.mapping["canonical_model"] != second.mapping["canonical_model"]


def test_sibling_generations_stay_separate_models(tmp_path: Path) -> None:
    """DM500 and DM501 differ by one digit and are not the same product."""

    mapping_service, _ = service(tmp_path, FakeProductClient(product(
        item(1, model_no="LS32DM500EKXKR"), item(2, model_no="LS32DM501EKXKR"))))
    assert mapping_service.resolve(
        seller_product_id=10001, vendor_item_id=1
    ).mapping["canonical_model"] == "32DM500"
    assert mapping_service.resolve(
        seller_product_id=10001, vendor_item_id=2
    ).mapping["canonical_model"] == "32DM501"


def test_the_same_vendor_item_in_two_accounts_does_not_share_a_model(
        tmp_path: Path) -> None:
    client = FakeProductClient(product(item(555, model_no="LS32DM501EKXKR")))
    ns, repository = service(tmp_path, client, COUPANG_OJE_NS)
    ns.resolve(seller_product_id=10001, vendor_item_id=555)
    plus = CoupangProductMappingService(
        account_code=COUPANG_OJE_PLUS, read_client=client,  # type: ignore[arg-type]
        repository=repository, catalog_repository=catalog(tmp_path))
    plus.save_manual_mapping(vendor_item_id=555, canonical_model="32DM500")
    assert repository.get(
        account_code=COUPANG_OJE_NS, vendor_item_id=555)["canonical_model"] == "32DM501"
    assert repository.get(
        account_code=COUPANG_OJE_PLUS, vendor_item_id=555)["canonical_model"] == "32DM500"


# --- the stored canonical form is today's canonical form -----------------

def test_a_confirmed_mapping_stores_an_already_canonical_model(
        tmp_path: Path) -> None:
    """Re-canonicalizing a stored value must be a no-op.

    The live store has 56 confirmed rows whose ``canonical_model`` is a full
    code -- ``LH50BEHHLGFXKR`` where the rule now yields ``50BEH`` -- because
    the canonicalizer learned the LH business-display notation after those rows
    were written and nothing re-read them. They still answer correctly, since
    the catalog matcher resolves both spellings to one key, but the column no
    longer holds what it claims to. This pins the property for new writes.
    """

    aliases = dict(catalog(tmp_path).catalog().get("aliases") or {})
    for stated in ("LS32DM501EKXKR", "BE50D", "32DM500"):
        result, _ = resolve(tmp_path, item(1, model_no=stated))
        stored = result.mapping["canonical_model"]
        assert stored is not None
        assert canonical_model_identity(stored, aliases=aliases) == stored, stated


# --- the two fields cannot disagree --------------------------------------

def test_the_repository_refuses_a_review_resolution_on_a_confirmed_row(
        tmp_path: Path) -> None:
    database = Database(tmp_path / "mapping.db")
    database.initialize()
    repository = CoupangProductMappingRepository(database)
    with pytest.raises(ValueError, match="cannot be CONFIRMED"):
        repository.upsert(
            account_code=COUPANG_OJE_NS, vendor_item_id=1,
            canonical_model="32DM501", mapping_source=AUTO_EXACT,
            mapping_status=CONFIRMED, resolution=MANUAL_REQUIRED)


def test_the_repository_refuses_a_confirming_resolution_on_a_review_row(
        tmp_path: Path) -> None:
    database = Database(tmp_path / "mapping.db")
    database.initialize()
    repository = CoupangProductMappingRepository(database)
    with pytest.raises(ValueError, match="cannot be NEEDS_REVIEW"):
        repository.upsert(
            account_code=COUPANG_OJE_NS, vendor_item_id=1,
            mapping_status=NEEDS_REVIEW, resolution=AUTO_EXACT)


@pytest.mark.parametrize("field, value", [
    ("model_evidence_class", "PROBABLY_FINE"),
    ("resolution", "GOOD_ENOUGH"),
])
def test_the_repository_refuses_an_invented_vocabulary(
        tmp_path: Path, field: str, value: str) -> None:
    database = Database(tmp_path / "mapping.db")
    database.initialize()
    repository = CoupangProductMappingRepository(database)
    with pytest.raises(ValueError, match="Invalid"):
        repository.upsert(
            account_code=COUPANG_OJE_NS, vendor_item_id=1,
            mapping_status=NEEDS_REVIEW, **{field: value})


def test_migration_38_adds_both_columns_without_touching_the_check(
        tmp_path: Path) -> None:
    database = Database(tmp_path / "mapping.db")
    database.initialize()
    with database.connection() as connection:
        columns = {
            row[1] for row in
            connection.execute("PRAGMA table_info(coupang_product_mappings)")
        }
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table'"
            " AND name='coupang_product_mappings'").fetchone()[0]
    assert {"model_evidence_class", "resolution"} <= columns
    # The status CHECK is unchanged, which is what keeps this an ALTER.
    assert "'CONFIRMED','NEEDS_REVIEW'" in sql.replace(" ", "")
