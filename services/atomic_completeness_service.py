"""No part of what the customer asked may leave the draft without a trace.

The pipeline already decomposes an inquiry, judges each part on its own, and
hands those verdicts to the model with instructions to address every one. What
it could not do was *check*. A model told to cover four questions and covering
three produced a draft that read perfectly well and quietly dropped one, and
nothing downstream noticed -- the Validator asks whether the answer is safe and
the eligibility gate asks whether it may be published; neither asks whether it
is complete.

"사장님 오늘 주문했는데 해피콜 및 기사님 빠른설치 부탁드릴게요" is the case that
showed it. Two requests in one sentence, and because they are joined by "및"
rather than by punctuation the splitter sees one part -- so the atomic
machinery had nothing to iterate over. The answer addressed the scheduling
request and said nothing about the 해피콜 at all.

So completeness is checked on *topics* rather than on split parts: the same
deterministic anchors the coverage evaluator already uses, applied to the
question and to the finished draft. A topic the customer raised and the draft
never touches is named explicitly, in one sentence that promises nothing, so
staff and the customer can both see it was noticed rather than lost.

Two deliberate limits:

* Only a *partial* answer is completed -- at least one topic addressed and at
  least one not. An answer that addresses nothing is not an answer with a gap;
  it is off-target, which the coverage soft gate already records, and bolting a
  deferral onto it would dress up the wrong reply as a considered one.
* The added sentence states no fact, no date and no availability. It says the
  remaining part needs a person, which is what is true.

Nothing here decides anything. The Validator still runs on the completed text
and the eligibility gate still decides publication on its own reasons.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from services.semantic_coverage_service import (
    RESPONSIVE_TOPICS,
    topics_of,
)


ANSWERED = "ANSWERED_WITH_EVIDENCE"
UNRESOLVED = "UNRESOLVED_REVIEW_REQUIRED"
UNDETERMINED = "UNDETERMINED"

# What to call each topic when telling the customer it still needs checking.
# Only topics that can be named plainly appear here; anything else falls back
# to the generic wording rather than inventing a label.
TOPIC_LABELS: dict[str, str] = {
    "NOTIFICATION": "해피콜·사전 연락 안내",
    "DELIVERY_WEEKEND": "주말·공휴일 배송 가능 여부",
    "DELIVERY_DURATION": "배송 소요기간",
    "DELIVERY_REGION": "배송 가능 지역",
    "DELIVERY_COST": "배송비",
    "DELIVERY_SCHEDULE": "배송 일정",
    "INSTALLATION_SCHEDULE": "설치 일정",
    "SCHEDULE_CHANGE": "일정 변경 요청",
    "INSTALLATION_METHOD": "설치 방법",
    "BRACKET": "브라켓 관련 사항",
    "PACKAGE_CONTENTS": "구성품 포함 여부",
    "WARRANTY_AS": "보증·A/S",
    "BENEFIT": "할인·혜택 적용 여부",
    "DAMAGE_DISPUTE": "파손 관련 처리",
    "CANCEL_RETURN": "취소·반품·교환",
    "PRODUCT_SPEC": "제품 사양",
    "PRODUCT_CONCEPT": "스마트TV·인터넷TV 차이",
    "STORE_PICKUP": "방문수령",
}

_DEFERRAL_PREFIX = "문의하신 "
_DEFERRAL_SUFFIX = " 부분은 담당자 확인 후 안내드리겠습니다."
_GENERIC_LABEL = "일부 내용"


@dataclass(frozen=True)
class QuestionCompleteness:
    question: str
    status: str
    covered_topics: tuple[str, ...] = ()
    uncovered_topics: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "status": self.status,
            "covered_topics": list(self.covered_topics),
            "uncovered_topics": list(self.uncovered_topics),
        }


@dataclass(frozen=True)
class CompletenessResult:
    total: int = 0
    answered: int = 0
    unresolved: int = 0
    undetermined: int = 0
    uncovered_topics: tuple[str, ...] = ()
    deferral_sentence: str = ""
    questions: tuple[QuestionCompleteness, ...] = field(default_factory=tuple)

    @property
    def needs_completion(self) -> bool:
        return bool(self.deferral_sentence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_questions": self.total,
            "answered": self.answered,
            "unresolved": self.unresolved,
            "undetermined": self.undetermined,
            "uncovered_topics": list(self.uncovered_topics),
            "completed": self.needs_completion,
            "questions": [item.to_dict() for item in self.questions],
        }


class AtomicCompletenessService:
    """Checks that every topic the customer raised survives into the draft."""

    def evaluate(
        self,
        *,
        question: str,
        answer: str,
        subquestions: Iterable[dict[str, Any]] | None = None,
    ) -> CompletenessResult:
        body = str(answer or "").strip()
        parts = [
            dict(item)
            for item in (subquestions or ())
            if str(item.get("question") or "").strip()
        ]
        if not parts:
            single = str(question or "").strip()
            if not single:
                return CompletenessResult()
            parts = [{"question": single, "manual_review_required": False}]
        if not body:
            return CompletenessResult(
                total=len(parts),
                undetermined=len(parts),
                questions=tuple(
                    QuestionCompleteness(
                        str(item["question"]), UNDETERMINED
                    )
                    for item in parts
                ),
            )

        answer_topics = topics_of(body)
        results: list[QuestionCompleteness] = []
        uncovered: list[str] = []
        for item in parts:
            text = str(item["question"])
            question_topics = topics_of(text)
            if not question_topics:
                results.append(
                    QuestionCompleteness(text, UNDETERMINED)
                )
                continue
            covered: list[str] = []
            missing: list[str] = []
            for topic in sorted(question_topics):
                accepted = {topic} | RESPONSIVE_TOPICS.get(topic, frozenset())
                (covered if answer_topics & accepted else missing).append(topic)
            status = (
                ANSWERED
                if covered and not missing
                else UNRESOLVED
                if missing
                else UNDETERMINED
            )
            results.append(
                QuestionCompleteness(
                    text, status, tuple(covered), tuple(missing)
                )
            )
            uncovered.extend(missing)

        ordered_uncovered = tuple(dict.fromkeys(uncovered))
        answered = sum(1 for item in results if item.status == ANSWERED)
        unresolved = sum(1 for item in results if item.status == UNRESOLVED)
        undetermined = sum(
            1 for item in results if item.status == UNDETERMINED
        )
        # Complete only a partial answer. Nothing addressed at all is a wrong
        # reply, not an incomplete one, and the coverage soft gate records that
        # separately.
        anything_covered = any(item.covered_topics for item in results)
        sentence = (
            self._deferral_sentence(ordered_uncovered)
            if ordered_uncovered and anything_covered
            else ""
        )
        return CompletenessResult(
            total=len(results),
            answered=answered,
            unresolved=unresolved,
            undetermined=undetermined,
            uncovered_topics=ordered_uncovered,
            deferral_sentence=sentence,
            questions=tuple(results),
        )

    @staticmethod
    def _deferral_sentence(topics: tuple[str, ...]) -> str:
        labels = [
            TOPIC_LABELS[topic] for topic in topics if topic in TOPIC_LABELS
        ]
        if not labels:
            labels = [_GENERIC_LABEL]
        # Two is as many as reads naturally; beyond that the sentence stops
        # being a sentence.
        named = ", ".join(labels[:2])
        return f"{_DEFERRAL_PREFIX}{named}{_DEFERRAL_SUFFIX}"

    @staticmethod
    def complete(answer_body: str, sentence: str) -> str:
        """Append the deferral to the answer body, once."""

        body = str(answer_body or "").strip()
        if not sentence or not body or sentence in body:
            return body
        return f"{body}\n\n{sentence}"
