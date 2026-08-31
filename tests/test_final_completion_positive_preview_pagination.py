from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from answer.models import AnswerResult, AnswerStatus
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.gpt_chat_repository import GptChatRepository
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_repository import LearningRepository
from repositories.post_review_repository import PostReviewRepository
from services.copilot_correction_learning_service import CopilotCorrectionLearningService
from services.historical_case_service import HistoricalCaseService
from services.learning_feedback_service import LearningFeedbackService
from services.positive_learning_service import PositiveLearningService
from services.similar_answer_service import SimilarAnswerService
from ui.review_workspace import paginate_items
from streamlit.testing.v1 import AppTest


def _inquiry(database: Database, external_id: str = "positive-1") -> int:
    return InquiryRepository(database).upsert_work_item({
        "store_code": "OJE_PLUS", "source_type": "PRODUCT_INQUIRY",
        "source_question_id": external_id, "external_inquiry_id": external_id,
        "inquiry_type": "배송", "title": "배송 문의",
        "content": "언제쯤 보내주시는 건가요?", "product_id": "p1",
        "product_name": "테스트 상품", "order_id": "ORDER-1",
        "registered_at": "2026-07-01T01:00:00Z", "source_answered": False,
        "source_status": "WAITING", "source_created_at": "2026-07-01T01:00:00Z",
        "source_updated_at": "2026-07-01T01:00:00Z", "is_private": False,
        "source_metadata_json": {}, "workflow_status": "NEW",
        "answer_status": "UNANSWERED", "post_status": "NOT_POSTED", "raw_json": {},
    }).inquiry_id


def _posted(database: Database, *, posted_at: str) -> tuple[int, int, str]:
    inquiry_id = _inquiry(database)
    answer = "현재 주문 정보를 기준으로 배송 일정을 확인해 안내드립니다."
    draft = AnswerRepository(database).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.GENERATED, category="배송", reason="safe",
            answer=answer, provider="rules", auto_answerable=True,
            needs_review=False, metadata={"selected_answer_route": "SAFE_RULE"},
        ),
    )
    with database.transaction() as connection:
        connection.execute(
            "UPDATE answer_drafts SET validation_status='PASS' WHERE id=?",
            (int(draft["id"]),),
        )
    review = PostReviewRepository(database)
    version, _ = review.finalize_auto(
        inquiry_id=inquiry_id, draft_id=int(draft["id"]), run_id="positive-run"
    )
    review.create_review_after_post(
        inquiry_id=inquiry_id, draft_id=int(draft["id"]),
        version_id=int(version["id"]), run_id="positive-run", route="SAFE_RULE",
        needs_staff_review=False, posted_at=posted_at,
    )
    with database.transaction() as connection:
        connection.execute(
            "UPDATE inquiries SET post_status='POSTED' WHERE id=?", (inquiry_id,)
        )
        connection.execute(
            """
            INSERT INTO naver_post_attempts(
              inquiry_id,answer_draft_id,idempotency_key,external_id,store_code,
              source_type,method,endpoint_kind,status,final_answer_hash,payload_hash,
              actor,started_at,completed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (inquiry_id, int(draft["id"]), "positive-key", "positive-1", "OJE_PLUS",
             "PRODUCT_INQUIRY", "POST", "PRODUCT_QNA", "POSTED", "a", "p",
             "SYSTEM_AUTO_POST", posted_at, posted_at),
        )
    return inquiry_id, int(version["id"]), str(version["answer_body"])


def test_positive_learning_observation_never_promotes(tmp_path) -> None:
    database = Database(tmp_path / "positive.db"); database.initialize()
    now = datetime(2026, 8, 7, tzinfo=UTC)
    inquiry_id, _, answer = _posted(
        database, posted_at=(now - timedelta(days=6)).isoformat()
    )
    service = PositiveLearningService(
        database, settings=SimpleNamespace(observation_days=7)
    )
    early = service.observe(inquiry_id=inquiry_id, seller_answer=answer, observed_at=now)
    assert early["reason"] == "MANUAL_APPROVAL_REQUIRED"
    assert LearningRepository(database).count() == 0
    accepted = service.observe(
        inquiry_id=inquiry_id, seller_answer=answer, observed_at=now + timedelta(days=1)
    )
    assert accepted == {
        "saved": False,
        "reason": "MANUAL_APPROVAL_REQUIRED",
        "learning": None,
    }
    again = service.observe(
        inquiry_id=inquiry_id, seller_answer=answer, observed_at=now + timedelta(days=3)
    )
    assert again["saved"] is False
    assert LearningRepository(database).count() == 0


class _FakeNaver:
    settings = SimpleNamespace(enabled=True, max_pages=5, page_size=100)
    def _token(self, store): return "token"
    def _fetch_page(self, inquiry_type, **kwargs):
        item = {"questionId": f"{inquiry_type}-{kwargs['from_datetime'].date()}", "answer": "과거 판매자 답변입니다."}
        return {"contents" if inquiry_type == "PRODUCT_INQUIRY" else "content": [item]}
    def _normalize(self, inquiry_type, raw, *, store_code):
        return {
            "store_code": store_code, "source_type": inquiry_type,
            "inquiry_type": inquiry_type, "external_inquiry_id": raw["questionId"],
            "title": "과거 문의", "content": "사용 방법을 알려주세요.",
            "seller_answer": raw["answer"], "source_answered": True,
            "source_created_at": "2025-01-01T00:00:00Z", "raw_payload": raw,
        }


def test_historical_preview_is_read_only_and_windowed(tmp_path, monkeypatch) -> None:
    database = Database(tmp_path / "preview.db"); database.initialize()
    monkeypatch.setattr(
        "services.historical_case_service.get_configured_stores",
        lambda: [SimpleNamespace(code="OJE_PLUS")],
    )
    monkeypatch.setenv("HISTORICAL_IMPORT_WINDOW_DAYS", "90")
    before = {}
    with database.connection() as connection:
        for table in ("historical_cases", "auto_sync_events", "naver_post_attempts"):
            before[table] = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    result = HistoricalCaseService(database, naver_sync=_FakeNaver()).preview_naver(
        from_datetime=datetime(2025, 1, 1, tzinfo=UTC),
        to_datetime=datetime(2025, 7, 15, tzinfo=UTC),
    )
    assert result["window_count"] == 3
    assert result["total_fetched"] == 6
    assert result["expected_new_count"] == 6
    with database.connection() as connection:
        for table, count in before.items():
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == count


def test_copilot_explicit_correction_learns_but_general_chat_does_not(tmp_path) -> None:
    database = Database(tmp_path / "copilot.db"); database.initialize()
    inquiry_id = _inquiry(database, "copilot-1")
    session = GptChatRepository(database).create_session(user_name="tester", inquiry_id=inquiry_id)
    service = CopilotCorrectionLearningService(database)
    saved = service.capture(
        inquiry_id=inquiry_id,
        message="이건 배송문의인데 왜 DPS 조회를 안 했어",
        chat_session_id=int(session["id"]), chat_message_id=1,
    )
    assert saved is not None
    assert saved["metadata_json"]["correction_type"] == "INQUIRY_CLASSIFICATION_CORRECTION"
    assert service.capture(
        inquiry_id=inquiry_id, message="화면이 너무 어두워",
        chat_session_id=int(session["id"]), chat_message_id=2,
    ) is None
    matches = SimilarAnswerService(LearningRepository(database)).search(
        "언제쯤 보내주시는 건가요?", store_code="OJE_PLUS"
    )
    assert any(row["metadata_json"].get("source_origin") == "COPILOT_CORRECTION" for row in matches)


def test_dashboard_pagination_defaults_and_clamps_for_30_and_2000_items() -> None:
    thirty = [{"id": value} for value in range(30)]
    first, page, total = paginate_items(thirty, 1, 15)
    assert [row["id"] for row in first] == list(range(15))
    assert (page, total) == (1, 2)
    second, page, total = paginate_items(thirty, 2, 15)
    assert [row["id"] for row in second] == list(range(15, 30))
    filtered, page, total = paginate_items(thirty[:7], 10, 15)
    assert len(filtered) == 7 and (page, total) == (1, 1)
    large, page, total = paginate_items([{"id": value} for value in range(2001)], 100, 15)
    assert len(large) == 15 and (page, total) == (100, 134)


def test_positive_learning_rejects_unknown_and_staff_review(tmp_path) -> None:
    now = datetime(2026, 8, 7, tzinfo=UTC)
    unknown_db = Database(tmp_path / "unknown.db"); unknown_db.initialize()
    inquiry_id, version_id, answer = _posted(
        unknown_db, posted_at=(now - timedelta(days=10)).isoformat()
    )
    with unknown_db.transaction() as connection:
        connection.execute(
            "UPDATE answer_versions SET naver_status='POST_UNKNOWN' WHERE id=?",
            (version_id,),
        )
    result = PositiveLearningService(
        unknown_db, settings=SimpleNamespace(observation_days=7)
    ).observe(inquiry_id=inquiry_id, seller_answer=answer, observed_at=now)
    assert result["reason"] == "MANUAL_APPROVAL_REQUIRED"

    review_db = Database(tmp_path / "review.db"); review_db.initialize()
    inquiry_id, _, answer = _posted(
        review_db, posted_at=(now - timedelta(days=10)).isoformat()
    )
    with review_db.transaction() as connection:
        connection.execute(
            "UPDATE post_reviews SET needs_staff_review=1 WHERE inquiry_id=?",
            (inquiry_id,),
        )
    result = PositiveLearningService(
        review_db, settings=SimpleNamespace(observation_days=7)
    ).observe(inquiry_id=inquiry_id, seller_answer=answer, observed_at=now)
    assert result["reason"] == "MANUAL_APPROVAL_REQUIRED"


def test_positive_learning_rejects_pre_promotion_edit_and_negative(tmp_path) -> None:
    now = datetime(2026, 8, 7, tzinfo=UTC)
    edited_db = Database(tmp_path / "edited-before-seven.db"); edited_db.initialize()
    inquiry_id, _, original = _posted(
        edited_db, posted_at=(now - timedelta(days=10)).isoformat()
    )
    PostReviewRepository(edited_db).capture_remote_naver_edit(
        inquiry_id=inquiry_id, answer_body="직원이 수정한 최종 답변입니다."
    )
    edited = PositiveLearningService(
        edited_db, settings=SimpleNamespace(observation_days=7)
    ).observe(inquiry_id=inquiry_id, seller_answer=original, observed_at=now)
    assert edited["reason"] == "MANUAL_APPROVAL_REQUIRED"
    assert LearningRepository(edited_db).count() == 0

    negative_db = Database(tmp_path / "negative-before-seven.db"); negative_db.initialize()
    inquiry_id, _, answer = _posted(
        negative_db, posted_at=(now - timedelta(days=10)).isoformat()
    )
    active = AnswerRepository(negative_db).active_for_inquiry(inquiry_id)
    LearningFeedbackService(negative_db).capture_dashboard_negative(
        inquiry_id=inquiry_id,
        original_answer_source="PROGRAM_GENERATED",
        original_answer_reference_id=int(active["id"]),
        correction_reason="FACT_ERROR",
        actor="tester",
    )
    negative = PositiveLearningService(
        negative_db, settings=SimpleNamespace(observation_days=7)
    ).observe(inquiry_id=inquiry_id, seller_answer=answer, observed_at=now)
    assert negative["reason"] == "MANUAL_APPROVAL_REQUIRED"
    assert LearningRepository(negative_db).count() == 0


def test_positive_learning_rejects_exact_excluded_answer_after_seven_days(
    tmp_path,
) -> None:
    now = datetime(2026, 8, 7, tzinfo=UTC)
    database = Database(tmp_path / "excluded-before-seven.db")
    database.initialize()
    inquiry_id, _, answer = _posted(
        database, posted_at=(now - timedelta(days=10)).isoformat()
    )
    active = AnswerRepository(database).active_for_inquiry(inquiry_id)
    LearningFeedbackService(database).capture_dashboard_excluded(
        inquiry_id=inquiry_id,
        original_answer_source="PROGRAM_GENERATED",
        original_answer_reference_id=int(active["id"]),
        exclusion_reason="NOT_REUSABLE",
        actor="tester",
    )
    excluded = PositiveLearningService(
        database, settings=SimpleNamespace(observation_days=7)
    ).observe(inquiry_id=inquiry_id, seller_answer=answer, observed_at=now)
    assert excluded["reason"] == "MANUAL_APPROVAL_REQUIRED"
    assert LearningRepository(database).count() == 0


def test_dashboard_pagination_apptest_keeps_page_and_separates_historical_state(tmp_path) -> None:
    path = tmp_path / "dashboard.db"
    database = Database(path); database.initialize()
    for index in range(30):
        _inquiry(database, f"page-{index:02d}")
    app = AppTest.from_string(f'''
from app import dashboard_work_items_from_database
from repositories.database import Database
from ui.review_workspace import render_review_workspace
import streamlit as st
db=Database(r"{path}")
db.initialize()
st.session_state.setdefault("historical_page", 9)
items=dashboard_work_items_from_database(db)
render_review_workspace(items, len(items), db, page_size=15)
''').run(timeout=60)
    assert not app.exception
    assert app.session_state["dashboard_page"] == 1
    app.button(key="dashboard_page_number_2").click().run(timeout=60)
    assert not app.exception
    assert app.session_state["dashboard_page"] == 2
    assert app.session_state["historical_page"] == 9
    assert app.session_state["selected_inquiry_key"] is not None
    app.button(key="dashboard_page_number_1").click().run(timeout=60)
    assert app.session_state["dashboard_page"] == 1
