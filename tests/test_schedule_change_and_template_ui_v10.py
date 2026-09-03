"""Schedule-change safety and Template/GPT policy honesty in the UI.

Two defects this pins.

1. A request to *change* an install/delivery/visit date was only recognised
   from a handful of exact phrases. "설치 날짜 미뤄주세요." classified as
   DELIVERY_OR_INSTALLATION_SCHEDULE -- a *lookup* -- so with a valid order
   and a successful DPS call the system would have replied with the current
   date and auto-posted it, silently ignoring the request. Separately, the
   order-id shortcut cleared the staff-review flag, so even a correctly
   classified change request stopped requiring a person when no order number
   was present.

2. The review screen said "기존 템플릿 우선 사용" / "템플릿 우선 답변 생성",
   which described the old template-first policy rather than the current one.

Routing/eligibility decisions only -- no provider calls.
"""
from __future__ import annotations

import pytest

from answer.engine import AnswerEngine
from answer.models import AnswerRequest, AnswerStatus
from answer.answer_validator import AnswerValidator
from services.answer_service import (
    EXACT_TEMPLATE_MATCH_KINDS,
    _template_may_answer,
    _template_unavailable_reason,
)
from services.auto_processing_eligibility_service import (
    SOFT_REASONS,
    AutoProcessingEligibilityService,
)
from services.inquiry_analysis_service import InquiryAnalysisService


ANALYSIS = InquiryAnalysisService()
ELIGIBILITY = AutoProcessingEligibilityService()
ENGINE = AnswerEngine()


def analyze(question: str):
    return ANALYSIS.analyze(
        AnswerRequest(question=question, product_name="삼성 TV")
    )


def evaluate(*, route: str, plan: dict, answer: str = "안내드립니다."):
    return ELIGIBILITY.evaluate(
        inquiry={"source_answered": 0, "post_status": "NOT_POSTED"},
        draft={
            "original_answer": answer,
            "validation_status": "PASSED",
            "validator_result_json": None,
            "review_status": "",
            "metadata_json": {"processing_plan": plan},
            "posted": False,
            "id": 1,
        },
        route=route,
    )


# --------------------------------------------------- schedule change intent

SCHEDULE_CHANGE_QUESTIONS = [
    # CASE D
    "25일 설치 예정인데 10일로 변경해주세요.",
    # CASE E
    "설치일을 내일로 당겨주세요.",
    "이번 주말로 설치일 변경 가능한가요?",
    "설치 날짜 미뤄주세요.",
    "설치일 연기 가능한가요?",
    # CASE G
    "배송일을 다른 날짜로 바꿔주세요.",
    # CASE H
    "오후 방문으로 변경해주세요.",
    "기사님 방문시간을 변경하고 싶습니다.",
    "방문시간 조정 부탁드려요.",
]


@pytest.mark.parametrize("question", SCHEDULE_CHANGE_QUESTIONS)
def test_case_d_e_g_h_schedule_change_is_recognised(question: str) -> None:
    assert analyze(question).inquiry_subtype == "SCHEDULE_CHANGE_REQUEST"


# Q&A Auto cannot reschedule an order, so staff review is required whether or
# not an order number is present. The order-id shortcut must not clear it.
@pytest.mark.parametrize("question", SCHEDULE_CHANGE_QUESTIONS)
def test_schedule_change_always_requires_staff_review(question: str) -> None:
    analysis = analyze(question)
    assert analysis.manual_review_required is True
    assert analysis.auto_answerable is False
    assert analysis.answer_strategy.value == "MANUAL_REVIEW"


# CASE D/E/G/H -- and the resulting plan must be a hard auto-post block.
def test_schedule_change_is_hard_blocked_from_auto_post() -> None:
    result = evaluate(
        route="GPT_DIRECT", plan={"analysis": {"manual_review_required": True}}
    )
    assert result.safe is False
    assert "POLICY_OR_HIGH_RISK_REVIEW" in result.reasons
    assert "POLICY_OR_HIGH_RISK_REVIEW" not in SOFT_REASONS


# CASE F -- a plain schedule *lookup* must not be swept up by the change
# detection. Whether it is held is a separate decision, made on the evidence
# about the order; being mistaken for a change request never is.
SCHEDULE_LOOKUPS = [
    "설치 예정일이 언제인가요?",
    "배송 언제 와요?",
    "설치일 알려주세요",
    "배송일 확인 부탁드립니다",
    "언제 설치되나요?",
]


@pytest.mark.parametrize("question", SCHEDULE_LOOKUPS)
def test_case_f_schedule_lookup_is_not_a_change_request(question: str) -> None:
    analysis = analyze(question)
    assert analysis.inquiry_subtype != "SCHEDULE_CHANGE_REQUEST"


@pytest.mark.parametrize("question", SCHEDULE_LOOKUPS)
def test_case_f_lookup_with_an_order_keeps_the_existing_route(
    question: str,
) -> None:
    """The order/DPS route this case used to assert, on a stated order.

    Without one the purchase-state policy holds the inquiry, so
    ``manual_review_required is False`` can no longer stand for "not a change
    request". Saying the order exists restores the route the case is about.
    """

    analysis = analyze(f"어제 주문했는데 {question}")
    assert analysis.inquiry_subtype != "SCHEDULE_CHANGE_REQUEST"
    assert analysis.manual_review_required is False
    assert analysis.requires_order_lookup is True


# Unrelated "변경" wording must not be captured either.
@pytest.mark.parametrize(
    "question",
    ["색상 변경 가능한가요?", "주소 변경하고 싶어요", "설치 방법 알려주세요"],
)
def test_unrelated_change_wording_is_not_schedule_change(
    question: str,
) -> None:
    assert analyze(question).inquiry_subtype != "SCHEDULE_CHANGE_REQUEST"


# ------------------------------------------------------- template authority

def template_gate(question: str) -> tuple[str, AnswerStatus, str | None]:
    request = AnswerRequest(
        question=question,
        product_name="삼성 TV",
        store_code="OJE_PLUS",
        inquiry_type="PRODUCT_INQUIRY",
    )
    public = ENGINE.generate(request)
    return (
        str(public.metadata.get("template_match_kind")),
        public.status,
        _template_unavailable_reason(public, request, AnswerValidator()),
    )


# CASE D -- a schedule change hits the engine's hard block, which produces a
# non-GENERATED result and therefore can never be a final answer.
def test_case_d_schedule_change_template_is_never_a_final_answer() -> None:
    kind, status, reason = template_gate("설치일 변경해주세요")
    assert kind == "FIXED_POLICY_HARD_BLOCK"
    assert status is not AnswerStatus.GENERATED
    assert reason == "NOT_FOUND"


# Section 6 -- FIXED_POLICY_HARD_BLOCK carries two different outcomes. The
# blocking ones are withheld by the status gate; only a genuine fixed answer
# (구매내역서) is allowed through. Both halves are pinned so the kind cannot
# quietly become a posting shortcut.
def test_hard_block_kind_only_answers_when_it_is_a_real_fixed_answer() -> None:
    _, blocking_status, blocking_reason = template_gate(
        "챗봇말고 사람이 답변해주세요"
    )
    assert blocking_status is not AnswerStatus.GENERATED
    assert blocking_reason == "NOT_FOUND"

    kind, answer_status, answer_reason = template_gate("구매내역서 발급해주세요")
    assert kind == "FIXED_POLICY_HARD_BLOCK"
    assert answer_status is AnswerStatus.GENERATED
    assert answer_reason is None


# CASE B/K -- a keyword-matched rule never wins the final answer.
def test_case_b_k_keyword_rule_is_not_a_final_answer() -> None:
    assert _template_may_answer(
        {"template_match_kind": "KEYWORD_LEARNED_RULE"}
    ) is False
    assert _template_may_answer(
        {"template_match_kind": "KEYWORD_SIMPLE_PRODUCT_USAGE"}
    ) is False


# CASE C/I/J -- the exact fixed kinds keep their authority.
@pytest.mark.parametrize("kind", sorted(EXACT_TEMPLATE_MATCH_KINDS))
def test_case_c_i_j_exact_kinds_keep_authority(kind: str) -> None:
    assert _template_may_answer({"template_match_kind": kind}) is True


# CASE L -- a hard review reason still blocks even with the template
# preference switched on (template_preferred is a generation input, not an
# auto-post permission).
def test_case_l_template_preference_does_not_bypass_hard_review() -> None:
    result = evaluate(
        route="TEMPLATE",
        plan={"is_high_risk": True, "template_preferred": True},
    )
    assert result.safe is False
    assert "POLICY_OR_HIGH_RISK_REVIEW" in result.reasons


# ------------------------------------------------------------------ the UI

# CASE A/M -- the review screen must describe the current policy, and the
# label change must be text only.
def test_case_a_m_ui_labels_describe_the_current_policy() -> None:
    import inspect

    import ui.review_workspace as module

    source = inspect.getsource(module)
    assert '"확정 운영 템플릿 사용"' in source
    assert "기존 템플릿 우선 사용" not in source
    assert "템플릿 우선 답변 생성" not in source
    assert "정확히 일치하는 고정 운영 정책이 있을 때만" in source
    # The checkbox still feeds the same backend argument it always did.
    assert "prefer_template=use_template" in source


# CASE N -- manual and automatic generation still share one service.
def test_case_n_manual_and_automatic_share_one_service() -> None:
    import inspect

    import services.automatic_draft_service as auto_module
    import ui.review_workspace as manual_module

    assert "generate_for_inquiry" in inspect.getsource(auto_module)
    assert "generate_for_inquiry" in inspect.getsource(manual_module)
