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


def mask_personal_information(text: object) -> str:
    value = str(text or "")
    value = EMAIL_PATTERN.sub("<masked-email>", value)
    value = PHONE_PATTERN.sub("<masked-phone>", value)
    value = LONG_NUMBER_PATTERN.sub(r"\1****\2", value)
    value = NAME_LABEL_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<masked-name>",
        value,
    )
    return value
