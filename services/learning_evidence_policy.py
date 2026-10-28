"""When an approved Learning answer may stand as evidence for a product fact.

The Product Knowledge DB is authoritative but partial.  Treating "the DB has no
row for this field" as "the product does not do this" is the single most
damaging mistake available here: it turns a gap in our records into a negative
claim about the product.  It also throws away the answers staff wrote and a
person explicitly approved -- the most reliable evidence the system has about
anything the DB has not catalogued yet.

So a missing fact stops meaning "unverified" *by itself*.  What replaces it is
not a weaker test but a different one, and every clause below already exists
somewhere in the pipeline; this module only insists that all of them hold at
once before an approved answer may settle a product-fact question:

  approved      a person reviewed and approved it (authority APPROVED, and
                never a seller_style_examples entry -- those are tone, not fact)
  same product  the compatibility gate resolved this candidate to the exact
                model, product id, or distinctive name.  For a strict product
                topic it hard-rejects anything less, so Model A's stand answer
                can never settle Model B's stand question
  on point      the approved answer actually supports *this* question, by the
                same answer-support measure retrieval uses -- not string
                similarity between the two questions
  definite      the answer states something.  A hedged answer is a person
                declining to commit, and inheriting that as a verified fact
                would publish a guess wearing a verified label
  undisputed    no VERIFIED product fact contradicts it

A Learning answer contradicted by a verified Product Fact is reported as a
conflict for a person to settle.  Two Learning answers that disagree are not
a data conflict: both reach GPT ②, which judges which one applies.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from answer.answer_format import extract_answer_body
from repositories.learning_repository import is_common_learning
from answer.learning_signal import detect_polarity
from services.auto_post_validation_service import INTERNAL_PLACEHOLDER

# Product identity verdicts the compatibility gate issues when it resolved the
# candidate to this exact product. Anything else it returns is either a hard
# reject or a policy-level (product-independent) match, neither of which may
# settle a fact about one specific model.
EXACT_PRODUCT_MATCHES = frozenset({
    "EXACT_MODEL", "EXACT_PRODUCT", "EXACT_NAME",
})

# A person hedging. The polarity detector already reads most hedged phrasings
# as UNCERTAIN; these catch the ones that pair a hedge with a definite verb
# ("가능할 것 같습니다"), where the affirmative marker would otherwise win.
#
# A literal-substring list is the wrong shape for Korean: the same hedge
# appears as 보입니다 / 보여집니다 / 보이며 / 보이는데, and listing every
# inflection is how "것으로 보입니다" came to be missing while "것 같" was
# present -- which let an approved answer reading "사용 가능하실 것으로
# 보입니다" stand as a definite fact. The patterns below match the *stem* of
# each hedge and let the ending vary.
HEDGE_MARKERS = (
    "것 같", "것같", "듯 합니다", "듯합니다", "아마", "추정", "예상됩니다",
    "확인이 필요", "확인 후", "확인해 보", "알아보",
)

_HEDGE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # ...것으로 보입니다 / 보여집니다 / 보이며 — the one that got through.
    re.compile(r"보입니다|보여집니다|보이며|보이는데|보여요"),
    # 예상 / 추정 / 추측 / 판단 / 사료 in any ending.
    re.compile(r"예상[되돼]|예상됩니다|추정[되됩]|추측|판단[되됩]|사료[되됩]"),
    # "...할 것으로", "...인 것으로" — a projection, not a statement.
    re.compile(r"[을ㄹ] 것으로|인 것으로|것으로 [보예판사]"),
    # 가능성 / ~일 수 있 / ~수도 있 — possibility, not fact.
    re.compile(r"가능성이|[울ㄹ] 수(?:도)? 있|있을 수 있|다를 수 있|상이할 수 있"),
    # Deferral: the writer is telling the customer to check elsewhere.
    re.compile(r"확인이 (?:필요|되어야)|확인 후|확인해 ?[보주]|확인 바랍|"
               r"확인하시기|확인해주시기|문의(?:해|하시어) ?보|알아보"),
    # Explicit uncertainty about their own statement.
    re.compile(r"정확하[지진] ?않|정확도가|불확실|명확하지 ?않|장담"),
    # Hearsay.
    re.compile(r"[로으로] 알고 ?있|알려져 ?있|듣기로"),
    # Approximation used as the answer itself.
    re.compile(r"대략|어느 ?정도|약간|보통은|일반적으로는"),
)


def claim_body(answer: object) -> str:
    """The part of the answer that states something, without the frame.

    Every answer this system writes is wrapped in the company template, and
    that template's closing reads "...담당자가 확인 후 안내드리겠습니다".
    Scanning the whole answer for hedges therefore found "확인 후" in the
    boilerplate of *every* answer, so no answer the pipeline had ever
    produced could serve as evidence -- including "LS27D400 모델은 스피커가
    내장되어 있지 않습니다", which is as definite as a claim gets.

    The frame says nothing about the product, so it is removed before the
    uncertainty check. A deferral written in the body ("주문·배송 상태 확인이
    필요합니다") still reads as a deferral.
    """

    body = extract_answer_body(str(answer or ""))
    return _text(body or answer)


# The subset of the markers above that means *the writer is guessing*, as
# opposed to the writer deferring. Both belong in ``hedge_reason`` -- neither
# kind of sentence commits to a fact, which is what the validator's conflict
# checks need to know -- but they are not the same finding when deciding what
# may be shown to the model as an approved answer.
#
# "결제 확인 후 설치 기사님 일정에 맞춰 배송이 진행됩니다" is the store's actual
# new-order policy, and it carries "확인 후" as a *sequence*, not a deferral.
# Rejecting it as a guess would have thrown away one of the few genuinely
# reusable delivery policies in the corpus, which is the false-rejection this
# work is meant to avoid rather than cause.
_ESTIMATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"보입니다|보여집니다|보이며|보이는데|보여요"),
    re.compile(r"예상[되돼]|예상됩니다|추정[되됩]|추측|판단[되됩]|사료[되됩]"),
    re.compile(r"[을ㄹ] 것으로|인 것으로|것으로 [보예판사]"),
    re.compile(r"가능성이|[울ㄹ] 수(?:도)? 있|있을 수 있|다를 수 있|상이할 수 있"),
    re.compile(r"정확하[지진] ?않|정확도가|불확실|명확하지 ?않|장담"),
    re.compile(r"[로으로] 알고 ?있|알려져 ?있|듣기로"),
    re.compile(r"대략|어느 ?정도|약간|보통은|일반적으로는"),
)
_ESTIMATION_MARKERS = ("것 같", "것같", "듯 합니다", "듯합니다", "아마", "추정", "예상됩니다")


def estimation_reason(answer: object) -> str | None:
    """Which *guess* marker this answer carries, or None.

    Narrower than ``hedge_reason`` on purpose: an answer that says a person
    will check is honest and reusable, while an answer that guesses at the
    fact is the one that must not be offered as grounds for a definite claim.
    """

    text = claim_body(answer)
    if not text:
        return "EMPTY"
    for marker in _ESTIMATION_MARKERS:
        if marker in text:
            return marker
    for pattern in _ESTIMATION_PATTERNS:
        found = pattern.search(text)
        if found:
            return found.group(0)
    return None


def hedge_reason(answer: object) -> str | None:
    """Which uncertainty marker this answer carries, or None."""

    text = claim_body(answer)
    if not text:
        return "EMPTY"
    for marker in HEDGE_MARKERS:
        if marker in text:
            return marker
    for pattern in _HEDGE_PATTERNS:
        found = pattern.search(text)
        if found:
            return found.group(0)
    return None

# A stated quantity in a declarative sentence: "HDMI 단자는 3개입니다",
# "본체 무게는 6.5kg입니다", "해상도는 3840x2160입니다".
#
# ``detect_polarity`` exists to catch two yes/no statements that flatly
# disagree, and returns UNCERTAIN for anything without a 가능/불가 style
# marker -- which is every measurement there is. Reading that UNCERTAIN as
# "the writer declined to commit" disqualified precisely the approved answers
# that state a number, so Learning could settle "지원하나요?" but never
# "몇 개인가요?" -- the questions it is most often approved for.
_QUANTITY_STATEMENT = re.compile(
    r"\d+(?:[.,]\d+)?\s*"
    r"(?:개|대|장|인치|cm|mm|m|kg|g|w|hz|ms|년|월|일|시간|분|x|×)?"
    r"[^.!?\n]*"
    r"(?:입니다|이에요|예요|됩니다|습니다|이며|이고|입니다\.)"
)

# Fact values that carry a polarity of their own. A boolean-ish product fact
# ("stand_detachable: false") is a claim about the product, and an approved
# answer saying the opposite contradicts it even though neither text contains
# Korean polarity grammar.
_NEGATIVE_VALUES = frozenset({
    "false", "no", "n", "0", "none", "unsupported", "미지원", "불가", "불가능",
    "없음", "아니오", "비지원", "미제공",
})
_AFFIRMATIVE_VALUES = frozenset({
    "true", "yes", "y", "1", "supported", "지원", "가능", "있음", "예", "제공",
})


@dataclass(frozen=True)
class LearningEvidenceDecision:
    """Whether approved Learning settles this question, and why or why not."""

    usable: bool
    reason: str
    conflict: bool = False
    learning_ids: tuple[int, ...] = ()
    subquestions: tuple[str, ...] = ()
    conflicts: tuple[dict[str, Any], ...] = dataclass_field(default=())

    def to_dict(self) -> dict[str, Any]:
        return {
            "usable": self.usable,
            "reason": self.reason,
            "conflict": self.conflict,
            "learning_ids": list(self.learning_ids),
            "subquestions": list(self.subquestions),
            "conflicts": [dict(item) for item in self.conflicts],
        }


def is_operator_common_row(item: Mapping[str, Any]) -> bool:
    """Whether this evidence row is an operator-approved common Learning.

    Two shapes reach this module. The repository row carries
    ``metadata_json``; the row projected into ``similar_approved_answers`` --
    which is what ``evaluate`` actually receives -- does not, and carries the
    ``operator_common_policy`` flag that ``SimilarAnswerService`` puts there
    instead. Reading only the metadata made the precedence below unreachable
    in the real pipeline while the unit tests passed, so both are read.
    """

    if bool(item.get("operator_common_policy")):
        return True
    metadata = item.get("metadata_json")
    return is_common_learning(metadata if isinstance(metadata, Mapping) else {})


def _text(value: object) -> str:
    return " ".join(str(value or "").split())


def is_hedged(answer: object) -> bool:
    """True when the answer declines to commit to what it is being read for.

    A hedge marker settles it outright, and a clear yes/no polarity settles the
    opposite. What remains is everything the polarity detector has no opinion
    about, which includes every measurement -- so a stated quantity in a
    declarative sentence counts as committing, and anything else stays hedged.
    """

    text = claim_body(answer)
    if not text:
        return True
    if hedge_reason(answer) is not None:
        return True
    if detect_polarity(text) != "UNCERTAIN":
        return False
    return not _QUANTITY_STATEMENT.search(text)


# A number with its middle blanked out -- "주문번호 2026****2541". Eleven live
# rows carry one, and one of those is factual-eligible. It is the same thing a
# ``<masked-order>`` token is: a record that something was removed. Copied into
# a new answer it would hand a customer an order number that does not exist.
#
# Digits are required on both sides so a decorative "***" separator, or a
# seller writing "★★★", is not mistaken for a redaction. Scoped to the Learning
# evidence policy rather than added to INTERNAL_PLACEHOLDER, which the
# validator also reads -- widening a published answer's blocking rule is a
# separate decision from deciding what may be reused as evidence.
_PARTIALLY_MASKED_NUMBER = re.compile(r"\d{2,}\*{2,}\d{2,}")


# --- provenance ------------------------------------------------------------
#
# "A person approved this" is one bit, and it was being asked to carry a
# distinction it cannot hold. 299 of the 306 human_verified rows are seller
# answers that were already posted on Naver and later marked verified in bulk;
# 7 are answers a member of staff actually edited or reviewed in this system.
# Ranking read the bit alone, so the 299 sat at or above the 7.
#
# The store already records the difference -- ``answer_provenance`` says who
# wrote the text, ``facts_authority`` says what it was accepted as -- so the
# class is read, never inferred. Nothing here guesses from the answer body.
#
# Staff who compose an answer from scratch (DIRECT_HUMAN) are deliberately
# absent: no column distinguishes them from an edited draft today, and
# inventing the class would mean guessing. They fall to UNKNOWN_PROVENANCE,
# which sits at the bottom of the ladder rather than the top.
APPROVED_EDITED = "APPROVED_EDITED"
APPROVED_UNEDITED = "APPROVED_UNEDITED"
SELLER_ANSWER_VERIFIED = "SELLER_ANSWER_VERIFIED"
SELLER_ANSWER = "SELLER_ANSWER"
HISTORICAL_PROMOTED = "HISTORICAL_PROMOTED"
UNKNOWN_PROVENANCE = "UNKNOWN_PROVENANCE"

# Authority *within* Learning, used only to order candidates relevance and the
# compatibility gate have already accepted as equally applicable. It is not a
# licence to admit anything: a class absent from this table keeps whatever the
# existing source table gives it.
#
# APPROVED_EDITED and APPROVED_UNEDITED share one number on purpose. The table
# used to read 8 and 6, which said an answer is more trustworthy because a
# member of staff had to change it. That is the wrong question. Both rows
# reached the store through the same gate -- a person read the final answer and
# approved it -- and the edit only records how the text got to the state they
# approved. An answer GPT wrote correctly the first time and an answer a member
# of staff rewrote are, once approved, the same claim about the world with the
# same person behind it.
#
# The distinction itself is kept: ``classify_provenance`` still returns
# APPROVED_EDITED / APPROVED_UNEDITED, both classes still appear here, and
# ``learning_source`` / ``answer_provenance`` still record which path an answer
# took, so operations can still ask how often drafts need editing. It just no
# longer decides which Learning to believe. Inside this tier, relevance,
# atomic-question match, product/model scope, validity and answer support
# choose -- all of which are about *this* question, which is what "which
# Learning is right here" actually depends on.
HUMAN_APPROVED_TRUST = 8
LEARNING_AUTHORITY: Mapping[str, int] = {
    APPROVED_EDITED: HUMAN_APPROVED_TRUST,
    APPROVED_UNEDITED: HUMAN_APPROVED_TRUST,
    # Bulk-verified Naver answers stay below both: nobody approved them one by
    # one through the review path, which is the distinction that does mean
    # something about trust.
    SELLER_ANSWER_VERIFIED: 4,
}

# The provenance classes a person explicitly approved, one at a time. Callers
# ask this rather than comparing the two names, so no new call site can
# reintroduce an edited/unedited trust gap.
HUMAN_APPROVED_PROVENANCES: frozenset[str] = frozenset(
    {APPROVED_EDITED, APPROVED_UNEDITED}
)


def human_approved_trust(item: Mapping[str, Any]) -> bool:
    """Whether this Learning row carries the human-approved trust tier."""

    return classify_provenance(item) in HUMAN_APPROVED_PROVENANCES


def classify_provenance(item: Mapping[str, Any]) -> str:
    """Which kind of answer a Learning row actually is."""

    metadata = item.get("metadata_json")
    metadata = metadata if isinstance(metadata, dict) else {}
    source = str(item.get("learning_source") or "").upper()
    origin = str(metadata.get("source_origin") or "").upper()
    provenance = str(metadata.get("answer_provenance") or "").upper()

    # Checked first: a promoted historical case is historical whatever else is
    # stamped on it, and must not reach a staff tier through its source name.
    if origin == HISTORICAL_PROMOTED:
        return HISTORICAL_PROMOTED
    if metadata.get("human_verified") is not True:
        return SELLER_ANSWER if source == SELLER_ANSWER else UNKNOWN_PROVENANCE
    if provenance == "STAFF_EDITED" or source == APPROVED_EDITED:
        return APPROVED_EDITED
    if provenance == "PROGRAM_GENERATED" or source == APPROVED_UNEDITED:
        return APPROVED_UNEDITED
    if provenance == "NAVER_POSTED":
        return SELLER_ANSWER_VERIFIED
    return UNKNOWN_PROVENANCE


# An answer that asks the customer for their order number only makes sense
# when the answer depends on *that customer's* order. Retrieval had no way to
# see this: a stored answer for "내 주문 언제 와요?" and one for "주문 전인데
# 며칠 걸려요?" share every delivery word, so the first was reused for the
# second and a customer who had explicitly not ordered yet was asked for an
# order number. The distinction is not a phrase to exclude, it is a scope --
# the semantic understanding already says whether this question needs
# customer-specific order evidence, and this reads whether the candidate
# answer only works inside that scope.
_ORDER_IDENTIFIER_REQUEST: tuple[re.Pattern[str], ...] = (
    # A demand, not a mention. "주문번호는 네이버 주문내역에서 확인하실 수
    # 있습니다" tells the customer where their own number lives and is
    # perfectly reusable before an order exists, so a bare 확인 is not enough
    # -- the verb has to be directed at the customer.
    re.compile(
        r"주문\s*번호[^\n]{0,24}"
        r"(?:알려|남겨|기재해|입력해|회신|부탁|요청|유도|"
        r"안내\s*(?:를)?\s*(?:해야|하는|필요)|"
        r"확인\s*(?:해|하여)?\s*주|확인이\s*필요|전달\s*(?:해|하여)?\s*주)"
    ),
    re.compile(
        r"(?:알려|남겨|기재|입력|전달|회신)[^\n]{0,14}주문\s*번호"
    ),
    re.compile(r"주문\s*번호를?\s*(?:함께|같이)?\s*(?:비밀글|비공개)"),
)


# The other half of the same scope. "설치예정일이 있을경우 날짜를 안내해야함"
# is sound advice about a customer whose installation is already booked, and
# nonsense for one who has not ordered -- there is no date to look up. Mirrors
# learning_context_service's schedule regex, duplicated here for the same
# reason answer_diff_classifier duplicates it: neither module may perturb the
# retrieval module's own behaviour.
_CURRENT_SCHEDULE_SCOPE = re.compile(
    r"예정일|도착일|설치일|출고일|배송\s*일자|내\s*주문|주문한\s*(?:제품|상품)"
)


def current_schedule_scope_reason(text: object) -> str | None:
    """Whether this text only makes sense for an order that already exists."""

    body = _text(text)
    if not body:
        return None
    found = _CURRENT_SCHEDULE_SCOPE.search(body)
    return found.group(0) if found else None


# A general delivery lead time: how long this product usually takes, as
# opposed to when one customer's order arrives. "43인치 제품의 경우 구매일로부터
# 설치까지 1~2주 정도 소요되실 예정입니다" is the first; "현재 확인되는 설치
# 예정일은 8월 14일" is the second, and the first is only reusable if it is
# read apart from the second.
#
# Read per sentence, because that is the unit a claim lives in. One approved
# answer routinely carries both a standing lead time and a fact that was only
# true that day -- "블랙은 스탠드 품절로 2주, 화이트는 3시까지 주문 시 당일
# 출고" -- and admitting or refusing the whole row would either reuse the stock
# fact or throw the standing policy away.
_LEAD_TIME = re.compile(
    r"\d+\s*[~\-～]\s*\d+\s*(?:영업일|일|주|개월)"
    r"|약\s*\d+\s*(?:영업일|일|주|개월)"
    r"|\d+\s*(?:영업일|주일|개월)"
    r"|\d+\s*(?:주|일)\s*(?:정도|가량|이내|안에|쯤|내외)"
    r"|당일\s*(?:출고|발송)"
)
# What the duration is the duration *of*. "작성 후 발송까지 1~2일" is how long a
# trade statement takes by e-mail and "5영업일 내에 발송" is a review reward;
# both carry a lead time and neither is about receiving the product. Bare 발송
# is therefore not a subject on its own -- documents are 발송 too -- while
# "구매일로부터 약 1-2주 소요되는 상품입니다" names no delivery word and is one.
_DELIVERY_SUBJECT = re.compile(
    r"배송|설치|도착|출고|수령|받아\s*보|받으실|납기|구매일\s*로부터"
)
_OTHER_SUBJECT = re.compile(
    r"명세서|영수증|상품권|포인트|리뷰|환불|환급|메일|네이버\s*폼|폼\s*작성"
)
# A lead time that holds only while something lasts: a stock-out, an event
# backlog, a surge. "삼성 감사 페스티벌 여파로 약 1~2주" was true for one
# fortnight in August and is not this product's lead time.
_TEMPORARY_LEAD_TIME = re.compile(
    r"품절|재고|입고|지연|여파|폭주|주문량|페스티벌|감사제|행사|이벤트|명절|연휴"
    # "희망하신 날짜와 1~2일 차이가 발생할 수 있으며" is a margin on a date
    # the customer chose, not how long the product takes.
    r"|차이|오차"
)
# A named day is one order's schedule, never a lead time.
_SPECIFIC_DAY = re.compile(
    r"\d{1,2}\s*월\s*\d{1,2}\s*일|(?<![\d.])\d{1,2}\s*/\s*\d{1,2}(?!\d)"
    r"|20\d{2}\s*[년./-]"
    r"|(?:오늘|내일|모레|금일|명일)\s*(?:출고|발송|도착|배송|설치)"
    r"|이번\s*주|다음\s*주"
    r"|[월화수목금토일]요일"
)
# Colour is the variant the corpus actually splits lead times on (M5 white
# ships the same day, black waits for the stand). A claim that names one is
# that variant's lead time, and is usable only when the customer's own option
# names the same one.
_VARIANT_WORDS = (
    "화이트", "블랙", "그레이", "실버", "베이지", "핑크", "네이비",
    "white", "black",
)


def _claim_sentences(answer: object) -> list[str]:
    body = extract_answer_body(str(answer or "")) or str(answer or "")
    return [
        " ".join(part.split())
        for part in re.split(r"(?<=[.!?])\s+|\n+|(?<=다\.)", body)
        if part.strip()
    ]


def general_delivery_estimate_claims(
    answer: object, *, option_name: object = None,
) -> tuple[str, ...]:
    """The sentences of ``answer`` that state a reusable general lead time.

    Empty when the answer states none. A sentence qualifies only when it names
    a duration of delivery or installation and nothing that ties it to one
    order, one day, one stock level or one event, and -- when it is scoped to a
    colour -- only when the customer's option is that colour. Without a known
    option a colour-scoped lead time is not used: the wider claim cannot be
    narrowed on a guess.
    """

    option = str(option_name or "").lower()
    claims: list[str] = []
    for sentence in _claim_sentences(answer):
        if not _LEAD_TIME.search(sentence):
            continue
        if not _DELIVERY_SUBJECT.search(sentence):
            continue
        if (
            _OTHER_SUBJECT.search(sentence)
            or _TEMPORARY_LEAD_TIME.search(sentence)
            or _SPECIFIC_DAY.search(sentence)
        ):
            continue
        named = [word for word in _VARIANT_WORDS if word in sentence.lower()]
        if named and not any(word in option for word in named):
            continue
        claims.append(sentence)
    return tuple(claims)


def order_identifier_request_reason(answer: object) -> str | None:
    """Which "tell us your order number" phrasing this answer carries, or None.

    Read as scope, never as a keyword ban: an answer that merely mentions an
    order number ("주문번호는 네이버 주문내역에서 확인하실 수 있습니다") is
    describing where to find one, not demanding one, and stays usable.
    """

    text = _text(answer)
    if not text:
        return None
    for pattern in _ORDER_IDENTIFIER_REQUEST:
        found = pattern.search(text)
        if found:
            return found.group(0)[:60]
    return None


def contamination_reason(value: object) -> str | None:
    """The redaction token this text carries, or None.

    ``<masked-phone>`` and its siblings are written when the pipeline removes
    something. A stored answer containing one is a record of a removal, not a
    statement about the product, and the token itself must never reach a
    customer -- so such an answer can neither prove a claim nor be shown to
    the model as an example worth copying.
    """

    text = _text(value)
    found = INTERNAL_PLACEHOLDER.search(text)
    if found:
        return found.group(0)
    partial = _PARTIALLY_MASKED_NUMBER.search(text)
    return partial.group(0) if partial else None


def usable_as_factual_evidence(item: Mapping[str, Any] | Any) -> bool:
    """Whether one retrieved item's text may ground a factual claim.

    Deliberately shallow: identity, approval and topic scope are judged by
    the compatibility gate and ``_qualifying`` below. This answers only the
    question those two never ask -- is the *text itself* the kind of thing
    that can prove something?
    """

    if not isinstance(item, Mapping):
        return False
    if item.get("style_only"):
        return False
    # Learning items carry ``answer``; historical cases are raw rows and carry
    # ``seller_answer``. Reading only one of them would silently drop a whole
    # evidence class from the grounding corpus.
    text = _text(
        item.get("answer")
        or item.get("final_answer")
        or item.get("seller_answer")
    )
    if not text:
        return False
    if contamination_reason(text) is not None:
        return False
    return not is_hedged(text)


_NUMBER = re.compile(r"\d+(?:\.\d+)?")

#: The letter runs that still mean a measurement when they follow a number.
#: Anything else glued to a number is part of a name, not a quantity.
_UNIT_SUFFIX = re.compile(
    "(?:mm|cm|m|inch|in|ft|kg|g|w|v|a|hz|khz|ms|sec|s|k|nit|p)",
    re.IGNORECASE,
)
_TRAILING_LETTERS = re.compile(r"[A-Za-z]+")
#: Characters that join digits into one identifier: a phone number, an order
#: number, a date, a masked number.
_IDENTIFIER_JOINERS = frozenset({"-", ".", "/", "*"})
#: No product specification is this many digits; an order number is.
_IDENTIFIER_DIGITS = 7


def _is_identifier_number(text: str, match: "re.Match[str]") -> bool:
    """Whether this number names something rather than measuring it.

    Reading every digit run as a claim is what turned a service-centre phone
    number into a conflicting specification: an A/S referral whose only
    numbers were 1588-3366 was reported as contradicting
    ``pivot_spec = -92 ~ +92``, because the two sets of numbers shared
    nothing. The answer said nothing about pivot at all.

    What separates the two is the company a number keeps, not its size.
    1588-3366 is one hyphen-joined identifier; -92 ~ +92 is two signed
    measurements. LS27HG400 glues digits to letters; 120mm glues them to a
    unit. So the neighbours decide, and a bare measurement is untouched.
    """

    before = text[:match.start()]
    after = text[match.end():]
    # Part of a longer joined run: a phone number, a date, a masked number.
    if before[-1:] in _IDENTIFIER_JOINERS and before[-2:-1].isdigit():
        return True
    if after[:1] == "*" or before[-1:] == "*":
        # A masked number: "2026****0811" is one order number.
        return True
    if after[:1] in _IDENTIFIER_JOINERS and after[1:2].isdigit():
        return True
    # Preceded by an ASCII letter: a model code. Korean is not excluded --
    # "약 3개" and "총 2채널" are measurements their own language marks.
    if before[-1:].isascii() and before[-1:].isalpha():
        return True
    # Followed by ASCII letters that are not a unit: also a model code.
    # The letters glued to the number have to be *exactly* a unit. "120mm"
    # and "6.5kg" are measurements; the "EKXKR" of LS27HG400EKXKR is not.
    letters = _TRAILING_LETTERS.match(after)
    if letters is not None and not _UNIT_SUFFIX.fullmatch(letters.group()):
        return True
    if len(match.group().split(".")[0]) >= _IDENTIFIER_DIGITS:
        return True
    return False


def quantities(value: object) -> frozenset[float]:
    """Every number ``value`` states as a measurement.

    Admitting measurements as definite (see ``is_hedged``) means the conflict
    checks have to be able to see a measurement disagree. Polarity cannot:
    "3개입니다" and "2개입니다" are both UNCERTAIN to it, so without this the
    two would have been treated as agreeing, and one of them published.

    Bare numbers, deliberately: a unit-aware comparison would have to decide
    that 6.5kg and 6500g are the same claim and that 3개 and 3.0 are too, and
    getting that wrong in either direction is worse than the small cost of
    treating an incidental number as a claim. Disagreement here only ever
    routes an inquiry to a person.

    What is not admitted is a number that names something --
    ``_is_identifier_number`` says which -- because a phone number sharing no
    digits with a specification is not a disagreement about the product.
    """

    text = _text(value)
    return frozenset(
        float(match.group())
        for match in _NUMBER.finditer(text)
        if not _is_identifier_number(text, match)
    )


def quantities_conflict(left: object, right: object) -> bool:
    """True when both state numbers and share none of them."""

    left_values = quantities(left)
    right_values = quantities(right)
    if not left_values or not right_values:
        return False
    return not (left_values & right_values)


def value_polarity(value: object) -> str:
    """AFFIRMATIVE/NEGATIVE for a boolean-ish fact value, else UNCERTAIN."""

    text = _text(value).lower().rstrip(".")
    if not text:
        return "UNCERTAIN"
    if text in _NEGATIVE_VALUES:
        return "NEGATIVE"
    if text in _AFFIRMATIVE_VALUES:
        return "AFFIRMATIVE"
    return detect_polarity(text)


# Where a policy-effective/approval time is recorded. ``updated_at`` is
# deliberately absent: it moves when a row is used, which is not when a person
# approved it. Moved here from ``similar_answer_service`` so the conflict
# resolver and the PK comparison read the same value.
_APPROVAL_TIME_KEYS = (
    "effective_at", "approved_at", "verified_at", "valid_from",
)

# How long a person's approval outranks the catalogue. Confirmed policy: inside
# the window the approval is the more recent statement about the product;
# outside it the catalogue is.
APPROVAL_PRECEDENCE_WINDOW = timedelta(days=90)

LEARNING_WINS = "LEARNING"
PRODUCT_FACT_WINS = "PRODUCT_FACT"

# A measurement condition printed into the fact's own value ("스탠드포함",
# "(가로x높이x깊이)"). When the catalogue states one and the stored answer does
# not repeat it, the two numbers were not necessarily measured the same way, so
# the condition is not confirmed and no winner may be chosen.
_CONDITION_MARKER = re.compile(
    r"스탠드\s*(?:포함|미포함|제외)|받침대\s*(?:포함|미포함)|벽\s*걸이\s*기준"
    r"|\(\s*가로|W\s*x|포장\s*(?:포함|기준)"
)


def approval_time(item: Mapping[str, Any]) -> datetime | None:
    """When a person approved this row, or None when nothing records it."""

    metadata = item.get("metadata_json")
    metadata = metadata if isinstance(metadata, dict) else {}
    for key in _APPROVAL_TIME_KEYS:
        raw = metadata.get(key) if key in metadata else item.get(key)
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            continue
        return parsed if parsed.tzinfo is not None else parsed.replace(
            tzinfo=timezone.utc)
    return None


def _same_exact_model(
    item: Mapping[str, Any],
    *,
    asked_product_id: object,
    exact_model_for: Any,
) -> str | None:
    """The one canonical model both listings are proven to sell, or None.

    ``learning_examples.model_code`` is blank on every row in the 29 conflicts
    measured on the operational copy, so the model has to come from the listing
    each side belongs to. ``exact_model_for`` is the catalogue's own
    ``_integrated_exact_model_for_listing``: an API ``modelName`` provenance
    for that exact product id, value equal to the model code, no compound
    identifier. Both sides must resolve through it and agree.

    Deliberately not ``_listing_declared_base_model``. That one splits a
    compound name ("LS27FM501EKXKR+이동식 스탠드") to give a listing some
    specification, which is the right trade for reading facts and the wrong one
    for deleting a person's answer: 6 of the 29 resolve only that way and they
    stay unresolved. A shared product id is not accepted on its own either --
    one listing can sell several option models, and the resolver is what proves
    it sells exactly one.
    """

    if not callable(exact_model_for):
        return None
    asked = str(asked_product_id or "").strip()
    origin = str(item.get("source_product_id") or "").strip()
    if not asked or not origin:
        return None
    asked_model = str(exact_model_for(asked) or "").strip()
    origin_model = str(exact_model_for(origin) or "").strip()
    if not asked_model or asked_model != origin_model:
        return None
    return asked_model


def fact_conflict_winner(
    item: Mapping[str, Any],
    *,
    fact_value: object,
    answered_at: datetime | None,
    asked_product_id: object = None,
    exact_model_for: Any = None,
) -> str | None:
    """Which side a conflict belongs to, or None when nothing may decide it.

    Confirmed policy, and every clause is a condition that must be *read*, not
    assumed. A winner is returned only when all of them hold:

      동일 canonical 모델   the row names the model it applies to and
                            compatibility matched that exact model. 29 of the
                            conflicts measured on the operational copy carry an
                            empty ``model_code``, so they never get here.
      동일 측정 조건        the catalogue value does not print a measurement
                            condition the stored answer leaves out.
      사람 건별 승인        ``HUMAN_APPROVED_PROVENANCES`` -- the review path,
                            not the ``human_verified`` bit.
      승인 시각 확인        ``approval_time`` found one.

    With all four in hand the window decides: inside it the approval is the
    newer statement and Learning wins, outside it the catalogue does. An
    unclear approval history leaves the catalogue in front, and anything that
    fails the first two conditions returns None -- the existing conflict hold.
    """

    # An operator's common Learning wins first, before the exact-model test.
    #
    # Confirmed policy: inside the range the operator approved, their explicit
    # common approval outranks the catalogue. The exact-model requirement below
    # exists to establish that a row is about *this* product; a common Learning
    # is about every product in the range it states, which the operator decided
    # and the retrieval pipeline already enforced -- this row only reaches here
    # after ``candidates()`` applied active/validity/market and compatibility
    # applied model and family. So the test it cannot pass is also the test it
    # does not need.
    #
    # Only the explicit approval qualifies. ``is_common_learning`` reads
    # ``metadata_json.common_learning``, written by the two approval paths
    # alone, so the 559 rows whose POLICY scope the lexical profiler inferred
    # and every ordinary Positive keep the existing precedence exactly.
    #
    # The measurement-condition test still applies: two numbers measured
    # differently are not a disagreement anybody can settle.
    if is_operator_common_row(item):
        fact_text = str(fact_value or "")
        if _CONDITION_MARKER.search(fact_text) and not _CONDITION_MARKER.search(
            str(item.get("answer") or "")
        ):
            return None
        return LEARNING_WINS

    model = str(item.get("model_code") or "").strip()
    if not model:
        # The row does not name its model. The listings on both sides may still
        # prove one, through the catalogue's own EXACT resolver.
        model = _same_exact_model(
            item,
            asked_product_id=asked_product_id,
            exact_model_for=exact_model_for,
        ) or ""
    if not model:
        return None
    compatibility = item.get("compatibility")
    compatibility = compatibility if isinstance(compatibility, Mapping) else {}
    if str(compatibility.get("product_match") or "").upper() != "EXACT_MODEL":
        return None
    fact_text = str(fact_value or "")
    if _CONDITION_MARKER.search(fact_text) and not _CONDITION_MARKER.search(
        str(item.get("answer") or "")
    ):
        return None
    if classify_provenance(item) not in HUMAN_APPROVED_PROVENANCES:
        # Approved, but not one at a time. The catalogue stays in front.
        return PRODUCT_FACT_WINS
    approved = approval_time(item)
    if approved is None or answered_at is None:
        return PRODUCT_FACT_WINS
    return (
        LEARNING_WINS
        if (answered_at - approved) <= APPROVAL_PRECEDENCE_WINDOW
        else PRODUCT_FACT_WINS
    )


def _qualifying(items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Approved, exact-product, on-point, definite candidates."""

    kept: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("authority") or "").upper() != "APPROVED":
            continue
        compatibility = item.get("compatibility")
        compatibility = (
            compatibility if isinstance(compatibility, Mapping) else {}
        )
        if (
            str(compatibility.get("product_match") or "")
            not in EXACT_PRODUCT_MATCHES
            # An operator's common Learning states its own range, and the
            # retrieval pipeline already enforced it -- active, in period, the
            # right market, the right model or family. The exact-product test
            # is how this function establishes that a row is about the asked
            # product, and for these rows the operator established it instead.
            #
            # Without this they never reach ``fact_conflicts`` at all, so the
            # confirmed precedence below could not apply to the rows it was
            # written for: a common Learning that disagrees with the catalogue
            # was simply absent from the comparison.
            #
            # ``usable`` becoming true for such a row changes one telemetry
            # field (``learning_verified`` in the auto-post decision record)
            # and no gate: auto-post allowance is decided by the eligibility
            # service, which does not read it.
            and not is_operator_common_row(item)
        ):
            continue
        # ``answer_support`` -- how much of the question's wording reappears in
        # the answer -- used to be a floor here. It measures lexical overlap,
        # and a correct answer phrased differently from the question scores
        # zero, so the floor rejected exactly the evidence a paraphrase should
        # have supplied. Approval, exact product identity and definiteness are
        # what this function can judge; whether the answer is on point is a
        # judgement about meaning and belongs to GPT ②.
        if is_hedged(item.get("answer")):
            continue
        kept.append(dict(item))
    return kept


def _fact_quantity_text(value: object) -> str:
    """The numbers a fact value states, whatever shape it is stored in.

    Values arrive as scalars (``3``), mappings (``{"inch": 43}``) and lists
    (``[450, 500, 550]``); flattening to text lets one number comparison read
    all three without teaching it each field's schema.
    """

    if isinstance(value, Mapping):
        return " ".join(str(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(str(item) for item in value)
    return str(value or "")


def _fields_the_text_is_about(text: str) -> tuple[frozenset[str], tuple[str, ...]]:
    """Which stored fields this text actually discusses.

    Reuses the routing Product Knowledge already uses to turn a question into
    field keys, plus the attribute families' own fragments -- the two
    vocabularies between them cover both spellings of a field ("speaker" is
    reached by a family fragment, "speaker_present" by the topic table).

    Imported here rather than at module scope: the catalogue repository is a
    heavy import that this module otherwise never needs.
    """

    from services.product_knowledge_service import (
        _family_fragments, fields_for_question,
    )

    body = _text(text)
    if not body:
        return frozenset(), ()
    routed, _topics = fields_for_question(body)
    fragments = tuple(
        part for part in _family_fragments(body.lower()) if part
    )
    return frozenset(routed), fragments


def _is_about_field(
    field_key: str, routed: frozenset[str], fragments: tuple[str, ...]
) -> bool:
    """Whether the text states something about *this* field, not its family.

    A fragment counts only when it *is* the field, never when it is a substring
    of a sibling's name. ``_family_fragments`` returns every target of a family
    the text touched -- it is built for retrieval, which would rather offer one
    candidate too many than miss the row that answers the question. Reading a
    fact conflict is the opposite trade, because it deletes a person's answer,
    and the substring rule crossed attributes inside one family:

      "RF단자가 있어 동축케이블 연결도 가능합니다" yields ('hdmi', 'usb',
      'port', 'ethernet', 'lan_'), and ``'usb' in 'usb_port_count'`` read that
      answer as a statement about the USB port count (inquiries 11205, 11207).

      "유선 LAN 포트는 지원하지 않습니다" reached ``hdmi_present`` the same way
      and, being a negative against an affirmative fact, became a conflict on
      the polarity path with no number involved at all.

    ``speaker`` and ``hdmi`` are still reached, because for those the fragment
    and the field are the same word. The topic table keeps its own membership
    check: it routes a question to the fields that question asked about, which
    is already the judgement wanted here. Retrieval's own matching is
    untouched -- this function has only ever been read from ``fact_conflicts``.
    """

    key = str(field_key or "").lower()
    if not key:
        return False
    if field_key in routed:
        return True
    return any(part == key for part in fragments)


def fact_conflicts(
    items: Iterable[Mapping[str, Any]],
    safe_facts: Iterable[Any],
    *,
    scope_for: Any = None,
) -> tuple[dict[str, Any], ...]:
    """Approved answers contradicted by a VERIFIED product fact.

    A verified fact is never silently overwritten by Learning.  When the two
    disagree the answer is not "trust the DB" either -- both are evidence a
    person produced, and which of them is stale is not something this code can
    know.  It is reported as a conflict so a person decides.

    ``scope_for`` maps one answer's matched sub-question to the field keys that
    sub-question put in play, or ``None`` for no narrowing. A compound inquiry
    needs it: this check compares a stored polarity with a stored value and
    knows what neither is about, so on "폐가전 수거되나요? 설치는 누가 하나요?
    해상도는요?" it reported the collection answer as contradicted by
    ``resolution=4K UHD`` -- a fact belonging to the third sub-question and to
    nothing the collection answer says. Each answer is now weighed only against
    the facts its own sub-question asked for.
    """

    usable = _qualifying(items)
    facts = [fact for fact in safe_facts if fact is not None]
    found: list[dict[str, Any]] = []
    for item in usable:
        answer_polarity = detect_polarity(item.get("answer"))
        allowed = None
        if callable(scope_for):
            allowed = scope_for(item.get("matched_subquestion"))
        # Which attributes this answer is about. ``scope_for`` narrows by what
        # the *sub-question* asked, which is not the same thing: a stored
        # answer matched to "피벗 기능이 가능한가요?" may be an A/S referral
        # that mentions no attribute at all, and comparing its sentence
        # polarity with ``speaker = 없음`` then made an unrelated affirmative
        # sentence contradict a verified absence. An answer can only
        # contradict a fact it talks about.
        routed, fragments = _fields_the_text_is_about(item.get("answer"))
        for fact in facts:
            field_key = getattr(fact, "field_key", "")
            if allowed is not None and field_key not in allowed:
                continue
            if not _is_about_field(field_key, routed, fragments):
                continue
            value = getattr(fact, "value", None)
            polarity = value_polarity(value)
            opposed_polarity = (
                answer_polarity != "UNCERTAIN"
                and polarity not in {"UNCERTAIN", answer_polarity}
            )
            # A verified count must also be able to contradict an approved
            # answer that states a different count; neither text carries a
            # polarity, so polarity alone would call them compatible. The
            # comparison reads the unit, so a number about something else in
            # the same sentence is not a disagreement -- see
            # ``_measurements_conflict``.
            opposed_quantity = _measurements_conflict(
                item.get("answer"), _fact_quantity_text(value),
                unit=getattr(fact, "unit", None),
            )
            if not opposed_polarity and not opposed_quantity:
                continue
            found.append({
                "learning_id": item.get("learning_example_id"),
                "learning_text": _text(item.get("answer"))[:200],
                "field_key": getattr(fact, "field_key", ""),
                "fact_value": _text(getattr(fact, "value", "")),
                "subquestion": item.get("matched_subquestion"),
                "kind": "PRODUCT_FACT_VS_APPROVED_LEARNING",
            })
    return tuple(found)


# A number with the unit written against it. Comparing bare numbers reads any
# digit in a sentence as a claim about any fact: on inquiry 11207 a Learning
# answer saying "섬 지역의 경우 설치까지 약 2주" was reported as contradicting
# ``usb_port_count = 1 x USB-A`` because {2} and {1} do not intersect, and on
# 11228 "다리사이 간격 747mm" contradicted the product's own
# ``1110.8 x 643.8 x 76.4 mm``. Reading the unit is what makes two numbers
# comparable at all.
_MEASURED_VALUE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(mm|cm|인치|kg|g|w|개|ch|주|일|년|회|%)",
    re.IGNORECASE,
)
# A catalogue value that lists several measurements ("1110.8 x 643.8 x 76.4
# mm"). Which of the three a loose number in an answer would correspond to is
# not something this check can know, so a listed value is never compared.
# "1 x USB-A" is not one of these: the x separates a count from a name.
_LISTED_MEASUREMENTS = re.compile(r"\d+(?:\.\d+)?\s*[x×]\s*\d+(?:\.\d+)?")


def _measured_values(text: object) -> dict[str, set[float]]:
    """Numbers in this text, grouped by the unit written against them."""

    found: dict[str, set[float]] = {}
    for number, unit in _MEASURED_VALUE.findall(_text(text)):
        try:
            value = float(number)
        except ValueError:
            continue
        found.setdefault(unit.lower(), set()).add(value)
    return found


def _measurements_conflict(
    answer: object, fact_value: object, *, unit: object = None,
) -> bool:
    """Whether the answer states a different value for the *same* measurement.

    Three conditions, each one a false positive measured on the operational
    copy. All 29 PK↔Learning conflicts recorded there were produced by bare
    number comparison and not one of them was a real disagreement:

      같은 단위    the answer has to state a number in the unit the catalogue
                   used -- from the value's own text, or from the fact's
                   ``unit`` column when the value is a bare number. 7 of the
                   29 shared no unit at all: "약 2주" against a USB port
                   count, "43인치" against a product depth.
      단일 측정값  a listed value is not compared. 22 of the 29 were a product
                   dimension triple against one loose millimetre figure -- a
                   stand's leg spacing, a VESA hole pitch.
      값 불일치    within one unit the sets must not intersect.

    A real count or weight disagreement still lands here: "무게는 30kg" against
    ``28.4kg`` shares the unit, is a single value, and the numbers differ.
    """

    if _LISTED_MEASUREMENTS.search(_text(fact_value)):
        return False
    # A fact stores its unit in its own column, so the value itself is often a
    # bare number ("3" with unit "개") or carries a tolerance the unit does not
    # sit against ("105.0 (±5.0)" with unit "mm"). When the column says the
    # unit, every number in the value is in that unit -- the value *is* that
    # measurement. Reading only the value text made a verified count
    # uncomparable and let a real disagreement through, which
    # ``test_verified_count_contradicting_learning_is_reported`` and
    # ``test_a_different_measurement_of_the_same_attribute_still_conflicts``
    # both pin.
    unit_text = _text(unit).strip().lower()
    if unit_text:
        numbers = quantities(fact_value)
        fact_measured = {unit_text: set(numbers)} if numbers else {}
    else:
        fact_measured = _measured_values(fact_value)
    answer_measured = _measured_values(answer)
    shared = set(fact_measured) & set(answer_measured)
    if not shared:
        return False
    return any(
        not (fact_measured[unit] & answer_measured[unit]) for unit in shared
    )


def _answers_measure_differently(left: object, right: object) -> bool:
    """Two stored answers stating different numbers for the same measurement.

    The same two conditions ``_measurements_conflict`` applies between an
    answer and a catalogue value, read between two answers: the unit has to be
    written in both texts, and a listed value ("1110.8 x 643.8 x 76.4 mm")
    is never compared because which of its figures a loose number corresponds
    to is not knowable here.
    """

    left_text, right_text = _text(left), _text(right)
    if _LISTED_MEASUREMENTS.search(left_text) or _LISTED_MEASUREMENTS.search(
        right_text
    ):
        return False
    left_measured = _measured_values(left_text)
    right_measured = _measured_values(right_text)
    shared = set(left_measured) & set(right_measured)
    if not shared:
        return False
    return any(
        not (left_measured[unit] & right_measured[unit]) for unit in shared
    )


def _common_rows_disagree(
    item: Mapping[str, Any] | None,
    approved: Iterable[Mapping[str, Any]],
    *,
    field_key: str,
    subquestion: object,
) -> bool:
    """Whether another operator-approved common row contradicts this one.

    The disagreement is read from the rows, not from the conflicts they
    produced, because the conflict list cannot show it. A row is reported as
    contradicted by a fact only when its polarity opposes the fact's value, so
    two rows that both appear there necessarily share that polarity -- they
    agree with each other -- while a genuinely opposed pair puts only one of
    them on the list. Grouping the conflicts instead counted how many distinct
    *wordings* contradicted one fact: two operator rows that both said 터치를
    지원합니다 in different words were declared a standoff and lost the
    priority, and 지원합니다 against 지원하지 않습니다 -- the case the operator
    has to settle -- was granted the win. Both were measured.

    Only rows matched to the same sub-question and talking about the same
    attribute are compared; the two axes are the ones ``fact_conflicts``
    itself uses, polarity and a measurement in a shared unit.
    """

    if item is None:
        return False
    text = item.get("answer")
    routed, fragments = _fields_the_text_is_about(text)
    if not _is_about_field(field_key, routed, fragments):
        return False
    polarity = detect_polarity(text)
    learning_id = item.get("learning_example_id")
    for other in approved:
        if other is item or other.get("learning_example_id") == learning_id:
            continue
        if not is_operator_common_row(other):
            continue
        if str(other.get("matched_subquestion") or "") != str(subquestion or ""):
            continue
        other_text = other.get("answer")
        other_routed, other_fragments = _fields_the_text_is_about(other_text)
        if not _is_about_field(field_key, other_routed, other_fragments):
            continue
        other_polarity = detect_polarity(other_text)
        if polarity != "UNCERTAIN" and other_polarity not in {
            "UNCERTAIN", polarity,
        }:
            return True
        if _answers_measure_differently(text, other_text):
            return True
    return False


def _supported_subquestions(context: Mapping[str, Any]) -> frozenset[str]:
    """Sub-questions retrieval itself resolved from approved Learning.

    Retrieval's verdict is the last word on whether the approved answer answers
    this particular sub-question: an item can clear every check above and still
    sit beside a sub-question that needs a live DPS date.
    """

    evidence = context.get("subquestion_evidence")
    if not isinstance(evidence, list):
        return frozenset()
    # A sub-question that retrieval attached approved Learning to. The coverage
    # label is no longer required: it is the same lexical measure the floor in
    # ``_qualifying`` used, and requiring it here would have re-imposed exactly
    # what was removed there. The deterministic statuses (NEEDS_DPS, CONFLICT,
    # DELIVERY_SCHEDULE_REVIEW) are still excluded, because for those the answer
    # comes from the current order or from a person, never from a past one.
    return frozenset(
        str(item.get("subquestion") or "")
        for item in evidence
        if isinstance(item, Mapping)
        and str(item.get("status") or "").upper() in {"ANSWERABLE", "CANDIDATE"}
        and str(item.get("source") or "").upper() == "ACTIVE_POSITIVE_LEARNING"
    )


def evaluate(
    *,
    learning_context: Mapping[str, Any] | None,
    safe_facts: Iterable[Any] = (),
    scope_for: Any = None,
    answered_at: datetime | None = None,
    asked_product_id: object = None,
    exact_model_for: Any = None,
) -> LearningEvidenceDecision:
    """Whether approved Learning may settle this inquiry's product facts."""

    context = learning_context if isinstance(learning_context, Mapping) else {}
    approved = context.get("similar_approved_answers")
    approved = approved if isinstance(approved, list) else []
    if not approved:
        return LearningEvidenceDecision(False, "NO_APPROVED_LEARNING")

    # Only a verified Product Fact can make a Learning answer a data conflict.
    # Two Learning answers that disagree are two candidates, and which one
    # fits this product and this time is GPT ②'s reading -- marking the
    # sub-question CONFLICT here decided it for the model.
    against_facts = fact_conflicts(approved, safe_facts, scope_for=scope_for)
    # A conflict the confirmed priority settles is not a standoff.
    #
    # ``fact_conflict_winner`` returns a side only when the model, the
    # measurement condition, the per-item approval and its time are all read
    # from the record. Where Learning wins the row stays usable and nothing is
    # disputed; where the catalogue wins only that row is dropped, and the
    # sub-question goes back to ordinary evidence instead of CONFLICT. Where
    # nothing may decide, the conflict travels on exactly as before.
    settled_against_learning: set[Any] = set()
    unsettled: list[dict[str, Any]] = []
    # Two operator-approved common Learnings that disagree with each other are
    # a standoff, not a win. Both outrank the catalogue, so granting the win
    # would pick one of them for the operator; instead the conflict travels on
    # and the sub-question is marked CONFLICT, which is the review channel this
    # module already has. ``_common_rows_disagree`` reads the rows themselves,
    # which is the only population where the disagreement is visible -- see its
    # docstring.
    for conflict in against_facts:
        learning_id = conflict.get("learning_id")
        item = next(
            (
                row for row in approved
                if row.get("learning_example_id") == learning_id
            ),
            None,
        )
        winner = (
            fact_conflict_winner(
                item,
                fact_value=conflict.get("fact_value"),
                answered_at=answered_at,
                asked_product_id=asked_product_id,
                exact_model_for=exact_model_for,
            )
            if item is not None else None
        )
        if winner == LEARNING_WINS:
            if is_operator_common_row(item) and _common_rows_disagree(
                item,
                approved,
                field_key=str(conflict.get("field_key") or ""),
                subquestion=conflict.get("subquestion"),
            ):
                unsettled.append(conflict)
                continue
            continue
        if winner == PRODUCT_FACT_WINS:
            settled_against_learning.add(learning_id)
            continue
        unsettled.append(conflict)
    against_facts = tuple(unsettled)
    # A conflict belongs to the sub-question it was found under, and each one
    # already records which. Refusing the whole inquiry discarded the Learning
    # of every other atom with it: on "피벗? 높낮이? 각도? 스피커?" one
    # disputed pivot answer removed all 17 candidates, including the four
    # about speakers. The disputed atoms lose their Learning; the independent
    # ones keep theirs, and the conflict still travels on the decision so the
    # caller marks those sub-questions CONFLICT exactly as before.
    disputed_subquestions = {
        str(conflict.get("subquestion") or "") for conflict in against_facts
    }
    disputed_ids = {
        conflict.get("learning_id") for conflict in against_facts
    }
    remaining = [
        item for item in approved
        if str(item.get("matched_subquestion") or "")
        not in disputed_subquestions
        and item.get("learning_example_id") not in disputed_ids
        and item.get("learning_example_id") not in settled_against_learning
    ] if (against_facts or settled_against_learning) else list(approved)
    if against_facts and not remaining:
        return LearningEvidenceDecision(
            False,
            "PRODUCT_FACT_VS_LEARNING_CONFLICT",
            conflict=True,
            conflicts=against_facts,
        )

    usable = _qualifying(remaining)
    if not usable:
        return LearningEvidenceDecision(
            False,
            "PRODUCT_FACT_VS_LEARNING_CONFLICT" if against_facts
            else "NO_QUALIFYING_APPROVED_LEARNING",
            conflict=bool(against_facts),
            conflicts=against_facts,
        )

    supported = _supported_subquestions(context)
    covered = tuple(
        str(item.get("matched_subquestion") or "")
        for item in usable
        if str(item.get("matched_subquestion") or "") in supported
    )
    if not covered:
        return LearningEvidenceDecision(
            False,
            "PRODUCT_FACT_VS_LEARNING_CONFLICT" if against_facts
            else "LEARNING_NOT_MAPPED_TO_EVIDENCE",
            conflict=bool(against_facts),
            conflicts=against_facts,
        )
    return LearningEvidenceDecision(
        True,
        "APPROVED_LEARNING_SUPPORTED",
        conflict=bool(against_facts),
        conflicts=against_facts,
        learning_ids=tuple(
            int(item["learning_example_id"])
            for item in usable
            if item.get("learning_example_id") is not None
        ),
        subquestions=tuple(dict.fromkeys(covered)),
    )
