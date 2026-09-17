"""Which "already answered" view an inquiry gets, and where its details come from.

The card and the detail panel read the same inquiry by different routes: the
list goes through ``dashboard_page``, the panel re-reads the row with
``get_by_source``.  The marketplace enrichment lived on the first one only, so
a card showed a product name while the panel beside it showed a dash.

The other half is the posted-answer tab.  A Naver answer is one this system
registered, tracked with NAVER_POSTED provenance.  A Coupang reply is not:
nothing was posted there, a person wrote it in Wing and it was read back with
the inquiry.  It is shown as that, and never given a posted provenance.
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
from ui.review_workspace import (
    ANSWER_VIEW_PRESENTATION,
    COUPANG_SELLER_VIEW,
    NAVER_POSTED_VIEW,
    _source_answer_view,
    _source_seller_answer,
    answer_view_presentation,
)

ACCOUNT = "OJE_NS"
STORE = "COUPANG_OJE_NS"
SPID = "15654321531"
VENDOR_ITEM = "93128932886"
REPLY = "안녕하세요 고객님 삼성서비스센터로 연락주셔야 할 부분입니다."
PRODUCT = "M50F / 모음전 / O"
OPTION = "삼성전자 스마트 모니터 M5 M50F"


def _database(tmp_path) -> Database:
    database = Database(tmp_path / "source-answer.db")
    database.initialize()
    return database


def _store(database: Database, *, answered: bool = True, account: str = ACCOUNT):
    payload = {
        "inquiryId": "160668556",
        "sellerProductId": SPID,
        "vendorItemId": VENDOR_ITEM,
        "content": "와이파이가안됩니다",
        "inquiryAt": "2026-09-16T11:00:00+09:00",
        "orderIds": [],
        "commentDtoList": (
            [{
                "inquiryCommentId": "c-1",
                "inquiryId": "160668556",
                "content": REPLY,
                "inquiryCommentAt": "2026-09-16T11:23:00+09:00",
            }] if answered else []
        ),
    }
    work_item = CoupangInquiryNormalizer().online(
        payload, account_code=account
    ).to_work_item()
    InquiryRepository(database).upsert_work_item(normalize_work_item(work_item))


def _seed_catalog(database: Database, *, account: str = ACCOUNT) -> None:
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
    # Inactive on purpose: the real row is not selling any more.
    catalog.set_product_active(
        account_code=account, seller_product_id=SPID, is_active=False
    )


def _detail(database: Database) -> dict:
    return InquiryRepository(database).get_by_source(
        STORE, "COUPANG_ONLINE_INQUIRY", "160668556"
    )


# --- the detail panel reads the same enrichment the list does --------------

def test_the_detail_row_carries_product_option_and_reply(tmp_path) -> None:
    database = _database(tmp_path)
    _store(database)
    _seed_catalog(database)

    row = _detail(database)

    assert row["product_name"] == PRODUCT
    assert row["option_name"] == OPTION
    assert row["seller_answer"] == REPLY


def test_the_two_read_routes_agree(tmp_path) -> None:
    """A card and the panel beside it must not disagree about one inquiry."""

    database = _database(tmp_path)
    _store(database)
    _seed_catalog(database)
    listed, _, _ = InquiryRepository(database).dashboard_page(
        store_codes=[STORE], source="ALL", queues=[], priorities=[],
        answer_status="ALL", delivery_only=False, search_query="",
        start_date="2026-01-01", end_date="2026-12-31",
        kpi_filter=None, page=1, page_size=30,
    )
    detail = _detail(database)

    for field in ("product_name", "option_name", "seller_answer"):
        assert listed[0][field] == detail[field]


def test_another_account_catalogue_is_not_borrowed(tmp_path) -> None:
    database = _database(tmp_path)
    _store(database, account=ACCOUNT)
    _seed_catalog(database, account="OJE_PLUS")
    row = _detail(database)
    assert not str(row["product_name"] or "").strip()
    assert not str(row["option_name"] or "").strip()


def test_a_naver_row_is_unaffected(tmp_path) -> None:
    database = _database(tmp_path)
    InquiryRepository(database).upsert_work_item({
        "store_code": "OJE_PLUS",
        "source_type": "PRODUCT_INQUIRY",
        "source_question_id": "n1",
        "title": "문의", "content": "질문",
        "product_name": "네이버 상품", "option_name": "네이버 옵션",
        "registered_at": "2026-09-16T10:00:00+09:00",
        "raw_json": {},
    })
    row = InquiryRepository(database).get_by_source(
        "OJE_PLUS", "PRODUCT_INQUIRY", "n1"
    )
    assert row["product_name"] == "네이버 상품"
    assert row["option_name"] == "네이버 옵션"


# --- which view an answered inquiry gets -----------------------------------

def test_naver_keeps_its_posted_answer_view() -> None:
    assert _source_answer_view(
        {"store_code": "OJE_PLUS", "source_answered": 1}
    ) == NAVER_POSTED_VIEW


@pytest.mark.parametrize(
    "store_code", ["COUPANG_OJE_NS", "COUPANG_OJE_PLUS"], ids=["ns", "plus"]
)
def test_coupang_gets_a_read_only_seller_view_instead(store_code) -> None:
    view = _source_answer_view({
        "store_code": store_code, "source_answered": 1, "seller_answer": REPLY,
    })
    assert view == COUPANG_SELLER_VIEW
    assert view != NAVER_POSTED_VIEW
    assert "네이버" not in view


def test_coupang_without_a_stored_reply_gets_no_view() -> None:
    """No body to show, and the Naver NOT_FETCHED wording is not reused."""

    assert _source_answer_view(
        {"store_code": STORE, "source_answered": 1, "seller_answer": ""}
    ) is None


def test_an_unanswered_inquiry_gets_no_view() -> None:
    assert _source_answer_view(
        {"store_code": STORE, "source_answered": 0, "seller_answer": REPLY}
    ) is None


# --- provenance ------------------------------------------------------------

def test_the_coupang_view_is_not_labelled_as_posted() -> None:
    _, provenance, _ = answer_view_presentation(COUPANG_SELLER_VIEW)
    assert provenance == "MARKETPLACE_SELLER_ANSWER"
    assert provenance != "NAVER_POSTED"
    assert "POSTED" not in provenance.replace("MARKETPLACE_SELLER_ANSWER", "")


def test_naver_provenance_is_unchanged() -> None:
    _, provenance, _ = answer_view_presentation(NAVER_POSTED_VIEW)
    assert provenance == "NAVER_POSTED"


def test_every_view_can_be_restored_after_a_rerun() -> None:
    """The rerun whitelist is the view map, so a new view cannot fall out."""

    assert COUPANG_SELLER_VIEW in ANSWER_VIEW_PRESENTATION
    assert NAVER_POSTED_VIEW in ANSWER_VIEW_PRESENTATION


def test_the_reply_reader_trims_and_tolerates_missing(tmp_path) -> None:
    assert _source_seller_answer({"seller_answer": f"  {REPLY}  "}) == REPLY
    assert _source_seller_answer({}) == ""
