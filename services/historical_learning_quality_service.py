from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any


WORD = re.compile(r"[가-힣A-Za-z0-9]+")
DATE_RANGE = re.compile(
    r"(?:\[?\s*\d{1,2}\s*[./-]\s*\d{1,2}\s*(?:[~～-]|부터)\s*"
    r"\d{1,2}\s*[./-]\s*\d{1,2}\s*\]?)|"
    r"(?:20\d{2}\s*[년./-]\s*\d{1,2}(?:\s*[월./-]\s*\d{1,2})?)|"
    r"(?:\d{1,2}\s*월\s*\d{1,2}\s*일(?:까지|부터)?)"
)
# Wording that bounds an answer to a window all by itself: a closure, a
# limited run, a claim about how things stand right now.
SELF_BOUNDED_PERIOD = re.compile(
    r"하계\s*휴가|휴무|설\s*연휴|추석|명절|기간\s*한정|한시적|"
    r"배송\s*지연\s*공지|브랜드\s*위크|쇼핑\s*라이브|라이브\s*전용\s*쿠폰|"
    r"현재.{0,30}(?:판매|재고).{0,8}(?:중|가능)",
    re.IGNORECASE,
)
# The *name* of a programme. Naming one is not being bound to one, and that
# was the whole difference: nine approved answers -- three of them
# human-verified -- were withheld from "포토리뷰 언제 받을 수 있나요?" because
# they say the words "리뷰 이벤트". What they actually state is a standing
# rule, "네이버폼 작성일 기준 다음달 초 5영업일 내에 지급", which is as true
# today as when it was written. Naming the programme makes an answer temporary
# only when the answer also puts a boundary on it.
PROGRAMME_NAME = re.compile(
    r"프로모션|이벤트|행사(?:\s*기간|\s*페이지)?|감사제|"
    r"온누리.{0,20}(?:혜택|환급)",
    re.IGNORECASE,
)
# What puts a boundary on a programme: an end, a cap, or a claim about how
# things stand at this moment. "감사제 주문량 증가로 배송이 평소보다 지연되고
# 있습니다" names no date and never ends with a word like 까지, yet it is only
# true while that is happening -- 평소보다 says so by comparing to normal.
PERIOD_BOUNDARY = re.compile(
    r"까지|마감|종료|선착순|소진|한정|기간\s*내|진행\s*중|진행중|"
    r"이번\s*(?:주|달|월)|다음\s*주까지|평소보다|현재|지금"
)


def is_time_bound(answer: str) -> bool:
    """Whether this answer is only true inside some window."""

    if DATE_RANGE.search(answer) or SELF_BOUNDED_PERIOD.search(answer):
        return True
    return bool(PROGRAMME_NAME.search(answer) and PERIOD_BOUNDARY.search(answer))


# The weaker question -- does this text mention anything time-flavoured at all.
# Kept for the re-audit service, which asks it of a *question* to decide event
# context. Answer eligibility uses ``is_time_bound``.
TEMPORARY = re.compile(
    f"{SELF_BOUNDED_PERIOD.pattern}|{PROGRAMME_NAME.pattern}", re.IGNORECASE
)


# A statement about *this* asker's own order: its identifier, or the day it is
# scheduled for. Both are true of exactly one customer, so neither can be
# evidence for anybody else.
#
# The list below grew from literal shapes and had gaps an ordinary sentence
# walks straight through. "고객님의 주문번호는 2026****1251 입니다" missed
# because a possessive 의 sits between the two words the pattern required
# adjacent, and "주문하신 상품은 8/7(금) 설치 예정" missed because the date
# detector only recognised ranges and 월/일 spelling, not a bare 8/7. Both were
# promoted as SAFE_REUSABLE evidence for other customers' inquiries.
#
# These three add the *kinds* rather than the two sentences: a possessive or
# relative reference to the asker's order, an identifier read back to them, and
# a specific calendar day given as their schedule.
# Naming the person makes any of these theirs. A bare demonstrative does not:
# "해당 제품은 QLED 패널을 사용합니다" points at the product under discussion,
# which is how every catalogue answer refers to it, so 해당 only marks the
# asker's own when it sits on something an order actually has.
_ASKERS_ORDER = (
    r"(?:고객님|귀하)(?:의|께서)?\s*(?:주문|상품|제품|배송|설치)"
    r"|해당\s*(?:주문|배송|설치|건)"
    r"|(?:주문|구매|결제)하신\s*(?:상품|제품|건)"
)
_IDENTIFIER_READ_BACK = (
    r"(?:주문|상품주문|판매|운송장|송장)\s*번호(?:는|은|가|:)\s*[\d*]{4,}"
)
# A named day paired with what is scheduled for it. The day alone is not
# enough -- "9월 5일까지 신청" is a campaign deadline, not one order's date --
# so the schedule word has to be there too.
_SCHEDULED_DAY = (
    r"(?:설치|배송|출고|도착)\s*(?:예정일?|예정)?\s*(?:은|는|이|가)?\s*"
    r"\d{1,2}\s*(?:[./월]\s*)\d{1,2}\s*(?:일)?"
    r"|\d{1,2}\s*[./월]\s*\d{1,2}\s*(?:일)?\s*\(?[월화수목금토일]?\)?\s*"
    r"(?:설치|배송|출고|도착)\s*예정"
)
ORDER_SPECIFIC = re.compile(
    rf"{_ASKERS_ORDER}|{_IDENTIFIER_READ_BACK}|{_SCHEDULED_DAY}|"
    r"배송\s*완료(?:로|처리)|"
    r"(?:발송|출고|구성품|스탠드|본품).{0,24}(?:누락|완료|진행)|"
    r"누락(?:된|된 것으로|으로)|익일\s*(?:출고|발송)|"
    r"(?:오늘|내일|금일)\s*(?:출고|발송|도착)|송장\s*번호|"
    r"판매\s*번호|(?:해당|고객님|현재).{0,12}배송\s*기사.{0,16}(?:배정|연락|방문)|"
    r"배송\s*기사.{0,8}(?:배정\s*완료|연락\s*완료)|"
    r"주문(?:건)?.{0,24}(?:삭제|취소\s*완료|환불\s*완료|\d+대만\s*출고)|"
    r"(?:송장|운송장|택배).{0,20}\d{3,4}[ -]\d{3,4}[ -]\d{3,4}",
    re.IGNORECASE,
)
AVOIDANCE = re.compile(
    r"확인(?:이|할 수)?\s*(?:어렵|불가)|담당자\s*확인|판매처에\s*문의|"
    r"정확한\s*안내가\s*어렵|직원\s*검토|확인.{0,6}어려|"
    r"담당자.{0,8}검토|검토.{0,8}필요",
    re.IGNORECASE,
)
LOW_INFORMATION = re.compile(
    r"^(?:안녕하세요[,.!\s]*)?(?:고객님[,.!\s]*)?"
    r"(?:네이버|오제앤에스|판매자)(?:입니다|입니다만)?[.!\s]*$",
    re.IGNORECASE,
)

CONCEPT_PATTERNS: dict[str, re.Pattern[str]] = {
    "RETURN": re.compile(r"반품|교환|환불|회수"),
    "PICKUP": re.compile(r"수거|택배|보내|기사.{0,8}회수|회수.{0,8}기사"),
    "PACKAGING": re.compile(r"박스|포장|개봉"),
    "DELIVERY_STATUS": re.compile(r"배송|도착|출고|발송|미도착|완료"),
    "DELIVERY_DATE": re.compile(r"언제|예정일|며칠|말일까지|소요\s*기간"),
    "INSTALLATION": re.compile(r"설치|자가\s*설치|벽걸이|스탠드"),
    "AFTER_SERVICE": re.compile(r"A/?S|에이에스|수리|고장|제품\s*문제", re.IGNORECASE),
    "PRODUCT_FUNCTION": re.compile(r"기능|OTT|넷플릭스|셋탑|패널|LED|QLED|화질", re.IGNORECASE),
    "PRODUCT_OPTION": re.compile(r"옵션|인치|모델|무빙|상품\s*종류"),
    "PROMOTION": re.compile(r"포인트|쿠폰|프로모션|행사|이벤트|할인|감사제"),
    "POLICY": re.compile(r"정책|규정|기준|비용|포함|접수|절차|방법"),
}
SUPPORTING_CONCEPTS = {"PACKAGING", "POLICY"}


@dataclass(frozen=True)
class HistoricalEligibility:
    status: str
    context_eligible: bool
    reasons: tuple[str, ...]
    alignment_score: float
    trust_score: float
    question_concepts: tuple[str, ...]
    answer_concepts: tuple[str, ...]
    temporary: bool
    order_specific: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "context_eligible": self.context_eligible,
            "reasons": list(self.reasons),
            "alignment_score": self.alignment_score,
            "trust_score": self.trust_score,
            "question_concepts": list(self.question_concepts),
            "answer_concepts": list(self.answer_concepts),
            "temporary": self.temporary,
            "order_specific": self.order_specific,
        }


class HistoricalLearningQualityService:
    """Deterministic, read-only eligibility for legacy employee answers.

    Stored Historical data is preserved.  The result controls only whether a
    row may become generation evidence for a different customer's inquiry.
    """

    @staticmethod
    def concepts(text: str) -> tuple[str, ...]:
        return tuple(
            name for name, pattern in CONCEPT_PATTERNS.items()
            if pattern.search(str(text or ""))
        )

    @staticmethod
    def _lexical_alignment(question: str, answer: str) -> float:
        left = {
            token for token in WORD.findall(question.lower())
            if len(token) > 1
        }
        right = {
            token for token in WORD.findall(answer.lower())
            if len(token) > 1
        }
        return len(left & right) / max(len(left), 1)

    @staticmethod
    def contradicts(left: str, right: str) -> bool:
        """Detect a small set of explicit, reusable-policy contradictions."""
        pairs = (
            (r"(?:비용|설치비).{0,12}(?:포함|무료)", r"(?:비용|설치비).{0,12}(?:별도|미포함|추가)"),
            (r"(?:기사|방문).{0,12}설치", r"자가\s*설치|직접\s*설치"),
            (r"(?:반품|교환).{0,12}가능", r"(?:반품|교환).{0,12}(?:불가|어렵)"),
            (r"(?:A/?S|수리).{0,12}(?:가능|접수)", r"(?:A/?S|수리).{0,12}(?:불가|지원하지)"),
        )
        return any(
            (re.search(a, left, re.IGNORECASE) and re.search(b, right, re.IGNORECASE))
            or (re.search(b, left, re.IGNORECASE) and re.search(a, right, re.IGNORECASE))
            for a, b in pairs
        )

    def assess(
        self,
        *,
        question: str,
        answer: str,
        stored_quality: float = 0.0,
        policy_risk: str = "NONE",
        active: bool = True,
        structured_temporary_valid: bool = False,
    ) -> HistoricalEligibility:
        question = str(question or "").strip()
        answer = str(answer or "").strip()
        q_concepts = self.concepts(question)
        a_concepts = self.concepts(answer)
        q_all, a_all = set(q_concepts), set(a_concepts)
        q_core = q_all - SUPPORTING_CONCEPTS or q_all
        a_core = a_all - SUPPORTING_CONCEPTS or a_all
        shared = q_core & a_core
        concept_coverage = len(shared) / max(len(q_core), 1)
        lexical = self._lexical_alignment(question, answer)
        alignment = round(0.78 * concept_coverage + 0.22 * lexical, 4)
        temporal = is_time_bound(answer)
        order_specific = bool(ORDER_SPECIFIC.search(answer))
        risk = str(policy_risk or "NONE").upper()
        unsupported_answer_concepts = a_core - q_core
        missing_question_concepts = q_core - a_core
        cross_domain_mismatch = bool(
            "RETURN" in q_all
            and ({"PICKUP", "PACKAGING"} & q_all)
            and "PRODUCT_FUNCTION" in a_all
            and "PRODUCT_FUNCTION" not in q_all
            and "PICKUP" not in a_all
        )
        mismatch = bool(
            q_concepts
            and a_concepts
            and (
                cross_domain_mismatch
                or not shared
                or (
                    unsupported_answer_concepts
                    and missing_question_concepts
                    and concept_coverage <= 0.50
                    and (lexical < 0.16 or len(q_core) >= 2)
                )
                or (
                    concept_coverage < 0.40
                    and unsupported_answer_concepts
                    and alignment < 0.36
                )
            )
        )
        low_relevance = bool(
            not mismatch and q_concepts and a_concepts and alignment < 0.18
        )
        reasons: list[str] = []
        if not active or not answer:
            status = "REVIEW_REQUIRED"
            reasons.append("INACTIVE_OR_EMPTY")
        elif risk in {"HIGH", "BLOCK", "BLOCKED"}:
            status = "POLICY_RISK"
            reasons.append("POLICY_RISK")
        elif temporal and not structured_temporary_valid:
            # Legacy rows have no trustworthy validity bounds.  A mixed
            # policy/event answer is therefore blocked as a whole rather than
            # being text-sliced and silently changing its meaning.
            status = "TEMPORARY_OR_EXPIRED"
            reasons.append("TEMPORARY_WITHOUT_STRUCTURED_VALIDITY")
        elif order_specific:
            status = "ORDER_SPECIFIC"
            reasons.append("PAST_ORDER_FACT_NOT_REUSABLE")
        elif mismatch:
            status = "QUESTION_ANSWER_MISMATCH"
            reasons.append("QUESTION_ANSWER_SEMANTIC_MISMATCH")
        elif low_relevance:
            status = "LOW_RELEVANCE"
            reasons.append("INSUFFICIENT_QUESTION_ANSWER_ALIGNMENT")
        elif len(answer) < 12 or LOW_INFORMATION.fullmatch(answer) or AVOIDANCE.search(answer):
            status = "REVIEW_REQUIRED"
            reasons.append("LOW_INFORMATION_ANSWER")
        else:
            status = "SAFE_REUSABLE"
            reasons.append("ALIGNED_STABLE_REUSABLE_CONTENT")
        eligible = status == "SAFE_REUSABLE"
        trust = float(stored_quality or 0.0)
        trust = 0.55 * trust + 0.45 * alignment
        if not eligible:
            trust *= 0.35
        return HistoricalEligibility(
            status=status,
            context_eligible=eligible,
            reasons=tuple(reasons),
            alignment_score=alignment,
            trust_score=round(max(0.0, min(trust, 1.0)), 4),
            question_concepts=q_concepts,
            answer_concepts=a_concepts,
            temporary=temporal,
            order_specific=order_specific,
        )

    def audit(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        counts: Counter[str] = Counter({
            "SAFE_REUSABLE": 0,
            "QUESTION_ANSWER_MISMATCH": 0,
            "TEMPORARY_OR_EXPIRED": 0,
            "ORDER_SPECIFIC": 0,
            "POLICY_RISK": 0,
            "LOW_RELEVANCE": 0,
            "CONFLICTING": 0,
            "REVIEW_REQUIRED": 0,
        })
        samples: dict[str, list[dict[str, Any]]] = {}
        usable = 0
        for row in rows:
            result = self.assess(
                question=str(row.get("question") or ""),
                answer=str(row.get("seller_answer") or ""),
                stored_quality=float(row.get("quality_score") or 0),
                policy_risk=str(row.get("policy_risk") or "NONE"),
                active=bool(row.get("active")),
            )
            counts[result.status] += 1
            usable += int(result.context_eligible)
            bucket = samples.setdefault(result.status, [])
            if len(bucket) < 3:
                bucket.append({
                    "id": int(row.get("id") or 0),
                    "question": str(row.get("question") or "")[:120],
                    "answer": str(row.get("seller_answer") or "")[:120],
                    "stored_quality": float(row.get("quality_score") or 0),
                    **result.to_dict(),
                })
        return {
            "total": len(rows),
            "runtime_context_usable": usable,
            "counts": dict(sorted(counts.items())),
            "samples": samples,
        }
