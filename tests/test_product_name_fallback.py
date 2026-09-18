"""Reading the product name of a row that was stored without one.

Coupang rows written before 1b39c35 recorded no product, because the payload
carries only a sellerProductId and the name is attached when the row is read
for display.  Those rows are not rewritten -- this resolves the name at read
time instead, and a stored name always wins.

Every test asserts the database was not written to.
"""

from __future__ import annotations

from typing import Any

import pytest

from repositories.coupang_product_catalog_repository import (
    CoupangProductCatalogRepository,
)
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.workflow_repository import WorkflowRepository
from services.coupang_inquiry_normalizer import CoupangInquiryNormalizer
from services.inquiry_sync_service import normalize_work_item
from ui.product_name_presenter import ProductNameResolver

SPID = "15654321531"
VENDOR_ITEM = "93128932886"
NS_NAME = "삼성 스마트모니터 M5 M50D 32인치 화이트"
PLUS_NAME = "삼성 스마트모니터 M7 M70D 43인치 블랙"
OPTION_NAME = "스탠드형 방문설치 32인치"


@pytest.fixture
def database(tmp_path) -> Database:
    value = Database(tmp_path / "fallback.db")
    value.initialize()
    return value


def seed_catalog(
    database: Database, *, account: str = "OJE_NS", product_name: str = NS_NAME,
) -> None:
    catalog = CoupangProductCatalogRepository(database)
    catalog.upsert_product(
        account_code=account,
        data={
            "sellerProductId": SPID, "status": "APPROVED",
            "sellerProductName": product_name,
        },
        sync_token="t",
    )
    catalog.upsert_option(
        account_code=account, seller_product_id=SPID,
        item={"vendorItemId": VENDOR_ITEM, "itemName": OPTION_NAME},
    )


def coupang_inquiry(
    database: Database, *, account: str = "OJE_NS", inquiry_id: str = "160959847",
) -> int:
    payload = {
        "inquiryId": inquiry_id, "sellerProductId": SPID,
        "vendorItemId": VENDOR_ITEM, "content": "스피커 내장되어 있나요?",
        "inquiryAt": "2026-09-17T10:00:00+09:00", "orderIds": [],
        "commentDtoList": [],
    }
    ready = normalize_work_item(
        CoupangInquiryNormalizer().online(payload, account_code=account).to_work_item()
    )
    row_id = InquiryRepository(database).upsert_work_item(ready).inquiry_id
    WorkflowRepository(database).initialize_steps(row_id)
    return row_id


def naver_inquiry(database: Database, *, product_name: str = "삼성 M5 32인치") -> int:
    row_id = InquiryRepository(database).upsert_work_item({
        "store_code": "OJE_PLUS", "source_type": "PRODUCT_INQUIRY",
        "source_question_id": "N-1", "inquiry_type": "상품",
        "title": "상품 문의", "content": "스피커 있나요?",
        "product_name": product_name, "option_name": "32인치",
        "raw_json": {},
    }).inquiry_id
    WorkflowRepository(database).initialize_steps(row_id)
    return row_id


def snapshot(database: Database) -> dict[str, Any]:
    """Everything the resolver must not change."""

    with database.connection() as connection:
        return {
            table: connection.execute(
                f"SELECT COUNT(*), COALESCE(SUM(LENGTH(COALESCE(product_name,''))),0)"
                f" FROM {table}"
            ).fetchone()[:]
            for table in ("inquiries", "learning_examples", "historical_cases")
        }


def coupang_source(database: Database, inquiry_id: int) -> dict[str, Any]:
    row = InquiryRepository(database).get(inquiry_id) or {}
    return {
        "store_code": row.get("store_code"),
        "source_type": row.get("source_type"),
        "source_question_id": row.get("source_question_id"),
    }


# --- A. a stored name always wins -----------------------------------------------

def test_a_stored_name_is_used_unchanged(database) -> None:
    seed_catalog(database)
    inquiry_id = coupang_inquiry(database)

    name = ProductNameResolver(database).resolve(
        stored="저장된 상품명",
        metadata={"product_identity": {"product_name": "메타데이터 상품명"}},
        **coupang_source(database, inquiry_id),
    )

    assert name == "저장된 상품명"


def test_a_stored_name_is_not_overwritten_by_the_marketplace(database) -> None:
    seed_catalog(database)
    inquiry_id = coupang_inquiry(database)

    assert ProductNameResolver(database).resolve(
        stored="A", metadata={}, **coupang_source(database, inquiry_id),
    ) == "A"


# --- B. the row's own metadata comes next ---------------------------------------

def test_metadata_names_the_product_when_the_column_is_empty(database) -> None:
    seed_catalog(database)
    inquiry_id = coupang_inquiry(database)

    name = ProductNameResolver(database).resolve(
        stored=None,
        metadata={"product_identity": {"product_name": "메타데이터 상품명"}},
        **coupang_source(database, inquiry_id),
    )

    assert name == "메타데이터 상품명"


def test_a_blank_column_and_blank_metadata_fall_through(database) -> None:
    seed_catalog(database)
    inquiry_id = coupang_inquiry(database)

    name = ProductNameResolver(database).resolve(
        stored="   ",
        metadata={"product_identity": {"product_name": ""}},
        **coupang_source(database, inquiry_id),
    )

    assert name == NS_NAME


# --- C/D. the marketplace enrichment is the last resort -------------------------

def test_the_marketplace_names_a_row_that_stored_nothing(database) -> None:
    seed_catalog(database)
    inquiry_id = coupang_inquiry(database)

    name = ProductNameResolver(database).resolve(
        stored=None, metadata=None, **coupang_source(database, inquiry_id),
    )

    assert name == NS_NAME
    # The inquiry row itself still stores no name; it was resolved on read.
    raw = InquiryRepository(database).get(inquiry_id)
    assert not str(raw.get("product_name") or "").strip()


# --- E/F. nothing is invented ---------------------------------------------------

def test_without_any_evidence_the_dash_remains(database) -> None:
    inquiry_id = coupang_inquiry(database)  # catalogue not seeded

    resolver = ProductNameResolver(database)
    source = coupang_source(database, inquiry_id)

    assert resolver.resolve(stored=None, metadata=None, **source) == ""
    assert resolver.for_display(stored=None, metadata=None, **source) == "-"


def test_no_id_model_or_option_is_shown_as_a_product_name(database) -> None:
    inquiry_id = coupang_inquiry(database)
    source = coupang_source(database, inquiry_id)

    shown = ProductNameResolver(database).for_display(
        stored=None,
        metadata={
            "canonical_model": "32DM501",
            "model_code": "32DM501",
            "market_provenance": {
                "seller_product_id": SPID, "vendor_item_id": VENDOR_ITEM,
            },
            "option_name": OPTION_NAME,
        },
        **source,
    )

    assert shown == "-"
    for forbidden in ("32DM501", SPID, VENDOR_ITEM, OPTION_NAME):
        assert forbidden not in shown


def test_an_unidentifiable_source_resolves_to_nothing(database) -> None:
    resolver = ProductNameResolver(database)

    assert resolver.resolve(stored=None, metadata=None) == ""
    assert resolver.resolve(
        stored=None, metadata=None, store_code="COUPANG_OJE_NS",
    ) == ""


# --- G. the two Coupang accounts never mix --------------------------------------

def test_one_account_never_borrows_the_others_product_name(database) -> None:
    seed_catalog(database, account="OJE_NS", product_name=NS_NAME)
    seed_catalog(database, account="OJE_PLUS", product_name=PLUS_NAME)
    ns = coupang_inquiry(database, account="OJE_NS", inquiry_id="17000040")
    plus = coupang_inquiry(database, account="OJE_PLUS", inquiry_id="17000041")
    resolver = ProductNameResolver(database)

    assert resolver.resolve(
        stored=None, metadata=None, **coupang_source(database, ns),
    ) == NS_NAME
    assert resolver.resolve(
        stored=None, metadata=None, **coupang_source(database, plus),
    ) == PLUS_NAME


def test_an_account_with_no_catalogue_gets_no_name(database) -> None:
    seed_catalog(database, account="OJE_NS")
    plus = coupang_inquiry(database, account="OJE_PLUS", inquiry_id="17000042")

    assert ProductNameResolver(database).resolve(
        stored=None, metadata=None, **coupang_source(database, plus),
    ) == ""


# --- H. Naver is not re-read at all ---------------------------------------------

def test_a_naver_row_keeps_exactly_what_it_stored(database) -> None:
    inquiry_id = naver_inquiry(database)
    source = coupang_source(database, inquiry_id)

    assert ProductNameResolver(database).resolve(
        stored="네이버 저장 상품명", metadata=None, **source,
    ) == "네이버 저장 상품명"
    # And a Naver row with nothing stored is never re-read through the
    # marketplace path; it simply has no name here.
    assert ProductNameResolver(database).resolve(
        stored=None, metadata=None, **source,
    ) == ""


def test_the_naver_path_performs_no_marketplace_lookup(database, monkeypatch) -> None:
    inquiry_id = naver_inquiry(database)
    resolver = ProductNameResolver(database)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a Naver row must not be re-read")

    monkeypatch.setattr(resolver.inquiries, "get_by_source", forbidden)

    assert resolver.resolve(
        stored=None, metadata=None, **coupang_source(database, inquiry_id),
    ) == ""


# --- N+1: one read per source ---------------------------------------------------

def test_rows_from_one_inquiry_are_read_once(database) -> None:
    seed_catalog(database)
    inquiry_id = coupang_inquiry(database)
    resolver = ProductNameResolver(database)
    source = coupang_source(database, inquiry_id)
    calls: list[tuple] = []
    original = resolver.inquiries.get_by_source

    def counting(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    resolver.inquiries.get_by_source = counting

    for _ in range(5):
        assert resolver.resolve(stored=None, metadata=None, **source) == NS_NAME

    assert len(calls) == 1


def test_a_source_with_no_name_is_not_re_read_either(database) -> None:
    inquiry_id = coupang_inquiry(database)
    resolver = ProductNameResolver(database)
    source = coupang_source(database, inquiry_id)
    calls: list[tuple] = []
    original = resolver.inquiries.get_by_source

    def counting(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    resolver.inquiries.get_by_source = counting

    for _ in range(4):
        assert resolver.resolve(stored=None, metadata=None, **source) == ""

    assert len(calls) == 1


def test_a_repository_failure_does_not_break_the_screen(database) -> None:
    inquiry_id = coupang_inquiry(database)
    resolver = ProductNameResolver(database)

    def exploding(*_args, **_kwargs):
        raise RuntimeError("catalogue unavailable")

    resolver.inquiries.get_by_source = exploding

    assert resolver.for_display(
        stored=None, metadata=None, **coupang_source(database, inquiry_id),
    ) == "-"


# --- I. nothing is written ------------------------------------------------------

def test_resolving_writes_nothing(database) -> None:
    seed_catalog(database)
    inquiry_id = coupang_inquiry(database)
    naver_inquiry(database)
    before = snapshot(database)

    resolver = ProductNameResolver(database)
    source = coupang_source(database, inquiry_id)
    for _ in range(3):
        resolver.resolve(stored=None, metadata=None, **source)
        resolver.resolve(stored="A", metadata=None, **source)
        resolver.for_display(stored=None, metadata=None)

    assert snapshot(database) == before


# --- J. no external call --------------------------------------------------------

def test_nothing_reaches_the_network(database, monkeypatch) -> None:
    import requests

    def forbidden(*_args, **_kwargs):
        raise AssertionError("no request may be made")

    for name in ("request", "get", "post"):
        monkeypatch.setattr(requests, name, forbidden)

    seed_catalog(database)
    inquiry_id = coupang_inquiry(database)

    assert ProductNameResolver(database).resolve(
        stored=None, metadata=None, **coupang_source(database, inquiry_id),
    ) == NS_NAME


# --- the two screens actually show it -------------------------------------------

def _historical_case(database: Database, *, inquiry_id: int, product_name=None) -> int:
    """A Historical case exactly as the old backfill wrote one: no name."""

    from repositories.historical_case_repository import HistoricalCaseRepository

    row = InquiryRepository(database).get(inquiry_id) or {}
    case, _ = HistoricalCaseRepository(database).upsert({
        "source": "COUPANG_ONLINE_HISTORY",
        "store_code": row.get("store_code"),
        "inquiry_id": inquiry_id,
        "external_inquiry_id": row.get("source_question_id"),
        "inquiry_type": row.get("source_type"),
        "question": "스피커 내장되어 있나요?",
        "question_normalized": "스피커 내장",
        "seller_answer": "네, 내장되어 있습니다.",
        "product_name": product_name,
        "product_id": None, "order_reference": None,
        "source_answered": True,
        "inquiry_created_at": "2026-09-17T10:00:00+09:00",
        "answer_updated_at": "2026-09-17T11:00:00+09:00",
        "source_payload_reference": "ref", "raw_json": {},
        "classification": "상품", "policy_risk": "NONE",
        "quality_score": 0.7, "confidence": 0.7, "active": True,
        "metadata_json": {"market": "COUPANG"},
        "case_key": f"ck-{inquiry_id}", "fingerprint": f"fp-{inquiry_id}",
    })
    return int(case["id"])


def _render_historical(database: Database) -> str:
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_string(f"""
from repositories.database import Database
from ui.historical_case_manager import render_historical_case_manager
render_historical_case_manager(Database(r"{database.path}"))
""").run(timeout=60)
    assert not app.exception, app.exception
    return "\n".join(
        str(element.value)
        for element in [*app.markdown, *app.caption, *app.info, *app.warning]
    )


def test_the_historical_detail_screen_shows_the_recovered_name(database) -> None:
    seed_catalog(database)
    inquiry_id = coupang_inquiry(database)
    _historical_case(database, inquiry_id=inquiry_id)
    before = snapshot(database)

    text = _render_historical(database)

    assert NS_NAME in text
    assert "상품: -" not in text
    # Reading the screen wrote nothing.
    assert snapshot(database) == before


def test_the_historical_screen_keeps_a_stored_name(database) -> None:
    seed_catalog(database)
    inquiry_id = coupang_inquiry(database)
    _historical_case(database, inquiry_id=inquiry_id, product_name="저장된 상품명")

    text = _render_historical(database)

    assert "저장된 상품명" in text
    assert NS_NAME not in text


def test_the_historical_screen_still_shows_a_dash_without_evidence(database) -> None:
    inquiry_id = coupang_inquiry(database)  # no catalogue
    _historical_case(database, inquiry_id=inquiry_id)

    assert "상품: -" in _render_historical(database)
