from __future__ import annotations

import re

from .config_loader import load_answer_wrapper


_INITIAL_WRAPPER = load_answer_wrapper()
DEFAULT_PREFIX = _INITIAL_WRAPPER.header
DEFAULT_CLOSING = _INITIAL_WRAPPER.footer.rsplit("\n\n", 1)[-1]
FINAL_FALLBACK_NOTICE = _INITIAL_WRAPPER.footer.rsplit("\n\n", 1)[0]
DEFAULT_FALLBACK_NOTICE = FINAL_FALLBACK_NOTICE.replace("\n", " ")

_LEADING_PRESENTATION = re.compile(
    r"\A\s*(?:"
    # A bare greeting is always redundant with the wrapper header, whether it
    # is followed by a newline, the end of the string, or content on the same
    # line (e.g. GPT writing "안녕하세요 상품명 기준..." with no punctuation
    # or line break in between, which previously slipped past this filter and
    # produced a duplicated greeting once the wrapper header was prepended).
    r"[♣♧]*안녕하세요[♣♧]*\s*[,!]?\s*고객님\s*[.!]?\s*(?:\n+|\Z|(?=\S))"
    r"|[♣♧]*안녕하세요[♣♧]*\s+오제앤에스\s*(?:입니다)?\s*[.!]?\s*(?:\n+|\Z|(?=\S))"
    r"|[♣♧]*안녕하세요[♣♧]*\s*[.,!]?\s*(?:\n+|\Z|(?=\S))"
    r"|오제\s*챗봇\s*\(?(?:Chat\s*Bot)?\)?(?:이|가)?\s*답변드립니다\s*[.!]?\s*(?:\n+|\Z)"
    r")",
    re.IGNORECASE,
)
_TRAILING_PRESENTATION = re.compile(
    r"(?:\n+|\A)\s*감사합니다\s*[.!]?\s*\Z",
    re.IGNORECASE,
)


def format_auto_answer(
    body: str,
    *,
    prefix: str = DEFAULT_PREFIX,
    fallback_notice: str = DEFAULT_FALLBACK_NOTICE,
    closing: str = DEFAULT_CLOSING,
) -> str:
    """Compatibility entry point; the Final wrapper has one owner."""

    del prefix, fallback_notice, closing
    return format_final_answer(body)


def format_final_answer(body: str) -> str:
    """Apply the Template Repository wrapper once without rewriting it."""

    clean = extract_answer_body(body)
    if not clean:
        return ""
    wrapper = load_answer_wrapper()
    return f"{wrapper.header}\n\n{clean}\n\n{wrapper.footer}"


def extract_answer_body(answer: str) -> str:
    text = str(answer or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    wrapper = load_answer_wrapper()
    leading_wrappers = tuple(
        dict.fromkeys(
            (wrapper.header, DEFAULT_PREFIX, *wrapper.legacy_headers)
        )
    )
    while text:
        matched_prefix = next(
            (prefix for prefix in leading_wrappers if text.startswith(prefix)),
            None,
        )
        if matched_prefix:
            text = text[len(matched_prefix) :].strip()
            continue
        normalized = _LEADING_PRESENTATION.sub("", text, count=1).strip()
        if normalized == text:
            break
        text = normalized
    suffixes = tuple(
        dict.fromkeys(
            (
                wrapper.footer,
                _INITIAL_WRAPPER.footer,
                *wrapper.legacy_footers,
            )
        )
    )
    matched_suffix = next(
        (suffix for suffix in suffixes if text.endswith(suffix)), None
    )
    if matched_suffix:
        text = text[: -len(matched_suffix)].strip()
    else:
        for footer in suffixes:
            notice = footer.rsplit("\n\n", 1)[0]
            marker = text.rfind(notice)
            if marker >= 0:
                text = text[:marker].strip()
                break
        closings = {
            footer.rsplit("\n\n", 1)[-1]
            for footer in suffixes
        }
        for closing in closings:
            if text.endswith(closing):
                text = text[: -len(closing)].strip()
                break
    while text:
        normalized = _TRAILING_PRESENTATION.sub("", text, count=1).strip()
        if normalized == text:
            break
        text = normalized
    return text


def combine_answer_bodies(*bodies: str) -> str:
    clean = [extract_answer_body(body) for body in bodies]
    return format_auto_answer("\n\n".join(body for body in clean if body))


def korean_date(iso_date: str) -> str:
    match = re.match(
        r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})",
        str(iso_date or ""),
    )
    if match:
        year, month, day = (int(part) for part in match.groups())
        return f"{year}년 {month}월 {day}일"
    try:
        year, month, day = (int(part) for part in iso_date.split("-"))
    except (TypeError, ValueError):
        return str(iso_date or "")
    return f"{year}년 {month}월 {day}일"
