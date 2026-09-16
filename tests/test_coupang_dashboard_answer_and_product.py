"""Coupang inquiries read as what they are: answered, and about a product.

Three symptoms on the server had one cause.  7,278 rows had ``source_answered``
NULL, an empty ``raw_json`` and no product name, so inquiries the seller had
already replied to on Wing showed as waiting for review and nothing could be
joined back to the product catalogue.

A work item says ``answered`` and ``raw_payload``.  The inquiries table stores
``source_answered`` and ``raw_json``.  The rename between them lives in
``normalize_work_item``, and the backfill wrote straight past it.
"""

from __future__ import annotations

from datetime import date

import pytest

from repositories.coupang_product_catalog_repository import (
    CoupangProductCatalogRepository,
)
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.coupang_inquiry_normalizer import CoupangInquiryNormalizer
from services.inquiry_sync_service import normalize_work_item

ACCOUNT = "OJE_NS"
STORE = "COUPANG_OJE_NS"
SELLER_PRODUCT_ID = "16253809108"
SELLER_ANSWER = "안녕하세요 고객님 삼성서비스센터로 연락주셔야 할 부분입니다."


def _database(tmp_path) -> Database:
    database = Database(tmp_path / "coupang-answer.db")
    database.initialize()
    return database


def _payload(inquiry_id: str, *, answered: bool) -> dict:
    return {
        "inquiryId": inquiry_id,
        "sellerProductId": SELLER_PRODUCT_ID,
        "vendorItemId": "vi-1",
        "sellerItemId": "si-1",
        "productId": "p-1",
        "content": "와이파이가안됩니다",
        "inquiryAt": "2026-09-16T11:00:00+09:00",
        "orderIds": [],
        "commentDtoList": (
            [{
                "inquiryCommentId": f"c-{inquiry_id}",
                "inquiryId": inquiry_id,
                "content": SELLER_ANSWER,
                "inquiryCommentAt": "2026-09-16T11:23:00+09:00",
            }] if answered else []
        ),
    }


def _store(database: Database, inquiry_id: str, *, answered: bool) -> int:
    """Persist the way the backfill and the live sync both now do."""

    work_item = CoupangInquiryNormalizer().online(
        _payload(inquiry_id, answered=answered), account_code=ACCOUNT
    ).to_work_item()
    return InquiryRepository(database).upsert_work_item(
        normalize_work_item(work_item)
    ).inquiry_id


def _seed_catalog(database: Database, name: str, *, account: str = ACCOUNT) -> None:
    CoupangProductCatalogRepository(database).upsert_product(
        account_code=account,
        data={
            "sellerProductId": SELLER_PRODUCT_ID,
            "status": "APPROVED",
            "sellerProductName": name,
        },
        sync_token="test",
    )


def _page(database: Database):
    rows, _, _ = InquiryRepository(database).dashboard_page(
        store_codes=[STORE], source="ALL", queues=[], priorities=[],
        answer_status="ALL", delivery_only=False, search_query="",
        start_date="2026-01-01", end_date="2026-12-31",
        kpi_filter=None, page=1, page_size=30,
    )
    return rows


# --- the rename that was being skipped -------------------------------------

def test_an_answered_inquiry_is_stored_as_answered(tmp_path) -> None:
    database = _database(tmp_path)
    _store(database, "1608522334", answered=True)
    row = InquiryRepository(database).get_by_source(
        STORE, "COUPANG_ONLINE_INQUIRY", "1608522334"
    )
    assert row["source_answered"] == 1
    assert row["answer_status"] == "ANSWERED"


def test_an_unanswered_inquiry_is_stored_as_unanswered(tmp_path) -> None:
    database = _database(tmp_path)
    _store(database, "1608522146", answered=False)
    row = InquiryRepository(database).get_by_source(
        STORE, "COUPANG_ONLINE_INQUIRY", "1608522146"
    )
    assert not row["source_answered"]
    assert row["answer_status"] == "UNANSWERED"


def test_the_payload_survives_into_raw_json(tmp_path) -> None:
    """Empty raw_json is what broke both the reply and the catalogue join."""

    database = _database(tmp_path)
    _store(database, "1608522334", answered=True)
    row = InquiryRepository(database).get_by_source(
        STORE, "COUPANG_ONLINE_INQUIRY", "1608522334"
    )
    assert row["raw_json"]["sellerProductId"] == SELLER_PRODUCT_ID
    assert len(row["raw_json"]["commentDtoList"]) == 1


def test_the_backfill_stores_the_same_way_the_sync_does(tmp_path, monkeypatch) -> None:
    """The path that produced the 7,278 rows."""

    from services.coupang_historical_inquiry_backfill_service import (
        CoupangHistoricalInquiryBackfillService,
    )

    for name, value in (
        ("COUPANG_ACCESS_KEY", "k"), ("COUPANG_SECRET_KEY", "s"),
        ("COUPANG_VENDOR_ID", "v"),
    ):
        monkeypatch.setenv(name, value)
    database = _database(tmp_path)

    class Client:
        def list_online_inquiries(self, **kwargs):
            page = int(kwargs["page_num"])
            return {"data": {
                "content": [_payload("1608522334", answered=True)] if page == 1 else [],
                "pagination": {"totalPages": 1},
            }}

    CoupangHistoricalInquiryBackfillService(
        database, client_factory=lambda _a: Client()
    ).backfill_account(ACCOUNT, start_date=date(2026, 9, 16), end_date=date(2026, 9, 16))

    row = InquiryRepository(database).get_by_source(
        STORE, "COUPANG_ONLINE_INQUIRY", "1608522334"
    )
    assert row["source_answered"] == 1
    assert row["raw_json"]["sellerProductId"] == SELLER_PRODUCT_ID


# --- what the dashboard shows ----------------------------------------------

def test_the_sellers_reply_is_carried_to_the_dashboard(tmp_path) -> None:
    database = _database(tmp_path)
    _store(database, "1608522334", answered=True)
    row = next(r for r in _page(database))
    assert row["seller_answer"] == SELLER_ANSWER


def test_an_unanswered_inquiry_carries_no_reply(tmp_path) -> None:
    database = _database(tmp_path)
    _store(database, "1608522146", answered=False)
    assert next(r for r in _page(database))["seller_answer"] is None


def test_the_product_name_comes_from_the_stored_catalogue(tmp_path) -> None:
    database = _database(tmp_path)
    _store(database, "1608522334", answered=True)
    _seed_catalog(database, "삼성 32인치 스마트 모니터")
    assert _page(database)[0]["product_name"] == "삼성 32인치 스마트 모니터"


def test_a_product_id_from_the_other_account_is_not_borrowed(tmp_path) -> None:
    """The same seller product id means a different product per account."""

    database = _database(tmp_path)
    _store(database, "1608522334", answered=True)
    _seed_catalog(database, "다른 계정 상품", account="OJE_PLUS")
    assert not str(_page(database)[0]["product_name"] or "").strip()


def test_no_catalogue_row_leaves_the_name_empty(tmp_path) -> None:
    database = _database(tmp_path)
    _store(database, "1608522334", answered=True)
    assert not str(_page(database)[0]["product_name"] or "").strip()


def test_one_catalogue_query_serves_the_whole_page(tmp_path) -> None:
    database = _database(tmp_path)
    for index in range(5):
        _store(database, f"16085223{index:02d}", answered=True)
    _seed_catalog(database, "삼성 32인치 스마트 모니터")
    rows = _page(database)
    assert len(rows) == 5
    assert {row["product_name"] for row in rows} == {"삼성 32인치 스마트 모니터"}


@pytest.mark.parametrize("answered", [True, False])
def test_answered_flag_reaches_the_dashboard_filter(tmp_path, answered) -> None:
    database = _database(tmp_path)
    _store(database, "1608522334", answered=answered)
    rows, _, _ = InquiryRepository(database).dashboard_page(
        store_codes=[STORE], source="ALL", queues=[], priorities=[],
        answer_status="ANSWERED" if answered else "UNANSWERED",
        delivery_only=False, search_query="",
        start_date="2026-01-01", end_date="2026-12-31",
        kpi_filter=None, page=1, page_size=30,
    )
    assert len(rows) == 1
