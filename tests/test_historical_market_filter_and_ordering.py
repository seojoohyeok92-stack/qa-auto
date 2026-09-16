"""Market-facing filters and one stable order for stored Historical cases.

Two things are being pinned here.  A reviewer picks 네이버 or 쿠팡, never
``COUPANG_OJE_NS`` -- the store code carries which account a row came from and
must survive the translation untouched.  And every listing surface returns the
same order, decided in SQL before ``LIMIT``, because a set ordered only within
a page loses rows across page boundaries.
"""

from __future__ import annotations

import pytest

from repositories.database import Database
from repositories.historical_case_repository import HistoricalCaseRepository
from services.historical_case_service import HistoricalCaseService
from ui.market_labels import (
    ALL_MARKETS,
    market_label,
    markets_for_stores,
    store_market_label,
    stores_in_market,
    stores_in_markets,
)

NAVER_STORE = "OJE_PLUS"
COUPANG_NS = "COUPANG_OJE_NS"
COUPANG_PLUS = "COUPANG_OJE_PLUS"


def _database(tmp_path) -> Database:
    database = Database(tmp_path / "historical-market.db")
    database.initialize()
    return database


def _seed(
    database: Database,
    *,
    external_id: str,
    store_code: str,
    inquiry_created_at: str | None,
    inquiry_type: str = "PRODUCT_INQUIRY",
) -> dict:
    service = HistoricalCaseService(database)
    case = service.prepare_case(
        {
            "title": "문의",
            "content": f"{external_id} 제품 기능이 궁금합니다.",
            "seller_answer": "해당 모델은 VESA 100x100 규격을 지원합니다.",
            "external_inquiry_id": external_id,
            "source_type": inquiry_type,
            "store_code": store_code,
            "source_answered": True,
            "source_created_at": inquiry_created_at,
        },
        source_reference=f"TEST:{external_id}",
    )
    case["quality_score"] = 0.80
    case["active"] = True
    stored, _ = HistoricalCaseRepository(database).upsert(case)
    return stored


# --- market labelling ------------------------------------------------------

def test_store_codes_are_grouped_into_markets() -> None:
    assert store_market_label(NAVER_STORE) == "네이버"
    assert store_market_label(COUPANG_NS) == "쿠팡"
    assert store_market_label(COUPANG_PLUS) == "쿠팡"


def test_market_options_follow_the_stores_that_exist() -> None:
    assert markets_for_stores([NAVER_STORE, COUPANG_NS, COUPANG_PLUS]) == [
        "NAVER", "COUPANG",
    ]
    assert markets_for_stores([COUPANG_NS]) == ["COUPANG"]


def test_internal_codes_never_become_the_display_name() -> None:
    labels = {
        store_market_label(code)
        for code in (NAVER_STORE, COUPANG_NS, COUPANG_PLUS)
    }
    assert labels == {"네이버", "쿠팡"}
    for internal in ("OJE_PLUS", "COUPANG_OJE_NS", "COUPANG_OJE_PLUS"):
        assert internal not in labels
    assert market_label("GMARKET") == "G마켓"


def test_coupang_market_covers_both_accounts() -> None:
    codes = [NAVER_STORE, COUPANG_NS, COUPANG_PLUS]
    assert set(stores_in_market(codes, "COUPANG")) == {COUPANG_NS, COUPANG_PLUS}
    assert stores_in_market(codes, "NAVER") == [NAVER_STORE]
    assert stores_in_market(codes, ALL_MARKETS) == codes


def test_several_markets_select_their_union() -> None:
    codes = [NAVER_STORE, COUPANG_NS, COUPANG_PLUS]
    assert set(stores_in_markets(codes, ["NAVER", "COUPANG"])) == set(codes)
    assert stores_in_markets(codes, None) == codes
    assert stores_in_markets(codes, [ALL_MARKETS]) == codes


# --- market filtering against stored rows ----------------------------------

def test_market_filter_selects_stored_rows_without_rewriting_them(tmp_path) -> None:
    database = _database(tmp_path)
    _seed(database, external_id="n1", store_code=NAVER_STORE,
          inquiry_created_at="2026-09-01T10:00:00+09:00")
    _seed(database, external_id="c1", store_code=COUPANG_NS,
          inquiry_created_at="2026-09-02T10:00:00+09:00")
    _seed(database, external_id="c2", store_code=COUPANG_PLUS,
          inquiry_created_at="2026-09-03T10:00:00+09:00")
    repository = HistoricalCaseRepository(database)
    stored_codes = repository.distinct_store_codes()

    naver = repository.list_cases(
        store_codes=stores_in_market(stored_codes, "NAVER")
    )
    coupang = repository.list_cases(
        store_codes=stores_in_market(stored_codes, "COUPANG")
    )

    assert {row["store_code"] for row in naver} == {NAVER_STORE}
    assert {row["store_code"] for row in coupang} == {COUPANG_NS, COUPANG_PLUS}
    # The provenance codes are exactly what they were.
    with database.connection() as connection:
        codes = {
            row[0] for row in connection.execute(
                "SELECT DISTINCT store_code FROM historical_cases"
            )
        }
    assert codes == {NAVER_STORE, COUPANG_NS, COUPANG_PLUS}


def test_empty_market_scope_returns_nothing_rather_than_everything(tmp_path) -> None:
    database = _database(tmp_path)
    _seed(database, external_id="n1", store_code=NAVER_STORE,
          inquiry_created_at="2026-09-01T10:00:00+09:00")
    assert HistoricalCaseRepository(database).list_cases(store_codes=[]) == []


# --- ordering --------------------------------------------------------------

def test_newest_inquiry_first_then_id_descending(tmp_path) -> None:
    database = _database(tmp_path)
    old = _seed(database, external_id="old", store_code=NAVER_STORE,
                inquiry_created_at="2026-01-01T09:00:00+09:00")
    tie_a = _seed(database, external_id="tie-a", store_code=NAVER_STORE,
                  inquiry_created_at="2026-09-01T10:00:00+09:00")
    tie_b = _seed(database, external_id="tie-b", store_code=COUPANG_NS,
                  inquiry_created_at="2026-09-01T10:00:00+09:00")
    newest = _seed(database, external_id="newest", store_code=COUPANG_PLUS,
                   inquiry_created_at="2026-09-05T10:00:00+09:00")

    rows = HistoricalCaseRepository(database).list_cases()

    assert [row["id"] for row in rows] == [
        newest["id"], tie_b["id"], tie_a["id"], old["id"]
    ]


def test_case_with_no_readable_inquiry_date_sorts_last(tmp_path) -> None:
    """Fallback must not lift a legacy row above genuinely recent inquiries."""

    database = _database(tmp_path)
    recent = _seed(database, external_id="recent", store_code=NAVER_STORE,
                   inquiry_created_at="2026-09-01T10:00:00+09:00")
    legacy = _seed(database, external_id="legacy", store_code=NAVER_STORE,
                   inquiry_created_at=None)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE historical_cases SET inquiry_created_at=NULL, "
            "answer_updated_at=NULL WHERE id=?",
            (int(legacy["id"]),),
        )

    rows = HistoricalCaseRepository(database).list_cases()
    assert rows[0]["id"] == recent["id"]
    assert rows[-1]["id"] == legacy["id"]


def test_paging_covers_every_row_exactly_once(tmp_path) -> None:
    """The boundary case: order is decided before LIMIT, not inside a page."""

    database = _database(tmp_path)
    for index in range(12):
        _seed(
            database, external_id=f"p{index:02d}", store_code=NAVER_STORE,
            inquiry_created_at=f"2026-09-{index + 1:02d}T10:00:00+09:00",
        )
    repository = HistoricalCaseRepository(database)
    everything = [row["id"] for row in repository.list_cases()]

    paged: list[int] = []
    for page in range(4):
        paged.extend(
            row["id"] for row in repository.list_cases(limit=5, offset=page * 5)
        )

    assert paged == everything
    assert len(paged) == len(set(paged)) == 12


def test_count_matches_what_listing_would_return(tmp_path) -> None:
    database = _database(tmp_path)
    _seed(database, external_id="n1", store_code=NAVER_STORE,
          inquiry_created_at="2026-09-01T10:00:00+09:00")
    _seed(database, external_id="c1", store_code=COUPANG_NS,
          inquiry_created_at="2026-09-02T10:00:00+09:00")
    repository = HistoricalCaseRepository(database)
    scope = {"store_codes": [COUPANG_NS]}
    assert repository.count_cases(**scope) == len(repository.list_cases(**scope))


def test_ordering_survives_filtering(tmp_path) -> None:
    database = _database(tmp_path)
    _seed(database, external_id="n-old", store_code=NAVER_STORE,
          inquiry_created_at="2026-01-01T10:00:00+09:00")
    c_old = _seed(database, external_id="c-old", store_code=COUPANG_NS,
                  inquiry_created_at="2026-02-01T10:00:00+09:00")
    c_new = _seed(database, external_id="c-new", store_code=COUPANG_PLUS,
                  inquiry_created_at="2026-09-01T10:00:00+09:00")

    rows = HistoricalCaseRepository(database).list_cases(
        store_codes=[COUPANG_NS, COUPANG_PLUS]
    )
    assert [row["id"] for row in rows] == [c_new["id"], c_old["id"]]


def test_inquiry_type_options_come_from_stored_values(tmp_path) -> None:
    database = _database(tmp_path)
    _seed(database, external_id="t1", store_code=NAVER_STORE,
          inquiry_created_at="2026-09-01T10:00:00+09:00",
          inquiry_type="PRODUCT_INQUIRY")
    _seed(database, external_id="t2", store_code=NAVER_STORE,
          inquiry_created_at="2026-09-02T10:00:00+09:00",
          inquiry_type="CUSTOMER_INQUIRY")
    assert HistoricalCaseRepository(database).distinct_inquiry_types() == [
        "CUSTOMER_INQUIRY", "PRODUCT_INQUIRY",
    ]


def test_market_filtering_does_not_touch_learning_state(tmp_path) -> None:
    database = _database(tmp_path)
    case = _seed(database, external_id="state-1", store_code=COUPANG_NS,
                 inquiry_created_at="2026-09-01T10:00:00+09:00")
    repository = HistoricalCaseRepository(database)
    before = repository.get(int(case["id"]))

    repository.list_cases(store_codes=[COUPANG_NS])
    repository.count_cases(store_codes=[COUPANG_NS])

    after = repository.get(int(case["id"]))
    for field in ("active", "promoted_learning_id", "quality_score", "policy_risk"):
        assert after[field] == before[field]
