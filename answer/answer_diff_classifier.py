from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from answer.evidence_support import content_stems
from answer.learning_signal import SignalKind, detect_polarity
from services.learning_compatibility_service import GENERIC_TOPICS, classify_topics

# Mirrors services/learning_service.py's module-level ``STALE_POLICY`` --
# the same regex that already blocks a legacy SELLER_ANSWER import from
# being saved when it contains a price, a specific calendar date, or an
# event-deadline phrase.  Duplicated rather than imported to avoid a
# services<->answer circular import (learning_service already imports the
# signal-extraction service that imports this module); see the module's
# other intentionally-duplicated constants for the same rationale.  Used
# here to gate the opposite direction: a Final answer matching it must
# never become a PERMANENT auto-extracted fact/correction candidate, since
# the content is event/period-scoped, not a stable operational fact
# (section 9 of the 4th-phase request).
TEMPORARY_CONTENT_PATTERN = re.compile(
    r"(?:\d{1,3}(?:,\d{3})*\s*원|\d{4}[./-]\d{1,2}[./-]\d{1,2}|이벤트\s*(?:기간|마감))"
)


class DiffCategory(str, Enum):
    STYLE_ONLY = "STYLE_ONLY"
    FACT_ADDED = "FACT_ADDED"
    FACT_REMOVED = "FACT_REMOVED"
    FACT_CORRECTED = "FACT_CORRECTED"
    POLICY_ADDED = "POLICY_ADDED"
    POLICY_CORRECTED = "POLICY_CORRECTED"
    SCOPE_CHANGED = "SCOPE_CHANGED"
    ANSWER_COMPLETENESS = "ANSWER_COMPLETENESS"
    UNRELATED_CONTENT_REMOVED = "UNRELATED_CONTENT_REMOVED"
    ORDER_LOOKUP_REQUIREMENT_CHANGED = "ORDER_LOOKUP_REQUIREMENT_CHANGED"
    DPS_REQUIREMENT_CHANGED = "DPS_REQUIREMENT_CHANGED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ExtractedSignalCandidate:
    signal_kind: SignalKind
    content_text: str
    diff_categories: tuple[DiffCategory, ...]
    rationale: str
    # Low-risk guidance (GOOD/BAD_PATTERN) can go live immediately; factual
    # claims (CORRECTION/VERIFIED_FACT) always start gated -- see
    # ConfirmationStatus / LearningSignalService.auto_extract_and_capture.
    low_risk: bool = False


# Reused verbatim from services/draft_generation_service.py's local
# ``avoidance_markers`` tuple (kept as a small, intentional duplication --
# see answer/evidence_support.py's TOKEN regex for the same precedent --
# because importing a local variable across modules isn't possible and
# this list is short enough that drift risk is low relative to coupling
# the diff classifier to the draft-recovery module).
AVOIDANCE_MARKERS = (
    "현재 확인된 정보만으로", "안내하기 어렵", "확인할 수 없", "판매처에",
    "담당자 확인", "직원 검토", "추가 확인", "확인이 필요", "확인 후 안내",
)

ORDER_NUMBER_REQUEST_PATTERN = re.compile(r"주문\s*번호")

# Mirrors learning_context_service.py's inline schedule-detection regex
# (explicit_current_schedule / asks_when).  Intentionally duplicated rather
# than imported so this classifier never risks perturbing the retrieval
# module's behavior -- see the module docstring above for the general
# duplication rationale.
_EXPLICIT_CURRENT_SCHEDULE = re.compile(
    r"(?:예정일|도착일|배송일|설치일|말일까지|기다리다|"
    r"내\s*주문|주문한\s*(?:제품|상품))",
    re.IGNORECASE,
)
_ASKS_WHEN = re.compile(r"언제\s*(?:오|도착|배송|설치)", re.IGNORECASE)

MAX_CANDIDATES_PER_DIFF = 3


def is_current_order_dependent(question: object, *, has_order_id: bool = False) -> bool:
    """Return True when a question is answerable only from CURRENT DPS/order data.

    Never derive a Learning fact/correction from a diff about this kind of
    question -- Current DPS/Order authority must never be weakened by
    Learning-tier signals (Acceptance Case F of the 3rd-phase work; reused
    unchanged here for the 4th-phase auto-extraction gate).
    """

    text = str(question or "")
    explicit = bool(_EXPLICIT_CURRENT_SCHEDULE.search(text))
    asks_when = bool(_ASKS_WHEN.search(text))
    return explicit or (has_order_id and asks_when)


def classify_answer_diff(
    *,
    question: str,
    program_answer: str,
    final_answer: str,
    has_order_id: bool = False,
) -> tuple[ExtractedSignalCandidate, ...]:
    """Deterministically classify what changed between Program and Final answers.

    Deliberately conservative: an ambiguous or purely stylistic diff yields
    no candidates at all rather than guessing.  No LLM call -- see the
    4th-phase completion report for why existing deterministic signals
    (content_stems, classify_topics, detect_polarity) were judged
    sufficient for the categories this needs to catch.
    """

    program = str(program_answer or "").strip()
    final = str(final_answer or "").strip()
    if not program or not final or program == final:
        return ()
    if is_current_order_dependent(question, has_order_id=has_order_id):
        return ()

    program_stems = content_stems(program)
    final_stems = content_stems(final)
    added = final_stems - program_stems
    removed = program_stems - final_stems
    union = program_stems | final_stems
    jaccard = len(program_stems & final_stems) / max(len(union), 1)

    program_polarity = detect_polarity(program)
    final_polarity = detect_polarity(final)
    polarity_flip = program_polarity != final_polarity and (
        program_polarity != "UNCERTAIN" or final_polarity != "UNCERTAIN"
    )

    program_avoids = any(marker in program for marker in AVOIDANCE_MARKERS)
    final_avoids = any(marker in final for marker in AVOIDANCE_MARKERS)
    avoidance_resolved = program_avoids and not final_avoids

    program_orders = bool(ORDER_NUMBER_REQUEST_PATTERN.search(program))
    final_orders = bool(ORDER_NUMBER_REQUEST_PATTERN.search(final))
    order_requirement_removed = program_orders and not final_orders

    question_topics = set(classify_topics(question)) - GENERIC_TOPICS
    program_topics = set(classify_topics(program)) - GENERIC_TOPICS
    final_topics = set(classify_topics(final)) - GENERIC_TOPICS
    unrelated_topics = (program_topics - question_topics) - final_topics

    candidates: list[ExtractedSignalCandidate] = []

    if unrelated_topics:
        candidates.append(ExtractedSignalCandidate(
            signal_kind=SignalKind.BAD_PATTERN,
            content_text="질문과 무관한 주제를 답변에 포함하지 않는다.",
            diff_categories=(DiffCategory.UNRELATED_CONTENT_REMOVED,),
            rationale=(
                "Program 답변에 있던 질문과 무관한 주제"
                f"({sorted(unrelated_topics)})가 Final 답변에서 제거됨"
            ),
            low_risk=True,
        ))

    if order_requirement_removed:
        candidates.append(ExtractedSignalCandidate(
            signal_kind=SignalKind.BAD_PATTERN,
            content_text=(
                "현재 주문 조회가 필요하지 않은 일반 문의에는 주문번호를 "
                "요구하지 않고 직접 안내한다."
            ),
            diff_categories=(DiffCategory.ORDER_LOOKUP_REQUIREMENT_CHANGED,),
            rationale="Program 답변의 주문번호 요구가 Final 답변에서 제거됨",
            low_risk=True,
        ))

    # A Final answer describing an event/period-scoped detail (a price, a
    # specific calendar date, an event deadline) must never become a
    # PERMANENT fact/correction candidate merely because a staff edit
    # touched it (Acceptance Case H) -- the auto-extraction pipeline has no
    # way to determine validity/expiry dates on its own, so it stays out of
    # the factual-evidence path entirely; an operator can still register it
    # manually with an explicit TEMPORARY validity window (3rd-phase UI).
    is_temporary_content = bool(TEMPORARY_CONTENT_PATTERN.search(final))

    fact_like = (
        (polarity_flip or avoidance_resolved)
        and bool(final_topics)
        and len(added) >= 1
        and not is_temporary_content
    )
    if fact_like:
        kind = (
            SignalKind.CORRECTION
            if (program_avoids or program_polarity != "UNCERTAIN")
            else SignalKind.VERIFIED_FACT
        )
        category = (
            DiffCategory.POLICY_CORRECTED
            if kind is SignalKind.CORRECTION
            else DiffCategory.FACT_CORRECTED
        )
        candidates.append(ExtractedSignalCandidate(
            signal_kind=kind,
            content_text=final,
            diff_categories=(category,),
            rationale=(
                f"판단이 변경됨 (polarity: {program_polarity}->{final_polarity}, "
                f"avoidance_resolved={avoidance_resolved})"
            ),
            low_risk=False,
        ))
        if avoidance_resolved:
            candidates.append(ExtractedSignalCandidate(
                signal_kind=SignalKind.GOOD_PATTERN,
                content_text=(
                    "근거가 있는 질문은 불필요하게 확인 요청으로 회피하지 "
                    "않고 직접 답변한다."
                ),
                diff_categories=(DiffCategory.ANSWER_COMPLETENESS,),
                rationale="Program 답변의 확인-회피 표현이 Final 답변에서 해소됨",
                low_risk=True,
            ))

    # An unmistakable diff exists (added/removed content, low overlap) but
    # none of the conservative rules above fired confidently -- classify as
    # UNKNOWN and generate nothing.  Ambiguous input never becomes a signal.
    return tuple(candidates[:MAX_CANDIDATES_PER_DIFF])


def classify_operator_note(
    *, question: str, note_text: str, has_order_id: bool = False,
) -> ExtractedSignalCandidate | None:
    """Turn an operator's free-text note into a candidate when -- and only
    when -- it makes an unambiguous, topic-scoped claim.

    A vague style complaint ("답변이 너무 길다") or anything without a
    classifiable topic yields nothing; the note is not dumped verbatim into
    evidence just because a reviewer typed something.
    """

    note = str(note_text or "").strip()
    if not note or len(note) > 400:
        return None
    if is_current_order_dependent(question, has_order_id=has_order_id):
        return None
    if ORDER_NUMBER_REQUEST_PATTERN.search(note) and detect_polarity(note) == "NEGATIVE":
        return ExtractedSignalCandidate(
            signal_kind=SignalKind.CORRECTION,
            content_text=note,
            diff_categories=(DiffCategory.ORDER_LOOKUP_REQUIREMENT_CHANGED,),
            rationale="운영 메모에서 주문번호 요구 정책에 대한 명확한 판단을 감지함",
            low_risk=False,
        )
    topics = set(classify_topics(note)) - GENERIC_TOPICS
    polarity = detect_polarity(note)
    if polarity == "UNCERTAIN" or not topics:
        return None
    return ExtractedSignalCandidate(
        signal_kind=SignalKind.CORRECTION,
        content_text=note,
        diff_categories=(DiffCategory.FACT_CORRECTED,),
        rationale=f"운영 메모에서 명확한 판단(polarity={polarity})을 감지함",
        low_risk=False,
    )
