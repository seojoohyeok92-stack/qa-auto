from __future__ import annotations

from datetime import UTC, datetime, timedelta

from streamlit.testing.v1 import AppTest

from answer.learning_feedback import CorrectionReason
from answer.models import AnswerResult, AnswerStatus
from config import NaverSyncSettings, StoreConfig
from repositories.answer_repository import AnswerRepository
from repositories.approval_repository import ApprovalRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_feedback_repository import LearningFeedbackRepository
from repositories.learning_repository import LearningRepository
from repositories.naver_posted_answer_repository import (
    NaverPostedAnswerRepository,
)
from services.approval_service import ApprovalService
from services.automatic_draft_service import AutomaticDraftService
from services.naver_inquiry_sync_service import NaverInquirySyncService
from ui.review_workspace import approval_learning_trace


def _settings() -> NaverSyncSettings:
    return NaverSyncSettings(
        enabled=True,
        lookback_days=7,
        page_size=100,
        max_pages=2,
        connect_timeout=1.0,
        read_timeout=2.0,
        max_retries=0,
        retry_backoff_seconds=0.0,
        max_runtime_seconds=30.0,
        lock_ttl_seconds=60,
    )


def _payload(*, answered: bool, answer: str | None = None) -> dict:
    value = {
        "questionId": "TRUTH-1",
        "question": "언제 설치되나요?",
        "productName": "테스트 TV",
        "answered": answered,
        "status": "ANSWERED" if answered else "WAITING",
        "createDate": "2026-08-10T09:00:00+09:00",
        "updateDate": "2026-08-10T10:00:00+09:00",
    }
    if answer is not None:
        value.update(
            {
                "sellerAnswer": answer,
                "answerContentId": "ANSWER-100",
                "answerRegistrationDateTime": "2026-08-10T09:30:00+09:00",
            }
        )
    return value


def _sync(database: Database, payload: dict) -> None:
    service = NaverInquirySyncService(
        database,
        settings=_settings(),
        token_provider=lambda **kwargs: "read-token",
        product_fetch=lambda **kwargs: {
            "contents": [payload],
            "totalPages": 1,
            "totalElements": 1,
            "last": True,
        },
        customer_fetch=lambda **kwargs: {
            "content": [], "totalPages": 1, "last": True,
        },
    )
    end = datetime(2026, 8, 11, tzinfo=UTC)
    result = service.sync_inquiries(
        stores=[StoreConfig("STORE", "스토어", "client", "secret", True)],
        inquiry_types=["PRODUCT_INQUIRY"],
        from_datetime=end - timedelta(days=7),
        to_datetime=end,
    )
    assert result.status == "SUCCESS"


def _database(tmp_path) -> Database:
    database = Database(tmp_path / "posted-truth.db")
    assert database.initialize() == list(range(1, 24))
    return database


def _draft(database: Database, inquiry_id: int) -> dict:
    return AnswerRepository(database).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.GENERATED,
            category="GENERAL",
            reason="test",
            answer="Program Answer는 제품 설명서를 확인하라고 안내합니다.",
            provider="rules",
            auto_answerable=True,
            needs_review=False,
        ),
    )


def _answered_with_existing_draft(tmp_path):
    database = _database(tmp_path)
    _sync(database, _payload(answered=False))
    inquiry = InquiryRepository(database).list()[0]
    draft = _draft(database, int(inquiry["id"]))
    _sync(
        database,
        _payload(answered=True, answer="네이버에 실제 등록된 답변입니다."),
    )
    return database, int(inquiry["id"]), draft


def test_answered_sync_persists_available_naver_answer(tmp_path) -> None:
    database = _database(tmp_path)
    _sync(database, _payload(answered=True, answer="실제 고객 답변"))
    inquiry = InquiryRepository(database).list()[0]
    posted = NaverPostedAnswerRepository(database).current(inquiry["id"])
    assert posted is not None
    assert posted["answer_body"] == "실제 고객 답변"
    assert posted["answer_id"] == "ANSWER-100"
    assert posted["posted_at"] == "2026-08-10T09:30:00+09:00"
    assert posted["fetch_status"] == "AVAILABLE"
    assert posted["provenance"] == "NAVER_POSTED"


def test_program_and_posted_answers_are_preserved_separately(tmp_path) -> None:
    database, inquiry_id, draft = _answered_with_existing_draft(tmp_path)
    assert "Program Answer" in AnswerRepository(database).get(
        draft["id"]
    )["original_answer"]
    posted = NaverPostedAnswerRepository(database).current(inquiry_id)
    assert posted["answer_body"] == "네이버에 실제 등록된 답변입니다."
    assert len(AnswerRepository(database).history_for_inquiry(inquiry_id)) == 1


def test_answered_without_body_is_not_filled_from_program_answer(tmp_path) -> None:
    database = _database(tmp_path)
    _sync(database, _payload(answered=False))
    inquiry_id = InquiryRepository(database).list()[0]["id"]
    _draft(database, inquiry_id)
    _sync(database, _payload(answered=True))
    posted = NaverPostedAnswerRepository(database).current(inquiry_id)
    assert posted["fetch_status"] == "NOT_FETCHED"
    assert posted["answer_body"] is None


def test_body_omission_on_later_sync_does_not_erase_known_truth(tmp_path) -> None:
    database = _database(tmp_path)
    _sync(database, _payload(answered=True, answer="보존할 실제 답변"))
    inquiry_id = InquiryRepository(database).list()[0]["id"]
    _sync(database, _payload(answered=True))
    posted = NaverPostedAnswerRepository(database).current(inquiry_id)
    assert posted["fetch_status"] == "AVAILABLE"
    assert posted["answer_body"] == "보존할 실제 답변"
    assert len(NaverPostedAnswerRepository(database).history(inquiry_id)) == 1


def test_answered_inquiry_never_promotes_existing_program_draft(tmp_path) -> None:
    database, inquiry_id, draft = _answered_with_existing_draft(tmp_path)
    outcome = AutomaticDraftService(database).ensure_for_inquiry(inquiry_id)
    assert outcome.status == "SKIPPED_ALREADY_ANSWERED"
    assert "Program Answer" in AnswerRepository(database).get(
        draft["id"]
    )["original_answer"]


def test_posted_answer_staff_correction_has_precise_learning_provenance(
    tmp_path,
) -> None:
    database, inquiry_id, draft = _answered_with_existing_draft(tmp_path)
    ApprovalService(database).save_edited_answer(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        edited_answer="주문정보를 확인한 뒤 설치일을 안내드리겠습니다.",
        correction_reason=CorrectionReason.FACT_ERROR.value,
        correction_note="실제 게시 답변의 설치일 안내가 부정확함",
        actor="staff-1",
    )
    feedback = LearningFeedbackRepository(database).for_inquiry(inquiry_id)
    assert len(feedback) == 1
    assert feedback[0]["original_answer_source"] == "NAVER_POSTED"
    assert feedback[0]["original_answer_reference_id"] is not None
    assert feedback[0]["metadata_json"]["evaluated_answer_provenance"] == (
        "NAVER_POSTED"
    )
    positive = LearningRepository(database).candidates(store_code="STORE")
    assert len(positive) == 2  # synchronized seller answer + staff correction
    correction = next(
        item
        for item in positive
        if item["metadata_json"].get("answer_provenance") == "STAFF_EDITED"
    )
    assert correction["posted"] is False
    assert correction["metadata_json"]["customer_truth_remains_naver_posted"]


def test_unanswered_staff_edit_keeps_program_generated_provenance(tmp_path) -> None:
    database = _database(tmp_path)
    _sync(database, _payload(answered=False))
    inquiry_id = InquiryRepository(database).list()[0]["id"]
    draft = _draft(database, inquiry_id)
    ApprovalService(database).save_edited_answer(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        edited_answer="직원이 수정한 답변",
        correction_reason=CorrectionReason.FACT_ERROR.value,
    )
    feedback = LearningFeedbackRepository(database).for_inquiry(inquiry_id)
    assert feedback[0]["original_answer_source"] == "PROGRAM_GENERATED"


def test_answered_workspace_defaults_to_naver_truth_and_keeps_program_tab(
    tmp_path,
) -> None:
    database, inquiry_id, _ = _answered_with_existing_draft(tmp_path)
    app = AppTest.from_string(
        f'''
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from ui.review_workspace import _render_answer_panel
db=Database(r"{database.path}")
db.initialize()
_render_answer_panel(db, InquiryRepository(db).get({inquiry_id}))
'''
    ).run(timeout=40)
    assert not app.exception
    assert app.segmented_control[0].options == [
        "Program Answer", "직원 수정본", "네이버 실제 등록 답변", "Final Answer"
    ]
    assert app.text_area[0].value == "네이버에 실제 등록된 답변입니다."
    captions = "\n".join(item.value for item in app.caption)
    assert "NAVER_POSTED" in captions
    assert "Source of Truth" in captions
    assert "답변 ID ANSWER-100" in captions
    app.segmented_control[0].set_value("Program Answer")
    app.run(timeout=40)
    assert "Program Answer" in app.text_area[0].value


def test_unedited_naver_answer_approval_promotes_only_posted_truth(
    tmp_path,
) -> None:
    database, inquiry_id, draft = _answered_with_existing_draft(tmp_path)
    before = LearningRepository(database).candidates(store_code="STORE")
    assert len(before) == 1
    assert before[0]["metadata_json"]["answer_provenance"] == "NAVER_POSTED"
    assert not before[0]["metadata_json"].get("human_verified")

    app = AppTest.from_string(
        f'''
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from ui.review_workspace import _render_answer_panel
db=Database(r"{database.path}")
db.initialize()
_render_answer_panel(db, InquiryRepository(db).get({inquiry_id}))
'''
    ).run(timeout=40)
    assert not app.exception
    assert app.segmented_control[0].value == "네이버 실제 등록 답변"
    assert app.text_area[0].value == "네이버에 실제 등록된 답변입니다."
    approve = next(button for button in app.button if button.label == "승인")
    assert approve.disabled is False
    approve.click()
    app.run(timeout=40)
    assert not app.exception
    assert app.segmented_control[0].value == "Final Answer"
    assert app.text_area[0].value == "네이버에 실제 등록된 답변입니다."
    rendered = "\n".join(item.value for item in app.markdown)
    captions = "\n".join(item.value for item in app.caption)
    assert "approval-result-card" in rendered
    assert "승인 완료" in rendered
    assert "Final Answer: NAVER_POSTED" in rendered
    assert "Positive Learning: 반영 완료" in rendered
    assert "Human Verified: YES" in rendered
    assert "Learning에서 확인" in captions

    positive = LearningRepository(database).candidates(store_code="STORE")
    assert len(positive) == 1
    verified = positive[0]
    assert verified["final_answer"] == "네이버에 실제 등록된 답변입니다."
    assert verified["answer_draft_id"] is None
    assert verified["metadata_json"]["answer_provenance"] == "NAVER_POSTED"
    assert verified["metadata_json"]["human_verified"] is True
    assert verified["metadata_json"]["customer_facing_truth"] is True
    assert "Program Answer" not in verified["final_answer"]
    assert AnswerRepository(database).get(draft["id"]) is not None


def test_posted_staff_correction_approval_keeps_naver_truth_and_staff_source(
    tmp_path,
) -> None:
    database, inquiry_id, draft = _answered_with_existing_draft(tmp_path)
    posted_before = NaverPostedAnswerRepository(database).current(inquiry_id)
    saved = ApprovalService(database).approve_posted_staff_correction(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        edited_answer="직원이 내부 학습용으로 교정한 답변입니다.",
        correction_reason=CorrectionReason.FACT_ERROR.value,
        correction_note="실제 사실관계 교정",
        actor="staff-1",
    )
    assert saved["metadata_json"]["answer_provenance"] == "STAFF_EDITED"
    assert saved["metadata_json"]["human_verified"] is True
    assert "직원이 내부 학습용으로 교정한 답변입니다." in saved["final_answer"]
    assert saved["posted"] is False
    posted_after = NaverPostedAnswerRepository(database).current(inquiry_id)
    assert posted_after["id"] == posted_before["id"]
    assert posted_after["answer_body"] == posted_before["answer_body"]
    feedback = LearningFeedbackRepository(database).for_inquiry(inquiry_id)
    assert [row["learning_signal_type"] for row in feedback] == ["NEGATIVE"]
    assert feedback[0]["original_answer_source"] == "NAVER_POSTED"


def test_posted_staff_edit_is_not_shown_as_approved_before_explicit_approval(
    tmp_path,
) -> None:
    database, inquiry_id, draft = _answered_with_existing_draft(tmp_path)
    service = ApprovalService(database)
    corrected = "승인 전 내부 수정본입니다."
    service.save_edited_answer(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        edited_answer=corrected,
        correction_reason=CorrectionReason.FACT_ERROR.value,
    )
    current_draft = AnswerRepository(database).get(draft["id"])
    state = ApprovalRepository(database).get_inquiry_approval(inquiry_id)
    before = approval_learning_trace(
        database,
        inquiry_id=inquiry_id,
        draft=current_draft,
        approval_state=state,
        source_answered=True,
    )
    assert before["approval_complete"] is False
    service.approve_posted_staff_correction(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        edited_answer=corrected,
    )
    after = approval_learning_trace(
        database,
        inquiry_id=inquiry_id,
        draft=AnswerRepository(database).get(draft["id"]),
        approval_state=state,
        source_answered=True,
    )
    assert after["approval_complete"] is True
    assert after["provenance"] == "STAFF_EDITED"
    assert after["human_verified"] is True


def test_posted_answer_migration_is_idempotent(tmp_path) -> None:
    database = _database(tmp_path)
    assert database.initialize() == []
    assert database.migration_versions() == list(range(1, 24))
