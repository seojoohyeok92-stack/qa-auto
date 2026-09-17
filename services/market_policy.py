"""Which marketplaces production may answer for, and what each is called.

This is the production-side counterpart to ``ui/market_labels``.  Services and
schedulers must not import that module: it belongs to the screen, and a
scheduler that reaches into the UI layer to decide what to process has put a
dashboard in charge of production.  Which market a store belongs to is decided
here by the same ``market_from_store_code`` that retrieval already uses, so
there is one rule rather than a ``startswith("COUPANG")`` in every service.

What production may do for a market is split by action, because the actions
open at different times.  Generating an answer for a person to review is not
posting it, and neither is sending a notification about it:

* answer generation   -- AnswerService, template/rule, GPT, validator, drafts,
                         approval and Final Answer, started by a person
* automatic generation -- the same, started without one (selection, sync,
                         the auto-post pipeline and its prewarm)
* DPS                 -- order lookup and delivery/installation schedules
* post                -- registering an answer at the marketplace, auto-post
* Kakao               -- notifications about generated or posted answers

A market absent from every set is collected and displayed and nothing else.
Opening an action for a market is adding it to that one set, not editing a
guard in each service.
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

# Phase 2-1: Coupang answers may be generated, reviewed and approved by a
# person.  Nothing generates them unasked, nothing looks up a Coupang order or
# DPS schedule, nothing posts them and nothing notifies about them -- each of
# those opens later by its own explicit decision.
ANSWER_GENERATION_MARKETS: frozenset[str] = frozenset({NAVER, COUPANG})
AUTOMATIC_GENERATION_MARKETS: frozenset[str] = frozenset({NAVER})
DPS_MARKETS: frozenset[str] = frozenset({NAVER})
POST_MARKETS: frozenset[str] = frozenset({NAVER})
KAKAO_MARKETS: frozenset[str] = frozenset({NAVER})

# Wording that names one marketplace's own procedure.  An answer for another
# market must not carry it: "네이버페이 > 결제내역" is wrong advice to a Coupang
# customer however correct the rest of the sentence is.
MARKET_SPECIFIC_WORDING: dict[str, tuple[str, ...]] = {
    NAVER: ("네이버", "스마트스토어", "smartstore", "naver"),
    COUPANG: ("쿠팡", "coupang", "로켓배송"),
}


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


def _market_in(market: object, markets: frozenset[str]) -> bool:
    return str(market or "").strip().upper() in markets


def is_store_answer_generation_enabled(store_code: object) -> bool:
    """Whether a person may generate and review answers for this store."""

    return _market_in(market_of(store_code), ANSWER_GENERATION_MARKETS)


def is_store_automatic_generation_enabled(store_code: object) -> bool:
    """Whether an answer may be generated without a person asking for it."""

    return _market_in(market_of(store_code), AUTOMATIC_GENERATION_MARKETS)


def is_store_dps_enabled(store_code: object) -> bool:
    """Whether order lookup and DPS may run for this store's inquiries."""

    return _market_in(market_of(store_code), DPS_MARKETS)


def is_store_post_enabled(store_code: object) -> bool:
    """Whether an answer may be registered at this store's marketplace."""

    return _market_in(market_of(store_code), POST_MARKETS)


def is_kakao_market_enabled(market: object) -> bool:
    """Whether Kakao notifications may be sent about this market."""

    return _market_in(market, KAKAO_MARKETS)


def post_enabled_store_codes(store_codes: Iterable[object]) -> list[str]:
    """Keep only the stores whose market production may post to.

    Used to scope the auto-post candidate query.  Filtering after the query
    would not be enough: the queue is ordered by arrival and limited, so a
    market that is collected but not posted would fill the page and starve
    the market that is.
    """

    return [
        text
        for code in store_codes
        if (text := str(code or "").strip()) and is_store_post_enabled(text)
    ]


def foreign_market_wording(text: object, market: object) -> tuple[str, ...]:
    """Words in ``text`` that belong to a marketplace other than ``market``.

    Empty for an unknown market: nothing can be foreign to no market.
    """

    own = str(market or "").strip().upper()
    if not own:
        return ()
    body = str(text or "").lower()
    return tuple(dict.fromkeys(
        word
        for other, words in MARKET_SPECIFIC_WORDING.items()
        if other != own
        for word in words
        if word.lower() in body
    ))
