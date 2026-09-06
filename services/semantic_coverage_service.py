"""Did the answer address what the customer actually asked?

The existing validator checks whether an answer is *safe*: no invented date, no
leaked internals, no personal data, nothing asserted beyond its evidence. It
does not check whether the answer is about the same subject as the question,
and that gap was measured, not guessed. When the shipping block handed
``install_existing_order_answer`` to 84 questions it did not answer -- among
them "보증기간이 얼마나 되나요?", "캐시백 받을 수 있나요?" and "배송 중 깨진
것 같은데 어떻게 하나요?" -- the validator passed **84 of 84**.

This is the second line for that gap.  It records every result, and clear
``FAIL``/``PARTIAL`` results also require staff review before eligibility can
allow an automatic post.  ``UNKNOWN`` remains observation-only so incomplete
anchors do not turn ordinary inquiries into false holds.

How it decides
--------------
Not string similarity. "언제 배송되나요?" against "배송 관련해서 확인해보겠습
니다" shares its most distinctive word and answers nothing, while "주말에도 받
을 수 있나요?" against "토요일 및 공휴일 배송 가능 여부는 확인이 필요합니다"
shares almost none and answers it squarely. Overlap measures the wrong thing.

Instead both sides are reduced to *topics* by deterministic anchors, and a
sub-question is covered when the answer speaks to its topic. Two consequences
worth naming, because they are the point rather than side effects:

* An answer that says it cannot confirm something still names that topic, so
  "브라켓 규격 정보가 확인되지 않아 호환 여부는 확답이 어렵습니다" covers a
  compatibility question. Admitting a limit is a response; only silence is not.
* A referral sentence covers nothing on its own. The notification template ends
  with "02-706-2678로 문의해 주세요", and treating a phone number as an answer
  to any question would whitewash exactly the 84 failures this exists to see.

Everything is computed from data the pipeline already produced -- the
deterministic sub-question split, the selected route, and the answer text. No
provider call is made, and no question is decided by a rule written for one
inquiry: when the anchors do not recognise a subject on either side the verdict
is UNKNOWN, which is worth more than a forced PASS or FAIL.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from answer.text_utils import compact, split_subquestions


COVERED = "COVERED"
UNCOVERED = "UNCOVERED"
UNKNOWN_SUBQUESTION = "UNKNOWN"

PASS = "PASS"
PARTIAL = "PARTIAL"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"

# The flag still permits a controlled rollout, but a clear missing core topic
# is a safety decision rather than telemetry: it cannot pass to auto-post.
ENABLED_ENV = "OJE_SEMANTIC_COVERAGE_ENABLED"


def is_enabled() -> bool:
    raw = str(os.environ.get(ENABLED_ENV, "1")).strip().lower()
    return raw not in {"0", "false", "no", "off"}


# --------------------------------------------------------------------------
# Topic anchors
#
# Each topic needs a subject *and*, where the bare noun is ambiguous, something
# that says what about it is being asked. "배송" alone is not a topic -- it is
# the occasion for half the inquiries in the store -- so DELIVERY_SCHEDULE
# wants a schedule word beside it and DELIVERY_DURATION wants a duration word.
# --------------------------------------------------------------------------

_DELIVERY = r"(?:배송|배달|발송|출고|도착|수령|받)"
_INSTALL = r"(?:설치|기사|기사님|방문)"
_WHEN = r"(?:언제|며칠|몇일|몇시|날짜|일정|예정일|요일)"


TOPIC_ANCHORS: dict[str, tuple[str, ...]] = {
    "NOTIFICATION": (
        r"알림톡", r"안내문자", r"문자안내", r"문자[는가를]?언제", r"카카오톡",
        r"연락[이은을]?(?:언제|미리|먼저|오|드리|주)", r"언제[^?!.]{0,6}연락",
        r"미리연락", r"사전연락", r"방문전(?:에)?연락", r"해피콜",
    ),
    "DELIVERY_WEEKEND": (
        rf"(?:토요일|일요일|주말|공휴일|휴일)[^?!.]{{0,14}}(?:{_DELIVERY}|설치|가능)",
        rf"(?:{_DELIVERY}|설치)[^?!.]{{0,10}}(?:토요일|일요일|주말|공휴일|휴일)",
    ),
    "DELIVERY_DURATION": (
        rf"{_DELIVERY}[^?!.]{{0,12}}(?:며칠|몇일|얼마나|얼마|어느정도|기간|기한)",
        rf"(?:며칠|몇일|얼마나|어느정도)[^?!.]{{0,12}}{_DELIVERY}",
        r"(?:며칠|몇일|얼마나|어느정도)[^?!.]{0,6}(?:소요|걸리|걸려|걸릴)",
        rf"(?:주문|구매|결제)[^?!.]{{0,10}}(?:며칠|몇일|얼마나|어느정도)",
        # "주문하면 바로 배송되나요" asks how soon without asking how long, and
        # matched nothing at all -- so the answer drifting to installer
        # scheduling could not even be observed. Immediacy is the same topic.
        rf"(?:바로|즉시|당일|곧바로)[^?!.]{{0,4}}{_DELIVERY}",
        # An explicit figure is the other way to answer it.
        r"\d+\s*(?:~|-|에서)?\s*\d*\s*(?:영업일|일|주|시간)[^?!.]{0,8}(?:소요|걸리|걸립|걸려|정도|이내|안에)",
        r"(?:소요|걸리|걸립|걸려)[^?!.]{0,8}\d+\s*(?:영업일|일|주|시간)",
        # Not a bare "영업일": the notification template ends with
        # "상담 가능 시간: 영업일 오전 10시 ~ 오후 5시", and counting business
        # hours as a delivery duration made that template answer "며칠 걸리나
        # 요?" again -- the exact failure being measured.
        r"당일발송", r"익일배송", r"당일출고",
        # An answer may respond to "how long" by explaining what the timing
        # depends on rather than by naming a number. That is a real answer to
        # the question, and it is how install products are honestly described.
        r"일정에\s*(?:맞춰|따라|맞추어)", r"순차적으로", r"기사님\s*배정",
    ),
    # The place names are not here -- see WRITTEN_FORM_ANCHORS below. 도서산간
    # stays: it is four syllables and describes a shipping category rather than
    # naming one place, so it survives compaction without colliding.
    "DELIVERY_REGION": (
        r"도서산간", r"배송[^?!.]{0,8}지역", r"지역[^?!.]{0,8}배송",
    ),
    "DELIVERY_COST": (
        r"배송비", r"배송료", r"택배비", r"추가운임", r"설치비",
    ),
    "DELIVERY_SCHEDULE": (
        rf"{_DELIVERY}[^?!.]{{0,6}}{_WHEN}", rf"{_WHEN}[^?!.]{{0,6}}{_DELIVERY}",
        r"배송예정일", r"배송일정", r"배송상태", r"배송조회", r"송장", r"운송장",
    ),
    "INSTALLATION_SCHEDULE": (
        rf"{_INSTALL}[^?!.]{{0,6}}{_WHEN}", rf"{_WHEN}[^?!.]{{0,6}}{_INSTALL}",
        r"설치예정일", r"설치일정", r"설치날짜",
    ),
    "SCHEDULE_CHANGE": (
        r"(?:일정|날짜|설치일|배송일|방문일|예정일)[^?!.]{0,10}(?:변경|바꿔|바꾸|옮겨|미뤄|미루|당겨|앞당|조율|연기)",
        r"(?:변경|바꿔|바꾸|옮겨|미뤄|당겨|앞당|조율|연기)[^?!.]{0,10}(?:일정|날짜|설치일|배송일|방문일|예정일)",
        # Asking to be moved *earlier* is the same request without naming a
        # date. "기사님 빠른설치 부탁드릴게요" carries no 변경 and no 날짜, so
        # it matched nothing and the request had no topic at all.
        r"(?:빠른|빨리|빠르게|서둘러|급하|최대한빨리|가능한빨리)[^?!.]{0,6}(?:설치|배송|출고|발송|방문)",
        r"(?:설치|배송|출고|발송|방문)[^?!.]{0,6}(?:빨리|빠르게|서둘러|앞당)",
    ),
    "INSTALLATION_METHOD": (
        r"벽걸이", r"타공", r"스탠드",
        r"자가설치", r"설치방법", r"어떻게설치", r"설치가능",
        rf"{_INSTALL}[^?!.]{{0,10}}(?:해주|하나요|해주시|설치도)",
        # The anchors above are shaped like the question -- 해주/하나요 -- so a
        # reply that answers it in the declarative registered nothing: "설치는
        # 전문 기사가 방문하여 진행합니다" carried no installation topic at all.
        # The gap was invisible while the completion pass's deferral supplied
        # the topic instead; removing that exposed it, and a safe compound
        # inquiry answered in full was being held for review.
        rf"{_INSTALL}[^?!.]{{0,10}}(?:진행합니다|진행해드|진행됩니다)",
        r"호환",
    ),
    "PACKAGE_CONTENTS": (
        r"구성품", r"동봉", r"포함되나요", r"포함인가요", r"같이오나요",
        r"함께오나요", r"따로구매", r"별도구매", r"별도로구매", r"따로준비",
        r"별도준비", r"따로사", r"별도판매",
    ),
    # Kept apart from INSTALLATION_METHOD on purpose. "벽걸이 가능한가요?" and
    # "브라켓도 따로 구매해야 하나요?" are two questions, and folding both into
    # one installation topic let an answer about wall mounting alone count as
    # covering the bracket question too.
    "BRACKET": (
        r"브라켓", r"거치대", r"월마운트", r"벽고정", r"베사", r"vesa",
    ),
    "WARRANTY_AS": (
        r"보증기간", r"보증", r"무상", r"a/?s", r"에이에스", r"서비스센터",
        r"수리", r"고장", r"불량",
    ),
    "BENEFIT": (
        r"캐시백", r"포인트", r"적립", r"할인", r"혜택", r"쿠폰", r"무이자",
        r"환급", r"온누리", r"감사제", r"이벤트",
    ),
    "DAMAGE_DISPUTE": (
        r"파손", r"깨[졌진져]", r"하자", r"손상", r"찍힘", r"흠집",
    ),
    "CANCEL_RETURN": (
        r"취소", r"환불", r"반품", r"교환",
    ),
    "ORDER_IDENTIFICATION": (
        r"주문번호", r"주문내역", r"구매내역", r"주문확인",
    ),
    # A customer's other item is an order-history question.  It must never be
    # answered by selecting a nearby catalog model or by a rule about the
    # current listing.
    "PURCHASED_OTHER_PRODUCT": (
        r"(?:다른|나머지)\s*(?:제품|상품|하나|모델)",
        r"같이\s*(?:주문|구매)한\s*(?:제품|상품|것)",
        r"(?:두|2)\s*개\s*(?:구매|주문).{0,16}(?:다른|나머지|모델)",
        r"(?:제가|내가)\s*(?:산|구매한|주문한).{0,16}(?:다른|나머지)",
    ),
    "PRODUCT_SPEC": (
        r"hdmi", r"단자", r"포트", r"인치", r"크기", r"사이즈", r"무게",
        r"해상도", r"화질", r"패널", r"사양", r"스펙", r"소비전력", r"등급",
        r"\d+(?:\.\d+)?\s*(?:mm|cm|kg|인치)", r"가로", r"세로", r"높이", r"폭",
        r"기능", r"지원하나요", r"블루투스", r"와이파이", r"usb",
    ),
    "STORE_PICKUP": (
        r"방문수령", r"직접수령", r"매장수령", r"픽업",
    ),
    # What the product *is*, as opposed to a property it has. Anchored on the
    # concept nouns alone rather than on the question shape ("차이가 뭔가요"),
    # because an answer explaining a smart TV states it -- it does not ask it.
    # Without this, "스마트티비는 처음인데 인터넷티비랑 다른건가요" carried no
    # topic at all, so a draft that ignored it was recorded as UNDETERMINED and
    # the question disappeared with nothing to show it had been dropped.
    "PRODUCT_CONCEPT": (
        r"스마트tv", r"스마트티비", r"인터넷tv", r"인터넷티비",
        r"셋톱", r"셋탑", r"스마트기능",
    ),
}

_COMPILED: dict[str, tuple[re.Pattern[str], ...]] = {
    topic: tuple(re.compile(pattern) for pattern in patterns)
    for topic, patterns in TOPIC_ANCHORS.items()
}


# --------------------------------------------------------------------------
# Anchors read from the sentence as written
#
# Every anchor above is matched against ``compact()``, which deletes spaces so
# that "배송 예정일" and "배송예정일" are one thing. That is right for a phrase
# whose spacing customers vary, and wrong for a proper noun: deleting the space
# manufactures the name out of two unrelated words.
#
# 제주 is the case that reached customers. It appears wherever one word ends in
# 제 and the next begins with 주 -- "어제 주문", "제 주문", "결제 주문",
# "실제 주문일", "감사제 주문량" -- none of which mentions the island.
# Measured over this repository's own inquiry corpus, 제주 matched 30 such
# fusions against 29 genuine mentions, and the fused ones put "문의하신 배송
# 가능 지역 부분은 담당자 확인 후 안내드리겠습니다." on answers to customers
# who had asked only when their order arrives.
#
# A place name is one word, so it is matched against the sentence with its
# spacing intact. The lookbehind covers the other half of the same problem:
# text genuinely written without a space ("실제주문일") must not supply the
# name either -- the name has to begin where a word begins.
#
# This is not a list of exceptions. It is where an anchor goes when it names a
# thing rather than describing a relationship between words; anything added
# here is subject to the same rule.
WRITTEN_FORM_ANCHORS: dict[str, tuple[str, ...]] = {
    "DELIVERY_REGION": (
        r"(?<![가-힣])제주",
        r"(?<![가-힣])울릉",
    ),
}

_COMPILED_WRITTEN: dict[str, tuple[re.Pattern[str], ...]] = {
    topic: tuple(re.compile(pattern) for pattern in patterns)
    for topic, patterns in WRITTEN_FORM_ANCHORS.items()
}

_WHITESPACE = re.compile(r"\s+")


def _as_written(sentence: str) -> str:
    """The sentence with its word boundaries intact, spacing normalised."""

    return _WHITESPACE.sub(" ", str(sentence or "")).strip()


# A question topic may be answered by a different answer topic. These are the
# only such edges, and each one is a real equivalence rather than a shortcut:
# a schedule *is* what a duration question is asking about for an install
# product, and a damage report is handled by the after-sales route.
RESPONSIVE_TOPICS: dict[str, frozenset[str]] = {
    "DELIVERY_DURATION": frozenset({"INSTALLATION_SCHEDULE"}),
    "DELIVERY_SCHEDULE": frozenset({"INSTALLATION_SCHEDULE"}),
    "INSTALLATION_SCHEDULE": frozenset({"DELIVERY_SCHEDULE"}),
    "DAMAGE_DISPUTE": frozenset({"WARRANTY_AS"}),
    "PACKAGE_CONTENTS": frozenset({"BRACKET"}),
    "BRACKET": frozenset({"PACKAGE_CONTENTS"}),
    # Asymmetric on purpose. "기존 스탠드와 호환되나요?" is answered by "베사
    # 규격과 본체 무게를 확인해 주세요" -- a mounting question answered in
    # bracket terms. The reverse does not hold: an answer about wall mounting
    # alone must still leave "브라켓도 따로 구매해야 하나요?" uncovered, which
    # is why BRACKET does not list INSTALLATION_METHOD above.
    "INSTALLATION_METHOD": frozenset({"BRACKET"}),
}

# Routes whose whole purpose is to ask the customer for what is needed before
# the question can be answered. Asking for the order number is a response to an
# order-specific question, not a failure to answer one. Taken from the route
# rather than the wording so a template rewrite cannot silently change it.
INFORMATION_REQUEST_ROUTES = frozenset({"ORDER_ID_REQUEST"})
ORDER_SPECIFIC_TOPICS = frozenset({
    "DELIVERY_SCHEDULE", "INSTALLATION_SCHEDULE", "ORDER_IDENTIFICATION",
    "SCHEDULE_CHANGE",
})


_SENTENCE_SPLIT = re.compile(r"[.!?\n]+")
_SCHEDULE_MARKER = re.compile(r"언제|며칠|몇일|몇시|예정일|일정")


def _sentence_topics(sentence: str) -> set[str]:
    flat = compact(sentence)
    if not flat:
        return set()
    found = {
        topic
        for topic, patterns in _COMPILED.items()
        if any(pattern.search(flat) for pattern in patterns)
    }
    written = _as_written(sentence)
    found |= {
        topic
        for topic, patterns in _COMPILED_WRITTEN.items()
        if any(pattern.search(written) for pattern in patterns)
    }
    # The notice is a subject of its own. "설치 예정일 관련 알림톡은 설치일
    # 전날 발송됩니다" names the schedule only to say when it is announced, and
    # letting that count as a schedule answer would re-admit most of the wrong
    # answers this exists to catch.
    #
    # Applied per sentence rather than to the whole text, because an answer
    # often does both in turn: "결제 확인 후 설치 기사님 일정에 맞춰 배송·설치
    # 가 진행됩니다. 설치 일정 관련 알림톡은 결제 후 발송되며..." really does
    # answer "언제 받을 수 있나요?" in its first sentence, and suppressing that
    # because a later sentence mentions the 알림톡 was the single largest
    # source of false positives in the Phase 1 audit.
    if "NOTIFICATION" in found:
        found -= {"DELIVERY_SCHEDULE", "INSTALLATION_SCHEDULE"}
    # "언제설치가능한가요?" asks *when*, and the method anchors read "설치가능"
    # as "is installation possible" -- a second, wrong subject on a sentence
    # that has only one. Left in, it made a clean confirmed-date answer look
    # like it had skipped a question about installation method.
    #
    # A sentence carrying a schedule marker is about the schedule; "벽걸이 설치
    # 가능한가요?" has no such marker and keeps its method reading.
    if found & {"DELIVERY_SCHEDULE", "INSTALLATION_SCHEDULE"} and _SCHEDULE_MARKER.search(flat):
        found -= {"INSTALLATION_METHOD"}
    return found


def topics_of(text: str) -> frozenset[str]:
    """The subjects a piece of text speaks to, by deterministic anchor."""

    body = str(text or "")
    if not compact(body):
        return frozenset()
    found: set[str] = set()
    for sentence in _SENTENCE_SPLIT.split(body):
        found |= _sentence_topics(sentence)
    return frozenset(found)


# The sentence ``atomic_completeness_service`` appends when a topic the customer
# raised went unanswered. It is generated, not written: a fixed frame around one
# of that module's own topic labels.
#
# It exists to report a gap -- "문의하신 배송비 부분은 담당자 확인 후
# 안내드리겠습니다." -- so staff and customer can both see what was missed. But
# naming the topic put the topic into the answer, and this module counted a
# topic in the answer as a topic addressed, so the sentence written to report
# the gap closed it. Inquiry 687718601 scored PARTIAL on the rule answer alone
# and PASS once the sentence was appended, and auto-posted with one of two
# questions unanswered. All eighteen labels behaved the same way, which put the
# coverage gate out of action for exactly the population it exists to catch.
#
# Only this generated frame is excluded, and deliberately nothing else. A
# person or the model writing "설치 일정 변경은 담당자 확인이 필요합니다" has
# said something true about the subject, and this evaluator has always counted
# that as a response -- admitting a limit is a response; only silence is not.
# Widening the exclusion to every mention of a check would withdraw that.
#
# The frame lives here, below the module that appends it, so both read the same
# constant and a change to the wording cannot leave the two disagreeing.
COMPLETION_DEFERRAL_PREFIX = "문의하신 "
COMPLETION_DEFERRAL_SUFFIX = " 부분은 담당자 확인 후 안내드리겠습니다."

# Matched against ``compact()``, which strips spaces and lowercases. The
# trailing full stop is dropped from the pattern because ``_SENTENCE_SPLIT``
# has already consumed it by the time a sentence reaches here.
_COMPLETION_DEFERRAL = re.compile(
    "%s.{1,40}%s" % (
        re.escape(compact(COMPLETION_DEFERRAL_PREFIX)),
        re.escape(compact(COMPLETION_DEFERRAL_SUFFIX).rstrip(".!?")),
    )
)


def is_completion_deferral(sentence: str) -> bool:
    """Is this the completion pass's own gap report rather than an answer?"""

    return bool(_COMPLETION_DEFERRAL.search(compact(sentence)))


def answered_topics_of(text: str) -> frozenset[str]:
    """The subjects an *answer* actually settles.

    The same anchors as :func:`topics_of`, minus the completion pass's own
    deferral sentences. Used for the answer side alone: a question defers
    nothing, and filtering one would delete the very topic being asked about.
    """

    body = str(text or "")
    if not compact(body):
        return frozenset()
    found: set[str] = set()
    for sentence in _SENTENCE_SPLIT.split(body):
        if is_completion_deferral(sentence):
            continue
        found |= _sentence_topics(sentence)
    return frozenset(found)


@dataclass(frozen=True)
class SubquestionCoverage:
    question: str
    status: str
    reason: str
    question_topics: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "status": self.status,
            "reason": self.reason,
            "topics": list(self.question_topics),
        }


@dataclass(frozen=True)
class SemanticCoverageResult:
    status: str
    reason: str
    total: int = 0
    covered: int = 0
    uncovered: int = 0
    unknown: int = 0
    score: float | None = None
    answer_topics: tuple[str, ...] = ()
    subquestions: tuple[SubquestionCoverage, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "total_subquestions": self.total,
            "covered_subquestions": self.covered,
            "uncovered_subquestions": self.uncovered,
            "unknown_subquestions": self.unknown,
            "score": self.score,
            "answer_topics": list(self.answer_topics),
            "subquestions": [item.to_dict() for item in self.subquestions],
            "phase": "DETERMINISTIC_COVERAGE_GATE",
        }


class SemanticCoverageService:
    """Measures question/answer correspondence. Decides nothing."""

    def evaluate(
        self,
        *,
        question: str,
        answer: str,
        route: str = "",
        subquestions: Iterable[str] | None = None,
    ) -> SemanticCoverageResult:
        answer_text = str(answer or "").strip()
        if not answer_text:
            return SemanticCoverageResult(UNKNOWN, "EMPTY_ANSWER")

        parts = [
            str(part).strip()
            for part in (
                subquestions
                if subquestions is not None
                else split_subquestions(question)
            )
            if str(part).strip()
        ]
        if not parts:
            single = str(question or "").strip()
            if not single:
                return SemanticCoverageResult(UNKNOWN, "EMPTY_QUESTION")
            parts = [single]

        answer_topics = answered_topics_of(answer_text)
        normalized_route = str(route or "").upper()
        information_request = normalized_route in INFORMATION_REQUEST_ROUTES

        if not answer_topics and not information_request:
            # Nothing recognisable to compare against. Saying so is worth more
            # than guessing in either direction.
            return SemanticCoverageResult(
                UNKNOWN,
                "ANSWER_TOPIC_UNRECOGNISED",
                total=len(parts),
                unknown=len(parts),
                subquestions=tuple(
                    SubquestionCoverage(
                        part, UNKNOWN_SUBQUESTION, "ANSWER_TOPIC_UNRECOGNISED"
                    )
                    for part in parts
                ),
            )

        results: list[SubquestionCoverage] = []
        for part in parts:
            question_topics = topics_of(part)
            if not question_topics:
                results.append(
                    SubquestionCoverage(
                        part,
                        UNKNOWN_SUBQUESTION,
                        "QUESTION_TOPIC_UNRECOGNISED",
                    )
                )
                continue
            accepted = set(question_topics)
            for topic in question_topics:
                accepted |= RESPONSIVE_TOPICS.get(topic, frozenset())
            if answer_topics & accepted:
                reason = "TOPIC_MATCH"
            elif information_request and (
                question_topics & ORDER_SPECIFIC_TOPICS
            ):
                reason = "ANSWERED_BY_INFORMATION_REQUEST"
            else:
                results.append(
                    SubquestionCoverage(
                        part,
                        UNCOVERED,
                        "NO_TOPIC_OVERLAP",
                        tuple(sorted(question_topics)),
                    )
                )
                continue
            results.append(
                SubquestionCoverage(
                    part, COVERED, reason, tuple(sorted(question_topics))
                )
            )

        covered = sum(1 for item in results if item.status == COVERED)
        uncovered = sum(1 for item in results if item.status == UNCOVERED)
        unknown = sum(
            1 for item in results if item.status == UNKNOWN_SUBQUESTION
        )
        judged = covered + uncovered
        score = round(covered / judged, 3) if judged else None

        if judged == 0:
            status, reason = UNKNOWN, "NO_JUDGEABLE_SUBQUESTION"
        elif uncovered == 0:
            status, reason = PASS, "ALL_JUDGED_SUBQUESTIONS_COVERED"
        elif covered == 0:
            status, reason = FAIL, "NO_SUBQUESTION_COVERED"
        else:
            status, reason = PARTIAL, "SOME_SUBQUESTIONS_UNCOVERED"

        return SemanticCoverageResult(
            status=status,
            reason=reason,
            total=len(results),
            covered=covered,
            uncovered=uncovered,
            unknown=unknown,
            score=score,
            answer_topics=tuple(sorted(answer_topics)),
            subquestions=tuple(results),
        )
