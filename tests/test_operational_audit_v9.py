"""Pre-launch audit: high-risk auto-post blocks and staff-edit Learning E2E.

Two things are pinned here.

1. High-risk operational situations (damage, complaint, compensation,
   liability, dispute) must never reach the customer automatically. Before
   this pass HIGH_RISK_WORDS only covered legal/injury wording, so
   "TV 액정이 깨져 왔어요." classified as UNCLASSIFIED and was eligible for
   auto-post. The taxonomy was extended -- no new enum, column, or migration.

2. When staff edit the answer on Naver and an operator then approves it,
   the *edited* answer (B) is what becomes Positive Learning -- not the
   original automatic answer (A).

No provider calls and no network: routing/eligibility decisions and real
repository chains only.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from answer.models import AnswerRequest
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_repository import LearningRepository
from repositories.naver_posted_answer_repository import (
    NaverPostedAnswerRepository,
)
from services.approval_service import ApprovalService
from services.auto_processing_eligibility_service import (
    SOFT_REASONS,
    AutoProcessingEligibilityService,
)
from services.inquiry_analysis_service import InquiryAnalysisService


ELIGIBILITY = AutoProcessingEligibilityService()
ANALYSIS = InquiryAnalysisService()

AUTO_ANSWER_A = "고장이 의심되면 삼성전자 고객센터로 문의해 주세요."
STAFF_EDITED_B = "네, 삼성전자 서비스센터를 통해 A/S 받으실 수 있습니다."


def is_high_risk(question: str) -> bool:
    analysis = ANALYSIS.analyze(
        AnswerRequest(question=question, product_name="삼성 TV")
    )
    return analysis.inquiry_subtype == "HIGH_RISK_OR_DISPUTE"


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


# ---------------------------------------------------------------- high risk

# CASE D/E/F/G -- damage, complaint, compensation, liability must all be
# recognised as high risk so the pipeline holds them for staff.
@pytest.mark.parametrize(
    "question",
    [
        # DAMAGE
        "TV 액정이 깨져 왔어요.",
        "제품이 찌그러져 있습니다.",
        "배송 중 깨진 것 같은데 어떻게 하나요?",
        "박스가 심하게 훼손되었습니다.",
        "제품이 파손된 것 같습니다.",
        # COMPLAINT
        "배송기사 너무 불친절합니다.",
        "서비스가 너무 불만입니다.",
        "상담 대응이 너무 엉망입니다.",
        # COMPENSATION
        "파손됐는데 얼마 보상해주실 건가요?",
        "손해배상 해주세요.",
        # LIABILITY / DISPUTE
        "누가 책임지나요?",
        "기사님 과실 아닌가요?",
        # pre-existing legal wording must keep working
        "법적 대응하겠습니다.",
        "소송하겠습니다.",
    ],
)
def test_high_risk_questions_are_classified_high_risk(question: str) -> None:
    assert is_high_risk(question) is True


# CASE A/B/H and section 10 -- the extension must not capture ordinary
# questions. These have to stay auto-answerable.
@pytest.mark.parametrize(
    "question",
    [
        "AS는 삼성서비스센터에서 하나요?",
        "감사합니다.",
        "배송 잘 부탁드립니다.",
        "반품하려면 어떻게 하나요?",
        "벽걸이 설치 가능한가요?",
        "보상판매 되나요?",  # trade-in, not compensation
        "설치는 어떻게 하나요?",
        "배송 언제 와요?",
        "튼튼한가요?",
        "구매내역서 발급해주세요",
        "온누리상품권 사용 가능한가요?",
    ],
)
def test_ordinary_questions_are_not_high_risk(question: str) -> None:
    assert is_high_risk(question) is False


# CASE P -- a fixed template matching the same inquiry must not rescue it.
# Eligibility reads plan.is_high_risk independently of the route, so even a
# TEMPLATE route stays blocked.
def test_case_p_high_risk_beats_a_fixed_template() -> None:
    result = evaluate(route="TEMPLATE", plan={"is_high_risk": True})
    assert result.safe is False
    assert "POLICY_OR_HIGH_RISK_REVIEW" in result.reasons


# CASE Q -- a high quality GPT draft must not rescue it either.
def test_case_q_high_risk_beats_a_good_gpt_draft() -> None:
    result = evaluate(
        route="GPT_DIRECT",
        plan={"is_high_risk": True},
        answer="고객님 불편을 드려 죄송합니다. 신속히 도와드리겠습니다.",
    )
    assert result.safe is False
    assert "POLICY_OR_HIGH_RISK_REVIEW" in result.reasons


# The high-risk reason must be HARD, never quietly downgraded to SOFT.
def test_high_risk_reason_is_hard_not_soft() -> None:
    assert "POLICY_OR_HIGH_RISK_REVIEW" not in SOFT_REASONS


# CASE X/Y -- the existing SOFT policy must survive this audit.
def test_case_x_y_soft_policy_still_allows_auto_post() -> None:
    result = evaluate(
        route="GPT_DIRECT", plan={"analysis": {"confidence": 0.4}}
    )
    assert result.safe is True
    assert "INTENT_CONFIDENCE_LOW" in result.soft_reasons


# CASE Z -- privacy stays a hard block.
def test_case_z_pii_is_still_hard_blocked() -> None:
    result = evaluate(
        route="GPT_DIRECT", plan={}, answer="연락처는 010-1234-5678 입니다."
    )
    assert result.safe is False
    assert result.decision == "BLOCKED"


# ------------------------------------------------- staff edit -> Learning

@pytest.fixture
def database(tmp_path: Path) -> Database:
    value = Database(tmp_path / "audit-v9.db")
    value.initialize()
    return value


def make_answered_inquiry(database: Database) -> int:
    """An inquiry Naver already reports as answered."""
    return InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": "AUDIT-E2E-1",
            "external_inquiry_id": "AUDIT-E2E-1",
            "inquiry_type": "PRODUCT_INQUIRY",
            "title": "AS 문의",
            "content": "AS는 삼성서비스센터에서 하나요?",
            "product_name": "삼성 TV",
            "source_answered": True,
            "raw_json": {},
        }
    ).inquiry_id


# CASE R -- auto answer A, staff edit B on Naver, operator approves ->
# Learning must hold B.
def test_case_r_staff_edited_answer_is_what_gets_learned(
    database: Database,
) -> None:
    inquiry_id = make_answered_inquiry(database)
    posted = NaverPostedAnswerRepository(database)

    # Sync first sees the answer Q&A Auto posted (A) ...
    posted.observe(
        inquiry_id=inquiry_id, answer_body=AUTO_ANSWER_A,
        answer_id="ANS-1", source_api="TEST_SYNC",
    )
    # ... then staff edit it on Naver and a later sync sees B.
    posted.observe(
        inquiry_id=inquiry_id, answer_body=STAFF_EDITED_B,
        answer_id="ANS-2", source_api="TEST_SYNC",
    )
    assert posted.current(inquiry_id)["answer_body"] == STAFF_EDITED_B

    saved = ApprovalService(database).approve_posted_answer(
        inquiry_id=inquiry_id, actor="관리자",
    )

    assert saved["final_answer"] == STAFF_EDITED_B
    assert saved["final_answer"] != AUTO_ANSWER_A

    stored = LearningRepository(database).get(int(saved["id"]))
    assert stored["final_answer"] == STAFF_EDITED_B


# CASE S -- editing on Naver alone must NOT create Learning. The approval
# button stays the human boundary.
def test_case_s_staff_edit_without_approval_creates_no_learning(
    database: Database,
) -> None:
    inquiry_id = make_answered_inquiry(database)
    posted = NaverPostedAnswerRepository(database)
    posted.observe(
        inquiry_id=inquiry_id, answer_body=AUTO_ANSWER_A,
        answer_id="ANS-1", source_api="TEST_SYNC",
    )
    posted.observe(
        inquiry_id=inquiry_id, answer_body=STAFF_EDITED_B,
        answer_id="ANS-2", source_api="TEST_SYNC",
    )
    # No approve_posted_answer call here.
    assert LearningRepository(database).for_inquiry(inquiry_id) == []
    assert LearningRepository(database).count() == 0


# CASE U -- the original automatic answer must remain traceable after the
# edit, so provenance is not lost by the overwrite.
def test_case_u_original_auto_answer_remains_in_provenance(
    database: Database,
) -> None:
    inquiry_id = make_answered_inquiry(database)
    posted = NaverPostedAnswerRepository(database)
    posted.observe(
        inquiry_id=inquiry_id, answer_body=AUTO_ANSWER_A,
        answer_id="ANS-1", source_api="TEST_SYNC",
    )
    posted.observe(
        inquiry_id=inquiry_id, answer_body=STAFF_EDITED_B,
        answer_id="ANS-2", source_api="TEST_SYNC",
    )
    bodies = [row["answer_body"] for row in posted.history(inquiry_id)]
    assert AUTO_ANSWER_A in bodies
    assert STAFF_EDITED_B in bodies


# CASE T -- revoking the approval removes it from retrieval while keeping
# the row for audit.
def test_case_t_revoked_learning_leaves_retrieval(database: Database) -> None:
    inquiry_id = make_answered_inquiry(database)
    posted = NaverPostedAnswerRepository(database)
    posted.observe(
        inquiry_id=inquiry_id, answer_body=STAFF_EDITED_B,
        answer_id="ANS-2", source_api="TEST_SYNC",
    )
    saved = ApprovalService(database).approve_posted_answer(
        inquiry_id=inquiry_id, actor="관리자",
    )
    repository = LearningRepository(database)
    learning_id = int(saved["id"])

    def in_retrieval() -> bool:
        return any(
            int(item["id"]) == learning_id
            for item in repository.candidates(store_code="OJE_PLUS")
        )

    assert in_retrieval() is True

    repository.revoke_human_verified(
        learning_id=learning_id,
        inquiry_id=inquiry_id,
        reason="잘못 승인했습니다.",
        actor="관리자",
        approval_history_id=0,
    )

    assert in_retrieval() is False
    # Row preserved for audit, just inactive.
    stored = repository.get(learning_id)
    assert stored is not None
    assert bool(stored["active"]) is False


# ------------------------------------------------------- deleted inquiry

# CASE V/W -- an inquiry whose Naver original is gone must never be posted
# to again. Characterization of already-correct behaviour: the error is
# non-retryable and the inquiry drops out of the auto-post candidate set,
# while its rows stay in the DB for internal review.
def test_case_v_deleted_naver_inquiry_is_never_auto_posted(
    database: Database,
) -> None:
    from repositories.auto_post_repository import (
        NON_RETRYABLE_TARGET_ERRORS,
        AutoPostRepository,
    )

    assert "REMOTE_TARGET_NOT_FOUND" in NON_RETRYABLE_TARGET_ERRORS

    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": "DELETED-1",
            "external_inquiry_id": "DELETED-1",
            "inquiry_type": "PRODUCT_INQUIRY",
            "content": "삭제된 문의",
            "raw_json": {},
        }
    ).inquiry_id
    repository = AutoPostRepository(database)
    assert len(repository.candidates(max_retries=1)) == 1

    with database.transaction() as connection:
        connection.execute(
            "UPDATE inquiries SET post_error_code=? WHERE id=?",
            ("REMOTE_TARGET_NOT_FOUND", inquiry_id),
        )

    assert repository.candidates(max_retries=1) == []
    # The inquiry itself is still readable for internal review.
    assert InquiryRepository(database).get(inquiry_id) is not None
