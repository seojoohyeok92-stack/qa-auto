"""Stale DPS schedules and unverifiable payment benefits.

Both defects share one shape: the system had a plausible-looking answer and
would have published it, even though nothing could establish the fact was
true *now*.

1. A DPS lookup can succeed and still return the schedule of an already
   completed delivery. Real case: an inquiry registered 2026-08-22 whose DPS
   installation_date was 2026-08-03. ``installation_confirmed`` had no
   freshness check, so the three-week-old date was treated as the current
   schedule and could be posted as "설치예정일은 8월 3일입니다."

2. Card/payment benefits change every promotion period and this system holds
   no verified current source for them, yet such questions were
   ``auto_answerable``.

Neither fix stops draft generation -- only automatic publishing.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from answer.facts import build_answer_facts
from answer.models import AnswerRequest, AnswerResult, AnswerStatus
from dps.dates import STALE_DPS_SCHEDULE, is_schedule_stale
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.auto_processing_eligibility_service import (
    SOFT_REASONS,
    AutoProcessingEligibilityService,
)
from services.auto_post_pipeline_service import AutoPostPipelineService
from services.inquiry_analysis_service import InquiryAnalysisService


ANALYSIS = InquiryAnalysisService()
ELIGIBILITY = AutoProcessingEligibilityService()

INQUIRY_DATE = "2026-08-22T10:00:00"
CARD_QUESTION = (
    "BC카드혜택 받으려고하는데 렉스카드 탑포인트 부산은행 "
    "이카드로 적용가능한가요?"
)


def _rule_result() -> AnswerResult:
    return AnswerResult(
        status=AnswerStatus.GENERATED, category="배송", reason="rule",
        answer="확인했습니다.", provider="rules", auto_answerable=True,
        needs_review=False,
    )


def facts_for(installation_date: str, *, registered_at: str = INQUIRY_DATE):
    request = AnswerRequest(
        question="언제쯤 받아볼 수 있을까요?",
        order_id="ORDER-1",
        metadata={
            "registered_at": registered_at,
            "dps": {
                "installation_date": installation_date,
                "installation_date_source": (
                    "DPS_ITEM_DETAIL_REQUIRED_DELIVERY_DATE"
                ),
                "date_parse_status": "PARSED",
                "requires_human_review": False,
                "lookup_status": "SUCCESS",
            },
        },
    )
    return build_answer_facts(request, _rule_result())


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


# ------------------------------------------------------------- stale dates

# CASE A -- the real production case.
def test_case_a_past_schedule_is_not_a_confirmed_date() -> None:
    facts = facts_for("2026-08-03")
    assert facts.installation["date"] is None
    assert facts.installation["installation_date_confirmed"] is False
    assert facts.installation["source"] is None
    assert facts.policy["requires_review"] is True
    assert facts.dps["schedule_validity"] == STALE_DPS_SCHEDULE
    # The lookup itself really did succeed and must not be misreported.
    assert facts.dps["lookup_status"] == "SUCCESS"


# CASE B -- same day is not stale.
def test_case_b_same_day_schedule_is_still_confirmed() -> None:
    facts = facts_for("2026-08-22")
    assert facts.installation["date"] == "2026-08-22"
    assert facts.installation["installation_date_confirmed"] is True
    assert facts.policy["requires_review"] is False
    assert facts.dps["schedule_validity"] is None


# CASE C -- a future schedule is unaffected.
def test_case_c_future_schedule_is_still_confirmed() -> None:
    facts = facts_for("2026-08-25")
    assert facts.installation["date"] == "2026-08-25"
    assert facts.installation["installation_date_confirmed"] is True


# CASE D -- reprocessing an older inquiry must judge it as of the inquiry
# date, not today, or every historical row would turn stale.
def test_case_d_old_inquiry_is_judged_against_its_own_date() -> None:
    facts = facts_for("2026-08-12", registered_at="2026-08-10T09:00:00")
    assert facts.installation["date"] == "2026-08-12"
    assert facts.installation["installation_date_confirmed"] is True
    assert facts.dps["schedule_validity"] is None


@pytest.mark.parametrize(
    ("schedule", "registered", "expected"),
    [
        ("2026-08-03", "2026-08-22T10:00:00", True),
        ("2026-08-22", "2026-08-22T10:00:00", False),
        ("2026-08-25", "2026-08-22T10:00:00", False),
        ("2026-08-12", "2026-08-10T09:00:00", False),
        (None, "2026-08-22T10:00:00", False),
        ("", "2026-08-22T10:00:00", False),
    ],
)
def test_staleness_boundaries(schedule, registered, expected) -> None:
    assert is_schedule_stale(schedule, registered_at=registered) is expected


# A stale schedule is a hard auto-post block via the existing DPS reason --
# no new eligibility code path was introduced.
def test_stale_schedule_hard_blocks_auto_post() -> None:
    result = evaluate(
        route="DELIVERY_WITH_INSTALLATION_DATE",
        plan={
            "requires_dps_lookup": True,
            "dps_lookup_status": "SUCCESS",
            "valid_dps_snapshot_available": False,
        },
    )
    assert result.safe is False
    assert "DPS_SNAPSHOT_NOT_VALIDATED" in result.reasons
    assert "DPS_SNAPSHOT_NOT_VALIDATED" not in SOFT_REASONS


# CASE I -- a healthy future schedule still auto-posts.
def test_case_i_valid_dps_schedule_still_auto_posts() -> None:
    result = evaluate(
        route="DELIVERY_WITH_INSTALLATION_DATE",
        plan={
            "requires_dps_lookup": True,
            "dps_lookup_status": "SUCCESS",
            "valid_dps_snapshot_available": True,
        },
    )
    assert result.safe is True


# ---------------------------------------------------------- card benefits

# CASE E -- a draft is still produced; only publishing is withheld.
def test_case_e_card_benefit_allows_draft_but_requires_review() -> None:
    analysis = ANALYSIS.analyze(
        AnswerRequest(question=CARD_QUESTION, product_name="삼성 TV")
    )
    assert analysis.can_generate_answer is True
    assert analysis.manual_review_required is True
    assert analysis.auto_answerable is False


@pytest.mark.parametrize(
    "question",
    [
        CARD_QUESTION,
        "무이자 할부 가능한가요?",
        "네이버 포인트 적립되나요?",
        "청구할인 대상인가요?",
        "제휴할인 있나요?",
        "캐시백 받을 수 있나요?",
    ],
)
def test_payment_benefit_questions_require_review(question: str) -> None:
    analysis = ANALYSIS.analyze(
        AnswerRequest(question=question, product_name="삼성 TV")
    )
    assert analysis.manual_review_required is True
    assert analysis.can_generate_answer is True


PAYMENT_REVIEW_REASON = "카드·결제 혜택은"


# The gate keys on the benefit, not on the word "카드" -- ordinary payment
# and product questions must not be swept in by it. Asserted on the reason
# rather than the flag, because some of these already carry an unrelated
# pre-existing review requirement of their own.
@pytest.mark.parametrize(
    "question",
    [
        "카드 결제 가능한가요?",
        "벽걸이 설치 가능한가요?",
        "배송 언제 와요?",
        "온누리상품권 사용 가능한가요?",
        "설치는 어떻게 하나요?",
    ],
)
def test_ordinary_questions_are_not_swept_into_review(question: str) -> None:
    analysis = ANALYSIS.analyze(
        AnswerRequest(question=question, product_name="삼성 TV")
    )
    assert not any(
        PAYMENT_REVIEW_REASON in reason for reason in analysis.reasons
    )


# Questions that were auto-answerable before must stay auto-answerable.
@pytest.mark.parametrize(
    "question",
    [
        "카드 결제 가능한가요?",
        "벽걸이 설치 가능한가요?",
        "배송 언제 와요?",
        "온누리상품권 사용 가능한가요?",
    ],
)
def test_ordinary_questions_stay_auto_answerable(question: str) -> None:
    analysis = ANALYSIS.analyze(
        AnswerRequest(question=question, product_name="삼성 TV")
    )
    assert analysis.manual_review_required is False


# The payment gate does fire on the benefit questions, by that same reason.
def test_payment_gate_is_the_source_of_the_card_review() -> None:
    analysis = ANALYSIS.analyze(
        AnswerRequest(question=CARD_QUESTION, product_name="삼성 TV")
    )
    assert any(
        PAYMENT_REVIEW_REASON in reason for reason in analysis.reasons
    )


# CASE F -- a past Learning answer cannot make a current benefit certain.
# The review requirement is a property of the question, so retrieval finding
# an old positive answer changes nothing about publishing.
def test_case_f_past_learning_does_not_confirm_current_benefit() -> None:
    analysis = ANALYSIS.analyze(
        AnswerRequest(
            question="지금도 XX카드 할인 적용되나요?", product_name="삼성 TV"
        )
    )
    assert analysis.manual_review_required is True
    result = evaluate(
        route="GPT_DIRECT",
        plan={"analysis": {"manual_review_required": True}},
        answer="네, 적용됩니다.",
    )
    assert result.safe is False
    assert "POLICY_OR_HIGH_RISK_REVIEW" in result.reasons


# ------------------------------------------------- POST is never called

class CountingPostService:
    """Fails the test loudly if the pipeline ever tries to publish."""

    def __init__(self) -> None:
        self.calls = 0

    def post(self, *args, **kwargs):  # pragma: no cover - must not run
        self.calls += 1
        raise AssertionError("POST must not be called for a blocked inquiry")


@pytest.fixture
def database(tmp_path: Path) -> Database:
    value = Database(tmp_path / "stale-v11.db")
    value.initialize()
    return value


def _blocked_inquiry(database: Database, question: str) -> int:
    return InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": "STALE-1",
            "external_inquiry_id": "STALE-1",
            "inquiry_type": "PRODUCT_INQUIRY",
            "title": "문의",
            "content": question,
            "product_name": "삼성 TV",
            "registered_at": INQUIRY_DATE,
            "raw_json": {},
        }
    ).inquiry_id


# Section 19 -- prove at the pipeline level that nothing is published.
@pytest.mark.parametrize(
    "question",
    ["언제쯤 받아볼 수 있을까요?", CARD_QUESTION],
)
def test_blocked_inquiries_never_reach_post(
    database: Database, question: str
) -> None:
    inquiry_id = _blocked_inquiry(database, question)
    if question != CARD_QUESTION:
        # Pin the pipeline-level assertion to an actual stale DPS result. A
        # delivery question with no order number now correctly posts the
        # confirmed order-number request template and is not stale-DPS input.
        AnswerRepository(database).create_program_draft(
            inquiry_id,
            AnswerResult(
                status=AnswerStatus.GENERATED,
                category="배송",
                reason="stale DPS fixture",
                answer="설치예정일은 2026년 8월 3일입니다.",
                provider="rules",
                auto_answerable=True,
                needs_review=False,
                metadata={
                    "selected_answer_route": "DELIVERY_WITH_INSTALLATION_DATE",
                    "generation_mode": "DPS",
                    "validator_result": {"status": "PASS", "passed": True},
                    "processing_plan": {
                        "requires_dps_lookup": True,
                        "dps_lookup_status": "SUCCESS",
                        "valid_dps_snapshot_available": False,
                        "analysis": {"manual_review_required": False},
                    },
                },
            ),
        )
    posts = CountingPostService()
    pipeline = AutoPostPipelineService(database, post_service=posts)

    outcome = pipeline.run_pending(
        run_id="RUN-1", owner_id="OWNER-1", max_retries=1,
        inquiry_ids=[inquiry_id],
    )

    assert posts.calls == 0
    assert outcome.succeeded_count == 0
