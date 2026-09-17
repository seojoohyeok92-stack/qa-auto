"""What a stored Coupang inquiry shows on screen.

A Coupang inquiry carries a seller product id and a vendor item id but no
product name, no option name and no answer body -- those live in the stored
catalogue and in the stored comment list.  None of it is fetched again to draw
a page.

The catalogue lookups deliberately ignore whether a listing is still selling.
This names what a past customer asked about, and a product that stopped
selling still had a name; the sync and matching screens keep their own
``is_active`` rules untouched.
"""

from __future__ import annotations

import pytest

from repositories.coupang_product_catalog_repository import (
    CoupangProductCatalogRepository,
)
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.coupang_inquiry_normalizer import CoupangInquiryNormalizer
from services.inquiry_sync_service import normalize_work_item
from services.market_policy import account_of_store, store_label

ACCOUNT = "OJE_NS"
STORE = "COUPANG_OJE_NS"
SPID = "15654321531"
VENDOR_ITEM = "93128932886"
REPLY = "안녕하세요 고객님 삼성서비스센터로 연락주셔야 할 부분입니다."
PRODUCT = "M50F / 모음전 / O"
OPTION = "삼성전자 스마트 모니터 M5 M50F"


def _database(tmp_path) -> Database:
    database = Database(tmp_path / "display.db")
    database.initialize()
    return database


def _store_inquiry(database: Database, account: str = ACCOUNT) -> int:
    payload = {
        "inquiryId": "160668556",
        "sellerProductId": SPID,
        "vendorItemId": VENDOR_ITEM,
        "content": "와이파이가안됩니다",
        "inquiryAt": "2026-09-16T11:00:00+09:00",
        "orderIds": [],
        "commentDtoList": [{
            "inquiryCommentId": "c-1",
            "inquiryId": "160668556",
            "content": REPLY,
            "inquiryCommentAt": "2026-09-16T11:23:00+09:00",
        }],
    }
    work_item = CoupangInquiryNormalizer().online(
        payload, account_code=account
    ).to_work_item()
    return InquiryRepository(database).upsert_work_item(
        normalize_work_item(work_item)
    ).inquiry_id


def _seed_catalog(
    database: Database, *, account: str = ACCOUNT, is_active: bool = False
) -> None:
    """Seeded inactive on purpose: that is the real row's state."""

    catalog = CoupangProductCatalogRepository(database)
    catalog.upsert_product(
        account_code=account,
        data={
            "sellerProductId": SPID,
            "status": "APPROVED",
            "sellerProductName": PRODUCT,
        },
        sync_token="test",
    )
    catalog.upsert_option(
        account_code=account,
        seller_product_id=SPID,
        item={"vendorItemId": VENDOR_ITEM, "itemName": OPTION},
    )
    catalog.set_product_active(
        account_code=account, seller_product_id=SPID, is_active=is_active
    )


def _page(database: Database) -> dict:
    rows, _, _ = InquiryRepository(database).dashboard_page(
        store_codes=[STORE], source="ALL", queues=[], priorities=[],
        answer_status="ALL", delivery_only=False, search_query="",
        start_date="2026-01-01", end_date="2026-12-31",
        kpi_filter=None, page=1, page_size=30,
    )
    return rows[0]


# --- product and option names ----------------------------------------------

@pytest.mark.parametrize("is_active", [False, True], ids=["inactive", "active"])
def test_product_name_is_shown_whether_or_not_it_still_sells(
    tmp_path, is_active
) -> None:
    database = _database(tmp_path)
    _store_inquiry(database)
    _seed_catalog(database, is_active=is_active)
    assert _page(database)["product_name"] == PRODUCT


@pytest.mark.parametrize("is_active", [False, True], ids=["inactive", "active"])
def test_option_name_is_shown_whether_or_not_it_still_sells(
    tmp_path, is_active
) -> None:
    database = _database(tmp_path)
    _store_inquiry(database)
    _seed_catalog(database, is_active=is_active)
    assert _page(database)["option_name"] == OPTION


def test_neither_name_is_borrowed_from_the_other_account(tmp_path) -> None:
    """The same ids mean different things in the other seller account."""

    database = _database(tmp_path)
    _store_inquiry(database, account=ACCOUNT)
    _seed_catalog(database, account="OJE_PLUS")
    row = _page(database)
    assert not str(row["product_name"] or "").strip()
    assert not str(row["option_name"] or "").strip()


def test_missing_catalogue_leaves_both_empty(tmp_path) -> None:
    database = _database(tmp_path)
    _store_inquiry(database)
    row = _page(database)
    assert not str(row["product_name"] or "").strip()
    assert not str(row["option_name"] or "").strip()


# --- the marketplace's own reply -------------------------------------------

def test_the_stored_reply_reaches_the_page(tmp_path) -> None:
    database = _database(tmp_path)
    _store_inquiry(database)
    assert _page(database)["seller_answer"] == REPLY


def test_an_unanswered_inquiry_has_no_reply(tmp_path) -> None:
    database = _database(tmp_path)
    payload = {
        "inquiryId": "160668557",
        "sellerProductId": SPID,
        "vendorItemId": VENDOR_ITEM,
        "content": "질문",
        "inquiryAt": "2026-09-16T11:00:00+09:00",
        "orderIds": [],
        "commentDtoList": [],
    }
    work_item = CoupangInquiryNormalizer().online(
        payload, account_code=ACCOUNT
    ).to_work_item()
    InquiryRepository(database).upsert_work_item(normalize_work_item(work_item))
    assert _page(database)["seller_answer"] is None


# --- store naming ----------------------------------------------------------

@pytest.mark.parametrize(
    "store_code,expected",
    [
        pytest.param("COUPANG_OJE_NS", "오제앤에스", id="ns"),
        pytest.param("COUPANG_OJE_PLUS", "오제플러스", id="plus"),
    ],
)
def test_a_coupang_store_is_named_by_its_seller_account(store_code, expected) -> None:
    assert store_label(store_code) == expected
    assert store_code not in store_label(store_code)


def test_a_naver_store_keeps_its_configured_name() -> None:
    assert store_label("OJE_PLUS") == "오제플러스"


def test_account_is_read_off_the_store_code() -> None:
    assert account_of_store("COUPANG_OJE_NS") == "OJE_NS"
    assert account_of_store("COUPANG_OJE_PLUS") == "OJE_PLUS"
    assert account_of_store("OJE_PLUS") is None


def test_an_unknown_store_falls_back_to_its_code(tmp_path) -> None:
    assert store_label("SOMETHING_ELSE") == "SOMETHING_ELSE"
    assert store_label(None) == "-"
