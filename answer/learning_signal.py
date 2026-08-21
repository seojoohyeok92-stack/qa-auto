from __future__ import annotations

import re
from enum import Enum


class SignalKind(str, Enum):
    """What kind of content an operator memo actually carries.

    Orthogonal to ``LearningSignalType`` (POSITIVE/NEGATIVE/EXCLUDED/...),
    which describes the *evaluation outcome*.  This describes the *content
    type* of the note attached to that evaluation, so the same POSITIVE
    review can carry a GOOD_PATTERN note and the same NEGATIVE review can
    carry a BAD_PATTERN or CORRECTION note.
    """

    REASON = "REASON"
    GOOD_PATTERN = "GOOD_PATTERN"
    BAD_PATTERN = "BAD_PATTERN"
    CORRECTION = "CORRECTION"
    VERIFIED_FACT = "VERIFIED_FACT"


SIGNAL_KIND_LABELS: dict[SignalKind, str] = {
    SignalKind.REASON: "평가 이유",
    SignalKind.GOOD_PATTERN: "좋은 패턴",
    SignalKind.BAD_PATTERN: "잘못된 패턴",
    SignalKind.CORRECTION: "사실 정정",
    SignalKind.VERIFIED_FACT: "운영 확인 Fact",
}

SIGNAL_KIND_BY_LABEL = {label: kind for kind, label in SIGNAL_KIND_LABELS.items()}

# CORRECTION/VERIFIED_FACT content may be used as factual evidence in a
# future answer.  GOOD_PATTERN/BAD_PATTERN are style/structure guidance only.
# REASON is neither -- it is the categorical justification for the
# evaluation itself and must never reach the answer-generation prompt as
# content.
FACTUAL_SIGNAL_KINDS = frozenset({SignalKind.CORRECTION, SignalKind.VERIFIED_FACT})
GUIDANCE_SIGNAL_KINDS = frozenset({SignalKind.GOOD_PATTERN, SignalKind.BAD_PATTERN})

PRODUCT_SCOPES = frozenset(
    {"MODEL", "VARIANT", "PRODUCT_FAMILY", "CATEGORY", "POLICY", "GLOBAL"}
)


class OriginKind(str, Enum):
    POSITIVE_REVIEW = "POSITIVE_REVIEW"
    NEGATIVE_REVIEW = "NEGATIVE_REVIEW"
    EXCLUSION_REVIEW = "EXCLUSION_REVIEW"
    HISTORICAL_REVIEW = "HISTORICAL_REVIEW"


def normalize_signal_kind(value: str | SignalKind | None) -> SignalKind:
    if value is None or not str(value).strip():
        return SignalKind.REASON
    if isinstance(value, SignalKind):
        return value
    normalized = str(value).strip()
    if normalized in SIGNAL_KIND_BY_LABEL:
        return SIGNAL_KIND_BY_LABEL[normalized]
    return SignalKind(normalized.upper())


def normalize_fact_scope(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().upper()
    if not normalized:
        return None
    if normalized not in PRODUCT_SCOPES:
        raise ValueError(f"Unknown fact scope: {value}")
    return normalized


# Generic Korean affirmative/negative polarity markers.  These are ordinary
# grammatical polarity cues (any "possible/not possible", "provided/not
# provided" phrasing), not a per-product or per-topic keyword list -- the
# same handful of markers applies to any verified fact or correction text,
# regardless of what product or policy it describes.
_NEGATION_MARKERS = re.compile(
    r"(불가능|불가(?:$|[^능])|안\s?됩니다|안\s?되(?:며|고|어|는)?|못\s?하|"
    r"어렵습니다|어려울|없습니다|아닙니다|않습니다|제한됩니다|"
    r"지원되지\s?않|해당되지\s?않|적용되지\s?않)"
)
_AFFIRMATION_MARKERS = re.compile(
    r"("
    r"(?<!불)가능합니다|(?<!불)가능해요|(?<!불)가능하며|(?<!불)가능합니다만|"
    r"(?<!안)(?<!안 )됩니다|지원합니다|제공됩니다|받으실\s?수\s?있|"
    r"(?<!불)이용\s?가능|적용됩니다|해당됩니다"
    r")"
)


def detect_polarity(text: object) -> str:
    """Classify a fact/correction statement as AFFIRMATIVE, NEGATIVE, or UNCERTAIN.

    A lightweight linguistic heuristic (not a semantic entailment check),
    used only to catch the clear-cut case of two ACTIVE verified facts that
    flatly disagree ("가능" vs "불가능") in the same product/topic scope --
    see ``facts_conflict``.
    """

    value = str(text or "")
    has_negation = bool(_NEGATION_MARKERS.search(value))
    has_affirmation = bool(_AFFIRMATION_MARKERS.search(value))
    if has_negation and not has_affirmation:
        return "NEGATIVE"
    if has_affirmation and not has_negation:
        return "AFFIRMATIVE"
    return "UNCERTAIN"


def facts_conflict(left: object, right: object) -> bool:
    """Return True only when both statements have a clear, opposite polarity."""

    left_polarity = detect_polarity(left)
    right_polarity = detect_polarity(right)
    if left_polarity == "UNCERTAIN" or right_polarity == "UNCERTAIN":
        return False
    return left_polarity != right_polarity
