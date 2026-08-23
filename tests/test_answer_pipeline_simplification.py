"""Answer pipeline after the single-call simplification.

Production reproduction, inquiry 686097134 (inquiry_id 2608): a six-question
product inquiry took ~74s and produced no answer at all -- provider=safe_rule,
route=REVIEW_REQUIRED_SAFE_DRAFT -- after

    GPT 자체 검토를 통과하지 못했습니다.
    GPT 자체 검토에서 사실 불일치를 확인했습니다.

and the stored analysis recorded "복합문의로 7개 질문을 각각 판단했습니다." for a
six-question inquiry.

Two defects:

  * Naver titles a product inquiry with the first line of its body, and the
    source adapter prepended the title unconditionally, so the customer's
    first question was counted, prompted and coverage-checked twice.
  * SELF_REVIEW was a provider round trip that graded the draft, and its
    verdict could veto an answer the deterministic validator would pass.
    Every field it reported -- speculation, fact consistency, coverage -- is
    already checked by AnswerValidator against the resolved facts.

The pipeline now derives the intent deterministically and grades with the
validator alone, so a normal generation costs one provider call (DRAFT).

Fakes only: no network, no real provider, no POST, no DPS automation.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from answer.governance_models import GptProviderSettings
from answer.models import AnswerRequest, AnswerResult, AnswerStatus
from answer.provider_errors import (
    GptProviderRetryableError,
    GptProviderTimeoutError,
)
from answer.providers.fake_gpt_provider import FakeGptProvider
from answer.providers.resilient_json_provider import ResilientJsonProvider
from answer.source_adapter import answer_request_from_inquiry
from answer.text_utils import restore_question_mark, split_subquestions
from services.hybrid_answer_service import HybridAnswerService
from services.inquiry_analysis_service import InquiryAnalysisService
from services.phase9_answer_policy import ORDER_ID_REQUEST_ANSWER


ANALYSIS = InquiryAnalysisService()

SIX_PART = (
    "A/S는 삼성서비스센터에서 하나요?\n\n"
    "설치는 기사님이 해주시나요?\n\n"
    "집에 있는 브라켓과 호환되나요?\n\n"
    "설치예정일은 언제인가요?\n\n"
    "카드 할인도 되나요?\n\n"
    "배송 중 파손되면 어떻게 하나요?"
)
SIX_PART_PARTIAL = (
    "A/S는 삼성전자 서비스센터를 통해 받으실 수 있습니다.\n"
    "설치는 전문 기사가 방문하여 진행합니다.\n"
    "기존 브라켓 호환 여부는 확인 후 안내드리겠습니다.\n"
    "설치예정일은 주문번호를 알려주시면 확인해 드리겠습니다.\n"
    "카드 혜택은 확인 후 안내드리겠습니다.\n"
    "배송 중 파손은 담당 직원이 확인 후 안내드리겠습니다."
)


def inquiry_row(content: str, *, title: str | None = None, **overrides) -> dict:
    row = {
        "id": 2608,
        "source_question_id": "686097134",
        "store_code": "OJE_PLUS",
        "inquiry_type": "PRODUCT_INQUIRY",
        # Naver's default: the title repeats the first line of the body.
        "title": content.splitlines()[0] if title is None else title,
        "content": content,
        "product_name": "삼성 50인치 TV",
        "raw_json": {},
    }
    row.update(overrides)
    return row


def request_for(content: str, *, dps: dict | None = None, **overrides):
    request = answer_request_from_inquiry(inquiry_row(content, **overrides))
    request.metadata["dps"] = dps or {
        "lookup_required": False,
        "lookup_status": "NOT_REQUIRED",
        "warnings": [],
    }
    request.metadata["phase9_analysis"] = ANALYSIS.analyze(request).to_dict()
    return request


def rule(*, needs_review: bool = True, answer: str = "안내드립니다.") -> AnswerResult:
    return AnswerResult(
        status=(
            AnswerStatus.NEEDS_REVIEW if needs_review else AnswerStatus.GENERATED
        ),
        category="설치/AS",
        reason="Rule",
        answer=answer,
        provider="rules",
        auto_answerable=not needs_review,
        needs_review=needs_review,
        matched_rule="설치/AS",
    )


def provider_for(answer: str, **draft_overrides) -> FakeGptProvider:
    draft = {
        "answer": answer,
        "confidence": 0.9,
        "used_facts": [],
        "missing_information": [],
        "requires_review": False,
        "warnings": [],
    }
    draft.update(draft_overrides)
    return FakeGptProvider(responses={"DRAFT": draft})


def run(request, provider: FakeGptProvider, rule_result: AnswerResult):
    """Wrap the fake the way production does, so telemetry is exercised."""

    wrapped = ResilientJsonProvider(provider, GptProviderSettings())
    outcome = HybridAnswerService(wrapped).generate(request, rule_result)
    hybrid = outcome.result.metadata.get("hybrid") or {}
    return outcome, hybrid, hybrid.get("provider_telemetry") or {}


# ------------------------------------------------- 686097134 regression

def test_six_questions_are_not_counted_as_seven() -> None:
    """The title repeating the first line must not add a sub-question."""

    request = request_for(SIX_PART)
    analysis = ANALYSIS.analyze(request)
    questions = split_subquestions(request.question)

    assert len(questions) == 6
    assert len(set(questions)) == 6, "a duplicated question was counted twice"
    assert all(question.strip() for question in questions), "empty sub-question"
    assert "6개 질문" in analysis.reasons[0]
    assert analysis.inquiry_subtype == "COMPOUND_MULTI_INTENT"


@pytest.mark.parametrize(
    "content",
    [
        SIX_PART,
        # CASE L -- several blank lines between the questions.
        SIX_PART.replace("\n\n", "\n\n\n\n"),
        SIX_PART.replace("\n\n", "\r\n\r\n"),
        SIX_PART + "\n\n\n",
    ],
)
def test_blank_lines_never_create_ghost_subquestions(content: str) -> None:
    questions = split_subquestions(request_for(content).question)
    assert len(questions) == 6
    assert len(set(questions)) == 6


def test_six_part_inquiry_keeps_its_partial_answer_in_one_call() -> None:
    request = request_for(SIX_PART)
    outcome, hybrid, telemetry = run(
        request, provider_for(SIX_PART_PARTIAL), rule()
    )
    validation = hybrid.get("validation") or {}
    answer = outcome.result.answer

    # No RULE_FALLBACK, no safe draft.
    assert hybrid["fallback_used"] is False, validation.get("errors")
    assert validation["errors"] == []
    assert outcome.result.provider.endswith("_hybrid")
    assert "정확한 정보 확인이 필요합니다" not in answer

    # The grounded sub-questions are answered...
    assert "서비스센터" in answer
    assert "전문 기사" in answer
    # ...and the unsupported ones are deferred rather than invented.
    assert "브라켓" in answer and "카드" in answer and "파손" in answer
    assert "주문번호" in answer
    import re

    assert not re.search(r"20\d{2}[-.년]", answer), "invented a date"

    # Drafted, but still held for staff because of the HARD parts.
    assert outcome.result.needs_review is True
    assert outcome.result.auto_answerable is False

    # One provider round trip for a normal generation.
    assert telemetry["provider_call_count"] == 1
    assert telemetry["tasks"] == ["DRAFT"]


def test_normal_generation_makes_no_understanding_or_self_review_call() -> None:
    provider = provider_for(SIX_PART_PARTIAL)
    run(request_for(SIX_PART), provider, rule())
    tasks = [call["task"] for call in provider.calls]
    assert tasks == ["DRAFT"], tasks


# ----------------------------------------------------------- CASE A--F

def test_case_a_single_general_product_inquiry_is_answered() -> None:
    request = request_for("이 제품 무게가 얼마나 되나요?")
    outcome, hybrid, telemetry = run(
        request, provider_for("약 15kg입니다."), rule(needs_review=False)
    )
    assert hybrid["fallback_used"] is False
    assert "15kg" in outcome.result.answer
    assert telemetry["provider_call_count"] == 1


def test_case_b_schedule_question_without_order_id_requests_it() -> None:
    """No order id: the safe order-number request template is used, and the
    provider is not asked to invent a schedule."""

    request = request_for("설치예정일은 언제인가요?")
    analysis = ANALYSIS.analyze(request)
    assert analysis.requires_order_id is True
    assert analysis.requires_dps_lookup is True

    provider = provider_for("무시되어야 하는 GPT 초안")
    outcome, hybrid, telemetry = run(
        request, provider, rule(answer=ORDER_ID_REQUEST_ANSWER)
    )
    # The customer gets the canonical order-number request...
    assert "일반 주문번호" in outcome.result.answer
    assert "상품주문번호가 아닌" in outcome.result.answer
    # ...the provider's draft is not used for this route...
    assert "무시되어야 하는" not in outcome.result.answer
    # ...no schedule is invented without an order and a DPS result...
    import re

    assert not re.search(r"20\d{2}[-.년]", outcome.result.answer)
    # ...and the route costs no provider round trip at all.
    assert telemetry["provider_call_count"] == 0
    assert provider.calls == []


def test_case_c_confirmed_dps_date_may_be_answered() -> None:
    dps = {
        "lookup_required": True,
        "lookup_status": "SUCCESS",
        "installation_date": "2026-09-01",
        "installation_date_source": "DPS_ITEM_DETAIL_REQUIRED_DELIVERY_DATE",
        "date_parse_status": "PARSED",
        "installation_status": "CONFIRMED",
        "warnings": [],
    }
    request = request_for(
        "설치예정일은 언제인가요?", dps=dps, order_id="2024010112345678"
    )
    outcome, hybrid, _ = run(
        request, provider_for("설치예정일은 2026-09-01입니다."), rule()
    )
    assert hybrid["fallback_used"] is False, (
        (hybrid.get("validation") or {}).get("errors")
    )
    assert "2026-09-01" in outcome.result.answer


def test_case_d_compound_all_answerable_is_answered_in_one_call() -> None:
    content = "A/S는 어디서 받나요?\n설치는 기사님이 해주시나요?\n보증기간은 얼마인가요?"
    answer = (
        "A/S는 삼성전자 서비스센터를 통해 받으실 수 있습니다.\n"
        "설치는 전문 기사가 방문하여 진행합니다.\n"
        "보증기간은 구입일로부터 2년입니다."
    )
    request = request_for(content)
    outcome, hybrid, telemetry = run(
        request, provider_for(answer), rule(needs_review=False)
    )
    assert hybrid["fallback_used"] is False
    assert len(split_subquestions(request.question)) == 3
    for needle in ("서비스센터", "전문 기사", "보증기간"):
        assert needle in outcome.result.answer
    assert telemetry["provider_call_count"] == 1


def test_case_e_order_id_subquestion_does_not_erase_the_others() -> None:
    content = "A/S는 어디서 받나요?\n설치는 기사님이 해주시나요?\n설치예정일은 언제인가요?"
    answer = (
        "A/S는 삼성전자 서비스센터를 통해 받으실 수 있습니다.\n"
        "설치는 전문 기사가 방문하여 진행합니다.\n"
        "설치예정일은 주문번호를 알려주시면 확인해 드리겠습니다."
    )
    request = request_for(content)
    outcome, hybrid, _ = run(request, provider_for(answer), rule())
    assert hybrid["fallback_used"] is False
    assert "서비스센터" in outcome.result.answer
    assert "전문 기사" in outcome.result.answer
    assert "주문번호" in outcome.result.answer


def test_case_f_manual_review_subquestion_does_not_erase_the_others() -> None:
    content = "A/S는 어디서 받나요?\n설치는 기사님이 해주시나요?\n카드 할인도 되나요?"
    answer = (
        "A/S는 삼성전자 서비스센터를 통해 받으실 수 있습니다.\n"
        "설치는 전문 기사가 방문하여 진행합니다.\n"
        "카드 혜택은 확인 후 안내드리겠습니다."
    )
    request = request_for(content)
    analysis = ANALYSIS.analyze(request)
    assert analysis.manual_review_required is True

    outcome, hybrid, _ = run(request, provider_for(answer), rule())
    assert hybrid["fallback_used"] is False
    assert "서비스센터" in outcome.result.answer
    assert "전문 기사" in outcome.result.answer
    # Held for staff, but the answerable parts survived.
    assert outcome.result.needs_review is True


# ----------------------------------------------------------- CASE G--I

def test_case_g_nonexistent_fact_is_blocked() -> None:
    request = request_for("이 제품 무게가 얼마나 되나요?")
    outcome, hybrid, _ = run(
        request,
        provider_for("약 15kg입니다.", used_facts=["order.not_a_real_key"]),
        rule(),
    )
    errors = (hybrid.get("validation") or {}).get("errors") or []
    assert any("존재하지 않는 Fact" in error for error in errors), errors


def test_case_h_hallucinated_installation_date_is_blocked() -> None:
    dps = {
        "lookup_required": True,
        "lookup_status": "SUCCESS",
        "installation_date": "2026-09-01",
        "installation_date_source": "DPS_ITEM_DETAIL_REQUIRED_DELIVERY_DATE",
        "date_parse_status": "PARSED",
        "installation_status": "CONFIRMED",
        "warnings": [],
    }
    request = request_for(
        "설치예정일은 언제인가요?", dps=dps, order_id="2024010112345678"
    )
    outcome, hybrid, _ = run(
        request, provider_for("설치예정일은 2026-12-25입니다."), rule()
    )
    assert hybrid["fallback_used"] is True
    assert "2026-12-25" not in outcome.result.answer


def test_case_i_personal_data_is_blocked() -> None:
    request = request_for("이 제품 무게가 얼마나 되나요?")
    outcome, hybrid, _ = run(
        request, provider_for("담당자 010-1234-5678로 연락주세요."), rule()
    )
    errors = (hybrid.get("validation") or {}).get("errors") or []
    assert any("개인정보" in error for error in errors), errors
    assert "010-1234-5678" not in outcome.result.answer


def test_speculation_is_still_blocked() -> None:
    request = request_for("집에 있는 브라켓과 호환되나요?")
    outcome, hybrid, _ = run(
        request, provider_for("기존 브라켓과 호환될 것 같습니다."), rule()
    )
    assert hybrid["fallback_used"] is True
    assert "호환될 것 같습니다" not in outcome.result.answer


# ----------------------------------------------------------- CASE J--K

def test_case_j_provider_timeout_falls_back_without_pretending_success() -> None:
    request = request_for(SIX_PART)
    provider = FakeGptProvider(fail_tasks={"DRAFT"})
    outcome, hybrid, telemetry = run(request, provider, rule())

    assert hybrid["fallback_used"] is True
    assert hybrid["fallback_reason"]
    assert outcome.result.answer.strip(), "a Program Answer must still exist"
    assert outcome.result.needs_review is True
    # The failed attempt is still on the record.
    assert telemetry["provider_call_count"] >= 1


def test_case_k_transient_errors_keep_their_retry_policy() -> None:
    class Flaky:
        name = "openai"

        def __init__(self) -> None:
            self.attempts = 0

        def generate_json(self, *, task, prompt, context):
            self.attempts += 1
            if self.attempts == 1:
                raise GptProviderRetryableError("503", status_code=503)
            return {
                "answer": "약 15kg입니다.", "confidence": 0.9,
                "used_facts": [], "missing_information": [],
                "requires_review": False, "warnings": [],
            }

    inner = Flaky()
    wrapped = ResilientJsonProvider(
        inner, GptProviderSettings(), sleeper=lambda _: None
    )
    outcome = HybridAnswerService(wrapped).generate(
        request_for("이 제품 무게가 얼마나 되나요?"), rule(needs_review=False)
    )
    assert inner.attempts == 2
    assert "15kg" in outcome.result.answer

    # A response timeout stays terminal.
    class AlwaysTimeout:
        name = "openai"

        def __init__(self) -> None:
            self.attempts = 0

        def generate_json(self, *, task, prompt, context):
            self.attempts += 1
            raise GptProviderTimeoutError("read timeout")

    slow = AlwaysTimeout()
    HybridAnswerService(
        ResilientJsonProvider(
            slow, GptProviderSettings(), sleeper=lambda _: None
        )
    ).generate(request_for("이 제품 무게가 얼마나 되나요?"), rule())
    assert slow.attempts == 1


# ------------------------------------------------------------ telemetry

def test_failed_generation_still_records_its_provenance() -> None:
    """A safe draft must not erase what the provider actually did."""

    request = request_for(SIX_PART)
    outcome, hybrid, telemetry = run(
        request, provider_for("기존 브라켓과 호환될 것 같습니다."), rule()
    )
    assert hybrid["fallback_used"] is True
    assert hybrid["draft"], "the rejected draft must be preserved"
    assert hybrid["validation"]["errors"], "the reason must be preserved"
    assert telemetry["provider_call_count"] >= 1
    assert telemetry["tasks"]
    for call in telemetry["calls"]:
        assert call["prompt_chars"] > 0
        assert "prompt" not in call and "context" not in call


def test_telemetry_carries_no_prompt_text() -> None:
    request = request_for(SIX_PART)
    _, _, telemetry = run(request, provider_for(SIX_PART_PARTIAL), rule())
    rendered = repr(telemetry)
    assert "A/S는 삼성서비스센터에서" not in rendered
    assert "서비스센터를 통해" not in rendered


# --------------------------------------------------- customer-facing text

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("A/S는 삼성서비스센터에서 하나요", "A/S는 삼성서비스센터에서 하나요?"),
        ("혹시 65인치는 취급 안하시는지요", "혹시 65인치는 취급 안하시는지요?"),
        # Requests and statements are left alone.
        ("설치방법 알려주세요", "설치방법 알려주세요"),
        ("제품이 불량입니다", "제품이 불량입니다"),
        ("이미 물음표 있음?", "이미 물음표 있음?"),
    ],
)
def test_echoed_question_keeps_its_question_mark(text: str, expected: str) -> None:
    assert restore_question_mark(text) == expected
