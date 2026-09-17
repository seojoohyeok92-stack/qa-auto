"""Which marketplaces production may answer for, and what each is called.

This is the production-side counterpart to ``ui/market_labels``.  Services and
schedulers must not import that module: it belongs to the screen, and a
scheduler that reaches into the UI layer to decide what to process has put a
dashboard in charge of production.  Which market a store belongs to is decided
here by the same ``market_from_store_code`` that retrieval already uses, so
there is one rule rather than a ``startswith("COUPANG")`` in every service.

The policy itself is one set.  A market that is not in it is collected and
displayed and nothing else: no draft, no GPT, no DPS, no validator, no post,
no notification.  Opening a market is adding it here, not editing a guard in
each service.
"""

from __future__ import annotations

from collections.abc import Iterable

from repositories.learning_repository import market_from_store_code

NAVER = "NAVER"
COUPANG = "COUPANG"

# What each marketplace is called when a person reads it.
MARKET_DISPLAY_NAMES: dict[str, str] = {
    NAVER: "네이버",
    COUPANG: "쿠팡",
    "GMARKET": "G마켓",
}

# Markets whose inquiries production may answer, post and notify about.
#
# Coupang is deliberately absent.  Its inquiries are collected into the same
# table and shown on the same dashboard, and that is as far as it goes until
# its answers have been reviewed and a Coupang post API exists.  Phase B opens
# answer generation, Phase C notifications, Phase D posting -- each by a
# separate, explicit decision.
PRODUCTION_ANSWER_MARKETS: frozenset[str] = frozenset({NAVER})


def market_of(store_code: object) -> str | None:
    """The market a store code belongs to."""

    return market_from_store_code(store_code)


def market_display_name(market: object) -> str:
    """The marketplace name as it should appear to a person."""

    code = str(market or "").strip().upper()
    if not code:
        return ""
    return MARKET_DISPLAY_NAMES.get(code, code)


def store_display_name(store_code: object) -> str:
    """The marketplace name for a store code, never the code itself."""

    return market_display_name(market_of(store_code))


def account_of_store(store_code: object) -> str | None:
    """The seller account a Coupang store code belongs to."""

    code = str(store_code or "").strip().upper()
    prefix = "COUPANG_"
    return code[len(prefix):] or None if code.startswith(prefix) else None


def store_label(store_code: object) -> str:
    """What to call this store on screen.

    A Coupang store code carries its seller account, and that is the useful
    half: an operator knows 오제앤에스, not COUPANG_OJE_NS.  Naver stores keep
    the name they are configured with.  Either way the raw code stays out of
    the screen.
    """

    code = str(store_code or "").strip()
    if not code:
        return "-"
    account = account_of_store(code)
    if account is not None:
        from config import COUPANG_ACCOUNT_DISPLAY_NAMES

        return COUPANG_ACCOUNT_DISPLAY_NAMES.get(account, account)
    try:
        from config import get_store_config

        return get_store_config(code).name or code
    except Exception:
        return code


def is_answer_market_enabled(market: object) -> bool:
    """Whether production may generate, post and notify for this market."""

    return str(market or "").strip().upper() in PRODUCTION_ANSWER_MARKETS


def is_store_answer_enabled(store_code: object) -> bool:
    """Whether production may act on inquiries from this store."""

    return is_answer_market_enabled(market_of(store_code))


def answer_enabled_store_codes(store_codes: Iterable[object]) -> list[str]:
    """Keep only the stores whose market production may answer for.

    Used to scope the auto-post candidate query.  Filtering after the query
    would not be enough: the queue is ordered by arrival and limited, so a
    market that is collected but not answered would fill the page and starve
    the market that is.
    """

    return [
        text
        for code in store_codes
        if (text := str(code or "").strip()) and is_store_answer_enabled(text)
    ]
