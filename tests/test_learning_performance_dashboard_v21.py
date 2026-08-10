from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from answer.models import AnswerResult, AnswerStatus
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_provenance_repository import LearningProvenanceRepository
from repositories.learning_repository import LearningRepository
from repositories.post_review_repository import PostReviewRepository
from services.learning_performance_service import LearningPerformanceService
from services.learning_service import LearningService
from services.historical_case_service import HistoricalCaseService
from services.post_review_service import PostReviewService
from core.time_utils import format_datetime_minute_kst


def _inquiry(database: Database, key: str, inquiry_type: str = "배송") -> int:
    return InquiryRepository(database).upsert_work_item({
        "store_code": "OJE_PLUS", "source_type": "PRODUCT_INQUIRY",
        "source_question_id": key, "external_inquiry_id": key,
        "inquiry_type": inquiry_type, "title": "배송 문의",
        "content": "언제 보내주시나요?", "product_name": "테스트 상품",
        "registered_at": datetime.now(UTC).isoformat(), "source_answered": False,
        "source_status": "WAITING", "source_created_at": datetime.now(UTC).isoformat(),
        "source_updated_at": datetime.now(UTC).isoformat(), "is_private": False,
        "source_metadata_json": {}, "workflow_status": "NEW",
        "answer_status": "UNANSWERED", "post_status": "NOT_POSTED", "raw_json": {},
    }).inquiry_id


def _post(database: Database, key: str, inquiry_type: str = "배송") -> tuple[int, int, int]:
    inquiry_id = _inquiry(database, key, inquiry_type)
    draft = AnswerRepository(database).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.GENERATED, category=inquiry_type, reason="safe",
            answer="현재 확인된 기준으로 안전하게 안내드립니다.", provider="rules",
            auto_answerable=True, needs_review=False,
            metadata={"selected_answer_route": "SAFE_RULE"},
        ),
    )
    with database.transaction() as connection:
        connection.execute("UPDATE answer_drafts SET validation_status='PASS' WHERE id=?", (draft["id"],))
    reviews = PostReviewRepository(database)
    version, _ = reviews.finalize_auto(
        inquiry_id=inquiry_id, draft_id=int(draft["id"]), run_id=key
    )
    reviews.create_review_after_post(
        inquiry_id=inquiry_id, draft_id=int(draft["id"]), version_id=int(version["id"]),
        run_id=key, route="SAFE_RULE", needs_staff_review=False,
        posted_at=datetime.now(UTC).isoformat(),
    )
    with database.transaction() as connection:
        connection.execute("UPDATE inquiries SET post_status='POSTED' WHERE id=?", (inquiry_id,))
    return inquiry_id, int(draft["id"]), int(version["id"])


def test_learning_performance_rates_sources_and_real_provenance(tmp_path: Path) -> None:
    database = Database(tmp_path / "performance.db"); database.initialize()
    unchanged_id, unchanged_draft, _ = _post(database, "unchanged", "배송")
    PostReviewService(database).complete_without_change(
        inquiry_id=unchanged_id, actor="tester"
    )
    unchanged_learning = next(
        row for row in LearningRepository(database).candidates(store_code="OJE_PLUS")
        if row["inquiry_id"] == unchanged_id
    )
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE learning_examples
            SET metadata_json=json_set(metadata_json,'$.acceptance_mode','AUTO_OBSERVATION')
            WHERE id=?
            """,
            (unchanged_learning["id"],),
        )
    provenance = LearningProvenanceRepository(database)
    provenance.record_context(
        inquiry_id=unchanged_id,
        learning=[{
            "learning_example_id": unchanged_learning["id"],
            "learning_source": unchanged_learning["learning_source"],
            "relevance": 0.91,
        }], historical=[],
    )
    provenance.attach_latest_context(inquiry_id=unchanged_id, draft_id=unchanged_draft)

    historical_service = HistoricalCaseService(database)
    historical_case = historical_service.prepare_case({
        "store_code": "OJE_PLUS", "source_type": "PRODUCT_INQUIRY",
        "external_inquiry_id": "performance-history", "title": "배송 문의",
        "content": "언제 보내주시나요?",
        "seller_answer": "현재 주문 정보 확인 후 배송 일정을 안내해 주세요.",
        "answered": True, "source_created_at": datetime.now(UTC).isoformat(),
    }, source_reference="TEST:performance")
    historical_row, _ = historical_service.repository.upsert(historical_case)
    provenance.record_context(
        inquiry_id=unchanged_id, learning=[], historical=[{
            "historical_case_id": historical_row["id"],
            "source": "HISTORICAL_REFERENCE", "relevance": 0.78,
        }],
    )
    provenance.attach_latest_context(inquiry_id=unchanged_id, draft_id=unchanged_draft)

    corrected_id, _, _ = _post(database, "corrected", "상품")
    reviews = PostReviewRepository(database)
    corrected_version, changed = reviews.capture_remote_naver_edit(
        inquiry_id=corrected_id, answer_body="직원이 수정한 최종 안내입니다."
    )
    assert changed is True
    LearningService(database).capture_auto_post_version(
        inquiry_id=corrected_id, version_id=int(corrected_version["id"]),
        source="AUTO_POST_CORRECTED",
    )

    data = LearningPerformanceService(database).snapshot()
    assert data["current_30"]["known"] == 2
    assert data["current_30"]["unchanged_rate"] == 50.0
    assert data["current_30"]["correction_rate"] == 50.0
    assert data["provenance"]["generated_with_learning"] == 1
    assert data["provenance"]["generated_with_historical"] == 1
    assert data["provenance"]["used"]["unchanged_rate"] == 100.0
    assert data["provenance"]["not_used"]["unchanged_rate"] == 0.0
    assert any(row["source_group"] == "POSITIVE_LEARNING" for row in data["sources"])
    assert any(
        row["source_group"] == "HISTORICAL_VERIFIED_LEARNING"
        for row in data["sources"]
    )
    assert {row["inquiry_type"] for row in data["types"]} == {"배송", "상품"}


def test_empty_performance_never_invents_rates(tmp_path: Path) -> None:
    database = Database(tmp_path / "empty.db"); database.initialize()
    data = LearningPerformanceService(database).snapshot()
    assert data["current_7"]["unchanged_rate"] is None
    assert data["current_30"]["correction_rate"] is None
    assert data["provenance"]["used"]["unchanged_rate"] is None
    assert data["trend"] == []


def test_generation_context_is_attached_only_to_actual_draft(tmp_path: Path) -> None:
    database = Database(tmp_path / "provenance.db"); database.initialize()
    source_id, _, _ = _post(database, "source")
    PostReviewService(database).complete_without_change(inquiry_id=source_id, actor="tester")
    learning = next(row for row in LearningRepository(database).candidates(store_code="OJE_PLUS"))
    target_id = _inquiry(database, "target")
    repository = LearningProvenanceRepository(database)
    repository.record_context(
        inquiry_id=target_id,
        learning=[{
            "learning_example_id": learning["id"],
            "learning_source": learning["learning_source"],
            "relevance": 0.87,
        }], historical=[],
    )
    draft = AnswerRepository(database).create_program_draft(
        target_id,
        AnswerResult(
            status=AnswerStatus.GENERATED, category="배송", reason="test",
            answer="참고자료를 포함한 답변입니다.", provider="rules",
            auto_answerable=True, needs_review=False, metadata={},
        ),
    )
    rows = repository.for_draft(int(draft["id"]))
    assert len(rows) == 1
    assert rows[0]["learning_example_id"] == learning["id"]
    assert rows[0]["relevance"] == 0.87


def test_dashboard_list_header_scroll_and_minute_format_are_separated() -> None:
    workspace = (Path(__file__).parents[1] / "ui" / "review_workspace.py").read_text(encoding="utf-8")
    css = (Path(__file__).parents[1] / "ui" / "dashboard.css").read_text(encoding="utf-8")
    assert workspace.index("_render_list_header(total_count)") < workspace.index('key="official_inquiry_rows_scroll"')
    assert workspace.index('key="official_inquiry_rows_scroll"') < workspace.index("_render_pagination(resolved_page")
    assert 'key="official_inquiry_list_panel"' in workspace
    assert "overflow-y: auto !important" in css[css.index("st-key-official_inquiry_rows_scroll"):]
    assert "received-time" in css and "text-overflow: clip" in css
    assert format_datetime_minute_kst("2026-08-07T06:24:59Z") == "2026-08-07 15:24"


def test_learning_performance_apptest_and_session_state(tmp_path: Path) -> None:
    from streamlit.testing.v1 import AppTest
    path = tmp_path / "app.db"
    Database(path).initialize()
    app = AppTest.from_string(f'''
import streamlit as st
from repositories.database import Database
from ui.learning_performance import render_learning_performance
db=Database(r"{path}")
db.initialize()
st.session_state.setdefault("dashboard_page", 5)
st.session_state.setdefault("historical_selected_case_id", 77)
render_learning_performance(db)
''').run(timeout=60)
    assert not app.exception
    assert app.session_state["dashboard_page"] == 5
    assert app.session_state["historical_selected_case_id"] == 77
    assert app.session_state["learning_performance_period"] == "최근 30일"
    rendered = "\n".join(item.value for item in [*app.markdown, *app.caption, *app.info])
    assert "Learning 성과" in rendered
    assert any(metric.value == "측정 데이터 부족" for metric in app.metric)
