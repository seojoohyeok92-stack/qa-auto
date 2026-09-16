from __future__ import annotations

from datetime import UTC, date, datetime

from config import COUPANG_OJE_NS, COUPANG_OJE_PLUS
from repositories.coupang_product_catalog_repository import CoupangProductCatalogRepository
from repositories.coupang_product_mapping_repository import (
    CONFIRMED,
    MANUAL,
    CoupangProductMappingRepository,
)
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.coupang_historical_inquiry_backfill_service import (
    CoupangHistoricalInquiryBackfillService,
)
from services.historical_case_service import HistoricalCaseService


class FakeReadClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls: list[dict] = []

    def list_online_inquiries(self, **kwargs):
        self.calls.append(kwargs)
        page = int(kwargs["page_num"])
        content = self.pages.get(page, [])
        return {
            "data": {
                "content": content,
                "pagination": {"totalPages": max(self.pages) if self.pages else 1},
            }
        }


def _database(tmp_path) -> Database:
    database = Database(tmp_path / "coupang-historical.db")
    database.initialize()
    return database


def _configure_accounts(monkeypatch) -> None:
    monkeypatch.setenv("COUPANG_ACCESS_KEY", "ns-access")
    monkeypatch.setenv("COUPANG_SECRET_KEY", "ns-secret")
    monkeypatch.setenv("COUPANG_VENDOR_ID", "ns-vendor")
    monkeypatch.setenv("COUPANG_2_ACCESS_KEY", "plus-access")
    monkeypatch.setenv("COUPANG_2_SECRET_KEY", "plus-secret")
    monkeypatch.setenv("COUPANG_2_VENDOR_ID", "plus-vendor")


def _inquiry(inquiry_id: str, vendor_item_id: str, comments) -> dict:
    return {
        "inquiryId": inquiry_id,
        "productId": f"p-{inquiry_id}",
        "sellerProductId": f"s-{inquiry_id}",
        "sellerItemId": f"si-{inquiry_id}",
        "vendorItemId": vendor_item_id,
        "content": "제품 기능을 알려주세요.",
        "inquiryAt": "2025-01-02T10:00:00+09:00",
        "orderIds": [],
        "commentDtoList": comments,
    }


def _comment(inquiry_id: str, suffix: str = "1") -> dict:
    return {
        "inquiryCommentId": f"c-{inquiry_id}-{suffix}",
        "inquiryId": inquiry_id,
        "content": "제품 설명서 기준으로 사용 방법을 안내드립니다.",
        "inquiryCommentAt": "2025-01-02T11:00:00+09:00",
    }


def _seed_mapping(database: Database, account: str, vendor: str, model: str, *, active: bool = True, seller: str | None = None) -> None:
    catalog = CoupangProductCatalogRepository(database)
    seller = seller or f"catalog-{account}-{vendor}"
    catalog.upsert_product(
        account_code=account,
        data={"sellerProductId": seller, "status": "APPROVED", "sellerProductName": "테스트 상품"},
        sync_token="test",
    )
    catalog.upsert_option(
        account_code=account,
        seller_product_id=seller,
        item={"vendorItemId": vendor, "itemName": "테스트 옵션"},
    )
    catalog.set_product_active(account_code=account, seller_product_id=seller, is_active=active)
    CoupangProductMappingRepository(database).upsert(
        account_code=account,
        vendor_item_id=vendor,
        seller_product_id=seller,
        canonical_model=model,
        mapping_source=MANUAL,
        mapping_status=CONFIRMED,
    )


def test_backfill_is_answered_only_paginated_account_scoped_and_idempotent(tmp_path, monkeypatch) -> None:
    _configure_accounts(monkeypatch)
    database = _database(tmp_path)
    _seed_mapping(database, COUPANG_OJE_NS, "v-shared", "32DM501")
    _seed_mapping(database, COUPANG_OJE_PLUS, "v-shared", "25HG400")
    clients = {
        COUPANG_OJE_NS: FakeReadClient({
            1: [_inquiry("same-id", "v-shared", [_comment("same-id")])],
            2: [_inquiry("ns-page-two", "v-shared", [_comment("ns-page-two")])],
        }),
        COUPANG_OJE_PLUS: FakeReadClient({1: [_inquiry("same-id", "v-shared", [_comment("same-id")])]}),
    }
    service = CoupangHistoricalInquiryBackfillService(
        database,
        client_factory=lambda account: clients[account.account_code],
    )
    start = end = date(2025, 1, 2)
    ns = service.backfill_account(COUPANG_OJE_NS, start_date=start, end_date=end)
    plus = service.backfill_account(COUPANG_OJE_PLUS, start_date=start, end_date=end)
    repeated = service.backfill_account(COUPANG_OJE_NS, start_date=start, end_date=end)

    assert ns.pages == 2 and ns.candidates_inserted == 2
    assert plus.candidates_inserted == 1
    assert repeated.candidates_duplicate == 2
    assert all(call["answered_type"] == "ANSWERED" for client in clients.values() for call in client.calls)
    assert all(call["page_size"] == 50 for client in clients.values() for call in client.calls)
    assert all((call["inquiry_end_at"] - call["inquiry_start_at"]).days <= 6 for client in clients.values() for call in client.calls)
    with database.connection() as connection:
        inquiries = connection.execute("SELECT store_code, source_question_id FROM inquiries ORDER BY store_code, source_question_id").fetchall()
        cases = connection.execute("SELECT store_code, external_inquiry_id, active FROM historical_cases ORDER BY store_code").fetchall()
        events = connection.execute("SELECT COUNT(*) FROM auto_sync_events").fetchone()[0]
    assert ("COUPANG_OJE_NS", "same-id") in {(row[0], row[1]) for row in inquiries}
    assert ("COUPANG_OJE_PLUS", "same-id") in {(row[0], row[1]) for row in inquiries}
    assert len(cases) == 3 and all(row[2] == 0 for row in cases)
    assert events == 0


def test_backfill_never_selects_empty_or_multi_comment_answers(tmp_path, monkeypatch) -> None:
    _configure_accounts(monkeypatch)
    database = _database(tmp_path)
    _seed_mapping(database, COUPANG_OJE_NS, "v-1", "32DM501")
    client = FakeReadClient({1: [
        _inquiry("no-comment", "v-1", []),
        _inquiry("multi-comment", "v-1", [_comment("multi-comment", "1"), _comment("multi-comment", "2")]),
    ]})
    result = CoupangHistoricalInquiryBackfillService(
        database, client_factory=lambda _account: client
    ).backfill_account(COUPANG_OJE_NS, start_date=date(2025, 1, 2), end_date=date(2025, 1, 2))
    assert result.skipped_no_answer == 1
    assert result.skipped_multi_comment_review == 1
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM inquiries").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM historical_cases").fetchone()[0] == 0


def test_current_model_policy_uses_only_active_coupang_canonical_models(tmp_path, monkeypatch) -> None:
    _configure_accounts(monkeypatch)
    database = _database(tmp_path)
    # Historical listing is inactive, but another current Coupang listing has the same model.
    _seed_mapping(database, COUPANG_OJE_NS, "v-historical", "32DM501", active=False)
    _seed_mapping(database, COUPANG_OJE_PLUS, "v-current", "32DM501", active=True)
    _seed_mapping(database, COUPANG_OJE_NS, "v-retired", "27DG700", active=False)
    client = FakeReadClient({1: [
        _inquiry("historical", "v-historical", [_comment("historical")]),
        _inquiry("retired", "v-retired", [_comment("retired")]),
    ]})
    result = CoupangHistoricalInquiryBackfillService(
        database,
        client_factory=lambda _account: client,
    ).backfill_account(COUPANG_OJE_NS, start_date=date(2025, 1, 2), end_date=date(2025, 1, 2))
    assert result.candidates_inserted == 1
    assert result.skipped_not_currently_operated == 1
    with database.connection() as connection:
        rows = connection.execute("SELECT metadata_json FROM historical_cases ORDER BY external_inquiry_id").fetchall()
    assert any("COUPANG_ACTIVE_CATALOG" in row[0] for row in rows)
    assert all("NAVER_ACTIVE_CATALOG" not in row[0] for row in rows)


def test_backfill_preserves_privacy_and_does_not_post_or_call_contact_center(tmp_path, monkeypatch) -> None:
    _configure_accounts(monkeypatch)
    database = _database(tmp_path)
    _seed_mapping(database, COUPANG_OJE_NS, "v-private", "32DM501")
    item = _inquiry("private", "v-private", [_comment("private")])
    item["content"] = "홍길동 010-1234-5678 주문번호 1234567890123456"
    item["commentDtoList"][0]["content"] = "주소 서울시 테스트로 1, 주문번호 1234567890123456"
    client = FakeReadClient({1: [item]})
    result = CoupangHistoricalInquiryBackfillService(
        database, client_factory=lambda _account: client
    ).backfill_account(COUPANG_OJE_NS, start_date=date(2025, 1, 2), end_date=date(2025, 1, 2))
    assert result.candidates_inserted == 1
    with database.connection() as connection:
        row = connection.execute("SELECT question, seller_answer, active FROM historical_cases").fetchone()
        posts = connection.execute("SELECT COUNT(*) FROM naver_post_attempts").fetchone()[0]
    assert "010-1234-5678" not in row[0]
    assert "1234567890123456" not in row[1]
    assert row[2] == 0 and posts == 0
    assert not hasattr(client, "list_contact_center_inquiries")


def test_approved_coupang_candidate_defaults_to_coupang_only_learning(tmp_path, monkeypatch) -> None:
    _configure_accounts(monkeypatch)
    database = _database(tmp_path)
    _seed_mapping(database, COUPANG_OJE_NS, "v-promote", "32DM501")
    item = _inquiry("promote", "v-promote", [_comment("promote")])
    item["inquiryAt"] = datetime.now(UTC).isoformat()
    item["commentDtoList"][0]["inquiryCommentAt"] = datetime.now(UTC).isoformat()
    client = FakeReadClient({1: [item]})
    result = CoupangHistoricalInquiryBackfillService(
        database, client_factory=lambda _account: client
    ).backfill_account(COUPANG_OJE_NS, start_date=date(2025, 1, 2), end_date=date(2025, 1, 2))
    assert result.candidates_inserted == 1
    historical = HistoricalCaseService(database)
    case = historical.repository.list_cases(limit=1)[0]
    assert case["active"] is False
    historical.repository.set_learning_enabled(int(case["id"]), True, actor="tester")
    with database.transaction() as connection:
        connection.execute(
            "UPDATE historical_cases SET quality_score=0.8, policy_risk='NONE' WHERE id=?",
            (int(case["id"]),),
        )
    promoted = historical.promote(int(case["id"]), actor="tester")
    assert promoted["store_code"] is None
    assert promoted["model_code"] == "32DM501"
    assert promoted["metadata_json"]["shared_cross_market_learning"] is False
    assert promoted["metadata_json"]["origin_market"] == "COUPANG"
    assert promoted["metadata_json"]["market_applicability"] == "COUPANG_ONLY"
