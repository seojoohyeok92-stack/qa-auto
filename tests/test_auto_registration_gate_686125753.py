"""Auto-registration gate for inquiry 686125753.

Production reproduction. The customer asked, with no spaces:

    삼성센터AS무상기간알려주세요
    배송기한얼마나생각하면될까요?

The generated answer was correct -- it answered the A/S warranty from
Learning evidence and, having no delivery evidence for a pre-order product
inquiry, explicitly declined to state a delivery period rather than inventing
one. The dashboard nevertheless showed 문의 유형 UNCLASSIFIED, 직원 검토 필요
and three warnings, and the answer could never be published.

Three separate causes, none of them a real safety finding:

  1. ``classify_topics`` guarded the "AS" keyword with a non-word boundary.
     Hangul is a word character, so "삼성센터AS무상기간" did not match and the
     A/S question classified as OTHER. The answer's genuine A/S content then
     read as an *unrequested* topic.
  2. ``QUESTION_ANSWER_ALIGNMENT`` compared the whole answer's topics against
     unsupported sub-questions, so a safe deferral ("확인된 배송기한 정보가
     없어 안내가 어렵습니다") was indistinguishable from an invented delivery
     period, and both forced review.
  3. ``manual_review_required`` carried both "this is high risk" and "the
     keyword classifier had no rule for this wording", and both hard-blocked
     auto-registration.

Drafting, reviewing and publishing stay separate decisions: an invented fact
must still block, and a genuinely risky inquiry must still need a person.

Fakes only -- no provider, no network, no posting.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from answer.answer_validator import AnswerValidator
from answer.fact_selection import FactSelectionService
from answer.facts import build_answer_facts
from answer.hybrid_models import (
    DraftResult,
    Emotion,
    IntentResult,
    SelfReviewResult,
)
from answer.models import AnswerRequest, AnswerResult, AnswerStatus
from answer.text_utils import split_subquestions
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.auto_post_pipeline_service import AutoPostPipelineService
from services.auto_processing_eligibility_service import (
    AutoProcessingEligibilityService,
)
from services.inquiry_analysis_service import InquiryAnalysisService
from services.learning_compatibility_service import classify_topics


# The exact customer text of inquiry 686125753.
QUESTION = "삼성센터AS무상기간알려주세요\n배송기한얼마나생각하면될까요?"

ANALYSIS = InquiryAnalysisService()
VALIDATOR = AnswerValidator()
ELIGIBILITY = AutoProcessingEligibilityService()

# What Learning actually supplied on the server: 8 selected, 3 used.
EVIDENCE = (
    "삼성전자 서비스센터에서 A/S를 받으실 수 있습니다. "
    "제품 수령 후 1년간 무상 A/S가 제공됩니다. 패널은 2년입니다."
)

# The answer the operator saw: A/S answered from evidence, delivery deferred.
SAFE_ANSWER = (
    "A/S는 삼성전자 서비스센터를 통해 받으실 수 있습니다.\n"
    "제품 수령 후 1년간 무상 A/S가 제공되며, 패널은 2년입니다.\n"
    "배송 일정은 주문 여부 확인이 필요합니다. 이미 주문하셨다면 주문번호를 "
    "알려주시면 설치예정일을 확인해 드리겠습니다.\n"
    "주문 전이라면 현재 확인된 배송기한 정보가 없어 정확한 기간 안내가 "
    "어렵습니다."
)


def _request(question: str = QUESTION) -> AnswerRequest:
    return AnswerRequest(
        inquiry_id=9001,
        question_id="686125753",
        inquiry_type="PRODUCT_INQUIRY",
        question=question,
        product_name="삼성 50인치 TV",
        metadata={
            "source_type": "PRODUCT_INQUIRY",
            "dps": {
                "lookup_required": False,
                "lookup_status": "NOT_REQUIRED",
                "warnings": [],
            },
        },
    )


def _rule() -> AnswerResult:
    return AnswerResult(
        status=AnswerStatus.NEEDS_REVIEW,
        category="설치/AS",
        reason="Rule",
        answer="A/S 관련 안내를 드립니다.",
        provider="rules",
        auto_answerable=False,
        needs_review=True,
        matched_rule="설치/AS",
    )


def _neutral_review() -> SelfReviewResult:
    return SelfReviewResult(
        passed=True,
        answered_all_questions=True,
        has_speculation=False,
        facts_consistent=True,
        requires_review=False,
        reason="Validator가 결정적으로 검증합니다.",
        warnings=(),
    )


def _validate(answer: str, *, question: str = QUESTION, evidence=EVIDENCE):
    request = _request(question)
    analysis = ANALYSIS.analyze(request)
    facts = build_answer_facts(request, _rule())
    selected = FactSelectionService().select(facts, analysis)
    questions = split_subquestions(question)
    intent = IntentResult(
        category="설치/AS",
        questions=questions,
        emotion=Emotion.NORMAL,
        urgency="NORMAL",
        confidence=0.9,
        requires_review=True,
        reason="복합문의",
    )
    subquestion_evidence = [
        {
            "subquestion": questions[0],
            "status": "ANSWERABLE",
            "evidence_coverage": "SUPPORTED",
        },
        {
            "subquestion": questions[-1],
            "status": "NO_RELIABLE_SOURCE",
            "evidence_coverage": "UNSUPPORTED",
        },
    ]
    return VALIDATOR.validate(
        facts,
        intent,
        DraftResult(answer=answer, confidence=0.8, used_facts=()),
        _neutral_review(),
        analysis=analysis,
        selected_facts=selected,
        subquestion_evidence=subquestion_evidence,
        evidence_texts=evidence,
    )


def _gate(
    *,
    validation_status: str = "PASS",
    validator: dict | None = None,
    review_status: str = "PENDING",
    route: str = "GPT_DIRECT",
    high_risk: bool = False,
    answer: str = SAFE_ANSWER,
):
    analysis = ANALYSIS.analyze(_request())
    plan = {"analysis": analysis.to_dict(), "is_high_risk": high_risk}
    return ELIGIBILITY.evaluate(
        inquiry={"source_answered": 0, "post_status": "NOT_POSTED"},
        draft={
            "original_answer": answer,
            "validation_status": validation_status,
            "validator_result_json": (
                {
                    "passed": True,
                    "status": "PASS",
                    "errors": [],
                    "review_signals": [],
                }
                if validator is None
                else validator
            ),
            "review_status": review_status,
            "metadata_json": {"processing_plan": plan, "hybrid": {}},
        },
        route=route,
    )


# ------------------------------------------------- cause 1: the classifier


def test_unspaced_korean_as_question_is_recognised() -> None:
    """An unspaced "삼성센터AS무상기간" is an A/S question, not OTHER."""

    assert "AS_SUPPORT" in classify_topics("삼성센터AS무상기간알려주세요")
    assert "AS_SUPPORT" in classify_topics("삼성센터AS")
    assert "AS_SUPPORT" in classify_topics("무상AS기간")
    assert "AS_SUPPORT" in classify_topics("A/S는 어떻게 받나요")


@pytest.mark.parametrize("text", ["ASUS 모니터", "GAS 레인지", "CLASS 정보"])
def test_latin_neighbours_are_still_not_as_questions(text: str) -> None:
    """The relaxed boundary must not turn ASUS or GAS into an A/S topic."""

    assert "AS_SUPPORT" not in classify_topics(text)


# ------------------------------ cause 2: safe hold vs. invented fact


def test_safe_delivery_hold_passes_validation() -> None:
    """Declining to state a delivery period is the wanted behaviour."""

    result = _validate(SAFE_ANSWER)
    assert result.status == "PASS", result.review_signals
    assert result.passed is True
    assert result.errors == ()
    assert result.review_signals == ()


def test_safe_hold_still_records_its_observation() -> None:
    """Relaxing the verdict must not throw the finding away."""

    result = _validate(SAFE_ANSWER)
    assert any(
        "ANSWER_CONTAINS_UNREQUESTED_TOPIC" in warning
        for warning in result.warnings
    )
    alignment = next(
        rule
        for rule in result.rules
        if rule.code == "QUESTION_ANSWER_ALIGNMENT"
    )
    assert alignment.status == "PASS"


@pytest.mark.parametrize(
    ("label", "answer", "expected_error"),
    [
        (
            "invented delivery period",
            "제품 수령 후 1년간 무상 A/S가 제공됩니다.\n"
            "배송은 보통 2주 정도 걸립니다.",
            "2주",
        ),
        (
            "invented A/S period",
            "제품 수령 후 3년간 무상 A/S가 제공됩니다.",
            "3년",
        ),
        (
            "invented card discount",
            "제품 수령 후 1년간 무상 A/S가 제공됩니다.\n"
            "국민카드 5% 할인이 적용됩니다.",
            "5%",
        ),
    ],
)
def test_invented_facts_are_still_blocked(
    label: str, answer: str, expected_error: str
) -> None:
    result = _validate(answer)
    assert result.status == "BLOCK", label
    assert result.passed is False, label
    assert any(expected_error in error for error in result.errors), (
        label,
        result.errors,
    )


def test_invented_bracket_compatibility_is_still_blocked() -> None:
    result = _validate(
        "제품 수령 후 1년간 무상 A/S가 제공됩니다.\n기존 브라켓과 호환됩니다."
    )
    assert result.status == "BLOCK"
    assert any("호환" in error for error in result.errors)


def test_asserting_an_unsupported_subquestion_still_signals() -> None:
    """The alignment rule must still fire on an assertion, not a deferral."""

    result = _validate(
        "제품 수령 후 1년간 무상 A/S가 제공됩니다.\n"
        "배송은 보통 2주 정도 걸립니다."
    )
    alignment = next(
        rule
        for rule in result.rules
        if rule.code == "QUESTION_ANSWER_ALIGNMENT"
    )
    assert alignment.status == "REVIEW_REQUIRED"
    assert "배송기한" in alignment.message


# ------------------------------------- cause 3: the auto-registration gate


def test_this_inquiry_now_holds_on_the_delivery_half_not_on_a_classifier_gap() -> None:
    """분류기 공백은 사라졌고, 남은 보류 사유는 운영정책이다.

    "삼성센터AS무상기간알려주세요 배송기한얼마나생각하면될까요?" 의 두 번째
    하위질문은 구매 전 고객의 배송 소요 기간 문의(유형 A)다. 확정된 운영정책상
    자동답변하지 않는다.

    이 파일이 원래 없애려던 결함 -- 문장이 키워드 표에 없다는 이유만으로
    UNCLASSIFIED 가 되어 근거 있는 답변까지 막히던 것 -- 은 그대로 해결되어
    있다. 지금 보류하는 이유는 분류 실패가 아니라 정책 판단이고, 조회는 여전히
    필요 없다.
    """

    result = _gate()
    analysis = ANALYSIS.analyze(_request())

    assert result.decision == "REVIEW_REQUIRED"
    assert "POLICY_OR_HIGH_RISK_REVIEW" in result.reasons
    assert "INTENT_UNCLASSIFIED_VALIDATOR_CLEAR" not in result.soft_reasons
    assert analysis.manual_review_required is True
    assert "UNCLASSIFIED" not in analysis.manual_review_sources
    assert analysis.requires_order_lookup is False
    assert analysis.requires_dps_lookup is False


@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        (
            "validator raised a review signal",
            {
                "validator": {
                    "passed": True,
                    "status": "REVIEW_REQUIRED",
                    "errors": [],
                    "review_signals": ["복합 질문 일부 미답변"],
                }
            },
        ),
        (
            "validator blocked",
            {
                "validation_status": "FAILED_INVALID_CONTENT",
                "validator": {
                    "passed": False,
                    "status": "BLOCK",
                    "errors": ["근거 없는 수치·기간을 확정했습니다: 2주"],
                    "review_signals": [],
                },
            },
        ),
        ("genuinely high risk", {"high_risk": True}),
        ("draft marked for review", {"review_status": "NEEDS_REVIEW"}),
        ("route is not auto-postable", {"route": "REVIEW_REQUIRED_SAFE_DRAFT"}),
    ],
)
def test_real_review_reasons_still_block(label: str, kwargs: dict) -> None:
    result = _gate(**kwargs)
    assert result.decision != "SAFE", label
    assert result.reasons, label


def test_unclassified_alone_never_outranks_a_validator_finding() -> None:
    """A validator finding blocks independently of the former intent gap."""

    blocked = _gate(
        validator={
            "passed": True,
            "status": "PASS",
            "errors": [],
            "review_signals": ["직원 확인이 필요합니다"],
        }
    )
    assert "INTENT_UNCLASSIFIED_VALIDATOR_CLEAR" not in blocked.soft_reasons
    assert "VALIDATOR_REVIEW_REQUIRED" in blocked.reasons


# --------------------------------------------------------- posting gate


class CountingPostService:
    """Stand-in for the Naver posting service."""

    def __init__(self) -> None:
        self.calls = 0

    def post(self, *args, **kwargs):  # pragma: no cover - must not run
        self.calls += 1
        raise AssertionError("POST must not be called for a blocked inquiry")


@pytest.fixture
def database(tmp_path: Path) -> Database:
    value = Database(tmp_path / "gate.db")
    value.initialize()
    return value


def test_a_blocked_inquiry_never_reaches_the_post_service(
    database: Database,
) -> None:
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": "686125753",
            "external_inquiry_id": "686125753",
            "inquiry_type": "PRODUCT_INQUIRY",
            "title": "상품문의",
            "content": QUESTION,
            "product_name": "삼성 50인치 TV",
            "registered_at": "2026-08-24T10:00:00+09:00",
            "raw_json": {},
        }
    ).inquiry_id
    posts = CountingPostService()

    outcome = AutoPostPipelineService(
        database, post_service=posts
    ).run_pending(
        run_id="RUN-686125753",
        owner_id="OWNER-686125753",
        max_retries=1,
        inquiry_ids=[inquiry_id],
    )

    # No draft exists, so there is nothing eligible and nothing is published.
    assert posts.calls == 0
    assert outcome.succeeded_count == 0


# ------- the relaxation must not reach a positively-classified review

DISPUTE_QUESTION = (
    "구매내역서 발급 가능한가요? 배송 중 파손됐는데 보상은 어떻게 되나요?"
)


@pytest.mark.parametrize(
    "question",
    [DISPUTE_QUESTION, "배송 중 파손되면 어떻게 하나요?"],
)
def test_risk_and_dispute_inquiries_are_never_treated_as_a_gap(
    question: str,
) -> None:
    """"직원 판단이 필요하다"고 분류된 문의는 계속 REVIEW_REQUIRED."""

    analysis = ANALYSIS.analyze(_request(question)).to_dict()
    assert analysis["manual_review_required"] is True
    assert not ELIGIBILITY._intent_unclassified(analysis), question

    result = ELIGIBILITY.evaluate(
        inquiry={"source_answered": 0, "post_status": "NOT_POSTED"},
        draft={
            "original_answer": "안내드리겠습니다.",
            "validation_status": "PASS",
            "validator_result_json": {
                "passed": True,
                "status": "PASS",
                "errors": [],
                "review_signals": [],
            },
            "review_status": "PENDING",
            "metadata_json": {
                "processing_plan": {"analysis": analysis},
                "hybrid": {},
            },
        },
        route="GPT_DIRECT",
    )
    assert result.decision == "REVIEW_REQUIRED", question
    assert "POLICY_OR_HIGH_RISK_REVIEW" in result.reasons, question


def test_only_an_unclassified_intent_counts_as_a_classifier_gap() -> None:
    assert ELIGIBILITY._intent_unclassified(
        {
            "question_category": "INFORMATION_INSUFFICIENT",
            "inquiry_subtype": "UNCLASSIFIED",
        }
    )
    # A positively classified review category is not a gap.
    assert not ELIGIBILITY._intent_unclassified(
        {
            "question_category": "MANUAL_REVIEW_REQUIRED",
            "inquiry_subtype": "HIGH_RISK_OR_DISPUTE",
        }
    )
    # An empty question is insufficient information, but it is not a gap in
    # the keyword tables -- there was nothing to classify.
    assert not ELIGIBILITY._intent_unclassified(
        {
            "question_category": "INFORMATION_INSUFFICIENT",
            "inquiry_subtype": "EMPTY_QUESTION",
        }
    )
    assert not ELIGIBILITY._intent_unclassified({})
