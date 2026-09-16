"""One dashboard for both marketplaces, and KPI cards that mean what they say.

Coupang inquiries were already in the table the dashboard reads; what kept
them off the screen was the market candidate list, which was built from the
Naver work queue, so their store codes were never among the ones the query
filters on.

The KPI half pins the distinction the cards depend on: three of them count
events inside the chosen window, two report a queue as it stands now, and the
timestamp each one counts on is the timestamp of the thing it names.
"""

from __future__ import annotations

import pytest

from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from ui.components import SOURCE_LABELS
from ui.market_labels import (
    ALL_MARKETS,
    market_label,
    markets_for_stores,
    store_market_label,
    stores_in_market,
)

NAVER_STORE = "OJE_PLUS"
COUPANG_NS = "COUPANG_OJE_NS"
COUPANG_PLUS = "COUPANG_OJE_PLUS"


def _database(tmp_path) -> Database:
    database = Database(tmp_path / "dashboard-market.db")
    database.initialize()
    return database


def _inquiry(
    database: Database,
    *,
    store_code: str,
    question_id: str,
    registered_at: str,
    source_type: str = "PRODUCT_INQUIRY",
    answered: bool = False,
) -> int:
    return InquiryRepository(database).upsert_work_item({
        "store_code": store_code,
        "source_type": source_type,
        "source_question_id": question_id,
        "inquiry_type": source_type,
        "title": "문의",
        "content": "제품 문의입니다.",
        "registered_at": registered_at,
        "source_created_at": registered_at,
        "source_answered": int(answered),
        "raw_json": {},
    }).inquiry_id


def _set(database: Database, inquiry_id: int, **columns) -> None:
    assignments = ", ".join(f"{key}=?" for key in columns)
    with database.transaction() as connection:
        connection.execute(
            f"UPDATE inquiries SET {assignments} WHERE id=?",
            (*columns.values(), int(inquiry_id)),
        )


def _draft(database: Database, inquiry_id: int, *, created_at: str) -> None:
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO answer_drafts(inquiry_id, final_answer, created_at, "
            "updated_at) VALUES(?,?,?,?)",
            (int(inquiry_id), "안내드립니다.", created_at, created_at),
        )


def _cards(database: Database, **kwargs):
    return InquiryRepository(database).dashboard_operational_card_counts(
        today_kst=None, **kwargs
    )


# --- market selection ------------------------------------------------------

def test_market_candidates_come_from_inquiries(tmp_path) -> None:
    database = _database(tmp_path)
    _inquiry(database, store_code=NAVER_STORE, question_id="n1",
             registered_at="2026-09-01T10:00:00+09:00")
    _inquiry(database, store_code=COUPANG_NS, question_id="c1",
             registered_at="2026-09-02T10:00:00+09:00",
             source_type="COUPANG_ONLINE_INQUIRY")

    codes = InquiryRepository(database).dashboard_store_codes()
    assert set(codes) == {NAVER_STORE, COUPANG_NS}
    assert markets_for_stores(codes) == ["NAVER", "COUPANG"]


def test_a_store_only_in_historical_is_not_a_dashboard_market(tmp_path) -> None:
    """Historical has its own store list; it must not create a dead choice."""

    database = _database(tmp_path)
    _inquiry(database, store_code=NAVER_STORE, question_id="n1",
             registered_at="2026-09-01T10:00:00+09:00")
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO historical_cases(source, store_code, external_inquiry_id, "
            "inquiry_type, question, question_normalized, case_key, fingerprint) "
            "VALUES('COUPANG_ONLINE_HISTORY',?,?,?,?,?,?,?)",
            (COUPANG_PLUS, "h1", "COUPANG_ONLINE_INQUIRY", "q", "q", "k1", "f1"),
        )
    assert InquiryRepository(database).dashboard_store_codes() == [NAVER_STORE]


@pytest.mark.parametrize(
    "market,expected",
    [
        pytest.param(ALL_MARKETS, {NAVER_STORE, COUPANG_NS, COUPANG_PLUS}, id="all"),
        pytest.param("NAVER", {NAVER_STORE}, id="naver"),
        pytest.param("COUPANG", {COUPANG_NS, COUPANG_PLUS}, id="coupang-both-accounts"),
    ],
)
def test_single_market_resolves_to_store_codes(market, expected) -> None:
    codes = [NAVER_STORE, COUPANG_NS, COUPANG_PLUS]
    assert set(stores_in_market(codes, market)) == expected


def test_internal_codes_are_never_shown(tmp_path) -> None:
    labels = {store_market_label(code) for code in (NAVER_STORE, COUPANG_NS, COUPANG_PLUS)}
    assert labels == {"네이버", "쿠팡"}
    assert market_label("GMARKET") == "G마켓"


def test_coupang_inquiry_type_has_a_korean_label() -> None:
    assert SOURCE_LABELS["COUPANG_ONLINE_INQUIRY"] == "상품문의"
    assert "COUPANG" not in SOURCE_LABELS["COUPANG_ONLINE_INQUIRY"]


# --- the merged list -------------------------------------------------------

def _page(database: Database, store_codes, **overrides):
    defaults = {
        "store_codes": store_codes, "source": "ALL", "queues": [],
        "priorities": [], "answer_status": "ALL", "delivery_only": False,
        "search_query": "", "start_date": "2026-01-01", "end_date": "2026-12-31",
        "kpi_filter": None, "page": 1, "page_size": 30,
    }
    defaults.update(overrides)
    rows, _, _ = InquiryRepository(database).dashboard_page(**defaults)
    return rows


def test_all_markets_show_naver_and_coupang_together(tmp_path) -> None:
    database = _database(tmp_path)
    _inquiry(database, store_code=NAVER_STORE, question_id="n1",
             registered_at="2026-09-01T10:00:00+09:00")
    _inquiry(database, store_code=COUPANG_NS, question_id="c1",
             registered_at="2026-09-02T10:00:00+09:00",
             source_type="COUPANG_ONLINE_INQUIRY")
    _inquiry(database, store_code=COUPANG_PLUS, question_id="c2",
             registered_at="2026-09-03T10:00:00+09:00",
             source_type="COUPANG_ONLINE_INQUIRY")
    codes = InquiryRepository(database).dashboard_store_codes()

    everything = _page(database, stores_in_market(codes, ALL_MARKETS))
    naver = _page(database, stores_in_market(codes, "NAVER"))
    coupang = _page(database, stores_in_market(codes, "COUPANG"))

    assert {row["store_code"] for row in everything} == {
        NAVER_STORE, COUPANG_NS, COUPANG_PLUS
    }
    assert {row["store_code"] for row in naver} == {NAVER_STORE}
    assert {row["store_code"] for row in coupang} == {COUPANG_NS, COUPANG_PLUS}


def test_date_range_applies_to_both_markets(tmp_path) -> None:
    database = _database(tmp_path)
    _inquiry(database, store_code=NAVER_STORE, question_id="n-old",
             registered_at="2026-01-05T10:00:00+09:00")
    _inquiry(database, store_code=COUPANG_NS, question_id="c-old",
             registered_at="2026-01-06T10:00:00+09:00",
             source_type="COUPANG_ONLINE_INQUIRY")
    _inquiry(database, store_code=NAVER_STORE, question_id="n-new",
             registered_at="2026-09-10T10:00:00+09:00")
    _inquiry(database, store_code=COUPANG_NS, question_id="c-new",
             registered_at="2026-09-11T10:00:00+09:00",
             source_type="COUPANG_ONLINE_INQUIRY")
    codes = InquiryRepository(database).dashboard_store_codes()

    windowed = _page(
        database, stores_in_market(codes, ALL_MARKETS),
        start_date="2026-09-01", end_date="2026-09-30",
    )
    assert {row["source_question_id"] for row in windowed} == {"n-new", "c-new"}


def test_end_date_includes_that_whole_day(tmp_path) -> None:
    database = _database(tmp_path)
    _inquiry(database, store_code=COUPANG_NS, question_id="late",
             registered_at="2026-09-16T23:58:00+09:00",
             source_type="COUPANG_ONLINE_INQUIRY")
    _inquiry(database, store_code=COUPANG_NS, question_id="next-day",
             registered_at="2026-09-17T00:02:00+09:00",
             source_type="COUPANG_ONLINE_INQUIRY")
    rows = _page(
        database, [COUPANG_NS],
        start_date="2026-09-01", end_date="2026-09-16",
    )
    assert {row["source_question_id"] for row in rows} == {"late"}


# --- FLOW cards ------------------------------------------------------------

def test_new_counts_when_the_customer_asked_not_when_the_row_was_written(
    tmp_path,
) -> None:
    """A backfill inserts old inquiries today; they are not today's questions."""

    database = _database(tmp_path)
    old = _inquiry(database, store_code=NAVER_STORE, question_id="backfilled",
                   registered_at="2024-03-01T10:00:00+09:00")
    # created_at is "now" for a row inserted now, which is the whole problem.
    _set(database, old, created_at="2026-09-16T01:00:00+00:00")
    _inquiry(database, store_code=NAVER_STORE, question_id="recent",
             registered_at="2026-09-10T10:00:00+09:00")

    windowed = _cards(
        database, store_codes=None,
        start_date="2026-09-01", end_date="2026-09-30",
    )
    assert windowed["NEW"]["value"] == 1
    assert windowed["NEW"]["kind"] == "FLOW"


def test_flow_cards_move_with_the_date_range(tmp_path) -> None:
    database = _database(tmp_path)
    _inquiry(database, store_code=NAVER_STORE, question_id="jan",
             registered_at="2026-01-10T10:00:00+09:00")
    _inquiry(database, store_code=NAVER_STORE, question_id="sep",
             registered_at="2026-09-10T10:00:00+09:00")

    january = _cards(database, start_date="2026-01-01", end_date="2026-01-31")
    september = _cards(database, start_date="2026-09-01", end_date="2026-09-30")
    both = _cards(database, start_date="2026-01-01", end_date="2026-12-31")

    assert january["NEW"]["value"] == 1
    assert september["NEW"]["value"] == 1
    assert both["NEW"]["value"] == 2


def test_flow_cards_move_with_the_market(tmp_path) -> None:
    database = _database(tmp_path)
    _inquiry(database, store_code=NAVER_STORE, question_id="n1",
             registered_at="2026-09-01T10:00:00+09:00")
    _inquiry(database, store_code=COUPANG_NS, question_id="c1",
             registered_at="2026-09-02T10:00:00+09:00",
             source_type="COUPANG_ONLINE_INQUIRY")
    _inquiry(database, store_code=COUPANG_PLUS, question_id="c2",
             registered_at="2026-09-03T10:00:00+09:00",
             source_type="COUPANG_ONLINE_INQUIRY")
    window = {"start_date": "2026-09-01", "end_date": "2026-09-30"}
    codes = InquiryRepository(database).dashboard_store_codes()

    assert _cards(database, store_codes=stores_in_market(codes, "NAVER"),
                  **window)["NEW"]["value"] == 1
    assert _cards(database, store_codes=stores_in_market(codes, "COUPANG"),
                  **window)["NEW"]["value"] == 2
    assert _cards(database, store_codes=stores_in_market(codes, ALL_MARKETS),
                  **window)["NEW"]["value"] == 3


def test_drafted_counts_when_the_draft_was_written(tmp_path) -> None:
    """The inquiry arrived in January; the draft was written in September."""

    database = _database(tmp_path)
    inquiry_id = _inquiry(database, store_code=NAVER_STORE, question_id="n1",
                          registered_at="2026-01-10T10:00:00+09:00")
    _draft(database, inquiry_id, created_at="2026-09-10T02:00:00+00:00")

    january = _cards(database, start_date="2026-01-01", end_date="2026-01-31")
    september = _cards(database, start_date="2026-09-01", end_date="2026-09-30")

    assert january["DRAFTED"]["value"] == 0
    assert september["DRAFTED"]["value"] == 1
    assert september["NEW"]["value"] == 0


def test_approved_counts_on_the_approval_timestamp(tmp_path) -> None:
    database = _database(tmp_path)
    inquiry_id = _inquiry(database, store_code=NAVER_STORE, question_id="n1",
                          registered_at="2026-01-10T10:00:00+09:00")
    _set(database, inquiry_id, approval_status="APPROVED",
         approved_at="2026-09-10T02:00:00+00:00")

    january = _cards(database, start_date="2026-01-01", end_date="2026-01-31")
    september = _cards(database, start_date="2026-09-01", end_date="2026-09-30")

    assert january["APPROVED"]["value"] == 0
    assert september["APPROVED"]["value"] == 1


def test_flow_cards_no_longer_repeat_the_same_number_twice(tmp_path) -> None:
    database = _database(tmp_path)
    _inquiry(database, store_code=NAVER_STORE, question_id="n1",
             registered_at="2026-09-01T10:00:00+09:00")
    cards = _cards(database, start_date="2026-09-01", end_date="2026-09-30")
    for code in ("NEW", "DRAFTED", "APPROVED"):
        assert "total" not in cards[code]


# --- STOCK cards -----------------------------------------------------------

@pytest.mark.parametrize("code", ["REVIEW", "ATTENTION"])
def test_stock_cards_ignore_the_date_range(tmp_path, code) -> None:
    database = _database(tmp_path)
    waiting = _inquiry(database, store_code=NAVER_STORE, question_id="n1",
                       registered_at="2024-02-01T10:00:00+09:00")
    _set(database, waiting, workflow_status="NEEDS_ATTENTION",
         approval_status="PENDING", post_status="NOT_POSTED")

    narrow = _cards(database, start_date="2026-09-01", end_date="2026-09-30")
    wide = _cards(database, start_date="2024-01-01", end_date="2026-12-31")

    assert narrow[code]["kind"] == "STOCK"
    assert narrow[code]["value"] == wide[code]["value"] == 1


@pytest.mark.parametrize("code", ["REVIEW", "ATTENTION"])
def test_stock_cards_follow_the_market(tmp_path, code) -> None:
    database = _database(tmp_path)
    naver = _inquiry(database, store_code=NAVER_STORE, question_id="n1",
                     registered_at="2026-09-01T10:00:00+09:00")
    _set(database, naver, workflow_status="NEEDS_ATTENTION",
         approval_status="PENDING", post_status="NOT_POSTED")
    _inquiry(database, store_code=COUPANG_NS, question_id="c1",
             registered_at="2026-09-02T10:00:00+09:00",
             source_type="COUPANG_ONLINE_INQUIRY")
    codes = InquiryRepository(database).dashboard_store_codes()

    assert _cards(database, store_codes=stores_in_market(codes, "NAVER"))[code]["value"] == 1
    # Coupang Phase 1 rows sit at workflow NEW, so this is a real zero.
    assert _cards(database, store_codes=stores_in_market(codes, "COUPANG"))[code]["value"] == 0


def test_coupang_phase_one_cards_are_zero_because_the_state_says_so(tmp_path) -> None:
    database = _database(tmp_path)
    _inquiry(database, store_code=COUPANG_NS, question_id="c1",
             registered_at="2026-09-02T10:00:00+09:00",
             source_type="COUPANG_ONLINE_INQUIRY")
    cards = _cards(database, store_codes=[COUPANG_NS],
                   start_date="2026-09-01", end_date="2026-09-30")
    assert cards["NEW"]["value"] == 1
    for code in ("DRAFTED", "APPROVED", "REVIEW", "ATTENTION"):
        assert cards[code]["value"] == 0


def test_a_market_with_no_stores_counts_nothing_not_everything(tmp_path) -> None:
    database = _database(tmp_path)
    _inquiry(database, store_code=NAVER_STORE, question_id="n1",
             registered_at="2026-09-01T10:00:00+09:00")
    cards = _cards(database, store_codes=[],
                   start_date="2026-09-01", end_date="2026-09-30")
    assert cards["NEW"]["value"] == 0
    assert cards["REVIEW"]["value"] == 0
