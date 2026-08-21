from __future__ import annotations

from datetime import UTC, datetime

from answer.learning_feedback import CorrectionReason, LearningSignalType
from answer.answer_provenance import AnswerProvenance
from answer.models import AnswerResult, AnswerStatus
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.historical_case_repository import HistoricalCaseRepository
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_feedback_repository import LearningFeedbackRepository
from repositories.learning_repository import LearningRepository
from repositories.workflow_repository import WorkflowRepository
from services.approval_service import ApprovalService
from services.historical_case_service import HistoricalCaseService
from services.learning_feedback_service import LearningFeedbackService


def make_context(tmp_path):
    database = Database(tmp_path / "feedback.db")
    assert database.initialize() == list(range(1, 29))
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": "FEEDBACK-1",
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
    return database, inquiry_id, draft


def make_historical_case(database: Database) -> dict:
    service = HistoricalCaseService(database)
    case = service.prepare_case(
        {
            "store_code": "OJE_PLUS",
            "source_type": "PRODUCT_INQUIRY",
            "external_inquiry_id": "HISTORY-FEEDBACK-1",
            "title": "배송 문의",
            "content": "언제 설치되나요?",
            "seller_answer": "무조건 내일 설치됩니다.",
            "answered": True,
            "source_created_at": datetime.now(UTC).isoformat(),
        },
        source_reference="TEST:feedback",
    )
    saved, _ = service.repository.upsert(case)
    return saved


def test_unchanged_answer_creates_no_correction_learning(tmp_path) -> None:
    database, inquiry_id, draft = make_context(tmp_path)
    saved = LearningFeedbackService(database).capture_staff_correction(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        correction_reason=CorrectionReason.FACT_ERROR,
    )
    assert saved == []
    assert LearningFeedbackRepository(database).for_inquiry(inquiry_id) == []


def test_staff_edit_approval_creates_positive_learning(tmp_path) -> None:
    database, inquiry_id, draft = make_context(tmp_path)
    service = ApprovalService(database)
    service.save_edited_answer(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        edited_answer="현재 주문정보를 확인한 뒤 설치일을 안내드리겠습니다.",
    )
    service.approve(inquiry_id=inquiry_id, draft_id=draft["id"])
    positive = LearningRepository(database).candidates(store_code="OJE_PLUS")
    assert len(positive) == 1
    assert positive[0]["learning_source"] == "APPROVED_EDITED"
    assert positive[0]["metadata_json"]["learning_signal_type"] == "POSITIVE"


def test_staff_edit_approval_apptest_shows_persisted_final_and_learning(
    tmp_path,
) -> None:
    from streamlit.testing.v1 import AppTest

    database, inquiry_id, draft = make_context(tmp_path)
    corrected = "직원이 사실관계를 확인해 수정한 최종 답변입니다."
    ApprovalService(database).save_edited_answer(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        edited_answer=corrected,
        correction_reason=CorrectionReason.FACT_ERROR.value,
    )
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
    approve = next(button for button in app.button if button.label == "승인")
    approve.click()
    app.run(timeout=40)
    assert not app.exception
    assert app.segmented_control[0].value == "Final Answer"
    assert corrected in app.text_area[0].value
    rendered = "\n".join(item.value for item in app.markdown)
    assert "승인 완료" in rendered
    assert "Final Answer: STAFF_EDITED" in rendered
    assert "Positive Learning: 반영 완료" in rendered


def test_fact_error_creates_positive_and_negative_with_note(tmp_path) -> None:
    database, inquiry_id, draft = make_context(tmp_path)
    service = ApprovalService(database)
    service.save_edited_answer(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        edited_answer="주문정보 확인 후 설치일을 안내드리겠습니다.",
        correction_reason=CorrectionReason.FACT_ERROR.value,
        correction_note="주문 확인 없이 일반 답변을 제공함",
        actor="staff-1",
    )
    service.approve(inquiry_id=inquiry_id, draft_id=draft["id"])

    positive = LearningRepository(database).candidates(store_code="OJE_PLUS")
    feedback = LearningFeedbackRepository(database).for_inquiry(inquiry_id)
    assert len(positive) == 1
    assert [item["learning_signal_type"] for item in feedback] == ["NEGATIVE"]
    assert feedback[0]["correction_note"] == "주문 확인 없이 일반 답변을 제공함"


def test_feedback_can_be_saved_after_answer_autosave(tmp_path) -> None:
    database, inquiry_id, draft = make_context(tmp_path)
    service = ApprovalService(database)
    corrected = "주문정보 확인 후 설치일을 안내드리겠습니다."
    service.save_edited_answer(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        edited_answer=corrected,
        autosave=True,
    )
    assert LearningFeedbackRepository(database).for_inquiry(inquiry_id) == []
    service.save_edited_answer(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        edited_answer=corrected,
        correction_reason=CorrectionReason.FACT_ERROR.value,
    )
    assert len(LearningFeedbackRepository(database).for_inquiry(inquiry_id)) == 1


def test_routing_error_creates_negative_and_intent_correction(tmp_path) -> None:
    database, inquiry_id, draft = make_context(tmp_path)
    ApprovalService(database).save_edited_answer(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        edited_answer="주문번호를 확인해 설치 일정을 조회하겠습니다.",
        correction_reason=CorrectionReason.ROUTING_ERROR.value,
        corrected_intent="DELIVERY_INSTALLATION_STATUS",
    )
    feedback = LearningFeedbackRepository(database).for_inquiry(inquiry_id)
    assert {item["learning_signal_type"] for item in feedback} == {
        "NEGATIVE",
        "INTENT_CORRECTION",
    }
    assert {
        item["corrected_intent"] for item in feedback
    } == {"DELIVERY_INSTALLATION_STATUS"}


def test_historical_bad_case_creates_negative_and_is_not_retrievable(
    tmp_path,
) -> None:
    database, _, _ = make_context(tmp_path)
    case = make_historical_case(database)
    saved = LearningFeedbackService(database).capture_historical_review(
        case_id=case["id"],
        correction_reason=CorrectionReason.DELIVERY_INSTALLATION_ERROR,
        correction_note="확정되지 않은 설치일 단정",
    )
    assert [item["learning_signal_type"] for item in saved] == ["NEGATIVE"]
    historical = HistoricalCaseRepository(database).get(case["id"])
    assert historical["active"] is False
    assert historical["metadata_json"]["learning_signal_type"] == "NEGATIVE"
    assert HistoricalCaseService(database).search("언제 설치", store_code="OJE_PLUS") == []


def test_historical_routing_error_creates_intent_correction(tmp_path) -> None:
    database, _, _ = make_context(tmp_path)
    case = make_historical_case(database)
    saved = LearningFeedbackService(database).capture_historical_review(
        case_id=case["id"],
        correction_reason=CorrectionReason.ROUTING_ERROR,
        corrected_intent="DELIVERY_INSTALLATION_STATUS",
    )
    assert {item["learning_signal_type"] for item in saved} == {
        "NEGATIVE",
        "INTENT_CORRECTION",
    }
    historical = HistoricalCaseRepository(database).get(case["id"])
    assert historical["metadata_json"]["learning_signal_type"] == "INTENT_CORRECTION"


def test_historical_exclusion_is_distinct_from_negative(tmp_path) -> None:
    database, _, _ = make_context(tmp_path)
    case = make_historical_case(database)
    saved = LearningFeedbackService(database).capture_historical_review(
        case_id=case["id"],
        correction_reason=CorrectionReason.OTHER,
        correction_note="오염된 데이터",
        excluded=True,
    )
    assert [item["learning_signal_type"] for item in saved] == ["EXCLUDED"]
    historical = HistoricalCaseRepository(database).get(case["id"])
    assert historical["metadata_json"]["learning_signal_type"] == "EXCLUDED"
    assert LearningFeedbackRepository(database).candidates("NEGATIVE") == []


def test_negative_feedback_never_appears_in_positive_candidates(tmp_path) -> None:
    database, inquiry_id, draft = make_context(tmp_path)
    ApprovalService(database).save_edited_answer(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        edited_answer="주문정보 확인 후 안내드리겠습니다.",
        correction_reason=CorrectionReason.FACT_ERROR.value,
    )
    negative = LearningFeedbackRepository(database).candidates(
        LearningSignalType.NEGATIVE.value
    )
    assert len(negative) == 1
    assert LearningRepository(database).candidates(store_code="OJE_PLUS") == []


def test_dashboard_negative_only_keeps_positive_candidates_empty(tmp_path) -> None:
    database, inquiry_id, draft = make_context(tmp_path)
    saved = LearningFeedbackService(database).capture_dashboard_negative(
        inquiry_id=inquiry_id,
        original_answer_source=AnswerProvenance.PROGRAM_GENERATED,
        original_answer_reference_id=draft["id"],
        correction_reason=CorrectionReason.FACT_ERROR,
        correction_note="확인되지 않은 사실을 단정함",
        actor="staff-1",
    )
    assert [row["learning_signal_type"] for row in saved] == ["NEGATIVE"]
    assert saved[0]["original_answer_source"] == "PROGRAM_GENERATED"
    assert saved[0]["original_answer_reference_id"] == draft["id"]
    assert saved[0]["source"] == "DASHBOARD_NEGATIVE_REVIEW"
    assert saved[0]["metadata_json"]["positive_learning_created"] is False
    assert LearningRepository(database).candidates(store_code="OJE_PLUS") == []


def test_dashboard_negative_duplicate_click_reuses_persisted_feedback(
    tmp_path,
) -> None:
    database, inquiry_id, draft = make_context(tmp_path)
    service = LearningFeedbackService(database)
    first = service.capture_dashboard_negative(
        inquiry_id=inquiry_id,
        original_answer_source=AnswerProvenance.PROGRAM_GENERATED,
        original_answer_reference_id=draft["id"],
        correction_reason=CorrectionReason.FACT_ERROR,
        correction_note="최초 메모",
    )
    second = service.capture_dashboard_negative(
        inquiry_id=inquiry_id,
        original_answer_source=AnswerProvenance.PROGRAM_GENERATED,
        original_answer_reference_id=draft["id"],
        correction_reason=CorrectionReason.FACT_ERROR,
        correction_note="최종 메모",
    )

    assert [row["id"] for row in second] == [row["id"] for row in first]
    persisted = LearningFeedbackRepository(
        database
    ).active_dashboard_evaluation(
        inquiry_id=inquiry_id,
        original_answer_source="PROGRAM_GENERATED",
        original_answer_reference_id=draft["id"],
    )
    assert len(persisted) == 1
    assert persisted[0]["correction_note"] == "최종 메모"


def test_dashboard_routing_negative_creates_intent_correction(tmp_path) -> None:
    database, inquiry_id, draft = make_context(tmp_path)
    saved = LearningFeedbackService(database).capture_dashboard_negative(
        inquiry_id=inquiry_id,
        original_answer_source="PROGRAM_GENERATED",
        original_answer_reference_id=draft["id"],
        correction_reason=CorrectionReason.ROUTING_ERROR,
        corrected_intent="DELIVERY_INSTALLATION_STATUS",
    )
    assert {row["learning_signal_type"] for row in saved} == {
        "NEGATIVE",
        "INTENT_CORRECTION",
    }
    assert {row["corrected_intent"] for row in saved} == {
        "DELIVERY_INSTALLATION_STATUS"
    }


def test_dashboard_negative_rejects_mismatched_reference(tmp_path) -> None:
    import pytest

    database, inquiry_id, draft = make_context(tmp_path)
    with pytest.raises(LookupError):
        LearningFeedbackService(database).capture_dashboard_negative(
            inquiry_id=inquiry_id,
            original_answer_source="PROGRAM_GENERATED",
            original_answer_reference_id=int(draft["id"]) + 999,
            correction_reason=CorrectionReason.FACT_ERROR,
        )


def test_dashboard_and_history_share_correction_taxonomy() -> None:
    import ui.historical_case_manager as historical_ui
    import ui.review_workspace as dashboard_ui

    assert (
        dashboard_ui.CORRECTION_REASON_LABELS
        is historical_ui.CORRECTION_REASON_LABELS
    )
    assert dashboard_ui.INTENT_OPTIONS is historical_ui.INTENT_OPTIONS


def test_dashboard_negative_apptest_saves_selected_program_answer(
    tmp_path,
) -> None:
    from streamlit.testing.v1 import AppTest

    database, inquiry_id, draft = make_context(tmp_path)
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
    app.segmented_control[0].set_value("Program Answer")
    app.run(timeout=40)
    reason = next(item for item in app.selectbox if item.label == "잘못된 이유")
    reason.set_value("사실 오류")
    app.run(timeout=40)
    note = next(
        item
        for item in app.text_input
        if item.label == "Negative 상세 메모 (선택)"
    )
    note.set_value("확인되지 않은 배송일을 단정함")
    app.run(timeout=40)
    save = next(
        button for button in app.button
        if button.label == "Negative Learning 저장"
    )
    assert save.disabled is False
    save.click()
    app.run(timeout=40)
    assert not app.exception
    feedback = LearningFeedbackRepository(database).for_inquiry(inquiry_id)
    assert len(feedback) == 1
    assert feedback[0]["learning_signal_type"] == "NEGATIVE"
    assert feedback[0]["original_answer_source"] == "PROGRAM_GENERATED"
    assert feedback[0]["original_answer_reference_id"] == draft["id"]
    assert LearningRepository(database).candidates(store_code="OJE_PLUS") == []
    rendered = "\n".join(item.value for item in app.markdown)
    for expected in (
        "Negative Learning 저장 완료",
        f"Feedback ID <b>{feedback[0]['id']}</b>",
        "사실 오류 (FACT_ERROR)",
        "확인되지 않은 배송일을 단정함",
        "평가 Answer provenance <b>PROGRAM_GENERATED</b>",
        f"Reference <b>{draft['id']}</b>",
        f"Learning Manager 검색 · Inquiry {inquiry_id}",
        "저장 시각",
    ):
        assert expected in rendered

    fresh_app = AppTest.from_string(
        f'''
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from ui.review_workspace import _render_answer_panel
db=Database(r"{database.path}")
db.initialize()
_render_answer_panel(db, InquiryRepository(db).get({inquiry_id}))
'''
    ).run(timeout=40)
    assert not fresh_app.exception
    fresh_rendered = "\n".join(item.value for item in fresh_app.markdown)
    assert "Negative Learning 저장 완료" in fresh_rendered
    assert f"Feedback ID <b>{feedback[0]['id']}</b>" in fresh_rendered
    assert "확인되지 않은 배송일을 단정함" in fresh_rendered


def test_feedback_migration_is_idempotent_and_legacy_rows_remain_positive(
    tmp_path,
) -> None:
    database, inquiry_id, draft = make_context(tmp_path)
    ApprovalService(database).approve(
        inquiry_id=inquiry_id, draft_id=draft["id"]
    )
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE learning_examples
            SET metadata_json=json_remove(metadata_json, '$.learning_signal_type')
            """
        )
    assert database.initialize() == []
    assert database.migration_versions() == list(range(1, 29))
    assert len(LearningRepository(database).candidates(store_code="OJE_PLUS")) == 1
