from __future__ import annotations

import re

# Same token shape as services.similar_answer_service.TOKEN.  Duplicated
# (rather than imported) to avoid a circular import: similar_answer_service
# imports this module to apply the answer-support bonus.
TOKEN = re.compile(r"[가-힣A-Za-z0-9]{2,}")


# Longest-suffix-first so a longer compound particle (e.g. "으로부터") is
# stripped before a shorter one it contains ("로").  This is a lightweight
# heuristic, not a morphological analyzer -- it only exists so the
# answer-support signal can see past Korean particles (구매처 vs 구매처를)
# without hardcoding any topic, product, or keyword.
_JOSA_SUFFIXES: tuple[str, ...] = tuple(
    sorted(
        [
            "에서부터", "으로부터", "이라면", "라면", "이지만", "지만",
            "으로는", "로는", "에는", "에도", "까지도",
            "이라고", "라고", "은", "는", "이", "가", "을", "를", "의", "에",
            "와", "과", "도", "만", "으로", "로", "에서", "부터", "까지",
            "보다", "처럼", "이랑", "랑", "한테", "께", "이라", "라",
        ],
        key=len,
        reverse=True,
    )
)

# Generic Korean interrogative/greeting scaffolding.  These are the same
# handful of grammatical fillers for *any* question (roughly "what/how/
# please/thank you" in English) -- excluding them is not a per-topic or
# per-case rule, it keeps every question's filler words from diluting the
# signal identically.
QUESTION_STOPWORDS: frozenset[str] = frozenset(
    {
        "하나요", "되나요", "가능한가요", "인가요", "나요", "되는지",
        "해야하나요", "무엇으로", "어떻게", "혹시", "문의합니다",
        "궁금합니다", "궁금해요", "알려주세요", "부탁드립니다",
        "감사합니다", "안녕하세요", "문의", "상품", "뭐라고", "뭐로",
        "뭔가요", "될까요", "인지", "있나요",
    }
)

# Purely additive: a candidate whose answer shares no content with the
# question keeps its original retrieval relevance unchanged, so the common
# case (question-similarity and answer-support already agree) never
# regresses.  Calibrated against the reconstructed 685858235 candidates so
# that a correct-but-differently-phrased answer (Historical #8) can outrank
# a wrong answer that merely shares question phrasing (Learning #7 /
# Historical #218) -- see tests/test_answer_support_reranking.py.
ANSWER_SUPPORT_WEIGHT = 0.6
SUPPORTED_THRESHOLD = 0.5


def _stem(token: str) -> str:
    for suffix in _JOSA_SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 2:
            return token[: -len(suffix)]
    return token


def content_stems(text: str) -> set[str]:
    """Content words with common particles/interrogative fillers removed."""

    result: set[str] = set()
    for token in TOKEN.findall(str(text or "").lower()):
        if token in QUESTION_STOPWORDS:
            continue
        stem = _stem(token)
        if stem in QUESTION_STOPWORDS:
            continue
        result.add(stem)
    return result


def answer_support_recall(question: object, candidate_answer: object) -> float:
    """How much of the question's content the candidate answer covers.

    Deliberately asymmetric (recall over the *question*), because the
    question this answers is "does this answer address what was asked",
    not "do these two texts look alike" -- that second question is already
    answered by the existing question-similarity retrieval score.
    """

    required = content_stems(question)
    covered = content_stems(candidate_answer)
    if not required or not covered:
        return 0.0
    return len(required & covered) / len(required)


def coverage_label(support: float) -> str:
    if support >= SUPPORTED_THRESHOLD:
        return "SUPPORTED"
    if support > 0.0:
        return "PARTIALLY_SUPPORTED"
    return "UNSUPPORTED"


def apply_answer_support(
    relevance: float, question: object, candidate_answer: object
) -> tuple[float, float]:
    """Return (boosted_relevance, answer_support) for one candidate.

    ``relevance`` is the caller's already-computed retrieval score (question
    similarity plus whatever product/model/intent bonuses already apply);
    this only adds a bonus on top of it, it never replaces or rescales it.
    """

    support = answer_support_recall(question, candidate_answer)
    boosted = float(relevance) + ANSWER_SUPPORT_WEIGHT * support
    return boosted, support
