from __future__ import annotations

import itertools

import pytest

from answer.learning_signal import OriginKind, SignalKind
from answer.models import AnswerResult, AnswerStatus
from repositories.answer_repository import AnswerRepository
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_repository import LearningRepository
from repositories.learning_signal_repository import LearningSignalRepository
from repositories.workflow_repository import WorkflowRepository
from services.approval_service import ApprovalService
from services.learning_signal_service import LearningSignalService
from tests.test_learning_feedback import make_context


_question_ids = itertools.count(1)


def _new_context(database):
    """Add one more inquiry+draft to an already-initialized ``database``.

    Unlike ``make_context`` this never re-asserts the migration list (which
    only holds true on a fresh, uninitialized database) and always uses a
    fresh ``source_question_id`` so each call is an independent inquiry
    rather than an upsert onto the same row.
    """

    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": f"REVOKE-{next(_question_ids)}",
            "inquiry_type": "PRODUCT_INQUIRY",
            "title": "배송 문의",
            "content": "언제 설치되나요?",
            "product_name": "삼성 TV",
            "post_status": "NOT_POSTED",
            "raw_json": {},
        }
    ).inquiry_id
    WorkflowRepository(database).initialize_steps(inquiry_id)
    draft = AnswerRepository(database).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.GENERATED,
            category="GENERAL",
            reason="test",
            answer="상품 설명서를 확인해 주세요.",
            provider="rules",
            auto_answerable=True,
            needs_review=False,
        ),
    )
    return inquiry_id, draft


QUESTION = "제주도 배송 설치 가능한가요?"
PROGRAM_ANSWER = "제주도 배송 여부는 확인이 필요합니다."
FINAL_ANSWER = "제주도 배송 및 설치 가능합니다."


@pytest.fixture(autouse=True)
def _auto_learning_env(monkeypatch):
    monkeypatch.setenv("AUTO_STRUCTURED_LEARNING_ENABLED", "true")
    monkeypatch.setenv("AUTO_VERIFIED_FACT_PROMOTION_ENABLED", "true")
    monkeypatch.setenv("AUTO_VERIFIED_FACT_MIN_CONFIRMATIONS", "3")
    yield


def _approve_with_diff(database, tmp_path, *, final_answer=FINAL_ANSWER):
    """Create one fresh inquiry+draft sharing ``database`` and approve it
    with an edit that the diff classifier turns into a CORRECTION/
    VERIFIED_FACT candidate."""

    inquiry_id, draft = _new_context(database)
    ApprovalService(database).save_edited_answer(
        inquiry_id=inquiry_id, draft_id=draft["id"], edited_answer=final_answer,
    )
    outcome = ApprovalService(database).approve(
        inquiry_id=inquiry_id, draft_id=draft["id"], actor="직원",
    )
    return inquiry_id, draft["id"], outcome


def _factual_signal_id(database, inquiry_id) -> int:
    signals = LearningSignalRepository(database).for_inquiry(inquiry_id)
    factual = [s for s in signals if s["signal_kind"] in {"CORRECTION", "VERIFIED_FACT"}]
    assert factual, f"expected a factual candidate for inquiry {inquiry_id}"
    return int(factual[0]["id"])


def test_end_to_end_approval_cancellation_revokes_only_its_own_confirmation(
    tmp_path,
) -> None:
    """Approve -> auto-learn -> cancel approval -> retrieval must reflect
    the revoke precisely (4th-phase mid-turn requirement)."""

    database, inquiry_a, draft_a = make_context(tmp_path)
    ApprovalService(database).save_edited_answer(
        inquiry_id=inquiry_a, draft_id=draft_a["id"], edited_answer=FINAL_ANSWER,
    )
    ApprovalService(database).approve(
        inquiry_id=inquiry_a, draft_id=draft_a["id"], actor="직원",
    )
    signal_id = _factual_signal_id(database, inquiry_a)
    repo = LearningSignalRepository(database)
    assert repo.live_confirmation_count(signal_id) == 1

    inquiry_b, _draft_b_id, _ = _approve_with_diff(database, tmp_path)
    inquiry_c, _draft_c_id, _ = _approve_with_diff(database, tmp_path)
    assert repo.live_confirmation_count(signal_id) == 3

    service = LearningSignalService(database)
    result = service.retrieve("언제 설치되나요?", store_code="OJE_PLUS")
    assert result["verified_facts"] or result["corrections"], (
        "3 independent confirmations with promotion enabled must be eligible"
    )

    # --- Cancel inquiry A's approval ---------------------------------
    positive_before = LearningRepository(database).for_inquiry(inquiry_a)
    learning_id_a = next(row["id"] for row in positive_before if row.get("active"))
    ApprovalService(database).cancel_approval_with_learning(
        inquiry_id=inquiry_a, draft_id=draft_a["id"],
        reason="테스트: 승인 취소", actor="관리자", learning_id=learning_id_a,
    )

    # Positive Learning for A is excluded from runtime candidates, but the
    # row itself is preserved (soft-revoked), not deleted.
    positive_candidates = LearningRepository(database).candidates(store_code="OJE_PLUS")
    assert learning_id_a not in {int(row["id"]) for row in positive_candidates}
    revoked_row = LearningRepository(database).get(learning_id_a)
    assert revoked_row is not None and revoked_row["active"] is False

    # Exactly A's confirmation is deactivated; B and C remain untouched.
    assert repo.live_confirmation_count(signal_id) == 2
    with database.connection() as connection:
        confirmations = connection.execute(
            "SELECT * FROM learning_signal_confirmations WHERE learning_signal_id=?",
            (signal_id,),
        ).fetchall()
    assert len(confirmations) == 3, "no confirmation row is ever deleted (audit history)"
    by_inquiry_a = [
        row for row in confirmations if row["inquiry_id"] == inquiry_a
    ]
    assert by_inquiry_a and by_inquiry_a[0]["active"] == 0
    assert by_inquiry_a[0]["revoked_reason"]

    # With only 2 of the 3 confirmations left, promotion threshold (3) is no
    # longer met -- the signal must drop out of usable evidence again.
    result_after_cancel = service.retrieve(
        "언제 설치되나요?", store_code="OJE_PLUS",
    )
    assert result_after_cancel["verified_facts"] == []
    assert result_after_cancel["corrections"] == []

    # --- Re-approve inquiry A with the same content ------------------
    ApprovalService(database).save_edited_answer(
        inquiry_id=inquiry_a, draft_id=draft_a["id"], edited_answer=FINAL_ANSWER,
    )
    ApprovalService(database).approve(
        inquiry_id=inquiry_a, draft_id=draft_a["id"], actor="직원",
    )
    assert repo.live_confirmation_count(signal_id) == 3, (
        "re-approving must reactivate the existing confirmation row, not "
        "duplicate it"
    )
    with database.connection() as connection:
        confirmations_after = connection.execute(
            "SELECT * FROM learning_signal_confirmations WHERE learning_signal_id=?",
            (signal_id,),
        ).fetchall()
    assert len(confirmations_after) == 3, "re-approval must not create a duplicate row"

    result_after_reapproval = service.retrieve(
        "언제 설치되나요?", store_code="OJE_PLUS",
    )
    assert result_after_reapproval["verified_facts"] or result_after_reapproval["corrections"]


def test_manual_signal_unaffected_by_unrelated_approval_cancellation(tmp_path) -> None:
    """A manually-registered VERIFIED_FACT never depends on the confirmation
    ledger at all, so cancelling an unrelated approval must never touch it."""

    database, inquiry_id, draft = make_context(tmp_path)
    manual_inquiry_id, manual_draft_id = inquiry_id, draft["id"]
    from repositories.inquiry_repository import InquiryRepository
    manual_inquiry = InquiryRepository(database).get(manual_inquiry_id)
    LearningSignalService(database).capture(
        origin_kind=OriginKind.POSITIVE_REVIEW,
        signal_kind=SignalKind.VERIFIED_FACT,
        content_text="운영 확인 결과 제주도 배송 및 설치가 가능합니다.",
        inquiry=manual_inquiry,
        question=QUESTION,
        product_name=manual_inquiry.get("product_name"),
    )

    # An unrelated inquiry is approved and its approval later cancelled.
    other_inquiry_id, other_draft_id, _ = _approve_with_diff(database, tmp_path)
    other_positive = LearningRepository(database).for_inquiry(other_inquiry_id)
    other_learning_id = next(row["id"] for row in other_positive if row.get("active"))
    ApprovalService(database).cancel_approval_with_learning(
        inquiry_id=other_inquiry_id, draft_id=other_draft_id,
        reason="무관한 승인 취소", actor="관리자", learning_id=other_learning_id,
    )

    result = LearningSignalService(database).retrieve(
        "제주도도 배송설치 가능한가요?", store_code="OJE_PLUS",
    )
    assert result["verified_facts"], (
        "a manual signal must stay eligible regardless of unrelated approval "
        "cancellations"
    )
