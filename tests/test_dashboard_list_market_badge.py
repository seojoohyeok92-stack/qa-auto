"""The market chip on an inquiry list row.

With 전체 selected nothing on a row said whether it was a Naver or a Coupang
inquiry.  The chip names the market from the store code through the shared
market rule, appears only when every market is listed, and never shows an
internal store code.  Display only: the rows, query and filters are unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from ui.market_labels import ALL_MARKETS, shows_market_badge
from ui.review_workspace import (
    INQUIRY_LIST_WIDTHS,
    INQUIRY_LIST_WIDTHS_WITH_MARKET,
    _market_badge_html,
)

NAVER_ROW = {
    "store_code": "OJE_PLUS", "source": "PRODUCT_INQUIRY", "inquiry_id": "n-9001",
    "product_name": "네이버 상품명", "content": "네이버 문의 내용", "answered": False,
    "queue": "AUTO_PROCESSABLE", "registered_at": "2026-09-17T10:00:00+09:00",
    "learning_labels": ["승인"],
}
COUPANG_NS_ROW = {
    "store_code": "COUPANG_OJE_NS", "source": "COUPANG_ONLINE_INQUIRY",
    "inquiry_id": "160936208", "product_name": "삼탠바이미", "content": "쿠팡 문의 내용",
    "answered": True, "registered_at": "2026-09-17T11:00:00+09:00", "learning_labels": ["-"],
}
COUPANG_PLUS_ROW = dict(COUPANG_NS_ROW, store_code="COUPANG_OJE_PLUS", inquiry_id="160936209",
                        answered=False)


# --- when -----------------------------------------------------------------

def test_only_the_all_markets_filter_shows_the_chip() -> None:
    assert shows_market_badge(ALL_MARKETS) is True
    assert shows_market_badge(None) is True
    assert shows_market_badge("NAVER") is False
    assert shows_market_badge("COUPANG") is False


# --- what -----------------------------------------------------------------

def test_a_naver_row_is_named_naver() -> None:
    html = _market_badge_html(NAVER_ROW, True)
    assert ">네이버<" in html and "naver" in html


@pytest.mark.parametrize("row", [COUPANG_NS_ROW, COUPANG_PLUS_ROW], ids=["ns", "plus"])
def test_a_coupang_row_is_named_coupang_without_its_store_code(row) -> None:
    html = _market_badge_html(row, True)
    assert ">쿠팡<" in html and "coupang" in html
    assert row["store_code"] not in html
    assert "OJE" not in html


@pytest.mark.parametrize("row", [NAVER_ROW, COUPANG_NS_ROW])
def test_a_single_market_filter_draws_no_chip(row) -> None:
    assert _market_badge_html(row, False) == ""


def test_a_market_added_to_the_rule_needs_no_list_change(monkeypatch) -> None:
    import ui.review_workspace as workspace

    monkeypatch.setattr(workspace, "store_market", lambda code: "GMARKET")
    assert ">G마켓<" in _market_badge_html({"store_code": "GMARKET_MAIN"}, True)


def test_a_row_without_a_store_draws_nothing() -> None:
    assert _market_badge_html({"store_code": ""}, True) == ""


def test_the_widened_type_column_keeps_the_row_the_same_width() -> None:
    assert sum(INQUIRY_LIST_WIDTHS_WITH_MARKET) == pytest.approx(sum(INQUIRY_LIST_WIDTHS))
    assert len(INQUIRY_LIST_WIDTHS_WITH_MARKET) == len(INQUIRY_LIST_WIDTHS)


def test_the_two_markets_have_their_own_tones() -> None:
    css = (Path(__file__).resolve().parents[1] / "ui" / "dashboard.css").read_text(encoding="utf-8")
    assert ".official-market-badge.naver" in css
    assert ".official-market-badge.coupang" in css


# --- the real list render -------------------------------------------------------

def _list_app(show: bool) -> None:
    from ui.review_workspace import _render_list, _render_list_header

    rows = [
        {"store_code": "OJE_PLUS", "source": "PRODUCT_INQUIRY", "inquiry_id": "n-9001",
         "product_name": "네이버 상품명", "content": "네이버 문의 내용", "answered": False,
         "queue": "AUTO_PROCESSABLE", "registered_at": "2026-09-17T10:00:00+09:00",
         "learning_labels": ["승인"]},
        {"store_code": "COUPANG_OJE_NS", "source": "COUPANG_ONLINE_INQUIRY",
         "inquiry_id": "160936208", "product_name": "삼탠바이미", "content": "쿠팡 문의 내용",
         "answered": True, "registered_at": "2026-09-17T11:00:00+09:00",
         "learning_labels": ["-"]},
    ]
    _render_list_header(2, show_market_badge=show)
    _render_list(rows, 2, show_market_badge=show)


def _rendered(show: bool) -> str:
    app = AppTest.from_function(_list_app, args=(show,))
    app.run()
    assert not app.exception
    return "\n".join(str(element.value) for element in app.markdown)


def test_all_markets_render_shows_both_chips_and_keeps_the_row() -> None:
    html = _rendered(True)

    assert ">네이버</span>" in html
    assert ">쿠팡</span>" in html
    assert "마켓·유형" in html
    # Internal codes stay off the screen.
    assert "COUPANG_OJE_NS" not in html and "OJE_PLUS" not in html
    # The rest of the row is what it was.
    for expected in ("고객문의", "상품문의", "삼탠바이미", "네이버 상품명", "쿠팡 문의 내용",
                     "답변완료", "생성가능", "승인", "official-learning-badge",
                     "received-time"):
        assert expected in html, expected


def test_a_single_market_render_has_no_chip_and_the_old_header() -> None:
    html = _rendered(False)

    assert "official-market-badge" not in html
    assert "마켓·유형" not in html and "문의유형" in html
    for expected in ("고객문의", "삼탠바이미", "답변완료", "생성가능", "승인", "received-time"):
        assert expected in html, expected
