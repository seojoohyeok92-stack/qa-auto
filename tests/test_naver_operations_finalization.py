from __future__ import annotations

from datetime import date, datetime, UTC
from pathlib import Path

from answer.models import AnswerResult, AnswerStatus
from config import StructuredSignalAutoLearningSettings
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_repository import LearningRepository
from services.positive_learning_service import PositiveLearningService


def _database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "naver-operations.db")
    database.initialize()
    return database


def _inquiry(database: Database, external_id: str) -> int:
    return InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": external_id,
            "external_inquiry_id": external_id,
            "title": "상품 문의",
            "content": "문의 내용",
            "raw_json": {},
        }
    ).inquiry_id


def _draft(database: Database, inquiry_id: int) -> int:
    draft = AnswerRepository(database).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.GENERATED,
            category="GENERAL",
            reason="test",
            answer="확인한 내용을 안내드립니다.",
            provider="rules",
            auto_answerable=True,
            needs_review=False,
            metadata={"selected_answer_route": "SAFE_RULE"},
        ),
    )
    return int(draft["id"])


def test_operational_cards_use_kst_flow_and_current_stock(tmp_path: Path) -> None:
    database = _database(tmp_path)
    before = _inquiry(database, "before-kst")
    review = _inquiry(database, "review")
    approved = _inquiry(database, "approved")
    attention = _inquiry(database, "attention")
    post_failed = _inquiry(database, "post-failed")
    review_draft = _draft(database, review)
    approved_draft = _draft(database, approved)

    with database.transaction() as connection:
        connection.execute(
            "UPDATE inquiries SET created_at=? WHERE id=?",
            ("2026-08-30T14:59:59+00:00", before),
        )
        connection.execute(
            "UPDATE inquiries SET created_at=? WHERE id IN (?,?,?,?)",
            ("2026-08-30T15:00:00+00:00", review, approved, attention, post_failed),
        )
        connection.execute(
            "UPDATE answer_drafts SET created_at=? WHERE id IN (?,?)",
            ("2026-08-30T15:00:00+00:00", review_draft, approved_draft),
        )
        connection.execute(
            "UPDATE inquiries SET workflow_status='REVIEW_PENDING' WHERE id=?",
            (review,),
        )
        connection.execute(
            """
            UPDATE inquiries
            SET workflow_status='POSTED', approval_status='APPROVED',
                approved_at='2026-08-30T15:00:00+00:00'
            WHERE id=?
            """,
            (approved,),
        )
        connection.execute(
            "UPDATE inquiries SET workflow_status='NEEDS_ATTENTION' WHERE id=?",
            (attention,),
        )
        connection.execute(
            """
            UPDATE inquiries
            SET workflow_status='REVIEW_PENDING', post_status='POST_FAILED'
            WHERE id=?
            """,
            (post_failed,),
        )

    cards = InquiryRepository(database).dashboard_operational_card_counts(
        today_kst=date(2026, 8, 31)
    )
    assert cards["NEW"] == {
        "value": 5, "today": 4, "total": 5, "kind": "FLOW"
    }
    assert cards["DRAFTED"] == {
        "value": 2, "today": 2, "total": 2, "kind": "FLOW"
    }
    assert cards["REVIEW"] == {
        "value": 3, "current": 3, "kind": "STOCK"
    }
    assert cards["APPROVED"] == {
        "value": 1, "today": 1, "total": 1, "kind": "FLOW"
    }
    assert cards["ATTENTION"] == {
        "value": 2, "current": 2, "kind": "STOCK"
    }


def test_elapsed_observation_never_creates_positive_learning(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    inquiry_id = _inquiry(database, "observation")
    result = PositiveLearningService(database).observe(
        inquiry_id=inquiry_id,
        seller_answer="오랫동안 수정되지 않은 답변입니다.",
        observed_at=datetime(2030, 1, 1, tzinfo=UTC),
    )
    assert result["reason"] == "MANUAL_APPROVAL_REQUIRED"
    assert result["saved"] is False
    assert LearningRepository(database).count() == 0


def test_environment_cannot_reenable_automatic_signal_promotion(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTO_VERIFIED_FACT_PROMOTION_ENABLED", "true")
    settings = StructuredSignalAutoLearningSettings.from_environment()
    assert settings.auto_verified_promotion_enabled is False
