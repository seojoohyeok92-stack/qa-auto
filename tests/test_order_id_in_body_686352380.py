"""Regression for 686352380: an order number stated in the message body.

Observed in production: the customer wrote

    23일 주문했는데요.. 주문번호 2026082351391541 입니다..
    혹시 배송일정을 알수 있을까요??

and the pipeline answered asking for the order number they had just given.

Cause: both ``InquiryAnalysisService`` and ``InquiryProcessingPlanService``
read the order id from the channel-supplied column only. A product-Q&A inquiry
is not opened from an order, so that column is empty; the number in the body
was detected but stayed CANDIDATE_FOUND, which routes to REQUEST_ORDER_ID.

Fix: ``answer_request_from_inquiry`` fills the empty column from a number the
customer explicitly *labelled* as their order number. Every channel-supplied
source still wins, an unlabelled 16-digit run is still only a candidate, and
two different labelled numbers stay ambiguous.

No Naver, no GPT, no DPS: the DPS result is a fixture and the provider is fake.
"""
from __future__ import annotations

import pytest

from answer.facts import build_answer_facts
from answer.models import AnswerRequest, AnswerResult, AnswerStatus
from answer.providers.fake_gpt_provider import FakeGptProvider
from answer.source_adapter import (
    answer_request_from_inquiry,
    order_id_from_text,
)
from services.hybrid_answer_service import HybridAnswerService
from services.inquiry_analysis_service import InquiryAnalysisService

ORDER_ID = "2026082351391541"
BODY = (
    "23일 주문했는데요..\n"
    f"주문번호 {ORDER_ID} 입니다..\n"
    "혹시 배송일정을 알수 있을까요??"
)
PRODUCT = "삼성 125.7cm(50인치) UHD 4K 1등급 비즈니스TV LH50BEFHLGFXKR 스탠드형"

# Wording that would mean the pipeline asked for something already given.
ORDER_REQUEST_PHRASES = (
    "주문번호가 필요", "주문번호를 알려", "일반 주문번호가 필요",
    "주문번호가 없어", "주문번호를 남겨", "주문번호 알려주",
)


def _request(text: str = BODY, **row_overrides) -> AnswerRequest:
    row = {
        "title": "상품 문의", "content": text, "product_name": PRODUCT,
        "inquiry_type": "PRODUCT_INQUIRY", "source_type": "PRODUCT_INQUIRY",
    }
    row.update(row_overrides)
    return answer_request_from_inquiry(row)


def _analyze(text: str = BODY, **row_overrides):
    return InquiryAnalysisService().analyze(_request(text, **row_overrides))


# ------------------------------------------------------ A / B. the real case
@pytest.mark.parametrize(
    "text",
    [
        BODY,
        f"주문번호 {ORDER_ID} 입니다. 배송일정을 알 수 있을까요?",
        f"주문 번호 {ORDER_ID} / 언제 배송되나요?",
        f"order no {ORDER_ID} 배송 예정일 알려주세요",
    ],
)
def test_AB_labelled_order_number_in_the_body_is_used(text):
    request = _request(text)
    analysis = _analyze(text)

    assert request.order_id == ORDER_ID
    assert analysis.order_id_present is True
    assert analysis.order_id_validated is True
    assert analysis.order_id_status.value == "VALIDATED"
    assert analysis.requires_order_lookup is True
    assert analysis.requires_dps_lookup is True
    assert analysis.delivery_question is True
    assert analysis.answer_strategy.value != "REQUEST_ORDER_ID"


def test_B_production_case_classifies_as_a_schedule_inquiry():
    analysis = _analyze()
    assert analysis.inquiry_type.value == "DELIVERY_INSTALLATION_STATUS"
    assert analysis.detected_intent == "DELIVERY_DATE"
    assert analysis.delivery_related is True


# ------------------------------------------------- C. no order number at all
def test_C_missing_order_number_keeps_the_existing_request_policy():
    analysis = _analyze("배송일정을 알 수 있을까요?")
    assert analysis.order_id_status.value == "MISSING"
    assert analysis.order_id_validated is False
    assert analysis.answer_strategy.value == "REQUEST_ORDER_ID"


def test_C_unlabelled_digits_stay_a_candidate_not_a_validated_order():
    """A bare 16-digit run could be a card or a tracking number."""

    analysis = _analyze(f"{ORDER_ID} 이거 맞나요? 배송일정 알려주세요")
    assert analysis.order_id_validated is False
    assert analysis.answer_strategy.value == "REQUEST_ORDER_ID"


def test_C_two_labelled_numbers_stay_ambiguous():
    analysis = _analyze(
        f"주문번호 {ORDER_ID} 하고 주문번호 2026082351391542 배송일정?"
    )
    assert analysis.order_id_status.value == "AMBIGUOUS"
    assert analysis.order_id_validated is False


@pytest.mark.parametrize(
    "text",
    [f"주문번호 {ORDER_ID} 입니다", "카드번호 1234567812345678 입니다",
     f"송장번호 {ORDER_ID}", "그냥 문의드립니다"],
)
def test_extractor_only_accepts_a_labelled_order_number(text):
    extracted = order_id_from_text(text)
    assert extracted == (ORDER_ID if "주문번호" in text else "")


def test_channel_supplied_column_always_wins():
    request = _request(order_id="9999999999999999")
    assert request.order_id == "9999999999999999"


# --------------------------------------------- D/E. no unnecessary DPS forcing
def test_D_order_number_does_not_force_dps_on_a_spec_question():
    analysis = _analyze(f"주문번호 {ORDER_ID} 입니다. HDMI 단자가 몇 개인가요?")
    assert analysis.order_id_validated is True
    assert analysis.requires_dps_lookup is False
    assert analysis.requires_order_lookup is False


def test_E_compound_keeps_schedule_and_spec_sources_independent():
    analysis = _analyze(
        f"주문번호 {ORDER_ID} 입니다. 배송일정하고 HDMI 단자 개수도 알려주세요."
    )
    assert analysis.order_id_validated is True
    assert analysis.requires_dps_lookup is True

    from services.product_knowledge_service import ProductKnowledgeService

    knowledge = ProductKnowledgeService().facts_for_inquiry(
        product_id="12139453925",
        questions=["배송일정 알려주세요", "HDMI 단자 개수"],
    )
    # The schedule half contributes no product fact; the spec half does.
    block = knowledge.prompt_block()
    assert "배송" not in block and "일정" not in block


# ------------------------------------------------------------ F. VALIDATED kept
@pytest.mark.parametrize(
    "text",
    [
        BODY,
        f"주문번호 {ORDER_ID} 입니다. 설치 예정일이 언제인가요?",
        f"주문번호 {ORDER_ID} 입니다. 언제쯤 받을 수 있나요?",
        f"주문번호 {ORDER_ID} 입니다. 배송 예정일 알려주세요.",
    ],
)
def test_F_validated_order_id_survives_intent_analysis(text):
    """A validated id must never be downgraded by later classification."""

    request = _request(text)
    analysis = _analyze(text)
    assert request.order_id == ORDER_ID
    assert analysis.order_id_validated is True
    assert analysis.order_id_status.value == "VALIDATED"


# ------------------------------------------------- delivery taxonomy regression
@pytest.mark.parametrize(
    "question",
    [
        "배송일정을 알 수 있을까요?",
        "언제 배송되나요?",
        "설치 예정일이 언제인가요?",
        "배송 예정일 알려주세요",
        "언제쯤 받을 수 있나요?",
    ],
)
def test_delivery_taxonomy_with_and_without_an_order_number(question):
    with_id = _analyze(f"주문번호 {ORDER_ID} 입니다. {question}")
    without_id = _analyze(question)

    # With the number: a lookup the pipeline can actually perform.
    assert with_id.delivery_question is True, question
    assert with_id.requires_dps_lookup is True, question
    assert with_id.order_id_validated is True, question
    assert with_id.answer_strategy.value != "REQUEST_ORDER_ID", question

    # Without it: never validated, and never answered as if it were.
    # Wording that carries no evidence of an existing order ("언제쯤 받을 수
    # 있나요?") is a pre-purchase question and keeps its general guidance;
    # anything that does imply an order still asks for the number.
    assert without_id.order_id_validated is False, question
    assert without_id.answer_strategy.value in {
        "REQUEST_ORDER_ID", "GENERAL_GUIDANCE",
    }, question


# --------------------------------------------------------- G/H. DPS fixtures
def _dps(confirmed: bool) -> dict:
    if confirmed:
        return {
            "lookup_required": True, "lookup_status": "SUCCESS",
            "installation_date": "2026-09-02",
            "installation_date_source":
                "DPS_ITEM_DETAIL_REQUIRED_DELIVERY_DATE",
            "date_parse_status": "PARSED",
            "required_delivery_date": "2026-09-02",
            "requires_human_review": False, "warnings": [],
        }
    return {
        "lookup_required": True, "lookup_status": "AUTOMATION_ERROR",
        "installation_date": None, "date_parse_status": "NOT_PARSED",
        "requires_human_review": True, "warnings": ["DPS_LOOKUP_FAILED"],
    }


def _facts_for(confirmed: bool):
    request = _request()
    request.metadata["dps"] = _dps(confirmed)
    rule = AnswerResult(
        status=AnswerStatus.NEEDS_REVIEW, category="배송/설치기존",
        reason="테스트", answer="", provider="rules",
        auto_answerable=False, needs_review=True, metadata={},
    )
    return request, build_answer_facts(request, rule)


def test_G_trusted_dps_date_reaches_the_answer_facts():
    _, facts = _facts_for(True)
    assert facts.installation["installation_date_confirmed"] is True
    assert facts.installation["date"] == "2026-09-02"


def test_H_untrusted_dps_never_yields_a_confirmed_date():
    _, facts = _facts_for(False)
    assert facts.installation["installation_date_confirmed"] is False
    assert facts.installation["date"] is None


def test_G_trusted_dps_date_reaches_the_generated_answer():
    """The fixture date must appear in what the provider was asked to write."""

    request, _ = _facts_for(True)
    provider = FakeGptProvider(responses={
        "DRAFT": {
            "answer": "문의하신 주문의 배송·설치 예정일은 2026년 9월 2일입니다.",
            "used_facts": ["installation.date"], "missing_information": [],
            "requires_review": False, "confidence": 0.95,
        },
        "SELF_REVIEW": {
            "passed": True, "answered_all_questions": True,
            "facts_consistent": True, "has_speculation": False,
            "requires_review": False, "warnings": [], "reason": "ok",
        },
    })
    rule = AnswerResult(
        status=AnswerStatus.NEEDS_REVIEW, category="배송/설치기존",
        reason="테스트", answer="", provider="rules",
        auto_answerable=False, needs_review=True, metadata={},
    )
    outcome = HybridAnswerService(provider).generate(request, rule)

    drafts = [call for call in provider.calls if call["task"] == "DRAFT"]
    assert drafts, "provider was never asked for a draft"
    prompt = drafts[0]["prompt"]
    assert "2026-09-02" in prompt, "the confirmed DPS date never reached the prompt"
    assert "2026" in outcome.result.answer


# ------------------------------------- the answer must not ask for the number
@pytest.mark.parametrize("phrase", ORDER_REQUEST_PHRASES)
def test_generated_answer_does_not_ask_for_the_given_order_number(phrase):
    request, _ = _facts_for(True)
    provider = FakeGptProvider(responses={
        "DRAFT": {
            "answer": "문의하신 주문의 배송·설치 예정일은 2026년 9월 2일입니다.",
            "used_facts": [], "missing_information": [],
            "requires_review": False, "confidence": 0.95,
        },
        "SELF_REVIEW": {
            "passed": True, "answered_all_questions": True,
            "facts_consistent": True, "has_speculation": False,
            "requires_review": False, "warnings": [], "reason": "ok",
        },
    })
    rule = AnswerResult(
        status=AnswerStatus.NEEDS_REVIEW, category="배송/설치기존",
        reason="테스트", answer="", provider="rules",
        auto_answerable=False, needs_review=True, metadata={},
    )
    outcome = HybridAnswerService(provider).generate(request, rule)
    assert phrase not in outcome.result.answer


def test_route_is_not_the_order_id_request_route():
    from repositories.database import Database  # noqa: F401  (import guard)

    analysis = _analyze()
    assert analysis.answer_strategy.value == "DIRECT_FACT_ANSWER"
    assert analysis.order_id_status.value == "VALIDATED"
