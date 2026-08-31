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
    """How many questions the inquiry carries, and them joined for display.

    Delegates to ``split_subquestions`` so the count staff see and the parts
    the classifier judges can never disagree. They used to be two separate
    splitters with two different regexes: on a numbered list whose items wrap
    onto a second line the classifier saw six fragments where this one saw
    four, and neither number matched the four questions the customer wrote.
    """

    text = str(question or "").strip()
    if not text:
        return 0, ""
    parts = split_subquestions(text)
    if not parts:
        parts = (text,)
    return max(1, len(parts)), " / ".join(parts[:6])


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
# MULTILINE so a list marker is recognised at the start of every line, not only
# the first: without it "2." and "3." stayed glued to their own question text.
_LIST_SPLIT = re.compile(r"(?:\n+|[?？]|(?:^|\s)\d+[.)]\s*)", re.M)
# An explicit list marker at the start of a line: "1.", "2)", " 3. ".
_NUMBERED_MARKER = re.compile(r"(?:^|\n)\s*\d+[.)]\s")
# A newline that is *not* followed by the next list marker -- i.e. a wrap
# inside the current item rather than the boundary of the next one.
_WRAPPED_LINE = re.compile(r"\n+(?!\s*\d+[.)]\s)")
# A non-whitespace marker lets a prose line wrap be joined without making the
# preceding polite declarative tail look like a run-on question boundary to
# _QUESTION_ENDING. It is removed before any part leaves this module.
_PROSE_WRAP_MARK = "␠"
_QUESTION_LINE_ENDING = re.compile(
    r"(?:[?？]|나요|까요|은가요|는가요|습니까|입니까|은지|는지|될지|할지"
    r"|알려주세요|여쭤봅니다|확인(?:해)?주세요|문의드립니다|문의합니다"
    r"|요청합니다|신청합니다|부탁드립니다)"
    r"[.!~…]*\s*$"
)


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


def _merge_prose_wrapped_lines(text: str) -> str:
    """Keep prose context with the question it explains.

    A newline is often only visual wrapping. Explicit question-bearing lines
    remain boundaries; surrounding declarative lines are attached to the next
    question. With no reliable question-bearing line we preserve the original
    newlines rather than guessing and accidentally merging real questions.
    """

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) <= 1:
        return text
    question_lines = [bool(_QUESTION_LINE_ENDING.search(line)) for line in lines]
    if not any(question_lines):
        return text
    if sum(question_lines) == 1:
        return _PROSE_WRAP_MARK.join(lines)

    segments: list[str] = []
    pending: list[str] = []
    for line, is_question in zip(lines, question_lines):
        pending.append(line)
        if is_question:
            segments.append(_PROSE_WRAP_MARK.join(pending))
            pending = []
    if pending:
        if segments:
            segments[-1] += _PROSE_WRAP_MARK + _PROSE_WRAP_MARK.join(pending)
        else:
            segments.append(_PROSE_WRAP_MARK.join(pending))
    return "\n".join(segments)


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
    # A customer who numbers their questions has already told us where the
    # boundaries are, and a newline inside one of those items is a line wrap
    # rather than a new question. Splitting on it anyway cut real inquiries
    # mid-sentence:
    #
    #   "3. 기존 벽에 타공구멍이 있는데
    #    같은 곳에 타공 설치 가능한지"
    #
    # became "기존 벽에 타공구멍이 있는데" -- a clause carrying no question,
    # which then classified as UNCLASSIFIED and, because the compound
    # aggregation ORs manual_review_required across parts, held the whole
    # four-question inquiry for review. Four questions were read as six, two of
    # them meaningless.
    #
    # Only when the markers are unambiguous: a single stray "1." is a sentence,
    # not a list.
    if len(_NUMBERED_MARKER.findall(text)) >= 2:
        text = _WRAPPED_LINE.sub(" ", text)
    else:
        text = _merge_prose_wrapped_lines(text)
    parts: list[str] = []
    for chunk in _LIST_SPLIT.split(text):
        chunk = (chunk or "").strip()
        if not chunk:
            continue
        marked = _QUESTION_ENDING.sub(
            lambda match: match.group(1) + _SUBQUESTION_MARK, chunk
        )
        for piece in marked.split(_SUBQUESTION_MARK):
            piece = piece.replace(_PROSE_WRAP_MARK, " ").strip()
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
    # "주문하면 바로 배송되나요" asks how soon without asking how long. It is
    # the same policy question, and leaving it out here meant the answer was
    # never rearranged to lead with it -- while the coverage evaluator, which
    # keeps its own anchor table, had already learned to recognise it. One
    # concept, two tables, and they drifted.
    rf"|(?:바로|즉시|당일|곧바로)[^?!.]{{0,4}}{_DELIVERY_SUBJECT}"
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


# Broad receipt words such as "받을 수", "기간" and "언제" are useful for
# delivery recall only after their subject is known. They also occur in A/S,
# repair, inspection, return and exchange questions. These predicates are
# shared by the classifier and rule engine so both layers make the same narrow
# exception without deleting the legitimate shipping keywords.
AFTER_SALES_QUERY = re.compile(
    r"(?<![a-z])a\s*/?\s*s(?![a-z])|에이에스|서비스\s*센터|삼성전자서비스|"
    r"무상\s*수리|수리|보증\s*기간|고장|불량|제품\s*이상|점검|"
    r"(?:화면|영상|재생)[^?!.]{0,12}(?:멈|안\s*(?:나|됨|돼|되))",
    re.IGNORECASE,
)
DIRECT_AFTER_SALES_QUERY = re.compile(
    r"(?<![a-z])a\s*/?\s*s(?![a-z])|에이에스|서비스\s*센터|삼성전자서비스|"
    r"무상\s*수리|수리|보증\s*기간|점검",
    re.IGNORECASE,
)
NON_DELIVERY_SERVICE_QUERY = re.compile(
    r"반품|환불|교환|취소|서비스\s*기간|보관\s*기간",
    re.IGNORECASE,
)
EXPLICIT_DELIVERY_CONTEXT_QUERY = re.compile(
    r"배송|택배|배달|발송|출고|도착|수령|운송|송장|배송\s*기사|"
    r"설치\s*(?:기사|일|날짜|예정|일정)|"
    r"(?:상품|제품|주문)[^?!.]{0,12}(?:받|오|배송|도착|출고|설치)",
    re.IGNORECASE,
)
BARE_RECEIPT_SCHEDULE_QUERY = re.compile(
    r"(?:언제|며칠|몇일)[^?!.]{0,12}받|받[^?!.]{0,12}(?:언제|며칠|몇일)",
    re.IGNORECASE,
)
SHIPPING_ANSWER_QUERY = re.compile(
    r"배송|택배|배달|발송|출고|도착|수령|영업일|도서산간",
    re.IGNORECASE,
)


def is_after_sales_question(question: object) -> bool:
    """Whether the question explicitly asks about A/S or a product failure."""

    return bool(AFTER_SALES_QUERY.search(str(question or "")))


def has_explicit_delivery_context(question: object) -> bool:
    """Whether delivery/installation itself, not bare receipt wording, is named."""

    text = str(question or "")
    if EXPLICIT_DELIVERY_CONTEXT_QUERY.search(text):
        return True
    # "TV가 고장이라 새 제품은 언제 받을 수 있나요" still asks about
    # delivery. A direct A/S subject keeps the same wording in the service
    # domain ("A/S는 언제 받을 수 있나요"), so only failure context receives
    # this narrow schedule exception.
    return bool(
        BARE_RECEIPT_SCHEDULE_QUERY.search(text)
        and not DIRECT_AFTER_SALES_QUERY.search(text)
    )


def is_non_delivery_service_question(question: object) -> bool:
    """Whether broad shipping words are governed by a non-delivery subject."""

    text = str(question or "")
    return bool(
        AFTER_SALES_QUERY.search(text) or NON_DELIVERY_SERVICE_QUERY.search(text)
    )


def is_shipping_only_answer(answer: object) -> bool:
    """Whether a template discusses shipping but contains no A/S guidance."""

    text = str(answer or "")
    return bool(SHIPPING_ANSWER_QUERY.search(text)) and not bool(
        AFTER_SALES_QUERY.search(text)
    )


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


# "스마트티비는 처음인데 인터넷티비랑 다른건가요?" is a product question, and it
# classified as UNCLASSIFIED -- which set manual_review_required and, through
# the compound OR, held a four-question inquiry for a person.
#
# The gate in front of PRODUCT_SPEC_OR_FEATURE is a list of product
# *attributes* (사양, 기능, 크기, 무게, HDMI, 인치...). It recognises a question
# that names a measurable property and misses one that asks what the product
# *is* or how it differs from something else -- the shape a customer uses when
# the concept, not the number, is what they do not know.
#
# Same construction as is_package_contents_question above: a subject in the
# product domain *and* a definition/comparison/independence relation, both
# required. Neither half alone is enough, so "무타공 설치인가요" (no product
# subject) and "제품 언제 오나요" (no concept relation) are untouched.
#
# This decides *classification* only. Whether any particular feature is true of
# this product is still answered from Product Knowledge or verified Learning,
# and stays unanswerable without them.
PRODUCT_CONCEPT_SUBJECT = re.compile(
    r"티비|tv|모니터|스마트tv|인터넷tv|셋톱|셋탑|제품|상품|기기|화면|넷플릭스|유튜브|스마트기능"
)
PRODUCT_CONCEPT_RELATION = re.compile(
    r"차이|다른건가|다른가요|다른가|무엇인가요|뭔가요|뭐예요|뭐인가요|어떤건가"
    r"|무슨\s*차이|같은건가|같은가요|인가요"
    r"|없이(?:도)?\s*(?:사용|시청|볼|되나|가능)"
)


def is_product_concept_question(question: object) -> bool:
    """Whether the question asks what the product is or how it differs."""

    text = compact(question)
    return bool(
        PRODUCT_CONCEPT_SUBJECT.search(text)
        and PRODUCT_CONCEPT_RELATION.search(text)
    )


# Which seller the customer bought from, however they name it. Used by two
# layers that must never disagree: the rule engine's event branch, which has to
# refuse to answer a seller-identity question with the rebate period, and the
# Learning compatibility gate, which has to refuse the same substitution in
# retrieved evidence.
#
# The engine's guard knew only "구매처". A customer wrote "판매처를 뭐라고
# 검색해야 하나요" after being told their entry was wrong, fell past it, and
# received the generic 온누리 answer -- the rebate window and an event URL,
# auto-posted, answering a question nobody had asked.
SELLER_IDENTITY_QUERY = re.compile(
    r"판매처|판매자|판매점|구매처|스토어\s*명|상호|업체\s*명|어디서\s*구매"
)


def is_seller_identity_question(question: object) -> bool:
    """Whether the customer is asking which seller to name."""

    return bool(SELLER_IDENTITY_QUERY.search(compact(question)))


# Whether the customer is asking for a commitment to a date, not for the
# policy. "오늘 주문하면 9일까지 받아볼 수 있을까요?" and "배송은 보통 며칠
# 걸리나요?" are both pre-purchase delivery questions, and only the second one
# standing policy can answer. The first names a deadline, and for an order
# that does not exist yet nothing in the system knows whether it will be met.
#
# Read on ``compact`` text, so every pattern is written without spaces.
#
# "언제까지" is deliberately excluded: that asks *when* the product arrives,
# which is the ordinary schedule question the existing routes already handle.
# What is caught here is the customer proposing a date and asking yes or no.
_DELIVERY_DEADLINE = re.compile(
    # 9일까지 / 9월5일까지 / 24일전까지 / 19일이전
    r"(?:\d{1,2}월)?\d{1,2}일(?:까지(?!도)|전까지|이전|안에|내에)"
    # 금요일까지 / 화요일전까지
    r"|[월화수목금토일]요일(?:까지|전|이전)"
    # 이번주안에 / 다음주까지 / 담주전에
    r"|(?:이번|금|다음|담)주(?:안에|내에|까지|전까지|전에)"
    r"|(?:이번|다음|담)달(?:안에|내에|까지)"
    r"|특정날짜까지"
    # 오늘까지 -- but not "오늘까지도", which is a complaint about how long
    # something has already been going on, not a deadline.
    r"|(?:오늘|내일|모레|주말)까지(?!도)"
)
_DELIVERY_ARRIVAL = re.compile(
    r"받|도착|배송|배달|설치|수령|오나요|올까요|와요|보내"
)
_ASKS_WHEN = re.compile(r"언제까지")


def is_delivery_deadline_question(question: object) -> bool:
    """Whether the customer asks if delivery/installation can meet a deadline."""

    text = compact(question)
    if _ASKS_WHEN.search(text):
        return False
    return bool(
        _DELIVERY_DEADLINE.search(text) and _DELIVERY_ARRIVAL.search(text)
    )


# Whether the customer is reporting that something they ordered did not arrive.
#
# Inquiry 325318746 -- "오베닉 스마트마운트 스탠드가 안왔어요" -- was answered
# with a description of the stand's model line and was eligible for auto-post.
# Nothing in the pipeline could see that the customer was telling us about a
# delivery, not asking about a product.
#
# This is not a wording problem. Whether a stand actually shipped is a question
# about the order, the outbound record and the packing list; it is work for a
# person, and an automatic reply of any kind marks the inquiry answered on
# Naver, which is where staff look for the ones still needing them.
#
# Precision matters more than reach here, because the consequence is refusing
# to answer at all. Two shapes qualify and nothing else does:
#
#   an arrival failure   "안 왔어요", "못 받았습니다", "누락됐어요" -- these say
#                        something about delivery and nothing else, so any
#                        ordered thing beside them is enough
#   a plain absence      "없어요" says nothing about delivery on its own
#                        ("전원 버튼이 없어요" is a product question), so it
#                        counts only next to a countable part of the order,
#                        and never inside a question
_ARRIVAL_FAILURE = r"""(?:안왔|안옴|안와|못받|미수령|누락|빠[졌진]|안들어있
|들어있지않|안들었|동봉안|동봉되지|미배송|미발송|안보내|(?<!나)오지않
|배송안[됬됐되])"""
# Joined without whitespace so the pattern can be written readably above.
# "(?<!나)오지않" keeps "화면이 나오지 않습니다" out: a screen that will not
# come on is a fault report, and shares four characters with a parcel that
# did not come.
_ARRIVAL_FAILURE = re.compile("".join(_ARRIVAL_FAILURE.split()))
_PLAIN_ABSENCE = re.compile(r"없어요|없습니다|없네요|없던데|안보[여이]")

# Physical things that arrive in the box or the shipment.
#
# "상품" and "제품" are deliberately absent. Almost every inquiry in the store
# carries the title "상품 문의", so treating that word as an ordered item made
# any "못 받" anywhere in a long message look like a missing delivery -- a
# customer writing "톡톡 답변을 못 받아 재문의드립니다" was flagged as a
# missing shipment.
_ORDERED_COMPONENT = re.compile(
    r"스탠드|스텐드|거치대|리모컨|리모콘|케이블|브라켓|사은품|증정품|구성품"
    r"|부속품|부품|어댑터|아답터|전원선|받침|나사|볼트|설명서|배터리|마운트"
    r"|선반|멀티탭|셋탑|셋톱|본체|스피커|다리"
)
# "상품" and "제품" are here despite almost every inquiry carrying the title
# "상품 문의": the proximity window and the not-a-shipment list do the work of
# telling "상품이 안왔어요" from "상품 문의 ... 답변을 못 받아".
_ORDERED_WHOLE = re.compile(r"티비|tv|모니터|택배|물건|상품|제품")

# Things a customer can fail to receive that are not the shipment: a reply, a
# call, a voucher. "상품권을 못 받았다" is a benefit question, not a parcel.
_NOT_A_SHIPMENT = re.compile(
    r"답변|연락|전화|문자|알림톡|톡톡|상품권|쿠폰|포인트|적립|환급|혜택"
    r"|캐시백|이벤트|당첨|리뷰|후기|주소"
    # A tracking number is *about* the shipment, so it never belongs here:
    # "스탠드 송장번호 알려주세요 스탠드가 안왔습니다" is a missing stand.
)

# Asking about the product rather than reporting about the shipment.
_PRODUCT_QUESTION = re.compile(
    r"포함(?:되|인|인가|하나)|기본구성|같이오|함께오|별도구매|따로구매|별도판매"
    r"|호환|맞나요|어떤모델|무슨모델|모델명|몇세대|종류|재고|사양|스펙"
    r"|가능한가요|가능할까요|인가요|일까요"
)
# When it will arrive, or whether a date can be met -- both are answerable and
# neither is a report that something is absent.
_SCHEDULE_QUESTION = re.compile(
    r"언제|며칠|몇일|예정일|배송일|일정|얼마나걸|어디쯤|까지받|까지배송"
    r"|늦어지|지연되|늦나요|늦어질"
    # Asking us to look the delivery up is asking where the parcel is, which
    # the pipeline answers from the order and DPS. "주문한 상품이 아직 안
    # 왔어요. 배송 조회해 주세요" is that question, not a report that a part
    # is missing from the box.
    r"|조회|확인해주|알려주"
)
# It arrived and then broke. Damage is its own thing and has its own handling.
_DAMAGE = re.compile(r"파손|깨[졌진져]|고장|불량|하자|손상|흠집|찍힘")
# "빠졌다" means two different things. A bolt missing from the box was never
# sent; a wheel that came off after assembly was. Only the second one talks
# about having used the product, so that is what separates them.
_AFTER_DELIVERY = re.compile(
    r"조립|설치하|설치후|설치받|사용중|사용하|쓰[다던고]|쓰는|장착후|받아서"
)

# "본체만 왔어요" names no failure -- it says what did arrive, and the report is
# in the word "만". The arrival verb is required so "스탠드만 구매 가능한가요"
# stays a purchase question.
_PARTIAL_DELIVERY = re.compile(
    r"(?:일부|" + _ORDERED_COMPONENT.pattern + r"|" + _ORDERED_WHOLE.pattern
    + r")[^가-힣]{0,4}만[^가-힣]{0,6}(?:왔|오고|도착|배송|받았|옴)"
)

# The two readings of "빠졌다", and the phrases that can only mean the first.
_DETACHED = re.compile(r"빠[졌진]")
_NEVER_ARRIVED = re.compile(
    r"안왔|안옴|안와|못받|미수령|누락|안들어있|들어있지않|동봉안|미배송|미발송"
)

# How far apart the thing and the failure may sit and still be one statement.
# "스탠드가 안왔어요" is four characters apart; a component named in one
# sentence and a "못 받았다" three sentences later are two different subjects.
_NEAR = 18


def _reports_absence_of_a_shipment(text: str) -> bool:
    """Whether a failure-to-arrive phrase actually attaches to an ordered item."""

    for failure in _ARRIVAL_FAILURE.finditer(text):
        window = text[max(0, failure.start() - _NEAR):failure.end() + _NEAR]
        if _NOT_A_SHIPMENT.search(window):
            continue
        if _ORDERED_COMPONENT.search(window) or _ORDERED_WHOLE.search(window):
            return True
    return False


def _absence_next_to_a_component(text: str) -> bool:
    """"없어요" only reports a missing part when it is talking about one.

    Without the distance check, a long message mentioning a 스텐드 in one
    sentence and "과거 글을 볼 수 없네요" in another read as a missing stand.
    """

    for absence in _PLAIN_ABSENCE.finditer(text):
        window = text[max(0, absence.start() - _NEAR):absence.end() + _NEAR]
        if _NOT_A_SHIPMENT.search(window):
            continue
        if _ORDERED_COMPONENT.search(window):
            return True
    return False


def is_missing_item_report(question: object) -> bool:
    """Whether the customer says something they ordered has not arrived."""

    text = compact(question)
    if not text:
        return False
    if _PRODUCT_QUESTION.search(text):
        return False
    if _reports_absence_of_a_shipment(text):
        if _DAMAGE.search(text):
            return False
        # "아직 미발송인데 좀 늦어지나요?" asks when the parcel will move. That
        # is a schedule question the pipeline can answer, and only when no
        # individual component is named -- "스탠드는 언제 오나요? TV는 받았어요"
        # still reports one part absent.
        if _SCHEDULE_QUESTION.search(text) and not _ORDERED_COMPONENT.search(text):
            return False
        # A part that came off during assembly arrived; it is not missing.
        detached_after_use = bool(
            _AFTER_DELIVERY.search(text)
            and _DETACHED.search(text)
            and not _NEVER_ARRIVED.search(text)
        )
        return not detached_after_use
    if _PARTIAL_DELIVERY.search(text):
        return True
    if _absence_next_to_a_component(text):
        return not (_SCHEDULE_QUESTION.search(text) or _DAMAGE.search(text))
    return False
