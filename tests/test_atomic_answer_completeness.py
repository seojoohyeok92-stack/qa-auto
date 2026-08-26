"""A question the draft never touches must not simply disappear.

The pipeline decomposes an inquiry, judges each part, and hands those verdicts
to the model with instructions to address every one. Nothing checked that it
did. A model told to cover four questions and covering three produced a draft
that read perfectly well and dropped one silently -- the validator asks whether
an answer is safe, the eligibility gate asks whether it may be published, and
neither asks whether it is complete.

"사장님 오늘 주문했는데 해피콜 및 기사님 빠른설치 부탁드릴게요" is the case that
exposed it, and it is worse than a model omission: the two requests are joined
by "및" rather than by punctuation, so the splitter returns *one* part and the
atomic machinery had nothing to iterate over at all. The reply addressed the
scheduling request and said nothing about the 해피콜.

Completeness is therefore checked on topics rather than on split parts, using
the same deterministic anchors the coverage evaluator already applies, and a
topic raised by the customer that the draft never touches is named in one
sentence that promises nothing.

Throughout, the separation from the previous stage holds: completing a draft
never makes it publishable.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.answer_service import AnswerService
from services.atomic_completeness_service import (
    ANSWERED,
    UNDETERMINED,
    UNRESOLVED,
    AtomicCompletenessService,
)
from services.auto_processing_eligibility_service import (
    AutoProcessingEligibilityService,
)
from services.hybrid_answer_service import HybridAnswerService
from services.inquiry_analysis_service import InquiryAnalysisService
from services.semantic_coverage_service import topics_of


PRODUCT = "삼성 125.7cm(50인치) UHD 4K 1등급 비즈니스TV LH50BEFHLGFXKR 스탠드형"
ORDER_NUMBER = "2026082198559811"

CASE_A = "주문하면 바로 배송되나요"
CASE_B = "혹시 토요일에도 배달 가능하나요?\n주문시 며칠 소요되나요"
CASE_C = "사장님 오늘 주문했는데 해피콜 및 기사님 빠른설치 부탁드릴게요"
CASE_D = (
    "1. 무타공 설치인가요?\n"
    "2. 브라켓 별도 구매해야하나요?\n\n"
    "3. 기존 벽에 타공구멍이 있는데\n"
    "같은 곳에 타공 설치 가능한지\n\n"
    "4. 스마트티비는 처음인데\n"
    "인터넷티비랑 다른건가요?"
)
CASE_E = "무타공 설치인가요?\n카드 할인도 되나요?"

CHANGE_REVIEW_BODY = (
    "요청하신 배송·설치 일정 변경은 담당자 확인이 필요합니다.\n\n"
    "주문 정보를 확인한 후 안내드리겠습니다."
)


def completeness(question: str, answer: str, subquestions=None):
    return AtomicCompletenessService().evaluate(
        question=question, answer=answer, subquestions=subquestions
    )


def analyse(question: str, *, order_id: str = "", source_type: str = "PRODUCT_INQUIRY"):
    from answer.source_adapter import answer_request_from_inquiry

    return InquiryAnalysisService().analyze(
        answer_request_from_inquiry(
            {
                "id": 1,
                "source_type": source_type,
                "inquiry_type": source_type,
                "source_question_id": "complete",
                "external_inquiry_id": "complete",
                "title": "문의",
                "content": question,
                "product_name": PRODUCT,
                "order_id": order_id,
                "product_order_id": "",
                "raw_json": {},
                "source_answered": 0,
                "post_status": "NOT_POSTED",
            }
        )
    )


# ==========================================================================
# 1. CASE C -- where the happycall request was lost
# ==========================================================================


def test_case_c_carries_two_meanings_that_the_splitter_cannot_separate() -> None:
    """The diagnosis, pinned: one part, two topics.

    "해피콜 및 기사님 빠른설치" joins two requests with a conjunction, not with
    punctuation, so sentence splitting yields a single part. Checking topics
    instead of parts is what makes both visible.
    """

    from answer.text_utils import split_subquestions

    assert len(split_subquestions(CASE_C)) == 1
    assert topics_of(CASE_C) == frozenset({"NOTIFICATION", "SCHEDULE_CHANGE"})


def test_expedite_request_is_a_schedule_topic() -> None:
    """"빠른설치 부탁" names no date and no 변경, and had no topic at all."""

    assert "SCHEDULE_CHANGE" in topics_of("기사님 빠른설치 부탁드릴게요")
    assert "SCHEDULE_CHANGE" in topics_of("최대한 빨리 배송 부탁드립니다")


def test_case_c_notes_the_untouched_happycall_request() -> None:
    result = completeness(CASE_C, CHANGE_REVIEW_BODY)

    assert result.uncovered_topics == ("NOTIFICATION",)
    assert result.needs_completion is True
    assert "해피콜" in result.deferral_sentence
    assert "담당자 확인" in result.deferral_sentence


def test_the_added_sentence_states_no_fact() -> None:
    """It may say a person will check. It may not say when, or whether."""

    import re

    sentence = completeness(CASE_C, CHANGE_REVIEW_BODY).deferral_sentence

    assert not re.search(r"\d", sentence)
    for forbidden in ("가능합니다", "발송됩니다", "됩니다만", "해드리겠습니다"):
        assert forbidden not in sentence


# ==========================================================================
# 2. Completeness states -- answered, unresolved, never absent
# ==========================================================================


def test_every_question_gets_a_state() -> None:
    result = completeness(
        CASE_E,
        "무타공 설치가 가능합니다.",
        [{"question": "무타공 설치인가요"}, {"question": "카드 할인도 되나요"}],
    )

    assert result.total == 2
    assert {item.status for item in result.questions} == {ANSWERED, UNRESOLVED}
    assert all(item.question for item in result.questions)


def test_a_fully_answered_inquiry_is_left_alone() -> None:
    result = completeness(
        CASE_E,
        "무타공 설치가 가능합니다. 카드 할인 적용 여부는 담당자 확인이 필요합니다.",
        [{"question": "무타공 설치인가요"}, {"question": "카드 할인도 되나요"}],
    )

    assert result.answered == 2
    assert result.needs_completion is False
    assert result.deferral_sentence == ""


def test_an_answer_addressing_nothing_is_not_completed() -> None:
    """An off-target reply is wrong, not incomplete.

    Appending a deferral would dress it up as considered; the coverage soft
    gate records the miss separately.
    """

    result = completeness(
        "보증기간이 얼마나 되나요?",
        "설치 예정일 관련 알림톡은 설치일 전날 발송됩니다.",
    )

    assert result.uncovered_topics == ("WARRANTY_AS",)
    assert result.needs_completion is False


def test_an_unrecognised_question_is_undetermined_not_invented() -> None:
    result = completeness("이거 어떤가요?", "확인해보겠습니다.")

    assert result.questions[0].status == UNDETERMINED
    assert result.needs_completion is False


def test_completion_is_idempotent() -> None:
    service = AtomicCompletenessService()
    sentence = completeness(CASE_C, CHANGE_REVIEW_BODY).deferral_sentence
    once = service.complete(CHANGE_REVIEW_BODY, sentence)
    twice = service.complete(once, sentence)

    assert once == twice
    assert once.count(sentence) == 1


def test_a_schedule_question_is_not_read_as_a_method_question() -> None:
    """"언제설치가능한가요?" asks when.

    The method anchors read "설치가능" as a second subject, which made a clean
    confirmed-date answer look like it had skipped a question.
    """

    assert topics_of("언제설치가능한가요?") == frozenset({"INSTALLATION_SCHEDULE"})
    assert "INSTALLATION_METHOD" in topics_of("벽걸이 설치 가능한가요?")


def test_a_confirmed_date_answer_is_not_completed() -> None:
    result = completeness(
        "언제설치가능한가요?", "확인 결과 설치 예정일은 2026년 8월 28일입니다."
    )

    assert result.answered == 1
    assert result.needs_completion is False


# ==========================================================================
# 3. End to end -- CASE A..H
# ==========================================================================


class _StubProvider:
    """Answers only the first question, on purpose."""

    name = "openai"

    def __init__(self, answer: str) -> None:
        self.answer = answer

    def generate_json(self, *, task, prompt, context):
        if task == "UNDERSTANDING":
            return {
                "category": "GENERAL", "questions": ["q"], "urgency": "NORMAL",
                "emotion": "NORMAL", "confidence": 0.95,
                "requires_review": False, "reason": "stub",
            }
        if task == "DRAFT":
            return {
                "answer": self.answer, "confidence": 0.9, "used_facts": [],
                "missing_information": [], "requires_review": False,
                "warnings": [], "learning_usage": [], "historical_usage": [],
                "feedback_signal_usage": [],
                "subquestion_results": [
                    {"subquestion": "q", "answered": True,
                     "status": "ANSWERABLE"},
                ],
            }
        if task == "SELF_REVIEW":
            return {
                "passed": True, "answered_all_questions": True,
                "has_speculation": False, "facts_consistent": True,
                "requires_review": False, "reason": "stub", "warnings": [],
            }
        raise AssertionError(task)


class _FakeDps:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def enrich(self, request, **kwargs):
        self.calls.append(request.order_id)
        request.metadata["dps"] = {
            "lookup_required": True, "lookup_status": "SUCCESS",
            "installation_date": "2026-08-28",
            "installation_date_display": "2026-08-28",
            "delivery_status": "구매요청", "installation_status": "구매요청",
            "change_request": False, "general_segments": [],
            "dps_segments": [], "warnings": [], "cache_used": True,
        }
        return SimpleNamespace(
            decision=SimpleNamespace(lookup_required=True),
            metadata=request.metadata["dps"], lookup_row=None,
        )

    def skip_for_phase9(self, request, **kwargs):
        request.metadata["dps"] = {
            "lookup_required": False, "lookup_status": "NOT_REQUIRED",
        }
        return SimpleNamespace(
            decision=SimpleNamespace(lookup_required=False),
            metadata=request.metadata["dps"], lookup_row=None,
        )


def run(tmp_path, label, question, *, order_id=None, gpt_answer="무타공 설치가 가능합니다."):
    database = Database(tmp_path / f"{label}.db")
    database.initialize()
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "S", "source_type": "NAVER",
            "source_question_id": f"complete-{label}",
            "inquiry_type": "CUSTOMER_INQUIRY" if order_id else "PRODUCT_INQUIRY",
            "title": "문의", "content": question, "product_name": PRODUCT,
            "order_id": order_id, "product_order_id": None, "raw_json": {},
        }
    ).inquiry_id
    dps = _FakeDps()
    try:
        AnswerService(
            database,
            dps_enrichment=dps,
            hybrid_service=HybridAnswerService(_StubProvider(gpt_answer)),
        ).generate_for_inquiry(inquiry_id)
    except Exception:
        pass

    draft = AnswerRepository(database).latest_for_inquiry(inquiry_id)
    if draft is None:
        return {"draft": None, "dps_calls": dps.calls}
    record = dict(draft)
    for key in ("metadata_json", "validator_result_json"):
        raw = record.get(key)
        if isinstance(raw, str):
            try:
                record[key] = json.loads(raw)
            except ValueError:
                record[key] = {}
    metadata = record.get("metadata_json") or {}
    eligibility = AutoProcessingEligibilityService().evaluate(
        inquiry=InquiryRepository(database).get(inquiry_id),
        draft=record,
        route=str(metadata.get("selected_answer_route") or ""),
    )
    return {
        "draft": record,
        "answer": record.get("original_answer") or "",
        "validation_status": record.get("validation_status"),
        "metadata": metadata,
        "completeness": metadata.get("atomic_completeness") or {},
        "eligibility": eligibility.decision,
        "auto_post_allowed": eligibility.decision == "SAFE",
        "dps_calls": dps.calls,
    }


def test_case_a_unchanged(tmp_path) -> None:
    """A: the direct-answer improvement survives, and still auto-posts."""

    result = run(tmp_path, "A", CASE_A)

    assert "바로 배송되는 방식은 아닙니다" in result["answer"]
    assert result["validation_status"] == "PASS"
    assert result["auto_post_allowed"] is True
    assert result["completeness"].get("completed") is False


def test_case_b_keeps_two_atomic_questions(tmp_path) -> None:
    """B: both halves survive as separate questions."""

    analysis = analyse(CASE_B)

    assert len(analysis.subquestion_analyses) == 2
    result = run(tmp_path, "B", CASE_B)
    assert result["draft"] is not None
    assert result["auto_post_allowed"] is False


def test_case_c_draft_addresses_both_requests(tmp_path) -> None:
    """C: the headline case. Both meanings visible, still held."""

    result = run(tmp_path, "C", CASE_C)

    assert result["draft"] is not None
    assert "일정 변경은 담당자 확인이 필요합니다" in result["answer"]
    assert "해피콜" in result["answer"]
    assert result["eligibility"] == "REVIEW_REQUIRED"
    assert result["auto_post_allowed"] is False
    assert result["dps_calls"] == []


def test_case_d_every_question_has_a_state(tmp_path) -> None:
    """D: four questions, four states -- none silently absent."""

    analysis = analyse(CASE_D)
    assert len(analysis.subquestion_analyses) == 4

    result = run(
        tmp_path, "D", CASE_D,
        gpt_answer=(
            "무타공 설치가 가능합니다. 브라켓은 별도 구매가 필요합니다. "
            "기존 타공 위치 재사용 가능 여부는 설치 환경에 따라 달라 확인이 필요합니다."
        ),
    )
    states = {
        item["question"]: item["status"]
        for item in result["completeness"].get("questions") or []
    }

    assert len(states) == 4
    assert set(states.values()) <= {ANSWERED, UNRESOLVED, UNDETERMINED}
    assert result["auto_post_allowed"] is False


def test_case_d_does_not_claim_the_site_condition_is_fine(tmp_path) -> None:
    """Q3 has no site-condition evidence and must not be answered as yes."""

    result = run(
        tmp_path, "D-site", CASE_D,
        gpt_answer="무타공 설치가 가능합니다. 브라켓은 별도 구매가 필요합니다.",
    )

    for invented in ("기존 타공 위치에 그대로 설치할 수 있습니다", "같은 구멍에 설치 가능합니다"):
        assert invented not in result["answer"]


def test_case_e_partial_answer_keeps_the_answerable_half(tmp_path) -> None:
    """E: one unresolved half must not delete the answered half."""

    result = run(tmp_path, "E", CASE_E)

    assert "무타공" in result["answer"]
    assert "담당자 확인" in result["answer"]
    assert result["completeness"].get("answered") == 1
    assert result["completeness"].get("unresolved") == 1
    assert result["eligibility"] == "REVIEW_REQUIRED"
    assert result["auto_post_allowed"] is False


def test_case_f_single_product_question_unchanged(tmp_path) -> None:
    """F: an ordinary single question keeps its existing route."""

    result = run(tmp_path, "F", "벽걸이 설치 가능한가요?",
                 gpt_answer="해당 제품은 벽걸이 설치가 가능합니다.")

    assert result["draft"] is not None
    assert result["completeness"].get("completed") is False


def test_case_g_order_number_still_uses_dps(tmp_path) -> None:
    """G: the DPS path is untouched, and its answer is not completed."""

    result = run(tmp_path, "G", "언제설치가능한가요?", order_id=ORDER_NUMBER)

    assert result["dps_calls"] == [ORDER_NUMBER]
    assert "2026년 8월 28일" in result["answer"]
    assert result["validation_status"] == "PASS"
    assert result["auto_post_allowed"] is True
    assert result["completeness"].get("completed") is False


def test_case_h_missing_order_number_still_asks_for_it(tmp_path) -> None:
    """H: the order-number request policy is untouched."""

    result = run(tmp_path, "H", "제가 주문한 상품 언제 배송되나요?")

    assert "일반 주문번호가 필요합니다" in result["answer"]
    assert result["metadata"].get("selected_answer_route") == "ORDER_ID_REQUEST"
    assert result["completeness"].get("completed") is False


def test_completion_never_makes_an_answer_publishable(tmp_path) -> None:
    """The separation, restated as an invariant.

    Completing a draft adds a sentence saying a person must check something.
    That can only ever make publication less likely, never more.
    """

    for label, question in (("inv-c", CASE_C), ("inv-e", CASE_E)):
        result = run(tmp_path, label, question)
        assert result["completeness"].get("completed") is True
        assert result["auto_post_allowed"] is False


def test_completeness_reasons_never_reach_the_eligibility_gate() -> None:
    """Structural guard, as for the coverage soft gate."""

    import inspect

    source = inspect.getsource(AutoProcessingEligibilityService)

    assert "atomic_completeness" not in source
    assert "ATOMIC_COMPLETENESS" not in source
