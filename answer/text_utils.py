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
