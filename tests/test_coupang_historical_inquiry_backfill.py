from __future__ import annotations

from datetime import UTC, date, datetime

from config import (
    COUPANG_OJE_NS,
    COUPANG_OJE_PLUS,
    OJE_PLUS_TOP_SELLER_PRODUCT_IDS,
)
from repositories.coupang_product_catalog_repository import CoupangProductCatalogRepository
from repositories.coupang_product_mapping_repository import (
    CONFIRMED,
    MANUAL,
    NEEDS_REVIEW,
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
    # OJE_PLUS counts as current stock only inside its operated Top 20 scope.
    _seed_mapping(
        database, COUPANG_OJE_PLUS, "v-shared", "25HG400",
        seller=OJE_PLUS_TOP_SELLER_PRODUCT_IDS[0],
    )
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
    _seed_mapping(
        database, COUPANG_OJE_PLUS, "v-current", "32DM501", active=True,
        seller=OJE_PLUS_TOP_SELLER_PRODUCT_IDS[0],
    )
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
    item["content"] = "홍길동 010-1234-5678 주문번호 1234567890123456 주소는 테스트로 123"
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
    assert "테스트로 123" not in row[0]
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


def test_backfill_excludes_delivery_cs_damage_and_temporary_cases(tmp_path, monkeypatch) -> None:
    _configure_accounts(monkeypatch)
    database = _database(tmp_path)
    cases = [
        ("delivery-date", "9월22일 배송예정이라는데 좀 더 빨리 배송 가능할까요?", "고객님건의 경우 09/12 예정으로 확인됩니다."),
        ("delivery-fast", "빨리 보내주세요", "고객님건의 경우 09/12 예정으로 확인됩니다."),
        ("delivery-when", "삼성TV 언제 배송되나요", "고객님 건 확인 시 14일 예정으로 확인됩니다."),
        ("cancel", "주문 취소하겠습니다", "처리 도와드리겠습니다."),
        ("discount", "추가 구매하려는데 할인 적용 가능한가요?", "현재 기본 판매자 자체 할인 적용 중입니다."),
        ("benefit", "온누리상품권을 아직 못받았습니다", "삼성닷컴으로 신청해주세요. 09/30일까지 신청기간입니다."),
        ("damage", "파손된 상품은 이미 회수했는데 새 상품은 언제 배송되나요?", "새 상품 배송을 확인하겠습니다."),
    ]
    payloads = []
    for case_id, question, answer in cases:
        vendor = f"v-{case_id}"
        _seed_mapping(database, COUPANG_OJE_NS, vendor, "32DM501")
        item = _inquiry(case_id, vendor, [_comment(case_id)])
        item["content"] = question
        item["commentDtoList"][0]["content"] = answer
        payloads.append(item)

    result = CoupangHistoricalInquiryBackfillService(
        database, client_factory=lambda _account: FakeReadClient({1: payloads})
    ).backfill_account(COUPANG_OJE_NS, start_date=date(2025, 1, 2), end_date=date(2025, 1, 2))

    assert result.fetched == 7
    assert result.skipped_learning_excluded == 7
    assert result.candidates_inserted == 0
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM historical_cases").fetchone()[0] == 0


def test_backfill_keeps_product_faq_and_marks_bundle_for_manual_review(tmp_path, monkeypatch) -> None:
    _configure_accounts(monkeypatch)
    database = _database(tmp_path)
    payloads = []
    for case_id, question, answer in (
        ("wifi", "Wi-Fi 연결 방법이 궁금합니다", "설정 메뉴에서 Wi-Fi를 선택한 후 네트워크에 연결해 주세요."),
        ("vesa", "VESA 이동식 거치대를 사용할 수 있나요?", "규격을 확인한 뒤 호환되는 거치대를 사용할 수 있습니다."),
        ("bundle", "스탠드와 같이 오나요?", "해당제품은 모니터와 스탠드 패키지상품이며 박스는 각각 갑니다."),
    ):
        vendor = f"v-{case_id}"
        _seed_mapping(database, COUPANG_OJE_NS, vendor, "32DM501")
        item = _inquiry(case_id, vendor, [_comment(case_id)])
        item["content"] = question
        item["commentDtoList"][0]["content"] = answer
        payloads.append(item)

    client = FakeReadClient({1: payloads})
    result = CoupangHistoricalInquiryBackfillService(
        database, client_factory=lambda _account: client
    ).backfill_account(COUPANG_OJE_NS, start_date=date(2025, 1, 2), end_date=date(2025, 1, 2))

    assert result.candidates_inserted == 3
    assert result.manual_review_candidates == 1
    rows = HistoricalCaseService(database).repository.list_cases(limit=10)
    by_id = {row["external_inquiry_id"]: row for row in rows}
    assert by_id["wifi"]["metadata_json"]["historical_candidate_decision"] == "KEEP"
    assert by_id["vesa"]["metadata_json"]["historical_candidate_decision"] == "KEEP"
    assert by_id["bundle"]["metadata_json"]["historical_candidate_decision"] == "MANUAL_REVIEW"
    assert by_id["bundle"]["metadata_json"]["historical_candidate_reason"] == "LISTING_OR_BUNDLE_SPECIFIC"
    assert all(row["metadata_json"]["origin_market"] == "COUPANG" for row in rows)
    assert all(row["metadata_json"]["market_applicability"] == "COUPANG_ONLY" for row in rows)


def test_backfill_preserves_general_installation_and_flags_unknown_information(tmp_path, monkeypatch) -> None:
    _configure_accounts(monkeypatch)
    database = _database(tmp_path)
    payloads = []
    for case_id, question, answer in (
        ("iptv", "IPTV와 Netflix, YouTube를 사용할 수 있나요?", "셋톱박스를 연결하고 앱을 이용할 수 있습니다."),
        ("move-install", "기존 제품 이동설치도 가능한가요?", "일반 설치 가능 여부는 설치 환경을 확인해 안내드립니다."),
        ("unknown", "이 모델의 특별 기능은 무엇인가요?", "정확한 내용은 확인 후 안내드리겠습니다."),
        ("mismatch", "VESA 거치대와 호환되나요?", "넷플릭스는 앱 메뉴에서 사용할 수 있습니다."),
    ):
        vendor = f"v-{case_id}"
        _seed_mapping(database, COUPANG_OJE_NS, vendor, "32DM501")
        item = _inquiry(case_id, vendor, [_comment(case_id)])
        item["content"] = question
        item["commentDtoList"][0]["content"] = answer
        payloads.append(item)

    result = CoupangHistoricalInquiryBackfillService(
        database, client_factory=lambda _account: FakeReadClient({1: payloads})
    ).backfill_account(COUPANG_OJE_NS, start_date=date(2025, 1, 2), end_date=date(2025, 1, 2))

    assert result.candidates_inserted == 4
    assert result.manual_review_candidates == 2
    rows = HistoricalCaseService(database).repository.list_cases(limit=10)
    by_id = {row["external_inquiry_id"]: row for row in rows}
    assert by_id["iptv"]["metadata_json"]["historical_candidate_decision"] == "KEEP"
    assert by_id["move-install"]["metadata_json"]["historical_candidate_decision"] == "KEEP"
    assert by_id["unknown"]["metadata_json"]["historical_candidate_decision"] == "MANUAL_REVIEW"
    assert by_id["unknown"]["metadata_json"]["historical_candidate_reason"] == "UNKNOWN_INFORMATION"
    assert by_id["mismatch"]["metadata_json"]["historical_candidate_decision"] == "MANUAL_REVIEW"
    assert by_id["mismatch"]["metadata_json"]["historical_candidate_reason"] == "UNKNOWN_INFORMATION"


# --- current operated model set: OJE_NS active UNION OJE_PLUS Top 20 active ---
#
# A scoped sync returns before the deactivation sweep a full sync runs, so
# OJE_PLUS rows an earlier unscoped sync left active are still active -- 613 of
# them on the server, outside the operated Top 20.  Reading is_active alone
# would take those for current stock.

TOP20_SELLER_ID = OJE_PLUS_TOP_SELLER_PRODUCT_IDS[0]
OUTSIDE_TOP20_SELLER_ID = "99999999999"


def _backfill(database, account, inquiries):
    client = FakeReadClient({1: inquiries})
    return CoupangHistoricalInquiryBackfillService(
        database, client_factory=lambda _account: client
    ).backfill_account(account, start_date=date(2025, 1, 2), end_date=date(2025, 1, 2))


def test_stale_oje_plus_listing_outside_top20_is_not_current_stock(tmp_path, monkeypatch) -> None:
    """Scenario A/D: the only active row for the model is a stale non-Top20 one."""

    _configure_accounts(monkeypatch)
    database = _database(tmp_path)
    _seed_mapping(database, COUPANG_OJE_PLUS, "v-past", "32BM501", active=False)
    _seed_mapping(
        database, COUPANG_OJE_PLUS, "v-stale", "32BM501",
        active=True, seller=OUTSIDE_TOP20_SELLER_ID,
    )
    result = _backfill(
        database, COUPANG_OJE_PLUS, [_inquiry("stale", "v-past", [_comment("stale")])]
    )
    assert result.candidates_inserted == 0
    assert result.skipped_not_currently_operated == 1
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM historical_cases").fetchone()[0] == 0
        # The common Inquiry is still kept; only the Learning candidate is refused.
        assert connection.execute("SELECT COUNT(*) FROM inquiries").fetchone()[0] == 1


def test_inactive_listing_passes_when_same_model_is_active_elsewhere(tmp_path, monkeypatch) -> None:
    """Scenario B: the past listing need not be the one still selling."""

    _configure_accounts(monkeypatch)
    database = _database(tmp_path)
    _seed_mapping(database, COUPANG_OJE_NS, "v-past", "32DM501", active=False)
    _seed_mapping(database, COUPANG_OJE_NS, "v-now", "32DM501", active=True)
    result = _backfill(
        database, COUPANG_OJE_NS, [_inquiry("reuse", "v-past", [_comment("reuse")])]
    )
    assert result.candidates_inserted == 1


def test_oje_plus_history_outside_top20_passes_on_top20_active_model(tmp_path, monkeypatch) -> None:
    """Scenario C: model identity decides, not the past sellerProduct."""

    _configure_accounts(monkeypatch)
    database = _database(tmp_path)
    _seed_mapping(
        database, COUPANG_OJE_PLUS, "v-past", "32DM501",
        active=False, seller=OUTSIDE_TOP20_SELLER_ID,
    )
    _seed_mapping(
        database, COUPANG_OJE_PLUS, "v-top", "32DM501",
        active=True, seller=TOP20_SELLER_ID,
    )
    result = _backfill(
        database, COUPANG_OJE_PLUS, [_inquiry("top20", "v-past", [_comment("top20")])]
    )
    assert result.candidates_inserted == 1


def test_current_model_set_is_the_union_of_both_accounts(tmp_path, monkeypatch) -> None:
    """Scenarios E and F: cross-account reuse stays allowed in both directions."""

    _configure_accounts(monkeypatch)
    plus_db = _database(tmp_path / "plus")
    _seed_mapping(plus_db, COUPANG_OJE_PLUS, "v-plus-past", "32DM501", active=False)
    _seed_mapping(plus_db, COUPANG_OJE_NS, "v-ns-now", "32DM501", active=True)
    assert _backfill(
        plus_db, COUPANG_OJE_PLUS, [_inquiry("e", "v-plus-past", [_comment("e")])]
    ).candidates_inserted == 1

    ns_db = _database(tmp_path / "ns")
    _seed_mapping(ns_db, COUPANG_OJE_NS, "v-ns-past", "32DM501", active=False)
    _seed_mapping(
        ns_db, COUPANG_OJE_PLUS, "v-plus-now", "32DM501",
        active=True, seller=TOP20_SELLER_ID,
    )
    assert _backfill(
        ns_db, COUPANG_OJE_NS, [_inquiry("f", "v-ns-past", [_comment("f")])]
    ).candidates_inserted == 1


def test_inactive_top20_listing_is_not_current_stock(tmp_path, monkeypatch) -> None:
    """Scenario G: being inside the operated scope is not being on sale."""

    _configure_accounts(monkeypatch)
    database = _database(tmp_path)
    _seed_mapping(database, COUPANG_OJE_PLUS, "v-past", "32DM501", active=False)
    _seed_mapping(
        database, COUPANG_OJE_PLUS, "v-top", "32DM501",
        active=False, seller=TOP20_SELLER_ID,
    )
    result = _backfill(
        database, COUPANG_OJE_PLUS, [_inquiry("idle", "v-past", [_comment("idle")])]
    )
    assert result.candidates_inserted == 0
    assert result.skipped_not_currently_operated == 1


def test_unconfirmed_mapping_does_not_make_a_model_current(tmp_path, monkeypatch) -> None:
    """Scenario H: only a CONFIRMED mapping may name an active listing's model."""

    _configure_accounts(monkeypatch)
    database = _database(tmp_path)
    _seed_mapping(database, COUPANG_OJE_NS, "v-past", "32DM501", active=False)
    catalog = CoupangProductCatalogRepository(database)
    catalog.upsert_product(
        account_code=COUPANG_OJE_NS,
        data={"sellerProductId": "unreviewed", "status": "APPROVED", "sellerProductName": "미확정"},
        sync_token="test",
    )
    catalog.upsert_option(
        account_code=COUPANG_OJE_NS, seller_product_id="unreviewed",
        item={"vendorItemId": "v-unreviewed", "itemName": "미확정 옵션"},
    )
    catalog.set_product_active(
        account_code=COUPANG_OJE_NS, seller_product_id="unreviewed", is_active=True
    )
    CoupangProductMappingRepository(database).upsert(
        account_code=COUPANG_OJE_NS, vendor_item_id="v-unreviewed",
        seller_product_id="unreviewed", canonical_model="32DM501",
        mapping_source=MANUAL, mapping_status=NEEDS_REVIEW,
    )
    result = _backfill(
        database, COUPANG_OJE_NS, [_inquiry("unconfirmed", "v-past", [_comment("unconfirmed")])]
    )
    assert result.candidates_inserted == 0
    assert result.skipped_not_currently_operated == 1
