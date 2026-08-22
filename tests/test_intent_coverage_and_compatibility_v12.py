"""Intent coverage for ordinary questions, and compatibility fact safety.

Two defects this pins.

1. The intent classifier is a keyword allow-list with no A/S, warranty or
   general-usage category, and plain "설치" was not matched. Ordinary
   questions like "AS는 삼성서비스센터에서 하나요?" and "설치는 어떻게
   하나요?" therefore fell through to the UNCLASSIFIED fallback, which sets
   manual_review_required=True -- so a perfectly answerable question was
   held for staff. Fixed by classifying them, NOT by letting UNCLASSIFIED
   auto-post.

2. Whether the customer's own bracket fits is a fact they buy on, yet
   "집에 상하좌우 브라켓이 있는데 이 제품을 사면 벽걸이로도 쓸 수 있나요?"
   evaluated as auto-postable, so the model's "확인하기 어렵습니다" draft
   would have been published automatically. Now a compatibility answer may
   only publish when an exact fixed template (the catalog's verified
   accessory rules or a Product DB fact) produced it.

Routing/eligibility decisions and fakes only -- no provider or network use.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from answer.models import AnswerRequest
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.auto_post_pipeline_service import AutoPostPipelineService
from services.auto_processing_eligibility_service import (
    SOFT_REASONS,
    AutoProcessingEligibilityService,
)
from services.inquiry_analysis_service import InquiryAnalysisService


ANALYSIS = InquiryAnalysisService()
ELIGIBILITY = AutoProcessingEligibilityService()

BRACKET_QUESTION = (
    "집에 상하좌우 브라켓이 있는데 이 제품을 사면 벽걸이로도 쓸 수 있나요?"
)


def analyze(question: str, product: str = "삼성 50인치 TV"):
    return ANALYSIS.analyze(
        AnswerRequest(question=question, product_name=product)
    )


def evaluate(*, route: str, analysis: dict, answer: str = "안내드립니다."):
    return ELIGIBILITY.evaluate(
        inquiry={"source_answered": 0, "post_status": "NOT_POSTED"},
        draft={
            "original_answer": answer,
            "validation_status": "PASSED",
            "validator_result_json": None,
            "review_status": "",
            "metadata_json": {"processing_plan": {"analysis": analysis}},
            "posted": False,
            "id": 1,
        },
        route=route,
    )


# ------------------------------------------------------- intent coverage

# CASE A/B/C -- ordinary questions must reach a real intent, not the
# UNCLASSIFIED fallback.
@pytest.mark.parametrize(
    "question",
    [
        "AS는 삼성서비스센터에서 하나요?",
        "삼성전자 서비스센터에서 A/S 받을 수 있나요?",
        "설치는 어떻게 하나요?",
        "기사님이 설치해주시나요?",
        "벽걸이 설치 가능한가요?",
        "스탠드로 설치되나요?",
        "폐가전 수거 가능한가요?",
        "제품 보증기간은 어떻게 되나요?",
        "튼튼한가요?",
        "인터넷 연결해서 사용할 수 있나요?",
    ],
)
def test_ordinary_questions_are_classified(question: str) -> None:
    analysis = analyze(question)
    assert analysis.inquiry_subtype != "UNCLASSIFIED"
    assert analysis.manual_review_required is False
    assert analysis.auto_answerable is True


# CASE S -- a genuinely unclassifiable question still goes to staff. The fix
# must not have turned UNCLASSIFIED into an auto-post path.
def test_case_s_genuinely_unclassifiable_still_requires_review() -> None:
    analysis = analyze("음...")
    assert analysis.inquiry_subtype == "UNCLASSIFIED"
    assert analysis.manual_review_required is True
    result = evaluate(
        route="GPT_DIRECT",
        analysis={"manual_review_required": True},
    )
    assert result.safe is False
    assert "POLICY_OR_HIGH_RISK_REVIEW" in result.reasons


# ----------------------------------------------------------- compatibility

# CASE D -- the real production case: no authoritative fact, so no auto-post.
def test_case_d_unverified_compatibility_is_blocked() -> None:
    analysis = analyze(BRACKET_QUESTION, "삼성 50인치 TV 스탠드")
    assert analysis.detected_intent == "PRODUCT_COMPATIBILITY"
    # A draft is still allowed; only publishing is withheld.
    assert analysis.can_generate_answer is True

    result = evaluate(
        route="GPT_FALLBACK",
        analysis={"detected_intent": "PRODUCT_COMPATIBILITY"},
        answer="현재 제공된 정보만으로 호환 여부를 확인하기 어렵습니다.",
    )
    assert result.safe is False
    assert "PRODUCT_COMPATIBILITY_NOT_VERIFIED" in result.reasons
    assert "PRODUCT_COMPATIBILITY_NOT_VERIFIED" not in SOFT_REASONS


# CASE E -- an exact fixed accessory/Product DB fact may still answer and post.
@pytest.mark.parametrize("route", ["TEMPLATE", "PRODUCT_DB"])
def test_case_e_authoritative_compatibility_still_posts(route: str) -> None:
    result = evaluate(
        route=route,
        analysis={"detected_intent": "PRODUCT_COMPATIBILITY"},
    )
    assert result.safe is True


@pytest.mark.parametrize(
    "question",
    [
        BRACKET_QUESTION,
        "기존 브라켓 쓸 수 있나요?",
        "이 스탠드 호환되나요?",
        "이 거치대 장착 가능한가요?",
    ],
)
def test_compatibility_questions_are_tagged(question: str) -> None:
    assert analyze(question, "삼성 50인치 TV 스탠드").detected_intent == (
        "PRODUCT_COMPATIBILITY"
    )


# CASE F/G -- plain feature questions must not be swept in by the tag.
@pytest.mark.parametrize(
    "question",
    [
        "인터넷 연결해서 사용할 수 있나요?",
        "HDMI 연결 가능한가요?",
        "설치는 어떻게 하나요?",
        "AS는 삼성서비스센터에서 하나요?",
    ],
)
def test_case_f_g_feature_questions_are_not_compatibility(
    question: str,
) -> None:
    assert analyze(question).detected_intent != "PRODUCT_COMPATIBILITY"


# ------------------------------------------- preserved existing policies

@pytest.mark.parametrize(
    ("question", "subtype"),
    [
        # CASE J/K -- schedule changes stay staff-only.
        ("설치일을 25일에서 10일로 변경해주세요.", "SCHEDULE_CHANGE_REQUEST"),
        ("배송일을 다른 날짜로 바꿔주세요.", "SCHEDULE_CHANGE_REQUEST"),
        # CASE L/M -- damage and complaint stay staff-only.
        ("제품이 파손돼서 왔습니다.", "HIGH_RISK_OR_DISPUTE"),
        (
            "배송이 너무 늦어서 너무 화가 납니다. 보상해주세요.",
            "HIGH_RISK_OR_DISPUTE",
        ),
    ],
)
def test_existing_hard_policies_are_preserved(
    question: str, subtype: str
) -> None:
    analysis = analyze(question)
    assert analysis.inquiry_subtype == subtype
    assert analysis.manual_review_required is True


# CASE N/O -- card benefit still blocked; plain card payment still allowed.
def test_case_n_o_card_policy_preserved() -> None:
    assert analyze("BC카드로 결제하면 할인되나요?").manual_review_required is True
    assert analyze("카드 결제 가능한가요?").manual_review_required is False


# CASE H/R -- schedule lookups keep their existing DPS/order route.
@pytest.mark.parametrize(
    "question", ["설치 예정일이 언제인가요?", "배송 언제 와요?"]
)
def test_case_h_r_schedule_lookup_unchanged(question: str) -> None:
    analysis = analyze(question)
    assert analysis.inquiry_subtype == "DELIVERY_OR_INSTALLATION_SCHEDULE"
    assert analysis.manual_review_required is False
    assert analysis.requires_dps_lookup is True


# ------------------------------------------------- POST is never called

class CountingPostService:
    def __init__(self) -> None:
        self.calls = 0

    def post(self, *args, **kwargs):  # pragma: no cover - must not run
        self.calls += 1
        raise AssertionError("POST must not be called for a blocked inquiry")


@pytest.fixture
def database(tmp_path: Path) -> Database:
    value = Database(tmp_path / "intent-v12.db")
    value.initialize()
    return value


def _inquiry(database: Database, question: str, product: str) -> int:
    return InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": f"V12-{abs(hash(question)) % 10000}",
            "external_inquiry_id": f"V12-{abs(hash(question)) % 10000}",
            "inquiry_type": "PRODUCT_INQUIRY",
            "title": "문의",
            "content": question,
            "product_name": product,
            "registered_at": "2026-08-23T10:00:00",
            "raw_json": {},
        }
    ).inquiry_id


# Section 14 -- prove nothing publishes, at the pipeline level.
@pytest.mark.parametrize(
    ("question", "product"),
    [
        (BRACKET_QUESTION, "삼성 50인치 TV 스탠드"),
        ("설치일을 25일에서 10일로 변경해주세요.", "삼성 TV"),
        ("제품이 파손돼서 왔습니다.", "삼성 TV"),
        ("배송이 너무 늦어서 화가 납니다. 보상해주세요.", "삼성 TV"),
        ("BC카드로 결제하면 할인되나요?", "삼성 TV"),
        ("음...", "삼성 TV"),
    ],
)
def test_blocked_inquiries_never_reach_post(
    database: Database, question: str, product: str
) -> None:
    inquiry_id = _inquiry(database, question, product)
    posts = CountingPostService()
    pipeline = AutoPostPipelineService(database, post_service=posts)

    outcome = pipeline.run_pending(
        run_id="RUN-V12", owner_id="OWNER-V12", max_retries=1,
        inquiry_ids=[inquiry_id],
    )

    assert posts.calls == 0
    assert outcome.succeeded_count == 0
