from __future__ import annotations

from datetime import UTC, datetime

from repositories.database import Database
from repositories.feedback_signal_provenance_repository import (
    FeedbackSignalProvenanceRepository,
)
from repositories.learning_provenance_repository import LearningProvenanceRepository
from repositories.learning_signal_repository import LearningSignalRepository
from services.historical_case_service import HistoricalCaseService


def make_database(tmp_path) -> Database:
    database = Database(tmp_path / "dashboard-inquiry-display.db")
    database.initialize()
    return database


def _insert_inquiry(database: Database, *, external_inquiry_id: str) -> int:
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO inquiries(
                store_code, source_type, source_question_id,
                external_inquiry_id, inquiry_type, title, content,
                product_name, raw_json
            ) VALUES ('OJE_PLUS','PRODUCT_INQUIRY',?,?,'PRODUCT_INQUIRY',
                '문의','문의 내용','삼성 TV', '{}')
            """,
            (external_inquiry_id, external_inquiry_id),
        )
        return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])


def _insert_learning_example(
    database: Database, *, inquiry_id: int, answer: str
) -> int:
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO learning_examples(
                source_key, inquiry_id, learning_source,
                question_original_masked, question_normalized, product_name,
                final_answer, rating
            ) VALUES (?, ?, 'SELLER_ANSWER', '질문', '질문', '삼성 TV', ?, 5)
            """,
            (f"dash-le-{answer[:8]}", inquiry_id, answer),
        )
        return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])


def _insert_draft(database: Database, *, inquiry_id: int) -> int:
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO answer_drafts(inquiry_id, program_status, provider)
            VALUES (?, 'GENERATED', 'test')
            """,
            (inquiry_id,),
        )
        return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])


def make_historical_case(
    database: Database, *, external_inquiry_id: str, inquiry_id: int | None = None
) -> dict:
    service = HistoricalCaseService(database)
    case = service.prepare_case(
        {
            "store_code": "OJE_PLUS",
            "source_type": "PRODUCT_INQUIRY",
            "external_inquiry_id": external_inquiry_id,
            "title": "배송 문의",
            "content": "제주도 배송 가능한가요?",
            "seller_answer": "제주도 배송 관련 상세페이지 참고 안내드립니다.",
            "product_name": "삼성 TV",
            "answered": True,
            "source_created_at": datetime.now(UTC).isoformat(),
        },
        source_reference="TEST:dashboard-display",
    )
    saved, _ = service.repository.upsert(case)
    if inquiry_id is not None:
        with database.transaction() as connection:
            connection.execute(
                "UPDATE historical_cases SET inquiry_id=? WHERE id=?",
                (inquiry_id, saved["id"]),
            )
    return saved


def test_learning_provenance_for_draft_prefers_original_inquiry_number(
    tmp_path,
) -> None:
    """Dashboard 'used Learning' rows must be traceable by the original
    platform inquiry number, not only the internal Learning PK -- many
    Learning rows (product inquiries) have no order number to fall back to.
    Internal PKs must remain present and unchanged for DB/debug use."""

    database = make_database(tmp_path)
    inquiry_id = _insert_inquiry(database, external_inquiry_id="684499854")
    learning_id = _insert_learning_example(
        database, inquiry_id=inquiry_id, answer="제주도 배송 가능합니다."
    )
    draft_id = _insert_draft(database, inquiry_id=inquiry_id)

    repository = LearningProvenanceRepository(database)
    repository.record_context(
        inquiry_id=inquiry_id,
        learning=[{
            "learning_example_id": learning_id,
            "learning_source": "SELLER_ANSWER",
            "relevance": 0.6,
            "answer_support": 0.6,
        }],
        historical=[],
    )
    repository.attach_latest_context(inquiry_id=inquiry_id, draft_id=draft_id)

    rows = repository.for_draft(draft_id)
    assert len(rows) == 1
    row = rows[0]
    # Internal PK preserved untouched for DB/debug traceability.
    assert int(row["learning_example_id"]) == learning_id
    # New display columns carry the original platform inquiry number.
    assert row["learning_external_inquiry_id"] == "684499854"
    assert row["learning_source_question_id"] == "684499854"
    assert row["learning_product_name"] == "삼성 TV"


def test_historical_provenance_for_draft_prefers_original_inquiry_number(
    tmp_path,
) -> None:
    database = make_database(tmp_path)
    case = make_historical_case(database, external_inquiry_id="685875593")
    draft_inquiry_id = _insert_inquiry(database, external_inquiry_id="000TARGET")
    draft_id = _insert_draft(database, inquiry_id=draft_inquiry_id)

    repository = LearningProvenanceRepository(database)
    repository.record_context(
        inquiry_id=draft_inquiry_id,
        learning=[],
        historical=[{
            "historical_case_id": int(case["id"]),
            "source": "HISTORICAL_VERIFIED_LEARNING",
            "relevance": 0.5,
            "answer_support": 0.3,
        }],
    )
    repository.attach_latest_context(inquiry_id=draft_inquiry_id, draft_id=draft_id)

    rows = repository.for_draft(draft_id)
    assert len(rows) == 1
    row = rows[0]
    # Internal PK preserved untouched.
    assert int(row["historical_case_id"]) == int(case["id"])
    # historical_cases carries its own external_inquiry_id directly (no join
    # needed) -- this must surface as the primary display identifier too.
    assert row["historical_external_inquiry_id"] == "685875593"


def test_orphaned_learning_row_without_external_number_falls_back_to_internal_id(
    tmp_path,
) -> None:
    """A Learning row imported without a linked inquiry (inquiry_id NULL,
    valid per schema) has no original inquiry number to show -- the
    internal Learning PK must still be preserved, and no number may be
    fabricated in its place."""

    database = make_database(tmp_path)
    learning_id = _insert_learning_example(
        database, inquiry_id=None, answer="일반 안내 답변입니다."
    )
    draft_inquiry_id = _insert_inquiry(database, external_inquiry_id="000TARGET2")
    draft_id = _insert_draft(database, inquiry_id=draft_inquiry_id)
    repository = LearningProvenanceRepository(database)
    repository.record_context(
        inquiry_id=draft_inquiry_id,
        learning=[{
            "learning_example_id": learning_id,
            "learning_source": "SELLER_ANSWER",
            "relevance": 0.5,
            "answer_support": 0.3,
        }],
        historical=[],
    )
    repository.attach_latest_context(inquiry_id=draft_inquiry_id, draft_id=draft_id)
    row = repository.for_draft(draft_id)[0]
    assert row["learning_external_inquiry_id"] is None
    assert row["learning_source_question_id"] is None
    assert int(row["learning_example_id"]) == learning_id


def test_feedback_signal_provenance_for_draft_carries_original_inquiry_number(
    tmp_path,
) -> None:
    database = make_database(tmp_path)
    inquiry_id = _insert_inquiry(database, external_inquiry_id="777888999")
    draft_id = _insert_draft(database, inquiry_id=inquiry_id)
    signal = LearningSignalRepository(database).upsert(
        {
            "source_key": "dash-signal-1",
            "signal_kind": "VERIFIED_FACT",
            "origin_kind": "POSITIVE_REVIEW",
            "inquiry_id": inquiry_id,
            "question_masked": "제주도 배송 가능한가요?",
            "content_text": "제주도 배송 및 설치가 가능합니다.",
            "product_scope": "POLICY",
            "topics_json": ["DELIVERY"],
            "product_identity_json": {"product_name": "삼성 TV"},
        }
    )
    repository = FeedbackSignalProvenanceRepository(database)
    repository.record_context(
        inquiry_id=inquiry_id,
        context_run_id="run-1",
        signals=[{
            "signal_id": signal["id"], "signal_kind": "VERIFIED_FACT",
            "source_label": "VERIFIED_FACT", "relevance": 0.6, "answer_support": 0.5,
        }],
    )
    repository.attach_latest_context(inquiry_id=inquiry_id, draft_id=draft_id)
    row = repository.for_draft(draft_id)[0]
    assert int(row["learning_signal_id"]) == int(signal["id"])
    assert row["signal_source_question_id"] == "777888999"
