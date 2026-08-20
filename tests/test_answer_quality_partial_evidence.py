from __future__ import annotations

from types import SimpleNamespace

from answer.answer_format import extract_answer_body, format_final_answer
from answer.hybrid_models import Emotion, IntentResult
from answer.models import AnswerRequest, AnswerResult, AnswerStatus
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.answer_service import AnswerService, _review_required_safe_result
from services.hybrid_answer_service import HybridAnswerService


# ---------------------------------------------------------------------------
# G. Greeting duplication: a wrapper header always opens with a greeting, so
# any greeting already present in a Learning/Historical/GPT answer body must
# be stripped exactly once before the wrapper is applied.  The original
# regex only stripped a leading greeting when it was followed by a line
# break or the end of the string; a GPT draft that wrote the greeting word
# directly followed by content on the same line ("안녕하세요 상품명 기준
# 판매 모델은...") slipped through and produced two greetings once the
# wrapper header (which starts with its own "안녕하세요") was prepended.
# ---------------------------------------------------------------------------


def test_greeting_directly_followed_by_content_is_stripped_once() -> None:
    body = "안녕하세요 상품명 기준 판매 모델은 BED입니다."
    wrapped = format_final_answer(body)
    assert wrapped.count("안녕하세요") == 1


def test_greeting_with_comma_followed_by_content_is_still_stripped() -> None:
    body = "안녕하세요, 고객님. 상품명 기준 판매 모델은 BED입니다."
    wrapped = format_final_answer(body)
    assert wrapped.count("안녕하세요") == 1


def test_bare_greeting_on_its_own_line_still_stripped() -> None:
    # Pre-existing behaviour (greeting on its own line) must not regress.
    body = "안녕하세요\n\n상품명 기준 판매 모델은 BED입니다."
    wrapped = format_final_answer(body)
    assert wrapped.count("안녕하세요") == 1


def test_extract_answer_body_removes_inline_greeting_without_eating_content() -> None:
    body = "안녕하세요 상품명 기준 판매 모델은 BED입니다."
    clean = extract_answer_body(body)
    assert clean == "상품명 기준 판매 모델은 BED입니다."


def test_format_final_answer_is_idempotent_for_inline_greeting() -> None:
    body = "안녕하세요 상품명 기준 판매 모델은 BED입니다."
    wrapped = format_final_answer(body)
    assert format_final_answer(wrapped) == wrapped


# ---------------------------------------------------------------------------
# _review_required_safe_result: the last-resort safety draft must echo what
# the customer actually asked (when GPT UNDERSTANDING already decomposed the
# inquiry) instead of a static "상품 사용 방법 또는 기능" category label
# that may not match the real question.
# ---------------------------------------------------------------------------


def test_safe_result_without_questions_keeps_generic_category_fallback() -> None:
    request = AnswerRequest(inquiry_type="PRODUCT_INQUIRY", question="문의")
    result = _review_required_safe_result(
        request, template_preferred=True, failure_code="ERROR"
    )
    assert "사용 방법 또는 기능" in result.answer


def test_safe_result_with_single_question_echoes_the_actual_question() -> None:
    request = AnswerRequest(inquiry_type="PRODUCT_INQUIRY", question="문의")
    result = _review_required_safe_result(
        request,
        template_preferred=True,
        failure_code="ERROR",
        questions=("65인치도 취급하시나요?",),
    )
    assert "65인치도 취급하시나요?" in result.answer
    assert "사용 방법 또는 기능" not in result.answer


def test_safe_result_with_compound_questions_lists_each_one() -> None:
    request = AnswerRequest(inquiry_type="CUSTOMER_INQUIRY", question="문의")
    questions = (
        "65인치도 취급하시나요?",
        "셋톱박스랑 같이 주문할 수 있나요?",
    )
    result = _review_required_safe_result(
        request,
        template_preferred=False,
        failure_code="ERROR",
        questions=questions,
    )
    for question in questions:
        assert question in result.answer
    assert "주문 또는 상품 관련 내용" not in result.answer


# ---------------------------------------------------------------------------
# Bounded corrective regeneration inside HybridAnswerService: a rejected
# draft must not immediately collapse the whole answer.  Exactly one retry
# is attempted with the concrete validator rejection reasons; if the retry
# passes it is used, and if it still fails the caller falls back to the
# existing (now question-aware) safety draft path unchanged.
# ---------------------------------------------------------------------------


class ScriptedProvider:
    """Deterministic provider returning a scripted response per GPT task."""

    def __init__(
        self,
        *,
        understanding: dict,
        draft_sequence: list[dict],
        review_sequence: list[dict],
    ) -> None:
        self.understanding = dict(understanding)
        self.draft_sequence = list(draft_sequence)
        self.review_sequence = list(review_sequence)
        self.calls: list[str] = []
        self.name = "scripted"

    def generate_json(self, *, task, prompt, context):
        normalized = str(task).upper()
        self.calls.append(normalized)
        if normalized == "UNDERSTANDING":
            return dict(self.understanding)
        if normalized == "DRAFT":
            return dict(self.draft_sequence.pop(0))
        if normalized == "SELF_REVIEW":
            return dict(self.review_sequence.pop(0))
        raise ValueError(f"Unexpected task: {task}")


def _understanding(questions: tuple[str, ...], category: str = "COMPOUND") -> dict:
    return {
        "category": category,
        "questions": list(questions),
        "emotion": "NORMAL",
        "urgency": "NORMAL",
        "confidence": 0.9,
        "requires_review": False,
        "reason": "test",
    }


def _draft(answer: str, *, requires_review: bool = False) -> dict:
    return {
        "answer": answer,
        "confidence": 0.9,
        "used_facts": [],
        "missing_information": [],
        "requires_review": requires_review,
        "warnings": [],
    }


def _review(*, passed: bool, has_speculation: bool = False) -> dict:
    return {
        "passed": passed,
        "answered_all_questions": passed,
        "has_speculation": has_speculation,
        "facts_consistent": True,
        "requires_review": not passed,
        "reason": "test",
        "warnings": [],
    }


def _rule_result(answer: str = "") -> AnswerResult:
    return AnswerResult(
        status=AnswerStatus.NOT_SUPPORTED,
        category="기타",
        reason="적용 가능한 고정 템플릿이 없습니다.",
        answer=answer,
        provider="template_fallback_context",
        auto_answerable=False,
        needs_review=False,
    )


def _request(question: str) -> AnswerRequest:
    return AnswerRequest(
        inquiry_id=1,
        question_id="QUALITY-1",
        store_code="OJE_PLUS",
        inquiry_type="CUSTOMER_INQUIRY",
        question=question,
        product_name="삼성 TV 55인치",
        order_id="",
        metadata={
            "dps": {
                "lookup_required": False,
                "lookup_status": "NOT_REQUIRED",
                "warnings": [],
            }
        },
    )


def test_corrective_regeneration_recovers_after_one_rejected_attempt() -> None:
    questions = ("65인치도 취급하시나요?", "셋톱박스랑 같이 주문할 수 있나요?")
    provider = ScriptedProvider(
        understanding=_understanding(questions),
        draft_sequence=[
            # Attempt 1: speculative claim -> blocked by the validator.
            _draft("65인치는 취급하지 않는 것 같습니다."),
            # Attempt 2 (corrective regeneration): no speculation, stays on
            # topic, and does not assert an unverified fact.
            _draft(
                "65인치 취급 여부와 셋톱박스 동시 주문 가능 여부는 "
                "정확한 확인이 필요하여 직원이 다시 안내드리겠습니다."
            ),
        ],
        review_sequence=[
            _review(passed=False, has_speculation=True),
            _review(passed=True),
        ],
    )
    hybrid = HybridAnswerService(provider, learning_context_provider=lambda *_: {})
    outcome = hybrid.generate(_request(" ".join(questions)), _rule_result())

    assert provider.calls.count("UNDERSTANDING") == 1
    assert provider.calls.count("DRAFT") == 2
    assert provider.calls.count("SELF_REVIEW") == 2
    assert outcome.fallback_used is False
    assert outcome.validation is not None and outcome.validation.passed is True
    assert "취급하지 않는 것 같습니다" not in outcome.result.answer
    assert "확인이 필요" in outcome.result.answer
    event_codes = [event.code for event in outcome.events]
    assert "GPT_CORRECTIVE_REGENERATION_STARTED" in event_codes
    assert "GPT_CORRECTIVE_REGENERATION_COMPLETED" in event_codes


def test_corrective_regeneration_is_bounded_to_a_single_retry() -> None:
    questions = ("65인치도 취급하시나요?",)
    speculative = _draft("아마 취급하지 않는 것 같습니다.")
    provider = ScriptedProvider(
        understanding=_understanding(questions),
        draft_sequence=[speculative, dict(speculative)],
        review_sequence=[
            _review(passed=False, has_speculation=True),
            _review(passed=False, has_speculation=True),
        ],
    )
    hybrid = HybridAnswerService(provider, learning_context_provider=lambda *_: {})
    outcome = hybrid.generate(_request(questions[0]), _rule_result())

    # Both attempts fail: exactly one retry happened (two DRAFT calls total),
    # never an unbounded loop, and the caller correctly falls back.
    assert provider.calls.count("DRAFT") == 2
    assert provider.calls.count("SELF_REVIEW") == 2
    assert outcome.fallback_used is True
    assert outcome.intent is not None
    assert list(outcome.intent.questions) == list(questions)


def test_request_order_id_strategy_never_triggers_regeneration() -> None:
    # The deterministic REQUEST_ORDER_ID draft never calls the provider for
    # DRAFT/SELF_REVIEW, so no regeneration branch should fire even though
    # this path is wired through the same generate() method.
    provider = ScriptedProvider(
        understanding=_understanding(("주문번호가 뭔가요?",), category="ORDER_ID"),
        draft_sequence=[],
        review_sequence=[],
    )
    hybrid = HybridAnswerService(provider, learning_context_provider=lambda *_: {})
    request = _request("주문번호가 뭔가요?")
    request.metadata["phase9_analysis"] = {
        "inquiry_type": "ORDER_INFO_REQUIRED",
        "inquiry_subtype": "",
        "requires_order_lookup": False,
        "requires_dps_lookup": False,
        "requires_order_id": True,
        "order_id_present": False,
        "order_id_validated": False,
        "order_id_status": "MISSING",
        "answer_strategy": "REQUEST_ORDER_ID",
        "selected_fact_keys": [],
        "confidence": 0.9,
        "reasons": [],
        "manual_review_required": False,
        "auto_answerable": False,
        "detected_intent": "ORDER_ID",
    }
    outcome = hybrid.generate(
        request,
        _rule_result("일반 주문번호를 남겨 주시면 확인해 드리겠습니다."),
    )
    # The deterministic REQUEST_ORDER_ID branch never generates a draft via
    # the provider, so the corrective-regeneration branch (which only
    # triggers after a real DRAFT call fails validation) cannot fire either.
    assert provider.calls.count("DRAFT") == 0
    event_codes = [event.code for event in outcome.events]
    assert "GPT_CORRECTIVE_REGENERATION_STARTED" not in event_codes


# ---------------------------------------------------------------------------
# Full pipeline (AnswerService -> HybridAnswerService -> AnswerValidator ->
# safe-draft formatting) proving the two fixes work together end to end,
# without hardcoding any specific product or question text.
# ---------------------------------------------------------------------------


class FakeDps:
    def __init__(self) -> None:
        self.lookup_calls = 0

    def enrich(self, request, **kwargs):
        self.lookup_calls += 1
        request.metadata["dps"] = {
            "lookup_required": False,
            "lookup_status": "NOT_REQUIRED",
            "warnings": [],
        }
        return SimpleNamespace(
            decision=SimpleNamespace(lookup_required=False),
            metadata=request.metadata["dps"],
            lookup_row=None,
        )

    def skip_for_phase9(self, request, **kwargs):
        return self.enrich(request, **kwargs)


class NotSupportedEngine:
    def generate(self, request):
        return _rule_result()


def _pipeline_inquiry(database: Database, source_id: str, content: str) -> int:
    return InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "CUSTOMER_INQUIRY",
            "source_question_id": source_id,
            "inquiry_type": "CUSTOMER_INQUIRY",
            "content": content,
            "product_name": "삼성 4K UHD 스마트 사이니지 TV 138.7cm(55인치)",
            "order_id": None,
            "product_order_id": None,
            "raw_json": {},
        }
    ).inquiry_id


def test_pipeline_falls_back_to_question_aware_safe_draft_when_ungrounded(
    tmp_path,
) -> None:
    database = Database(tmp_path / "quality-fallback.db")
    database.initialize()
    question = "혹시 65인치는 취급 안하시는지요?"
    inquiry_id = _pipeline_inquiry(database, "QUALITY-FALLBACK", question)

    speculative = _draft("아마 취급하지 않는 것 같습니다.")
    provider = ScriptedProvider(
        understanding=_understanding((question,), category="PRODUCT_GENERAL"),
        draft_sequence=[speculative, dict(speculative)],
        review_sequence=[
            _review(passed=False, has_speculation=True),
            _review(passed=False, has_speculation=True),
        ],
    )
    hybrid = HybridAnswerService(provider, learning_context_provider=lambda *_: {})
    outcome = AnswerService(
        database,
        engine=NotSupportedEngine(),
        dps_enrichment=FakeDps(),
        hybrid_service=hybrid,
    ).generate_for_inquiry(inquiry_id)

    assert outcome.result.status is AnswerStatus.NEEDS_REVIEW
    assert outcome.result.metadata["selected_answer_route"] == (
        "REVIEW_REQUIRED_SAFE_DRAFT"
    )
    # The safety draft must reflect the real question, not the old static
    # "사용 방법 또는 기능"/"주문 또는 상품 관련 내용" category label.
    assert question in outcome.result.answer
    assert "사용 방법 또는 기능" not in outcome.result.answer
    assert outcome.result.answer.count("안녕하세요") == 1


def test_pipeline_question_aware_fallback_generalizes_to_a_policy_payment_question(
    tmp_path,
) -> None:
    # Same mechanism as the sibling-variant-availability case above, proven
    # against an unrelated topic (a promotion/payment criterion question) to
    # confirm nothing here is specific to one product or one keyword set.
    database = Database(tmp_path / "quality-fallback-policy.db")
    database.initialize()
    question = "앱에 표시된 금액과 옵션 제외 금액 중 어떤 금액을 입력해야 하나요?"
    inquiry_id = _pipeline_inquiry(database, "QUALITY-FALLBACK-POLICY", question)

    speculative = _draft("아마 옵션 제외 금액을 입력하시면 될 것 같습니다.")
    provider = ScriptedProvider(
        understanding=_understanding((question,), category="PROMOTION_EVENT"),
        draft_sequence=[speculative, dict(speculative)],
        review_sequence=[
            _review(passed=False, has_speculation=True),
            _review(passed=False, has_speculation=True),
        ],
    )
    hybrid = HybridAnswerService(provider, learning_context_provider=lambda *_: {})
    outcome = AnswerService(
        database,
        engine=NotSupportedEngine(),
        dps_enrichment=FakeDps(),
        hybrid_service=hybrid,
    ).generate_for_inquiry(inquiry_id)

    assert question in outcome.result.answer
    assert "주문 또는 상품 관련 내용" not in outcome.result.answer
    assert outcome.result.answer.count("안녕하세요") == 1


def test_pipeline_recovers_grounded_partial_answer_via_corrective_regeneration(
    tmp_path,
) -> None:
    database = Database(tmp_path / "quality-recovery.db")
    database.initialize()
    question = "혹시 65인치는 취급 안하시는지요?"
    inquiry_id = _pipeline_inquiry(database, "QUALITY-RECOVER", question)

    provider = ScriptedProvider(
        understanding=_understanding((question,), category="PRODUCT_GENERAL"),
        draft_sequence=[
            _draft("65인치는 취급하지 않는 것 같습니다."),
            _draft(
                "취급 사이즈 여부는 정확한 확인이 필요하여 담당 직원이 "
                "다시 안내드리겠습니다."
            ),
        ],
        review_sequence=[
            _review(passed=False, has_speculation=True),
            _review(passed=True),
        ],
    )
    hybrid = HybridAnswerService(provider, learning_context_provider=lambda *_: {})
    outcome = AnswerService(
        database,
        engine=NotSupportedEngine(),
        dps_enrichment=FakeDps(),
        hybrid_service=hybrid,
    ).generate_for_inquiry(inquiry_id)

    assert provider.calls.count("DRAFT") == 2
    assert outcome.result.metadata.get("selected_answer_route") != (
        "REVIEW_REQUIRED_SAFE_DRAFT"
    )
    assert "취급하지 않는 것 같습니다" not in outcome.result.answer
    assert "확인이 필요" in outcome.result.answer
