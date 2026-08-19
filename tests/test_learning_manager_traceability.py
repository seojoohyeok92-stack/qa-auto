from __future__ import annotations

from streamlit.testing.v1 import AppTest

from answer.learning_feedback import CorrectionReason
from answer.models import AnswerResult, AnswerStatus
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_feedback_repository import LearningFeedbackRepository
from repositories.learning_repository import LearningRepository
from services.approval_service import ApprovalService
from services.learning_feedback_service import LearningFeedbackService
from ui.learning_manager import (
    DEFAULT_COLUMNS,
    _display_row,
    _filter_rows,
    _inquiry_type_label,
    _learning_status_label,
    _paginate_rows,
)


def _approved_staff_learning(
    tmp_path,
    *,
    database=None,
    name="1",
    source_created_at="2026-08-19T00:12:00Z",
    registered_at="2026-08-19T00:12:00Z",
):
    database = database or Database(tmp_path / "learning-manager.db")
    database.initialize()
    source_question_id = f"TRACE-APPROVAL-{name}"
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "STORE",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": source_question_id,
            "external_inquiry_id": f"EXTERNAL-{name}",
            "source_created_at": source_created_at,
            "registered_at": registered_at,
            "inquiry_type": "PRODUCT_GENERAL",
            "title": f"승인 건 추적 문의 {name}",
            "content": f"학습 매니저에서 이 문의 {name}을 찾을 수 있나요?",
            "product_name": f"운영 상품 {name}",
            "post_status": "NOT_POSTED",
            "raw_json": {},
        }
    ).inquiry_id
    draft = AnswerRepository(database).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.GENERATED,
            category="GENERAL",
            reason="test",
            answer=f"Program Answer 원본 {name}",
            provider="rules",
            auto_answerable=True,
            needs_review=False,
        ),
    )
    service = ApprovalService(database)
    service.save_edited_answer(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        edited_answer=f"직원이 검증하고 수정한 답변 {name}",
        actor="staff-trace",
    )
    service.approve(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        actor="staff-trace",
    )
    return database, inquiry_id, draft


def test_learning_manager_rows_expose_approval_trace_fields(tmp_path) -> None:
    database, inquiry_id, draft = _approved_staff_learning(tmp_path)
    rows = LearningRepository(database).manager_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["inquiry_id"] == inquiry_id
    assert row["answer_draft_id"] == draft["id"]
    assert row["learning_source"] == "APPROVED_EDITED"
    assert row["provenance"] == "STAFF_EDITED"
    assert row["signal_type"] == "POSITIVE"
    assert row["human_verified"] is True
    assert row["source_question_id"] == "TRACE-APPROVAL-1"
    assert row["external_inquiry_id"] == "EXTERNAL-1"
    assert row["inquiry_source_type"] == "PRODUCT_INQUIRY"
    assert row["inquiry_occurred_at"] == "2026-08-19T00:12:00Z"
    assert row["created_at"]


def test_learning_manager_searches_inquiry_reference_and_provenance(
    tmp_path,
) -> None:
    database, inquiry_id, _ = _approved_staff_learning(tmp_path)
    rows = LearningRepository(database).manager_rows()
    assert _filter_rows(rows, query=str(inquiry_id)) == rows
    assert _filter_rows(rows, query="승인 건 추적 문의") == rows
    assert _filter_rows(rows, query="TRACE-APPROVAL-1") == rows
    assert _filter_rows(rows, query="EXTERNAL-1") == rows
    assert _filter_rows(rows, query="운영 상품 1") == rows
    assert _filter_rows(rows, provenance="STAFF_EDITED") == rows
    assert _filter_rows(rows, human_verified="YES") == rows
    assert _filter_rows(rows, signal_type="NEGATIVE") == []


def test_learning_manager_separates_negative_from_positive_grid(tmp_path) -> None:
    database, inquiry_id, draft = _approved_staff_learning(tmp_path)
    LearningFeedbackService(database).capture_dashboard_negative(
        inquiry_id=inquiry_id,
        original_answer_source="PROGRAM_GENERATED",
        original_answer_reference_id=draft["id"],
        correction_reason=CorrectionReason.FACT_ERROR,
        correction_note="잘못된 사실",
    )
    positive = LearningRepository(database).manager_rows()
    feedback = LearningFeedbackRepository(database).manager_rows()
    assert len(positive) == 1
    assert positive[0]["signal_type"] == "POSITIVE"
    assert len(feedback) == 1
    assert feedback[0]["learning_signal_type"] == "NEGATIVE"
    assert _filter_rows(positive, signal_type="NEGATIVE") == []


def test_manager_rows_sort_by_source_created_at_with_stable_id_tiebreak(
    tmp_path,
) -> None:
    database = Database(tmp_path / "learning-manager-sort.db")
    database.initialize()
    _, old_inquiry_id, _ = _approved_staff_learning(
        tmp_path,
        database=database,
        name="old",
        source_created_at="2026-08-17T00:00:00Z",
        registered_at="2026-08-20T00:00:00Z",
    )
    _, _, _ = _approved_staff_learning(
        tmp_path,
        database=database,
        name="tie-low",
        source_created_at="2026-08-19T00:00:00Z",
    )
    _, _, _ = _approved_staff_learning(
        tmp_path,
        database=database,
        name="tie-high",
        source_created_at="2026-08-19T00:00:00Z",
    )
    with database.transaction() as connection:
        connection.execute(
            "UPDATE learning_examples SET updated_at='2030-01-01T00:00:00Z' "
            "WHERE inquiry_id=?",
            (old_inquiry_id,),
        )

    rows = LearningRepository(database).manager_rows()
    assert [row["source_question_id"] for row in rows] == [
        "TRACE-APPROVAL-tie-high",
        "TRACE-APPROVAL-tie-low",
        "TRACE-APPROVAL-old",
    ]


def test_manager_rows_sort_with_registered_and_learning_created_fallbacks(
    tmp_path,
) -> None:
    database = Database(tmp_path / "learning-manager-fallback.db")
    database.initialize()
    _, registered_id, _ = _approved_staff_learning(
        tmp_path,
        database=database,
        name="registered",
        source_created_at=None,
        registered_at="2026-08-18T00:00:00Z",
    )
    _, created_id, _ = _approved_staff_learning(
        tmp_path,
        database=database,
        name="created",
        source_created_at=None,
        registered_at=None,
    )
    with database.transaction() as connection:
        connection.execute(
            "UPDATE inquiries SET source_created_at=NULL WHERE id=?",
            (registered_id,),
        )
        connection.execute(
            "UPDATE inquiries SET source_created_at=NULL, registered_at=NULL "
            "WHERE id=?",
            (created_id,),
        )
        connection.execute(
            "UPDATE learning_examples SET created_at='2026-08-17T00:00:00Z', "
            "updated_at='2030-01-01T00:00:00Z' WHERE inquiry_id=?",
            (created_id,),
        )

    rows = LearningRepository(database).manager_rows()
    assert [row["source_question_id"] for row in rows] == [
        "TRACE-APPROVAL-registered",
        "TRACE-APPROVAL-created",
    ]
    assert rows[0]["inquiry_occurred_at"] == "2026-08-18T00:00:00Z"
    assert rows[1]["inquiry_occurred_at"] == "2026-08-17T00:00:00Z"


def test_feedback_rows_follow_real_inquiry_time_order(tmp_path) -> None:
    database = Database(tmp_path / "learning-manager-feedback-sort.db")
    database.initialize()
    created: list[tuple[int, int]] = []
    for name, timestamp in (("feedback-old", "2026-08-17T00:00:00Z"),
                            ("feedback-new", "2026-08-19T00:00:00Z")):
        _, inquiry_id, draft = _approved_staff_learning(
            tmp_path,
            database=database,
            name=name,
            source_created_at=timestamp,
        )
        LearningFeedbackService(database).capture_dashboard_negative(
            inquiry_id=inquiry_id,
            original_answer_source="PROGRAM_GENERATED",
            original_answer_reference_id=draft["id"],
            correction_reason=CorrectionReason.FACT_ERROR,
            correction_note=f"{name} 사실 오류",
        )
        created.append((inquiry_id, draft["id"]))
    with database.transaction() as connection:
        connection.execute(
            "UPDATE learning_feedback SET updated_at='2030-01-01T00:00:00Z' "
            "WHERE inquiry_id=?",
            (created[0][0],),
        )

    rows = LearningFeedbackRepository(database).manager_rows()
    assert [row["source_question_id"] for row in rows] == [
        "TRACE-APPROVAL-feedback-new",
        "TRACE-APPROVAL-feedback-old",
    ]


def test_operator_labels_cover_status_type_number_and_kst() -> None:
    human_verified = {
        "learning_source": "APPROVED_UNEDITED",
        "validator_result": "HUMAN_VERIFIED_NAVER_POSTED",
        "active": True,
    }
    assert _learning_status_label(human_verified) == "Positive 승인"
    assert _learning_status_label(
        {"learning_signal_type": "NEGATIVE", "active": True}
    ) == "Negative"
    assert _learning_status_label(
        {"learning_signal_type": "EXCLUDED", "active": True}
    ) == "학습 제외"
    assert _learning_status_label({"active": True}) == "미평가"
    assert _learning_status_label(
        {"signal_type": "POSITIVE", "active": False}
    ) == "Positive 승인 취소"
    assert _learning_status_label(
        {"learning_signal_type": "NEGATIVE", "active": False}
    ) == "Negative 취소"
    assert _learning_status_label(
        {"learning_signal_type": "EXCLUDED", "active": False}
    ) == "학습 제외 취소"
    assert _inquiry_type_label({"inquiry_source_type": "PRODUCT_INQUIRY"}) == "상품문의"
    assert _inquiry_type_label({"inquiry_source_type": "CUSTOMER_INQUIRY"}) == "고객문의"
    assert _inquiry_type_label({"inquiry_source_type": "PARTNER_QNA"}) == "PARTNER_QNA"

    display = _display_row(
        {
            **human_verified,
            "source_created_at": "2026-08-19T00:12:00Z",
            "source_question_id": "685073788",
            "external_inquiry_id": "324865954",
            "inquiry_source_type": "PRODUCT_INQUIRY",
            "question_original_masked": "질문 원문",
            "final_answer": "답변 원문",
        }
    )
    assert tuple(display) == DEFAULT_COLUMNS
    assert display["문의일시"] == "2026-08-19 09:12"
    assert display["네이버 문의번호"] == "685073788"
    assert display["문의유형"] == "상품문의"
    fallback = _display_row(
        {**human_verified, "external_inquiry_id": "324865954"}
    )
    assert fallback["네이버 문의번호"] == "324865954"


def test_learning_manager_pagination_is_stable_and_has_no_duplicates() -> None:
    rows = [{"id": value} for value in range(45, 0, -1)]
    first, first_page, total_pages = _paginate_rows(rows, 1, 20)
    second, second_page, _ = _paginate_rows(rows, 2, 20)
    assert (first_page, second_page, total_pages) == (1, 2, 3)
    assert [row["id"] for row in first] == list(range(45, 25, -1))
    assert [row["id"] for row in second] == list(range(25, 5, -1))
    assert {row["id"] for row in first}.isdisjoint(
        {row["id"] for row in second}
    )


def test_learning_manager_apptest_explains_role_and_renders_trace(tmp_path) -> None:
    database, inquiry_id, draft = _approved_staff_learning(tmp_path)
    LearningFeedbackService(database).capture_dashboard_negative(
        inquiry_id=inquiry_id,
        original_answer_source="PROGRAM_GENERATED",
        original_answer_reference_id=draft["id"],
        correction_reason=CorrectionReason.FACT_ERROR,
        correction_note="운영 화면 Negative 표시 검증",
    )
    app = AppTest.from_string(
        f'''
from repositories.database import Database
from ui.learning_manager import render_learning_manager
db=Database(r"{database.path}")
db.initialize()
render_learning_manager(db)
'''
    ).run(timeout=40)
    assert not app.exception
    rendered = "\n".join(
        item.value for item in [*app.title, *app.subheader, *app.caption]
    )
    assert "Learning Manager" in rendered
    assert "조회하고" in rendered and "추적" in rendered
    labels = {metric.label for metric in app.metric}
    assert {
        "저장된 Positive",
        "활성 Positive",
        "Human Verified",
        "Negative",
        "Intent Correction",
    } <= labels
    assert app.text_input[0].label == "문의/참조 검색"
    assert app.dataframe
    table = app.dataframe[0].value
    assert tuple(table.columns) == DEFAULT_COLUMNS
    assert "Learning ID" not in table.columns
    assert "문의 ID" not in table.columns
    assert "Draft ID" not in table.columns
    rendered_table = table.to_string()
    assert "TRACE-APPROVAL-1" in rendered_table
    assert "2026-08-19 09:12" in rendered_table
    assert "Positive 승인" in rendered_table
    assert "상품문의" in rendered_table
    assert "STAFF_EDITED" not in rendered_table
    feedback_table = app.dataframe[1].value
    assert tuple(feedback_table.columns) == DEFAULT_COLUMNS
    assert "Negative" in feedback_table.to_string()


def test_learning_manager_search_reruns_keep_learning_route(tmp_path) -> None:
    database, _, _ = _approved_staff_learning(tmp_path)
    app = AppTest.from_string(
        f'''
import streamlit as st
from repositories.database import Database
from ui.learning_manager import render_learning_manager
st.session_state.setdefault("current_page", "learning")
st.session_state.setdefault("production_admin_mode", True)
if st.session_state.get("current_page") == "learning":
    db=Database(r"{database.path}")
    db.initialize()
    render_learning_manager(db)
else:
    st.title("Dashboard Home")
'''
    ).run(timeout=40)
    query = next(item for item in app.text_input if item.label == "문의/참조 검색")
    query.set_value("TRACE-APPROVAL-1")
    app = app.run(timeout=40)
    assert not app.exception
    assert app.session_state["current_page"] == "learning"
    assert app.title[0].value == "Learning Manager"

    query = next(item for item in app.text_input if item.label == "문의/참조 검색")
    query.set_value("")
    app = app.run(timeout=40)
    assert not app.exception
    assert app.session_state["current_page"] == "learning"
    assert app.title[0].value == "Learning Manager"


def test_learning_detail_selection_uses_stable_learning_id(tmp_path) -> None:
    database = Database(tmp_path / "learning-detail-selection.db")
    database.initialize()
    for name in ("first", "second", "third"):
        _approved_staff_learning(tmp_path, database=database, name=name)
    rows = LearningRepository(database).manager_rows()
    selected_id = int(rows[1]["id"])
    selected_number = rows[1]["source_question_id"]
    app = AppTest.from_string(
        f'''
from repositories.database import Database
from ui.learning_manager import render_learning_manager
db=Database(r"{database.path}")
db.initialize()
render_learning_manager(db)
'''
    ).run(timeout=40)
    detail = next(
        item for item in app.selectbox
        if item.label == "상세 조회 항목"
        and item.key == "learning_manager_positive_selected_id"
    )
    detail.set_value(selected_id)
    app = app.run(timeout=40)
    assert not app.exception
    assert app.session_state["learning_manager_positive_selected_id"] == selected_id
    captions = "\n".join(item.value for item in app.caption)
    assert selected_number in captions

    # A filter that removes the selected ID must reset to the actual remaining
    # ID instead of opening another row at the old display index.
    query = next(item for item in app.text_input if item.key == "learning_manager_query")
    query.set_value("TRACE-APPROVAL-first")
    app = app.run(timeout=40)
    remaining = LearningRepository(database).manager_rows()
    expected_id = next(
        int(row["id"])
        for row in remaining if row["source_question_id"] == "TRACE-APPROVAL-first"
    )
    assert not app.exception
    assert app.session_state["learning_manager_positive_selected_id"] == expected_id
