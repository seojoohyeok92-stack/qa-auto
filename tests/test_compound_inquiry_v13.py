"""Compound (multi-question) inquiry handling.

Reproduced defect: the classifier was single-intent and first-match-wins over
the whole message. The real six-part inquiry contained "파손", so the
HIGH_RISK_OR_DISPUTE branch won outright and

  * can_generate_answer became False -- no Program Answer was produced at
    all, leaving the operator with "답변 생성 버튼을 눌러 초안을 생성하세요";
  * requires_dps_lookup became False -- so the "설치예정일은 언제인가요?"
    sub-question's DPS step was skipped.

Sub-questions are now judged individually and the verdicts combined:
requirements and review flags OR together, while the ability to answer
survives if any part is answerable. Partial answering and whole-inquiry
publishing are deliberately separate.

Routing/eligibility and fakes only -- no provider or network use.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from answer.models import AnswerRequest
from answer.text_utils import split_subquestions
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.auto_post_pipeline_service import AutoPostPipelineService
from services.auto_processing_eligibility_service import (
    AutoProcessingEligibilityService,
)
from services.inquiry_analysis_service import InquiryAnalysisService


ANALYSIS = InquiryAnalysisService()
ELIGIBILITY = AutoProcessingEligibilityService()

SIX_PART = (
    "A/S는 삼성서비스센터에서 하나요?\n"
    "설치는 기사님이 해주시나요?\n"
    "집에 있는 브라켓과 호환되나요?\n"
    "설치예정일은 언제인가요?\n"
    "카드 할인도 되나요?\n"
    "배송 중 파손되면 어떻게 하나요?"
)
NO_PUNCTUATION = (
    "A/S 어디서 받아요 설치는 기사님이 해주시나요 "
    "기존 브라켓도 쓸 수 있는지 궁금해요"
)


def analyze(question: str, product: str = "삼성 50인치 TV"):
    return ANALYSIS.analyze(
        AnswerRequest(question=question, product_name=product)
    )


def evaluate(question: str, *, route: str = "GPT_DIRECT"):
    analysis = analyze(question)
    return ELIGIBILITY.evaluate(
        inquiry={"source_answered": 0, "post_status": "NOT_POSTED"},
        draft={
            "original_answer": "안내드립니다.",
            "validation_status": "PASSED",
            "validator_result_json": None,
            "review_status": "",
            "metadata_json": {
                "processing_plan": {"analysis": analysis.to_dict()}
            },
            "posted": False,
            "id": 1,
        },
        route=route,
    )


# --------------------------------------------------------- decomposition

def test_six_part_inquiry_decomposes_into_six_questions() -> None:
    assert len(split_subquestions(SIX_PART)) == 6


# CASE K -- proving this is not a '?' split: there is no '?' in the text.
def test_case_k_punctuation_free_korean_is_decomposed() -> None:
    assert "?" not in NO_PUNCTUATION
    parts = split_subquestions(NO_PUNCTUATION)
    assert len(parts) == 3
    # A trailing "궁금해요" is filler, not a fourth question.
    assert all(part != "궁금해요" for part in parts)


def test_single_question_is_not_split() -> None:
    assert len(split_subquestions("AS는 삼성서비스센터에서 하나요?")) == 1


# ------------------------------------------------------------- CASE G

# The exact production reproduction: both original symptoms must be gone.
def test_case_g_six_part_inquiry_generates_draft_and_keeps_dps() -> None:
    analysis = analyze(SIX_PART)
    # Symptom 1: a Program Answer must now be possible.
    assert analysis.can_generate_answer is True
    # Symptom 2: the 설치예정일 sub-question keeps the DPS requirement alive.
    assert analysis.requires_dps_lookup is True
    assert analysis.requires_order_lookup is True
    # And it is still held for staff, with the compatibility reason kept.
    assert analysis.manual_review_required is True
    assert analysis.detected_intent == "PRODUCT_COMPATIBILITY"

    result = evaluate(SIX_PART)
    assert result.safe is False


# --------------------------------------------------- per-case aggregation

# CASE A -- every sub-question safe, so the compound stays auto-postable.
def test_case_a_all_safe_compound_is_auto_postable() -> None:
    question = "A/S는 어디서 받나요? 설치는 기사님이 해주시나요?"
    analysis = analyze(question)
    assert analysis.can_generate_answer is True
    assert analysis.manual_review_required is False
    assert evaluate(question).safe is True


@pytest.mark.parametrize(
    ("question", "reason"),
    [
        # CASE B -- compatibility
        (
            "A/S는 삼성서비스센터에서 하나요? 집에 있는 브라켓과 호환되나요?",
            "PRODUCT_COMPATIBILITY_NOT_VERIFIED",
        ),
        # CASE C -- card benefit
        (
            "설치는 기사님이 해주시나요? BC카드 할인도 되나요?",
            "POLICY_OR_HIGH_RISK_REVIEW",
        ),
        # CASE D -- damage
        (
            "A/S는 어디서 받나요? 배송 중 파손되면 어떻게 하나요?",
            "POLICY_OR_HIGH_RISK_REVIEW",
        ),
        # CASE H -- an exact template must not cover the whole inquiry
        (
            "구매내역서 발급 가능한가요? 배송 중 파손됐는데 보상은 어떻게 되나요?",
            "POLICY_OR_HIGH_RISK_REVIEW",
        ),
        # CASE I -- a schedule change alongside a lookup
        (
            "설치예정일이 언제인가요? 그리고 그 날짜를 10일로 변경해주세요.",
            "POLICY_OR_HIGH_RISK_REVIEW",
        ),
        # CASE K -- compatibility, no punctuation
        (NO_PUNCTUATION, "PRODUCT_COMPATIBILITY_NOT_VERIFIED"),
    ],
)
def test_one_hard_subquestion_blocks_the_whole_inquiry(
    question: str, reason: str
) -> None:
    analysis = analyze(question)
    # The safe part is still draftable for staff to work from.
    assert analysis.can_generate_answer is True
    result = evaluate(question)
    assert result.safe is False
    assert reason in result.reasons


# CASE E -- a schedule sub-question keeps order/DPS requirements even when
# the representative intent is something else.
def test_case_e_schedule_subquestion_keeps_dps_requirement() -> None:
    analysis = analyze(
        "A/S는 어디서 받나요? 설치는 기사님이 해주시나요? "
        "설치예정일은 언제인가요?"
    )
    assert analysis.requires_dps_lookup is True
    assert analysis.requires_order_lookup is True
    assert analysis.manual_review_required is False


# CASE I -- a schedule change must not be mistaken for a lookup just because
# a lookup sits next to it.
def test_case_i_change_request_survives_next_to_a_lookup() -> None:
    analysis = analyze(
        "설치예정일이 언제인가요? 그리고 그 날짜를 10일로 변경해주세요."
    )
    assert analysis.manual_review_required is True
    assert analysis.detected_intent == "SCHEDULE_CHANGE"


# CASE J -- ORDER_ID_REQUEST policy is untouched by aggregation.
def test_case_j_order_requirement_survives_in_a_compound() -> None:
    analysis = analyze("A/S는 어디서 받나요? 배송은 언제 오나요?")
    assert analysis.requires_order_lookup is True
    assert analysis.manual_review_required is False


# CASE L -- single-question behaviour must be unchanged.
@pytest.mark.parametrize(
    ("question", "manual"),
    [
        ("AS는 삼성서비스센터에서 하나요?", False),
        ("설치는 어떻게 하나요?", False),
        ("온누리상품권 사용 가능한가요?", False),
        ("구매내역서 발급 가능한가요?", False),
        # Pre-existing behaviour, unchanged here: the imperative form is not
        # a question, so it falls to UNCLASSIFIED and is held for staff.
        ("구매내역서 발급해주세요", True),
        ("폐가전 수거 가능한가요?", False),
        ("설치 예정일이 언제인가요?", False),
    ],
)
def test_case_l_single_question_regression(question: str, manual: bool) -> None:
    analysis = analyze(question)
    assert analysis.inquiry_subtype != "COMPOUND_MULTI_INTENT"
    assert analysis.manual_review_required is manual


# CASE M -- single-question HARD policies must be unchanged.
@pytest.mark.parametrize(
    "question",
    [
        "설치일을 25일에서 10일로 변경해주세요.",
        "제품이 파손돼서 왔습니다.",
        "배송기사 너무 불친절합니다.",
        "손해배상 해주세요.",
        "BC카드로 결제하면 할인되나요?",
    ],
)
def test_case_m_single_question_hard_regression(question: str) -> None:
    assert analyze(question).manual_review_required is True


# ------------------------------------------------- POST is never called

class CountingPostService:
    def __init__(self) -> None:
        self.calls = 0

    def post(self, *args, **kwargs):  # pragma: no cover - must not run
        self.calls += 1
        raise AssertionError("POST must not be called for a blocked inquiry")


@pytest.fixture
def database(tmp_path: Path) -> Database:
    value = Database(tmp_path / "compound-v13.db")
    value.initialize()
    return value


def _inquiry(database: Database, question: str) -> int:
    key = f"V13-{abs(hash(question)) % 100000}"
    return InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": key,
            "external_inquiry_id": key,
            "inquiry_type": "PRODUCT_INQUIRY",
            "title": "복합문의",
            "content": question,
            "product_name": "삼성 50인치 TV",
            "registered_at": "2026-08-23T10:00:00",
            "raw_json": {},
        }
    ).inquiry_id


# Section 12 -- prove nothing publishes, at the pipeline level.
@pytest.mark.parametrize(
    "question",
    [
        SIX_PART,
        NO_PUNCTUATION,
        "A/S는 삼성서비스센터에서 하나요? 집에 있는 브라켓과 호환되나요?",
        "설치는 기사님이 해주시나요? BC카드 할인도 되나요?",
        "A/S는 어디서 받나요? 배송 중 파손되면 어떻게 하나요?",
        "구매내역서 발급 가능한가요? 배송 중 파손됐는데 보상은 어떻게 되나요?",
        "설치예정일이 언제인가요? 그리고 그 날짜를 10일로 변경해주세요.",
    ],
)
def test_hard_compound_inquiries_never_reach_post(
    database: Database, question: str
) -> None:
    inquiry_id = _inquiry(database, question)
    posts = CountingPostService()
    pipeline = AutoPostPipelineService(database, post_service=posts)

    outcome = pipeline.run_pending(
        run_id="RUN-V13", owner_id="OWNER-V13", max_retries=1,
        inquiry_ids=[inquiry_id],
    )

    assert posts.calls == 0
    assert outcome.succeeded_count == 0
