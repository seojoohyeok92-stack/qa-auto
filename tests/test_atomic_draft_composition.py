"""A draft worth reading is not the same decision as a draft worth publishing.

The previous stage taught the classifier to judge each atomic question on its
own and kept those verdicts on the analysis. Nothing downstream read them, so
two things still went wrong:

* "사장님 오늘 주문했는데 해피콜 및 기사님 빠른설치 부탁드릴게요" is a single
  request for staff action, and the pre-generation gate skipped it as "not
  worth generating". Staff opened the inquiry to a blank reply -- even though
  phase9 owns exactly this inquiry and answers it from a deterministic safe
  template that states no date and promises nothing.

* The four-question installation inquiry reached the model as one blob. The
  per-question verdicts and the evidence retrieval had already paired with each
  question were flattened away, so the model could not be told which parts it
  had grounds for and which it must defer.

The invariant these pin, in both directions:

    Draft EXISTS            does not imply     Auto Post ALLOWED
    Auto Post HOLD          does not imply     Draft ABSENT
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from answer.models import AnswerStatus
from answer.answer_format import korean_date

# The DPS schedule these doubles report must not sit before the day the
# inquiry was registered: is_schedule_stale() then correctly refuses to present
# it, the answer cannot confirm a date, and eligibility falls to
# REVIEW_REQUIRED. Pinning a literal date made that true only until the day
# passed -- these tests began failing when UTC rolled past 2026-08-28. The
# fixture now states what it always meant, "an appointment still ahead",
# and derives the Korean rendering from the same value.
UPCOMING_DATE = (
    datetime.now(timezone.utc) + timedelta(days=1)
).strftime("%Y-%m-%d")
UPCOMING_DATE_KR = korean_date(UPCOMING_DATE)

from answer.engine import AnswerEngine
from answer.source_adapter import answer_request_from_inquiry
from answer.text_utils import is_product_concept_question
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.answer_service import AnswerService
from services.auto_processing_eligibility_service import (
    AutoProcessingEligibilityService,
)
from services.draft_generation_service import _atomic_question_payload
from services.hybrid_answer_service import HybridAnswerService
from services.inquiry_analysis_service import InquiryAnalysisService
from services.pre_generation_gate import PreGenerationGate


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
# A compound where one part genuinely needs a person: card benefits change
# every promotion and nothing here holds today's terms.
CASE_PARTIAL = "무타공 설치인가요?\n카드 할인도 되나요?"


def analyse(question: str, *, order_id: str = "", source_type: str = "PRODUCT_INQUIRY"):
    return InquiryAnalysisService().analyze(
        answer_request_from_inquiry(
            {
                "id": 1,
                "source_type": source_type,
                "inquiry_type": source_type,
                "source_question_id": "compose",
                "external_inquiry_id": "compose",
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
# 1. CASE C -- a hold must not mean a blank reply
# ==========================================================================


def test_case_c_is_no_longer_skipped_before_generation() -> None:
    analysis = analyse(CASE_C, source_type="CUSTOMER_INQUIRY")
    decision = PreGenerationGate.evaluate_plan(
        analysis=analysis.to_dict(),
        plan={"needs_staff_review": True, "is_high_risk": False},
    )

    assert decision.skip_generation is False


def test_a_genuinely_unanswerable_inquiry_is_still_skipped() -> None:
    """The gate did not become a rubber stamp."""

    decision = PreGenerationGate.evaluate_plan(
        analysis={
            "inquiry_subtype": "CANCEL_RETURN_EXCHANGE",
            "manual_review_required": True,
            "delivery_question": False,
        },
        plan={"needs_staff_review": True, "is_high_risk": False},
    )

    assert decision.skip_generation is True


def test_high_risk_still_skips_even_for_a_delivery_inquiry() -> None:
    decision = PreGenerationGate.evaluate_plan(
        analysis={
            "inquiry_subtype": "SCHEDULE_CHANGE_REQUEST",
            "manual_review_required": True,
            "delivery_question": True,
        },
        plan={"needs_staff_review": True, "is_high_risk": True},
    )

    assert decision.skip_generation is True


# ==========================================================================
# 2. CASE A -- answer the question first, footnote second
# ==========================================================================


def core_lines(answer: str) -> list[str]:
    return [
        line.strip()
        for line in (answer or "").split("\n")
        if line.strip()
        and "안녕하세요" not in line
        and "챗봇" not in line
    ]


@pytest.mark.parametrize(
    "question", [CASE_A, "주문하면 보통 며칠 걸리나요?", "배송기간이 어떻게 되나요?"]
)
def test_case_a_leads_with_the_delivery_answer_not_the_notice(
    question: str,
) -> None:
    lines = core_lines(AnswerEngine().answer(PRODUCT, question, "").answer or "")

    assert lines, question
    assert "알림톡" not in lines[0], question
    assert "배송" in lines[0] or "설치" in lines[0], question


def test_case_a_states_no_duration_it_cannot_support() -> None:
    """"Not immediate" follows from the confirmed policy. A number does not."""

    import re

    answer = AnswerEngine().answer(PRODUCT, CASE_A, "").answer or ""

    assert "바로 배송되는 방식은 아닙니다" in answer
    assert not re.search(r"\d+\s*(?:영업일|일|주)\s*(?:이내|정도|소요|걸)", answer)


def test_the_notice_question_still_leads_with_the_notice() -> None:
    lines = core_lines(
        AnswerEngine().answer(PRODUCT, "설치일 알림톡 언제 오나요?", "").answer or ""
    )

    assert "알림톡" in lines[0]


# ==========================================================================
# 3. CASE D taxonomy -- a concept question is a product question
# ==========================================================================


@pytest.mark.parametrize(
    "question",
    [
        "스마트티비는 처음인데 인터넷티비랑 다른건가요",
        "스마트TV가 뭔가요",
        "일반 TV랑 스마트TV 차이가 뭔가요",
        "인터넷 연결해서 보는 TV인가요",
        "셋톱박스 없이 사용할 수 있나요",
    ],
)
def test_product_concept_questions_are_classified(question: str) -> None:
    analysis = analyse(question)

    assert analysis.inquiry_subtype == "PRODUCT_SPEC_OR_FEATURE", question
    assert analysis.manual_review_required is False, question
    assert analysis.requires_order_lookup is False, question
    assert analysis.requires_dps_lookup is False, question


@pytest.mark.parametrize(
    "question",
    [
        "무타공 설치인가요",
        "브라켓 별도 구매해야하나요",
        "언제 배송되나요?",
        "벽걸이 설치 가능한가요?",
        "토요일에도 배송되나요?",
        "설치일 알림톡 언제 오나요?",
    ],
)
def test_the_concept_predicate_does_not_swallow_other_meanings(
    question: str,
) -> None:
    assert not is_product_concept_question(question)


def test_classification_does_not_authorise_inventing_the_feature() -> None:
    """Classifying it is not knowing it.

    A concept question is now routed to the product-evidence path; whether any
    given feature is true of this product still has to come from Product
    Knowledge or verified Learning.
    """

    analysis = analyse("스마트TV가 뭔가요")

    assert analysis.inquiry_subtype == "PRODUCT_SPEC_OR_FEATURE"
    assert analysis.answer_strategy.value == "GENERAL_GUIDANCE"
    # The rule engine holds no such fact and must not improvise one.
    assert not (AnswerEngine().answer(PRODUCT, "스마트TV가 뭔가요", "").answer or "")


def test_case_d_classifies_all_four_questions() -> None:
    analysis = analyse(CASE_D)

    assert len(analysis.subquestion_analyses) == 4
    assert all(
        record["inquiry_subtype"] != "UNCLASSIFIED"
        for record in analysis.subquestion_analyses
    )


# ==========================================================================
# 4. Atomic questions reach draft generation, paired with their own evidence
# ==========================================================================


def test_each_question_is_handed_over_with_its_own_verdict() -> None:
    analysis = analyse(CASE_D)
    payload = _atomic_question_payload(analysis, {})

    assert len(payload) == 4
    assert [item["index"] for item in payload] == [1, 2, 3, 4]
    for item in payload:
        for key in (
            "question", "inquiry_subtype", "detected_intent", "answerable",
            "review_required", "evidence_status", "learning_ids",
            "unresolved_reason",
        ):
            assert key in item


def test_evidence_stays_paired_with_the_question_it_was_found_for() -> None:
    """A Learning found for the bracket question is not grounds for another."""

    analysis = analyse(CASE_D)
    payload = _atomic_question_payload(
        analysis,
        {
            "subquestion_evidence": [
                {
                    "subquestion": "브라켓 별도 구매해야하나요",
                    "status": "ANSWERABLE",
                    "evidence_coverage": "SUPPORTED",
                    "source": "ACTIVE_POSITIVE_LEARNING",
                    "learning_ids": [19553],
                }
            ]
        },
    )

    bracket = next(i for i in payload if "브라켓" in i["question"])
    assert bracket["learning_ids"] == [19553]
    for other in payload:
        if other is bracket:
            continue
        assert other["learning_ids"] == []
        assert other["evidence_status"] is None


def test_a_single_question_needs_no_breakdown() -> None:
    assert _atomic_question_payload(analyse(CASE_A), {}) == []


def test_the_prompt_carries_the_breakdown_and_its_rules() -> None:
    from services.draft_generation_service import (
        ATOMIC_QUESTION_INSTRUCTIONS,
    )

    joined = " ".join(ATOMIC_QUESTION_INSTRUCTIONS)

    assert "빠뜨리지" in joined
    assert "추측" in joined
    assert "회피" in joined


# ==========================================================================
# 5. Partial answerability -- one unresolved part does not erase the rest
# ==========================================================================


def test_partial_inquiry_keeps_the_answerable_parts() -> None:
    analysis = analyse(CASE_PARTIAL)

    assert len(analysis.subquestion_analyses) == 2
    assert len(analysis.answerable_subquestions) == 1
    assert len(analysis.unresolved_subquestions) == 1
    answerable = analysis.answerable_subquestions[0]
    assert "무타공" in answerable["question"]


def test_partial_inquiry_still_holds_the_whole_thing_for_review() -> None:
    analysis = analyse(CASE_PARTIAL)

    assert analysis.manual_review_required is True
    assert analysis.auto_answerable is False


def test_partial_payload_marks_only_the_unresolved_part() -> None:
    payload = _atomic_question_payload(analyse(CASE_PARTIAL), {})

    unresolved = [item for item in payload if item["review_required"]]
    answerable = [item for item in payload if item["answerable"]]

    assert len(unresolved) == 1
    assert len(answerable) == 1
    assert unresolved[0]["unresolved_reason"]
    assert answerable[0]["unresolved_reason"] is None


# ==========================================================================
# 6. CASE B -- two policy questions, judged separately
# ==========================================================================


def test_case_b_is_two_questions_with_separate_verdicts() -> None:
    analysis = analyse(CASE_B)
    payload = _atomic_question_payload(analysis, {})

    assert len(payload) == 2
    assert "토요일" in payload[0]["question"]
    assert "소요" in payload[1]["question"]


def test_case_b_weekend_half_is_not_answered_without_a_basis() -> None:
    """No weekend rule exists in the shipping config; none may be invented."""

    answer = AnswerEngine().answer(PRODUCT, CASE_B, "").answer or ""

    for invented in ("토요일에도 배송이 가능", "주말에도 배송이 가능", "토요일 배송이 가능합니다"):
        assert invented not in answer


def test_case_b_duration_half_keeps_its_own_answer() -> None:
    """The unresolved weekend half must not delete the duration answer."""

    answer = AnswerEngine().answer(PRODUCT, "주문시 며칠 소요되나요", "").answer or ""

    assert answer.strip()
    assert "배송" in answer or "설치" in answer


# ==========================================================================
# 7. The invariant, end to end on a temp database
# ==========================================================================


class _StubProvider:
    """Answers only the parts it is told are answerable."""

    name = "openai"

    def __init__(self) -> None:
        self.draft_prompts: list[str] = []

    def generate_json(self, *, task, prompt, context):
        if task == "UNDERSTANDING":
            return {
                "category": "GENERAL", "questions": ["q"], "urgency": "NORMAL",
                "emotion": "NORMAL", "confidence": 0.95,
                "requires_review": False, "reason": "stub",
            }
        if task == "DRAFT":
            self.draft_prompts.append(prompt)
            return {
                "answer": "무타공 설치가 가능합니다. 카드 할인 적용 여부는 담당자 확인이 필요합니다.",
                "confidence": 0.9, "used_facts": [],
                "missing_information": [], "requires_review": False,
                "warnings": [], "learning_usage": [], "historical_usage": [],
                "feedback_signal_usage": [],
                "subquestion_results": [
                    {"subquestion": "무타공 설치인가요", "answered": True,
                     "status": "ANSWERABLE"},
                    {"subquestion": "카드 할인도 되나요", "answered": False,
                     "status": "NO_RELIABLE_SOURCE"},
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
            "installation_date": UPCOMING_DATE,
            "installation_date_display": UPCOMING_DATE,
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


def run_pipeline(tmp_path, label: str, question: str, order_id: str | None = None):
    database = Database(tmp_path / f"{label}.db")
    database.initialize()
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "S", "source_type": "NAVER",
            "source_question_id": f"compose-{label}",
            "inquiry_type": "CUSTOMER_INQUIRY" if order_id else "PRODUCT_INQUIRY",
            "title": "문의", "content": question, "product_name": PRODUCT,
            "order_id": order_id, "product_order_id": None, "raw_json": {},
        }
    ).inquiry_id
    provider = _StubProvider()
    error: str | None = None
    try:
        AnswerService(
            database,
            dps_enrichment=_FakeDps(),
            hybrid_service=HybridAnswerService(provider),
        ).generate_for_inquiry(inquiry_id)
    except Exception as exc:
        error = type(exc).__name__

    draft = AnswerRepository(database).latest_for_inquiry(inquiry_id)
    if draft is None:
        return {"error": error, "draft": None}, provider
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
        "error": error,
        "draft": record,
        "answer": record.get("original_answer") or "",
        "validation_status": record.get("validation_status"),
        "metadata": metadata,
        "eligibility": eligibility.decision,
        "reasons": tuple(eligibility.reasons),
        "auto_post_allowed": eligibility.decision == "SAFE",
    }, provider


def test_case_c_draft_exists_and_is_still_held(tmp_path) -> None:
    """The whole point of this stage, proven end to end."""

    result, _ = run_pipeline(tmp_path, "case-c", CASE_C)

    assert result["draft"] is not None, "staff were handed a blank reply"
    assert result["answer"].strip()
    assert "일정 변경은 담당자 확인이 필요합니다" in result["answer"]
    assert result["eligibility"] == "REVIEW_REQUIRED"
    assert result["auto_post_allowed"] is False


def test_partial_inquiry_draft_exists_and_is_still_held(tmp_path) -> None:
    """Answerable parts answered, unresolved part deferred, inquiry held."""

    result, provider = run_pipeline(tmp_path, "partial", CASE_PARTIAL)

    assert result["draft"] is not None
    assert result["answer"].strip()
    assert "무타공" in result["answer"]
    assert "담당자 확인" in result["answer"]
    assert result["eligibility"] == "REVIEW_REQUIRED"
    assert result["auto_post_allowed"] is False


def test_the_model_was_told_which_parts_it_may_answer(tmp_path) -> None:
    _, provider = run_pipeline(tmp_path, "prompted", CASE_PARTIAL)

    assert provider.draft_prompts, "no draft prompt was built"
    prompt = provider.draft_prompts[0]
    assert "atomic_questions" in prompt
    assert "review_required" in prompt
    assert "무타공 설치인가요" in prompt
    assert "카드 할인도 되나요" in prompt


def test_case_d_draft_exists_and_is_still_held(tmp_path) -> None:
    result, _ = run_pipeline(tmp_path, "case-d", CASE_D)

    assert result["draft"] is not None
    assert result["answer"].strip()
    analysis = (result["metadata"].get("processing_plan") or {}).get(
        "analysis"
    ) or {}
    assert len(analysis.get("subquestion_analyses") or []) == 4
    assert result["auto_post_allowed"] is False


def test_a_clean_single_question_still_auto_posts(tmp_path) -> None:
    """The separation cuts both ways: nothing here made publishing harder."""

    result, _ = run_pipeline(
        tmp_path, "clean", "언제설치가능한가요?", order_id=ORDER_NUMBER
    )

    assert result["draft"] is not None
    assert result["validation_status"] == "PASS"
    assert result["eligibility"] == "SAFE"
    assert result["auto_post_allowed"] is True


def test_missing_order_number_still_asks_for_it(tmp_path) -> None:
    result, _ = run_pipeline(tmp_path, "no-order", "제가 주문한 상품 언제 배송되나요?")

    assert "일반 주문번호가 필요합니다" in result["answer"]
    assert result["metadata"].get("selected_answer_route") == "ORDER_ID_REQUEST"


def test_already_answered_inquiry_is_still_blocked(tmp_path) -> None:
    result, _ = run_pipeline(
        tmp_path, "answered", "언제설치가능한가요?", order_id=ORDER_NUMBER
    )
    inquiry = {"source_answered": 1, "post_status": "NOT_POSTED"}
    eligibility = AutoProcessingEligibilityService().evaluate(
        inquiry=inquiry, draft=result["draft"],
        route=str(result["metadata"].get("selected_answer_route") or ""),
    )

    assert eligibility.decision == "BLOCKED"
    assert eligibility.stage == "IDEMPOTENCY"


def test_semantic_coverage_blocks_clear_partial_answer_before_auto_post(tmp_path) -> None:
    """A core sub-question omission must become staff review."""

    result, _ = run_pipeline(tmp_path, "soft", CASE_PARTIAL)
    coverage = result["metadata"].get("semantic_coverage") or {}

    assert coverage.get("phase") == "DETERMINISTIC_COVERAGE_GATE"
    assert result["draft"]["program_status"] == AnswerStatus.NEEDS_REVIEW.value
