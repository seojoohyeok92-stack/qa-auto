"""Which knowledge from one marketplace may answer another's question.

``is_market_applicable`` decides this from the row's origin, and the rule it
applies -- Naver knowledge may answer a Coupang question, not the reverse --
is about the *corpus*: the Naver corpus has been curated for years and a
Coupang inquiry about the same product deserves it.  What it cannot see is
what an individual row actually says.

Measured on the 26.10.12 server snapshot, the Coupang candidate pool was
1,831 rows: Coupang's own 765 plus 1,066 Naver rows, every one of them
carrying no applicability metadata at all.  Among those 1,066, once the
company template frame was removed, 88 claims still routed the customer
through a Naver-only surface -- 네이버폼 (170 mentions), 톡톡 (46),
네이버페이 (34), 거래명세표 (24), 스마트스토어 (8).  They ranked: asked
"거래명세서 발급 받을 수 있나요?", all eight of the top-eight Learning rows
offered to a Coupang inquiry were Naver document-procedure answers, one of
them explaining how to capture a 네이버쇼핑 주문내역.

So this module adds the test the origin rule does not make: a claim may cross
a market boundary only if it does not send the customer to a different
marketplace's own channel.

**It reads the claim, not the answer.**  Every answer this system writes ends
in a template footer naming 네이버 톡톡, so scanning whole answers condemned
307 of the 1,066 rows -- 211 of them for the boilerplate alone, including a
model's Bluetooth specification.  ``extract_answer_body`` removes the frame
first, exactly as ``claim_body`` does for the hedge check, for the same
reason and with the same consequence if skipped.

**It does not read the existing scope taxonomy**, because that taxonomy is
blind on half this corpus: 537 of the 1,066 rows carry no ``product_scope``
at all, and 58 of the 88 channel-bound claims are in that half.  In the other
direction ``scope=POLICY`` covers 242 rows of which only 24 are channel-bound
-- a manufacturer's warranty terms are policy-shaped and perfectly shareable.
Scope answers "which products does this apply to"; it was never asked which
marketplace, and widening it to mean that would be wrong in both directions.

**A row that states its own applicability is still believed.**  COMMON and
``<MARKET>_ONLY`` are an operator's explicit decision and keep the authority
they already had; this gate speaks only where the row says nothing, which is
every legacy row and therefore the whole cross-market surface today.
"""

from __future__ import annotations

import re

from .answer_format import extract_answer_body

#: Surfaces that belong to exactly one marketplace: naming one is naming that
#: market.  Deliberately not a list of market names -- "네이버" appears in
#: answers that merely mention where a customer bought, and in one Naver row
#: that tells a Coupang buyer to use 구매처(쿠팡).  What makes a claim
#: unusable elsewhere is the channel it routes the customer through.
MARKET_CHANNELS: dict[str, re.Pattern[str]] = {
    "NAVER": re.compile(
        r"톡톡"
        r"|스마트\s*스토어"
        r"|네이버\s*(?:페이|폼|쇼핑|스토어|톡톡)"
        r"|N\s*Pay"
        r"|거래\s*명세[서표]"
        r"|상품\s*주문\s*번호"
        # One market's storefront, which is what a purchase link is. The
        # naver.com shortener is deliberately absent: the same short link
        # serves the 거래명세서 form for Coupang buyers too.
        r"|smartstore\.naver\.com"
        r"|shopping\.naver\.com",
        re.IGNORECASE,
    ),
    "COUPANG": re.compile(
        r"쿠팡\s*(?:윙|wing|페이|이츠)"
        r"|wing\.coupang"
        r"|로켓\s*(?:배송|와우)",
        re.IGNORECASE,
    ),
}

#: Words that name a marketplace itself.  On their own they are not a reason
#: to withhold a claim; they qualify the generic procedure terms below.
MARKET_NAMES: dict[str, re.Pattern[str]] = {
    "NAVER": re.compile(r"네이버|smartstore|naver", re.IGNORECASE),
    "COUPANG": re.compile(r"쿠팡|coupang", re.IGNORECASE),
}

#: Procedures every marketplace has its own version of.  A claim that walks
#: the customer through one is market-bound, but only once it says whose --
#: "반품은 수령 후 7일 이내 가능합니다" is a statement of consumer law and
#: crosses freely, while "네이버 결제내역에서 반품 신청" does not.
MARKET_PROCEDURES = re.compile(
    r"구매처"
    r"|주문\s*(?:내역|목록|상세|번호)"
    r"|결제\s*내역"
    r"|구매\s*내역"
    r"|반품\s*(?:신청|접수)"
    r"|교환\s*(?:신청|접수)"
    r"|취소\s*(?:신청|접수)"
    r"|환불\s*(?:신청|접수)"
    r"|영수증\s*(?:발급|출력)",
)


#: A claim whose answer *is* a marketplace's name: "구매처는 네이버로 기재
#: 부탁드립니다".  These carry no procedure noun at all -- fourteen of them
#: are a single terse sentence -- and they are the most directly wrong thing
#: that can cross, because the value a Coupang buyer must enter is 쿠팡.
MARKET_AS_VALUE = re.compile(r"기재|입력|적어|적으시|선택|작성|써\s*주|기입")

#: Politeness that carries no claim, removed before asking whether the
#: claim is nothing but a market name.
_COPULA = re.compile(r"입니다|이에요|예요|이다|로요|요")


#: Every token either tier can match, as one pattern.  A claim is a substring
#: of its answer, so an answer none of these appear in cannot produce a claim
#: that does -- which lets the frame stripping be skipped entirely.  Worth it:
#: stripping three fields costs about 0.7 ms a row and the pool holds up to
#: 2,000, and two thirds of the measured Naver corpus has no signal at all.
_ANY_MARKET_SIGNAL = re.compile(
    "|".join(
        pattern.pattern
        for pattern in (*MARKET_CHANNELS.values(), *MARKET_NAMES.values())
    ),
    re.IGNORECASE,
)


def foreign_market_channel_in(market: object, *answers: object) -> str:
    """``foreign_market_channel`` over raw answers, frame stripped on demand.

    The cheap pattern runs on the stored text; only an answer that mentions
    some marketplace at all is worth un-wrapping, and only the un-wrapped
    claim decides -- the template footer names 네이버 톡톡 in every answer
    this system has ever written.
    """

    raw = " ".join(str(answer or "") for answer in answers)
    if not _ANY_MARKET_SIGNAL.search(raw):
        return ""
    return foreign_market_channel(claim_text(*answers), market)


def claim_text(*answers: object) -> str:
    """The answers' claims with the company template frame removed.

    The frame is what every answer shares, so anything found only there is a
    fact about the template and not about this claim.
    """

    return " ".join(
        body for body in (extract_answer_body(str(a or "")) for a in answers)
        if body
    )


def _is_bare_market_name(text: str, market: str) -> bool:
    """Whether the claim says nothing but this market's name."""

    stripped = _COPULA.sub("", text)
    stripped = re.sub(r"[^0-9A-Za-z가-힣]+", "", stripped)
    return bool(stripped) and bool(
        MARKET_NAMES[market].fullmatch(stripped)
    )


def foreign_market_channel(claim: object, market: object) -> str:
    """Which other marketplace's channel this claim routes the customer to.

    ``""`` when none does.  Both tiers are required to name a market that is
    not the reading one: a Naver-origin claim that says "구매처(쿠팡)" is
    about Coupang and is not withheld from Coupang.
    """

    text = str(claim or "")
    if not text.strip():
        return ""
    reading = str(market or "").strip().upper()
    # A claim that names the reading market as well is not routing the
    # customer elsewhere -- it is the manufacturer's own procedure listing
    # every marketplace it applies to. Measured: five rows telling the
    # customer to enter 구매처 as "구매해주신 마켓(네이버,옥션,G마켓,쿠팡)"
    # are exactly the ones a Coupang buyer needs, while two reading
    # "구매처 항목은 '네이버'로 선택해 주세요" are not. Only the second tier
    # is exempted -- a named channel such as 네이버폼 is unusable elsewhere
    # however many markets the surrounding sentence lists.
    names_reading = (
        reading in MARKET_NAMES and bool(MARKET_NAMES[reading].search(text))
    )
    for other, channels in MARKET_CHANNELS.items():
        if other == reading:
            continue
        if channels.search(text):
            return other
        if names_reading:
            continue
        if not MARKET_NAMES[other].search(text):
            continue
        if MARKET_PROCEDURES.search(text) or MARKET_AS_VALUE.search(text):
            return other
        # The whole claim is the other market's name -- "네이버입니다." is
        # the shortest form of the same answer, with the question it answered
        # ("구매처가 어디인가요") left behind in the inquiry.
        if _is_bare_market_name(text, other):
            return other
    return ""


def crosses_markets_safely(
    *, claim: object, origin_market: object, market: object,
) -> bool:
    """Whether knowledge from ``origin_market`` may answer ``market``.

    Only asked once the origin rule has already allowed the row, and only
    about a row that states no applicability of its own.  Same-market reads
    are never gated: a Naver inquiry's own corpus is unchanged by any of this.

    ``claim`` is the frame-stripped text.  Callers holding the raw answers
    should use ``answers_cross_markets_safely``, which strips only when there
    is something to strip for.
    """

    origin = str(origin_market or "").strip().upper()
    reading = str(market or "").strip().upper()
    if not reading or not origin or origin == reading:
        return True
    return not foreign_market_channel(claim, reading)


def answers_cross_markets_safely(
    *, origin_market: object, market: object, answers: tuple[object, ...],
) -> bool:
    """``crosses_markets_safely`` for a caller holding the stored answers."""

    origin = str(origin_market or "").strip().upper()
    reading = str(market or "").strip().upper()
    if not reading or not origin or origin == reading:
        return True
    return not foreign_market_channel_in(reading, *answers)
