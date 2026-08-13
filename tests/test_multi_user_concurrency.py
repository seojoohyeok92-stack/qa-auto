from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Callable

import pytest
from streamlit.testing.v1 import AppTest

from answer.answer_provenance import AnswerProvenance
from answer.exceptions import StaleAnswerStateError
from answer.learning_conflict import LearningConflictError
from answer.models import AnswerResult, AnswerStatus
from repositories.answer_repository import AnswerRepository
from repositories.approval_repository import ApprovalRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_feedback_repository import LearningFeedbackRepository
from repositories.learning_repository import LearningRepository
from repositories.naver_posted_answer_repository import NaverPostedAnswerRepository
from repositories.workflow_repository import WorkflowRepository
from services.approval_service import ApprovalLockedError, ApprovalService
from services.learning_feedback_service import LearningFeedbackService
from ui.review_workspace import approval_learning_trace


def _context(tmp_path, name: str):
    database = Database(tmp_path / f"{name}.db")
    database.initialize()
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": f"RACE-{name}",
            "inquiry_type": "PRODUCT_GENERAL",
            "title": "동시 작업 테스트",
            "content": "같은 답변을 동시에 평가합니다.",
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
            answer="저장된 프로그램 답변입니다.",
            provider="rules",
            auto_answerable=True,
            needs_review=False,
        ),
    )
    return database, inquiry_id, draft


def _race(*actions: Callable[[], object]) -> list[object]:
    barrier = Barrier(len(actions))

    def run(action: Callable[[], object]) -> object:
        barrier.wait(timeout=10)
        try:
            return action()
        except Exception as error:  # Results intentionally include the loser.
            return error

    with ThreadPoolExecutor(max_workers=len(actions)) as executor:
        futures = [executor.submit(run, action) for action in actions]
        return [future.result(timeout=20) for future in futures]


def _positive(database: Database, inquiry_id: int, draft_id: int):
    return ApprovalService(database).approve(
        inquiry_id=inquiry_id,
        draft_id=draft_id,
        actor="positive-user",
    )


def _negative(database: Database, inquiry_id: int, draft_id: int):
    return LearningFeedbackService(database).capture_dashboard_negative(
        inquiry_id=inquiry_id,
        original_answer_source=AnswerProvenance.PROGRAM_GENERATED,
        original_answer_reference_id=draft_id,
        correction_reason="FACT_ERROR",
        actor="negative-user",
    )


def _excluded(database: Database, inquiry_id: int, draft_id: int):
    return LearningFeedbackService(database).capture_dashboard_excluded(
        inquiry_id=inquiry_id,
        original_answer_source=AnswerProvenance.PROGRAM_GENERATED,
        original_answer_reference_id=draft_id,
        exclusion_reason="NOT_REUSABLE",
        actor="excluded-user",
    )


@pytest.mark.parametrize("opposite", [_negative, _excluded])
def test_positive_race_allows_only_one_active_evaluation(
    tmp_path, opposite
) -> None:
    database, inquiry_id, draft = _context(
        tmp_path, f"positive-{opposite.__name__}"
    )
    results = _race(
        lambda: _positive(database, inquiry_id, int(draft["id"])),
        lambda: opposite(database, inquiry_id, int(draft["id"])),
    )

    positives = [row for row in LearningRepository(database).for_inquiry(inquiry_id) if row["active"]]
    feedback = [row for row in LearningFeedbackRepository(database).for_inquiry(inquiry_id) if row["active"]]
    assert bool(positives) != bool(feedback)
    assert sum(not isinstance(result, Exception) for result in results) == 1


def test_negative_excluded_race_allows_only_one_active_signal(tmp_path) -> None:
    database, inquiry_id, draft = _context(tmp_path, "negative-excluded")
    results = _race(
        lambda: _negative(database, inquiry_id, int(draft["id"])),
        lambda: _excluded(database, inquiry_id, int(draft["id"])),
    )

    active = [row for row in LearningFeedbackRepository(database).for_inquiry(inquiry_id) if row["active"]]
    primary = {row["learning_signal_type"] for row in active}
    assert not ({"NEGATIVE", "EXCLUDED"} <= primary)
    assert sum(not isinstance(result, Exception) for result in results) == 1


def test_duplicate_positive_race_creates_one_learning_row(tmp_path) -> None:
    database, inquiry_id, draft = _context(tmp_path, "duplicate-positive")
    results = _race(
        lambda: _positive(database, inquiry_id, int(draft["id"])),
        lambda: _positive(database, inquiry_id, int(draft["id"])),
    )
    assert len(LearningRepository(database).for_inquiry(inquiry_id)) == 1
    assert sum(not isinstance(result, Exception) for result in results) == 1


@pytest.mark.parametrize("action", [_negative, _excluded])
def test_duplicate_feedback_race_reuses_source_key(tmp_path, action) -> None:
    database, inquiry_id, draft = _context(
        tmp_path, f"duplicate-{action.__name__}"
    )
    results = _race(
        lambda: action(database, inquiry_id, int(draft["id"])),
        lambda: action(database, inquiry_id, int(draft["id"])),
    )
    rows = LearningFeedbackRepository(database).for_inquiry(inquiry_id)
    expected = 1 if action is _excluded else 1
    assert len([row for row in rows if row["learning_signal_type"] != "INTENT_CORRECTION"]) == expected
    assert all(not isinstance(result, Exception) for result in results)


def test_stale_staff_edit_is_blocked_and_first_content_survives(tmp_path) -> None:
    database, inquiry_id, draft = _context(tmp_path, "stale-edit")
    version = str(draft["updated_at"])
    service = ApprovalService(database)
    service.save_edited_answer(
        inquiry_id=inquiry_id,
        draft_id=int(draft["id"]),
        edited_answer="사용자 A가 저장한 답변",
        expected_updated_at=version,
    )
    with pytest.raises(StaleAnswerStateError):
        service.save_edited_answer(
            inquiry_id=inquiry_id,
            draft_id=int(draft["id"]),
            edited_answer="사용자 B의 오래된 답변",
            expected_updated_at=version,
        )
    assert "사용자 A가 저장한 답변" in AnswerRepository(database).get(
        int(draft["id"])
    )["edited_answer"]


def test_stale_final_answer_write_is_blocked(tmp_path) -> None:
    database, _, draft = _context(tmp_path, "stale-final")
    version = str(draft["updated_at"])
    repository = AnswerRepository(database)
    repository.save_final_answer(
        int(draft["id"]), "사용자 A Final Answer", expected_updated_at=version
    )
    with pytest.raises(StaleAnswerStateError):
        repository.save_final_answer(
            int(draft["id"]),
            "사용자 B stale Final Answer",
            expected_updated_at=version,
        )
    assert "사용자 A Final Answer" in repository.get(int(draft["id"]))[
        "final_answer"
    ]


def test_cancel_then_stale_reapproval_is_blocked(tmp_path) -> None:
    database, inquiry_id, draft = _context(tmp_path, "cancel-race")
    ApprovalService(database).approve(
        inquiry_id=inquiry_id, draft_id=int(draft["id"])
    )
    approved = AnswerRepository(database).get(int(draft["id"]))
    version = str(approved["updated_at"])
    learning_id = LearningRepository(database).for_inquiry(inquiry_id)[0]["id"]
    results = _race(
        lambda: ApprovalService(database).cancel_approval_with_learning(
            inquiry_id=inquiry_id,
            draft_id=int(draft["id"]),
            learning_id=int(learning_id),
            reason="다른 사용자가 승인 취소",
            expected_updated_at=version,
        ),
        lambda: ApprovalService(database).approve(
            inquiry_id=inquiry_id,
            draft_id=int(draft["id"]),
            expected_updated_at=version,
        ),
    )
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert any(
        isinstance(result, (StaleAnswerStateError, ApprovalLockedError))
        for result in results
    )
    assert ApprovalRepository(database).get_inquiry_approval(inquiry_id)[
        "approval_status"
    ] == "PENDING"
    assert LearningRepository(database).get(int(learning_id))["active"] is False


def test_legacy_naver_human_verified_restores_without_approval_history(
    tmp_path,
) -> None:
    database, inquiry_id, _ = _context(tmp_path, "legacy-naver")
    with database.transaction() as connection:
        connection.execute(
            "UPDATE inquiries SET source_answered=1, approval_status='PENDING', approved_at=NULL WHERE id=?",
            (inquiry_id,),
        )
    posted = NaverPostedAnswerRepository(database).observe(
        inquiry_id=inquiry_id,
        answer_body="네이버에 실제 등록된 답변",
        answer_id="ANSWER-LEGACY",
        source_api="TEST_READ_ONLY",
    )
    learning = ApprovalService(database).approve_posted_answer(
        inquiry_id=inquiry_id,
        actor="legacy-verifier",
        positive_reason="CONTENT_ACCURATE",
        positive_note="기존 운영 데이터",
    )
    assert learning["answer_draft_id"] is None
    assert ApprovalRepository(database).history_for_inquiry(inquiry_id) == []

    trace = approval_learning_trace(
        Database(database.path),
        inquiry_id=inquiry_id,
        draft=None,
        approval_state=ApprovalRepository(database).get_inquiry_approval(inquiry_id),
        source_answered=True,
    )
    assert trace["approval_complete"] is True
    assert trace["positive_learning_id"] == learning["id"]
    assert trace["provenance"] == "NAVER_POSTED"
    assert trace["final_reference_id"] == posted["id"]
    assert trace["verified_by"] == "legacy-verifier"

    fresh = AppTest.from_string(
        f'''
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from ui.review_workspace import _render_answer_panel
db=Database(r"{database.path}")
db.initialize()
_render_answer_panel(db, InquiryRepository(db).get({inquiry_id}))
'''
    ).run(timeout=40)
    assert not fresh.exception
    rendered = "\n".join(item.value for item in fresh.markdown)
    assert "승인 완료" in rendered
    assert f"Learning ID: {learning['id']}" in rendered
    assert f"Reference: {posted['id']}" in rendered
    assert "Verified By: legacy-verifier" in rendered

    with pytest.raises(LearningConflictError):
        LearningFeedbackService(database).capture_dashboard_negative(
            inquiry_id=inquiry_id,
            original_answer_source=AnswerProvenance.NAVER_POSTED,
            original_answer_reference_id=int(posted["id"]),
            correction_reason="FACT_ERROR",
        )
    assert LearningFeedbackRepository(database).for_inquiry(inquiry_id) == []


def test_different_staff_answer_identity_can_be_evaluated(tmp_path) -> None:
    database, inquiry_id, draft = _context(tmp_path, "different-identity")
    _negative(database, inquiry_id, int(draft["id"]))
    service = ApprovalService(database)
    service.save_edited_answer(
        inquiry_id=inquiry_id,
        draft_id=int(draft["id"]),
        edited_answer="직원이 새로 작성한 별도 답변",
    )
    service.approve(inquiry_id=inquiry_id, draft_id=int(draft["id"]))
    positive = LearningRepository(database).for_inquiry(inquiry_id)[0]
    assert positive["metadata_json"]["answer_provenance"] == "STAFF_EDITED"
    assert LearningFeedbackRepository(database).for_inquiry(inquiry_id)[0][
        "active"
    ] is True


def test_streamlit_sessions_keep_private_navigation_state() -> None:
    script = '''
import streamlit as st
st.session_state.setdefault("dashboard_page", 1)
st.session_state.setdefault("selected_inquiry_key", "first")
if st.button("next"):
    st.session_state["dashboard_page"] = 5
    st.session_state["selected_inquiry_key"] = "fifth"
st.write(st.session_state["dashboard_page"])
st.write(st.session_state["selected_inquiry_key"])
'''
    user_a = AppTest.from_string(script).run(timeout=20)
    user_b = AppTest.from_string(script).run(timeout=20)
    user_a.button[0].click().run(timeout=20)

    assert user_a.session_state["dashboard_page"] == 5
    assert user_a.session_state["selected_inquiry_key"] == "fifth"
    assert user_b.session_state["dashboard_page"] == 1
    assert user_b.session_state["selected_inquiry_key"] == "first"
