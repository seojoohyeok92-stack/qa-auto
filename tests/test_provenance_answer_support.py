from __future__ import annotations

from repositories.database import Database
from repositories.learning_provenance_repository import LearningProvenanceRepository


def make_database(tmp_path) -> Database:
    database = Database(tmp_path / "provenance-answer-support.db")
    database.initialize()
    return database


def test_migration_26_adds_answer_support_columns_and_is_idempotent(tmp_path) -> None:
    database = make_database(tmp_path)
    assert 26 in database.migration_versions()
    # Re-running initialize() must never fail or duplicate columns/rows.
    database.initialize()
    with database.connection() as connection:
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(answer_learning_provenance)"
            )
        }
    for expected in (
        "answer_support_score", "evidence_coverage",
        "provider_claimed_usage", "system_verified_usage",
    ):
        assert expected in columns


def _insert_inquiry(database: Database) -> int:
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO inquiries(
                store_code, source_type, source_question_id, inquiry_type,
                title, content, product_name, raw_json
            ) VALUES ('OJE_PLUS','PRODUCT_INQUIRY','PROV-1','PRODUCT_INQUIRY',
                '문의','문의 내용','상품', '{}')
            """
        )
        return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])


def _insert_learning_example(database: Database, *, answer: str) -> int:
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO learning_examples(
                source_key, learning_source, question_original_masked,
                question_normalized, final_answer, rating
            ) VALUES (?, 'SELLER_ANSWER', '질문', '질문', ?, 5)
            """,
            (f"prov-le-{answer[:8]}", answer),
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


def test_system_verified_usage_diverges_from_provider_self_report(tmp_path) -> None:
    """The provider can claim it used a reference even when the final answer
    text does not actually contain that reference's content -- provenance
    must record both signals separately (section 12/13 of the request)."""

    database = make_database(tmp_path)
    inquiry_id = _insert_inquiry(database)
    used_id = _insert_learning_example(
        database, answer="구매처는 네이버로 선택해주시면 됩니다."
    )
    draft_id = _insert_draft(database, inquiry_id=inquiry_id)

    repository = LearningProvenanceRepository(database)
    run_id = repository.record_context(
        inquiry_id=inquiry_id,
        learning=[{
            "learning_example_id": used_id,
            "learning_source": "SELLER_ANSWER",
            "relevance": 0.5,
            "answer_support": 0.4,
        }],
        historical=[],
    )
    assert run_id is not None
    repository.attach_latest_context(inquiry_id=inquiry_id, draft_id=draft_id)

    # Provider *claims* it used the reference, but the final answer text
    # shares nothing with the reference's own answer.
    repository.finalize_for_draft(
        draft_id=draft_id,
        result_metadata={
            "hybrid": {
                "draft": {
                    "answer": "확인이 필요한 내용으로 담당자가 안내드리겠습니다.",
                    "learning_usage": [
                        {
                            "learning_id": used_id,
                            "answer_supported": True,
                            "reason": "PROVIDER_CLAIMS_USE",
                        }
                    ],
                },
                "validation": {"passed": True},
            }
        },
    )
    rows = repository.for_draft(draft_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["usage_status"] == "USED"
    assert row["provider_claimed_usage"] == 1
    assert row["system_verified_usage"] == "UNCONFIRMED"
    assert row["answer_support_score"] == 0.4
    assert row["evidence_coverage"] == "PARTIALLY_SUPPORTED"


def test_system_verified_usage_confirms_when_answer_actually_reuses_reference(
    tmp_path,
) -> None:
    database = make_database(tmp_path)
    inquiry_id = _insert_inquiry(database)
    reference_answer = "구매처는 네이버로 선택해주시면 됩니다."
    used_id = _insert_learning_example(database, answer=reference_answer)
    draft_id = _insert_draft(database, inquiry_id=inquiry_id)

    repository = LearningProvenanceRepository(database)
    repository.record_context(
        inquiry_id=inquiry_id,
        learning=[{
            "learning_example_id": used_id,
            "learning_source": "SELLER_ANSWER",
            "relevance": 0.6,
            "answer_support": 0.6,
        }],
        historical=[],
    )
    repository.attach_latest_context(inquiry_id=inquiry_id, draft_id=draft_id)
    repository.finalize_for_draft(
        draft_id=draft_id,
        result_metadata={
            "hybrid": {
                "draft": {
                    "answer": reference_answer,
                    "learning_usage": [
                        {
                            "learning_id": used_id,
                            "answer_supported": True,
                            "reason": "ACTIVE_POSITIVE_LEARNING",
                        }
                    ],
                },
                "validation": {"passed": True},
            }
        },
    )
    row = repository.for_draft(draft_id)[0]
    assert row["system_verified_usage"] == "CONFIRMED"
