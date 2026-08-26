from __future__ import annotations

import re


def normalize_space(text: object) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def normalize_newlines(text: object) -> str:
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.split("\n")]
    return "\n".join(lines).strip()


def normalize_question_text(text: object) -> str:
    return normalize_newlines(text)


def normalize_product_name(text: object) -> str:
    return normalize_space(text)


def normalize_option_name(text: object) -> str:
    return normalize_space(text)


def compact(text: object) -> str:
    return re.sub(r"\s+", "", str(text or "").lower())


def has_any(text: str, words: list[str]) -> bool:
    return any(word in text for word in words)


def find_any(text: str, words: list[str]) -> str | None:
    for word in words:
        if word in text:
            return word
    return None


def has_date_or_order_hint(text: str) -> bool:
    c = compact(text)
    if re.search(r"\d{1,2}\s*[./월-]\s*\d{1,2}", text):
        return True
    return any(k in c for k in ["주문", "구매", "결제", "오늘", "내일", "언제", "배송", "설치일"])


def normalize_for_comparison(text: object) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", str(text or "").lower())


def estimate_question_count(question: str) -> tuple[int, str]:
    text = str(question or "").strip()
    if not text:
        return 0, ""
    parts = [
        part.strip()
        for part in re.split(
            r"(?:\n+|[?？]|(?:^|\s)\d+[.)]\s*)",
            text,
        )
        if part and part.strip()
    ]
    meaningful = [part for part in parts if len(part) >= 4]
    if not meaningful:
        meaningful = [text]
    return max(1, len(meaningful)), " / ".join(meaningful[:6])


EMAIL_PATTERN = re.compile(
    r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])"
)
PHONE_PATTERN = re.compile(
    r"(?<!\d)(?:\+?82[- ]?)?0(?:1[016789]|2|[3-6][1-5])"
    r"[ -]?\d{3,4}[ -]?\d{4}(?!\d)"
)
LONG_NUMBER_PATTERN = re.compile(r"(?<!\d)(\d{4})\d{4,}(\d{4})(?!\d)")
NAME_LABEL_PATTERN = re.compile(
    r"(?i)(고객명|이름|customer[_ ]?name)(\s*[:=]\s*)"
    r"([가-힣A-Za-z]{2,20})"
)

# Numbers published for customers to call. They are contact details of an
# organisation, not personal data, so redacting one produces an answer that
# hides the very thing the customer needs -- and, once such an answer is stored
# as a learning example, the redaction token is what later answers copy.
#
# Deliberately an explicit list of approved numbers, never a pattern: a rule
# broad enough to recognise "a company number" would also stop masking real
# customers whose number happens to fit it. Adding an entry is a decision about
# one specific published number.
OFFICIAL_CONTACT_NUMBERS = (
    "1588-3366",     # 삼성전자 고객센터 (approved)
    "02-706-2678",   # 오제앤에스 고객센터 (approved)
)
_OFFICIAL_CONTACT_PATTERN = re.compile(
    "|".join(
        re.escape(number).replace(r"\-", r"[- ]?")
        for number in OFFICIAL_CONTACT_NUMBERS
    )
) if OFFICIAL_CONTACT_NUMBERS else None


def is_official_contact_number(value: object) -> bool:
    """True when the text is an approved published contact number."""

    text = re.sub(r"\s+", "", str(value or ""))
    if not text or _OFFICIAL_CONTACT_PATTERN is None:
        return False
    match = _OFFICIAL_CONTACT_PATTERN.fullmatch(text)
    return match is not None


def contains_personal_phone(text: object) -> bool:
    """True only when a phone number that is *not* an approved one appears.

    Used wherever a check asks "does this answer expose a phone number". The
    company's published number appearing in a customer answer is the intended
    outcome, not an exposure, so a bare pattern search reports it as a privacy
    failure and blocks a correct answer.
    """

    return any(
        not is_official_contact_number(match.group(0))
        for match in PHONE_PATTERN.finditer(str(text or ""))
    )


def _mask_phones_except_official(value: str) -> str:
    """Mask phone numbers, leaving approved published ones intact."""

    def replace(match: re.Match[str]) -> str:
        found = match.group(0)
        return found if is_official_contact_number(found) else "<masked-phone>"

    return PHONE_PATTERN.sub(replace, value)


def mask_personal_information(text: object) -> str:
    value = str(text or "")
    value = EMAIL_PATTERN.sub("<masked-email>", value)
    value = _mask_phones_except_official(value)
    value = LONG_NUMBER_PATTERN.sub(r"\1****\2", value)
    value = NAME_LABEL_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<masked-name>",
        value,
    )
    return value


# Korean sentence-final question endings. A compound inquiry often arrives
# without any '?' at all ("A/S 어디서 받아요 설치는 기사님이 해주시나요 ..."),
# so splitting on punctuation alone would collapse it into one question.
# Written as insert-then-split rather than a lookbehind, because the endings
# differ in length and Python requires fixed-width lookbehind.
_QUESTION_ENDING = re.compile(
    r"(나요|까요|은가요|는가요|가요|아요|어요|해요|되요|돼요"
    r"|습니까|입니까|나여|은지|는지|될지|할지"
    r"|궁금해요|궁금합니다|알려주세요|여쭤봅니다)(?=\s)"
)
_SUBQUESTION_MARK = "␟"
# A trailing "궁금해요" carries no question of its own; keeping it would
# add a meaningless sub-question that then classifies as UNCLASSIFIED and
# would hold an otherwise safe compound inquiry for review.
_FILLER_ONLY = re.compile(r"(?:궁금해요|궁금합니다|알려주세요|여쭤봅니다|입니다)[.!]*")
_LIST_SPLIT = re.compile(r"(?:\n+|[?？]|(?:^|\s)\d+[.)]\s*)")


# Korean polite interrogative tails, and the imperative tails that look like
# them but are requests. Used only to punctuate a question echoed back to the
# customer -- never for classification.
_INTERROGATIVE_TAIL = re.compile(r"(?:요|까|죠|쥬|나|니)$")
_REQUEST_ENDING = re.compile(r"(?:주세요|주십시오|바랍니다|부탁드립니다|하세요)$")


def restore_question_mark(text: object) -> str:
    """Put back the question mark that splitting removed.

    split_subquestions cuts on "?" and on interrogative endings, so a
    sub-question comes back without its punctuation. That is right for
    classification and wrong for anything shown to the customer: echoing
    문의주신 "...하나요" back reads like a transcription error.
    """

    value = str(text or "").strip()
    if not value or value[-1] in "?？!！.。":
        return value
    if _REQUEST_ENDING.search(value):
        # "설치방법 알려주세요" is a request, not a question.
        return value
    if _INTERROGATIVE_TAIL.search(value):
        return f"{value}?"
    return value


def split_subquestions(
    question: object, *, minimum_length: int = 4
) -> tuple[str, ...]:
    """Break a compound inquiry into its meaningful sub-questions.

    Splits first on the explicit separators an inquiry usually carries
    (newlines, question marks, numbered lists), then on Korean interrogative
    endings so a run-on question without punctuation is still seen as several
    questions rather than one.
    """

    text = str(question or "").strip()
    if not text:
        return ()
    parts: list[str] = []
    for chunk in _LIST_SPLIT.split(text):
        chunk = (chunk or "").strip()
        if not chunk:
            continue
        marked = _QUESTION_ENDING.sub(
            lambda match: match.group(1) + _SUBQUESTION_MARK, chunk
        )
        for piece in marked.split(_SUBQUESTION_MARK):
            piece = piece.strip()
            if piece:
                parts.append(piece)
    meaningful = [
        part
        for part in parts
        if len(part) >= minimum_length and not _FILLER_ONLY.fullmatch(part)
    ]
    # The same question asked twice is still one question. A customer
    # repeating themselves, or a channel that echoes the first line as the
    # inquiry title, must not inflate the sub-question count: the extra copy
    # would be classified, prompted and coverage-checked as if the customer
    # wanted two separate answers.
    deduplicated = list(dict.fromkeys(meaningful))
    return tuple(deduplicated) if deduplicated else (text,)


# "When is my order shipping / arriving / being installed?" -- a question about
# one customer's own schedule, whatever noun they use for it.
#
# Defined here, in the lowest layer, because two places need exactly the same
# notion and must never drift: the classifier decides routing from it, and the
# rule engine uses it to refuse a question this block does not answer.
_SCHEDULE_PARTICLE = r"[가이은는을를도]?"
_WHEN = r"(?:언제|며칠|몇일)"
# The noun may be followed by a schedule word before the particle:
# "배송 예정일이 언제", "설치 일정이 언제".
_SCHEDULE_NOUN = r"(?:\s*(?:예정일|예정|일정|날짜))?"
CURRENT_DELIVERY_SCHEDULE_QUERY = re.compile(
    rf"(?:배송|발송|출고|도착|수령){_SCHEDULE_NOUN}{_SCHEDULE_PARTICLE}\s*(?:쯤)?\s*{_WHEN}"
    rf"|{_WHEN}\s*(?:쯤)?\s*(?:배송|발송|출고|도착|수령|받|보내|오)"
)
CURRENT_INSTALLATION_SCHEDULE_QUERY = re.compile(
    rf"(?:설치|기사|기사님|방문){_SCHEDULE_NOUN}{_SCHEDULE_PARTICLE}\s*(?:쯤)?\s*{_WHEN}"
    rf"|{_WHEN}\s*(?:쯤)?\s*(?:설치|방문)"
)

# Words that make the *notice* the subject rather than the shipment. The
# classifier checks this before the shapes above; the rule engine has no such
# ordering, so its second-line guard consults this explicitly -- otherwise
# "알림톡은 언제 오나요?" would be refused by a branch that answers it well.
NOTICE_SUBJECT_QUERY = re.compile(r"알림톡|안내\s*문자|문자\s*안내|연락\s*(?:이|은)?\s*언제")


# The allow-list for install_existing_order_answer ("설치 예정일 관련 알림톡은
# 설치일 전날 수취인의 카카오톡으로 발송됩니다").
#
# That template used to be the shipping block's *default*: for an install
# product, anything mentioning a shipping keyword that matched no earlier
# branch received it. Enumerating the test corpus found 75 questions getting
# that body and only 7 asking about the notice -- among the rest were
# "보증기간이 얼마나 되나요?", "캐시백 받을 수 있나요?" and "배송 중 깨진 것
# 같은데 어떻게 하나요?", a damage report answered with kakao guidance. Since
# the block is a FIXED_POLICY_SHIPPING match kind, the default also outranked
# GPT and was published.
#
# So the template is now opt-in. It answers exactly one thing: when and how
# the schedule is announced. NOTICE_SUBJECT_QUERY covers most of it but reads
# only "연락이 언제"; customers write the halves in either order
# ("설치기사님한테 언제 연락이 오나요?"), and ask for advance contact without
# using either word ("기사님 방문 전에 연락 오나요?").
DELIVERY_NOTICE_QUERY = re.compile(
    r"알림톡|안내문자|문자안내|문자는언제|문자가언제|"
    r"연락(?:이|은|을)?(?:언제|미리|먼저|오|주)|"
    r"언제[^?!.]{0,6}연락|미리연락|사전연락|방문전(?:에)?연락"
)


def is_delivery_notice_question(question: str) -> bool:
    """Whether the customer asks when or how the schedule is announced."""

    return bool(DELIVERY_NOTICE_QUERY.search(compact(question)))


# "이번 주말에 설치해주세요." is not a question about installation; it tells us
# when to come. It was classified GENERAL_INSTALLATION_GUIDANCE -- ordinary
# information -- because the schedule-change predicate looks for a change verb
# (변경/바꿔/미뤄) beside a schedule noun (설치일/배송일), and this sentence has
# neither: there is no existing date being moved.
#
# Both halves are required, which is what keeps the policy question out.
# "주말 설치 가능한가요?" names a time but asks whether we ever do it;
# "벽걸이로 설치해주세요" is an instruction with no schedule in it at all.
_SCHEDULE_TIME_TARGET = re.compile(
    r"오늘|내일|모레|글피|이번주말|주말|평일|이번주|다음주|담주|이번달|다음달|"
    r"월요일|화요일|수요일|목요일|금요일|토요일|일요일|"
    r"\d{1,2}월\d{1,2}일|\d{1,2}일에|오전|오후"
)
_OPERATIONAL_IMPERATIVE = re.compile(
    r"(?:설치|배송|배달|방문|출고|발송)(?:을|를)?(?:해|해서)?"
    r"(?:주세요|주시겠|주실|주시면|부탁|해주|해주세요)"
)


def is_operational_schedule_request(question: str) -> bool:
    """Whether the customer instructs us to deliver or install at a given time."""

    text = compact(question)
    if not _OPERATIONAL_IMPERATIVE.search(text):
        return False
    return bool(_SCHEDULE_TIME_TARGET.search(text))


# "주문시 며칠 소요되나요" and "토요일에도 배달 가능하나요" are questions about
# the delivery *policy*, not about one customer's shipment. Neither names an
# order, so neither can be answered from an order lookup -- and neither is
# answered by the existing-order notification template ("알림톡은 설치일 전날
# 발송됩니다"), which the shipping block used to hand out as its last resort.
#
# The two are kept apart because the right outcome differs. A duration
# question has an honest answer for an install product: it depends on when the
# installer can be scheduled. A weekend question does not -- the shipping
# config holds no Saturday or holiday rule at all, so the only safe reply is
# to decline and let the evidence pipeline and a person handle it. Inventing
# one would be exactly the fabrication the rest of the pipeline exists to stop.
_DELIVERY_SUBJECT = r"(?:배송|배달|발송|출고|도착|수령|받)"
_HOW_LONG = r"(?:며칠|몇일|얼마나|얼마|어느\s*정도)"
GENERAL_DELIVERY_DURATION_QUERY = re.compile(
    rf"{_DELIVERY_SUBJECT}[^?!.]{{0,12}}(?:{_HOW_LONG}|기간)"
    rf"|{_HOW_LONG}[^?!.]{{0,12}}{_DELIVERY_SUBJECT}"
    rf"|{_DELIVERY_SUBJECT}\s*(?:기간|기한)"
    rf"|{_HOW_LONG}[^?!.]{{0,6}}(?:소요|걸리|걸려|걸릴)"
    rf"|(?:주문|구매|결제)[^?!.]{{0,10}}{_HOW_LONG}"
)
# "보증기간이 얼마나 되나요", "A/S 무상기간" -- a duration, but not delivery's.
_NON_DELIVERY_DURATION = re.compile(
    r"보증|무상|a/?s|에이에스|반품|환불|취소|교환|점검|보관"
)
WEEKEND_DELIVERY_POLICY_QUERY = re.compile(
    rf"(?:토요일|일요일|주말|공휴일|휴일)[^?!.]{{0,14}}(?:{_DELIVERY_SUBJECT}|설치|가능)"
    rf"|(?:{_DELIVERY_SUBJECT}|설치)[^?!.]{{0,10}}(?:토요일|일요일|주말|공휴일|휴일)"
)
# "토요일로 배송일 변경해주세요" names Saturday too, but it asks us to move the
# schedule. That belongs to the schedule-change policy, not here.
_SCHEDULE_CHANGE_REQUEST = re.compile(
    r"변경|바꿔|바꾸|옮겨|미뤄|당겨|땡겨|앞당|조율"
    r"|(?:설치|배송|배달)\s*해\s*주세요|(?:설치|배송|배달)\s*부탁"
)


def is_weekend_delivery_policy_question(question: str) -> bool:
    """Whether the customer asks *whether* we deliver on a weekend or holiday."""

    text = compact(question)
    if _SCHEDULE_CHANGE_REQUEST.search(text):
        return False
    return bool(WEEKEND_DELIVERY_POLICY_QUERY.search(text))


def is_general_delivery_policy_question(question: str) -> bool:
    """Whether the customer asks how long delivery generally takes."""

    text = compact(question)
    if _NON_DELIVERY_DURATION.search(text):
        return False
    return bool(GENERAL_DELIVERY_DURATION_QUERY.search(text))


# "배송 올 때 공구도 같이 오나요?" -- the shipment is the *occasion*, not the
# subject. What is being asked about is an item and whether it comes in the box.
#
# The rule engine's shipping block is entered on a bare keyword OR ("배송",
# "언제", "받을수", "도착", "설치기사님"...), which is recall-first by design.
# That is fine as long as something afterwards checks what the question is
# actually about; nothing did, so a parcel product plus the word 배송 was
# enough to return the shipping-duration policy. Real inquiry: "배송 올때
# 조립에 필요한 일회용 공구도 같이 오나요? 제가 개별로 준비해야하는 공구가
# 있나요?" was answered with "택배배송 상품은 오후 3시 이전 결제 주문에 한해
# 당일 발송되며..." and auto-posted.
#
# Two things must both be present, so this stays narrow: a noun naming an
# *item*, and wording asking whether it is included or must be prepared.
# "배송은 보통 며칠 걸리나요?" names no item and is untouched.
ITEM_SUBJECT = re.compile(
    r"공구|드라이버|렌치|육각|나사|볼트|피스|"
    r"부속|부품|구성품|구성|악세서리|액세서리|사은품|"
    r"리모컨|케이블|전선|어댑터|아답터|충전기|설명서|매뉴얼|"
    r"받침대|스탠드|거치대|브라켓|브래킷|배터리|건전지"
)
INCLUSION_OR_PREPARATION = re.compile(
    r"같이\s*(?:오|옵|보내|배송|포함|들어|드리)|"
    r"함께\s*(?:오|옵|보내|배송|포함|들어|드리)|"
    r"동봉|"
    r"포함\s*(?:되|인가|하나|돼|됩|인지)|"
    r"들어\s*있|들었|들어있|"
    r"가지고\s*오|가져\s*오|챙겨\s*오|"
    r"따로\s*(?:준비|구매|사|구입)|"
    r"별도\s*(?:로)?\s*(?:준비|구매|사|구입)|"
    r"개별\s*(?:로)?\s*(?:준비|구매|사|구입)|"
    # Deliberately not "필요한가/있어야": those are the ordinary way to ask
    # anything ("스탠드 설치에 타공이 필요한가요?"), and matching them would
    # pull installation questions in here. What is wanted is the customer
    # asking whether *they* must supply the item.
    r"준비해야|준비하나|준비물"
)


def is_package_contents_question(question: object) -> bool:
    """Whether the question asks what comes with the product, not about shipping.

    Both halves are required -- an item and an inclusion/preparation attribute
    -- so a shipping-duration question is never captured and a bare product
    question ("스탠드 색상이 뭔가요?") is not either.
    """

    text = compact(question)
    return bool(ITEM_SUBJECT.search(text) and INCLUSION_OR_PREPARATION.search(text))
