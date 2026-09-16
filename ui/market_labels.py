"""What a marketplace is called on screen, and which stores belong to it.

Screens talk about 네이버 and 쿠팡; the database talks about ``OJE_PLUS`` and
``COUPANG_OJE_NS``.  Those internal codes carry provenance -- which account a
row came from -- and are never rewritten, so the translation lives here and
only here.

Which market a store belongs to is decided by
``repositories.learning_repository.market_from_store_code``, which retrieval
already uses.  Reusing it keeps one rule: a screen cannot group stores one way
while the runtime filter groups them another.
"""

from __future__ import annotations

from collections.abc import Iterable

from repositories.learning_repository import market_from_store_code

ALL_MARKETS = "ALL"
ALL_MARKETS_LABEL = "전체"

# Ordered, so the picker reads the same way every time.  A market that turns up
# in the data without an entry here keeps its own code as the label, which is
# how a new marketplace shows up before anyone names it.
MARKET_LABELS: dict[str, str] = {
    "NAVER": "네이버",
    "COUPANG": "쿠팡",
    "GMARKET": "G마켓",
}


def market_label(market: str | None) -> str:
    """The on-screen name for one market code."""

    code = str(market or "").strip().upper()
    if not code:
        return "-"
    return MARKET_LABELS.get(code, code)


def store_market(store_code: str | None) -> str | None:
    """Which market a store code belongs to."""

    return market_from_store_code(store_code)


def store_market_label(store_code: str | None) -> str:
    """The on-screen market name for one store code."""

    return market_label(store_market(store_code))


def markets_for_stores(store_codes: Iterable[str]) -> list[str]:
    """The markets present in a set of store codes, in display order."""

    found = {
        market
        for code in store_codes
        if (market := store_market(code)) is not None
    }
    ordered = [code for code in MARKET_LABELS if code in found]
    ordered.extend(sorted(found - set(ordered)))
    return ordered


def stores_in_market(store_codes: Iterable[str], market: str | None) -> list[str]:
    """The store codes belonging to one market.

    ``ALL`` (or nothing) means every store given, so a caller can pass the
    result straight to a query without special-casing the unfiltered view.
    """

    codes = [str(code) for code in store_codes if str(code or "").strip()]
    selected = str(market or ALL_MARKETS).strip().upper()
    if not selected or selected == ALL_MARKETS:
        return codes
    return [code for code in codes if store_market(code) == selected]


def stores_in_markets(
    store_codes: Iterable[str], markets: Iterable[str] | None
) -> list[str]:
    """The store codes belonging to any of several markets."""

    codes = [str(code) for code in store_codes if str(code or "").strip()]
    if markets is None:
        return codes
    wanted = {str(market).strip().upper() for market in markets if str(market or "").strip()}
    if not wanted or ALL_MARKETS in wanted:
        return codes
    return [code for code in codes if store_market(code) in wanted]
