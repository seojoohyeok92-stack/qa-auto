from __future__ import annotations

import json

import pytest

from repositories.database import Database
from repositories.learning_feedback_repository import LearningFeedbackRepository
from repositories.learning_repository import LearningRepository
from ui.learning_manager import _filter_rows


def _database(tmp_path) -> Database:
    database = Database(tmp_path / "learning-manager-search.db")
    database.initialize()
    return database


def _positive_values(index: int, *, old: bool = False) -> tuple[object, ...]:
    marker = "오래된 Positive 삼성 감사제" if old else f"최근 Positive {index}"
    return (
        f"positive-{index}",
        "TEMPLATE" if old else "APPROVED_EDITED",
        marker,
        marker.lower(),
        "관리 검색 답변",
        5,
        json.dumps(
            {
                "learning_signal_type": "POSITIVE",
                "answer_provenance": "OLD_PROVENANCE" if old else "STAFF_EDITED",
            },
            ensure_ascii=False,
        ),
        "2020-01-01T00:00:00Z" if old else "2026-08-31T00:00:00Z",
    )


def _feedback_values(index: int, *, old: bool = False) -> tuple[object, ...]:
    marker = "오래된 Feedback 벽걸이" if old else f"최근 Feedback {index}"
    return (
        f"feedback-{index}",
        "STAFF_CORRECTION",
        "FACT_ERROR",
        marker,
        "NEGATIVE",
        "OLD_FEEDBACK_SOURCE" if old else "DASHBOARD_NEGATIVE_REVIEW",
        marker,
        marker,
        json.dumps(
            {"answer_provenance": "OLD_FEEDBACK_PROVENANCE" if old else "PROGRAM_GENERATED"},
            ensure_ascii=False,
        ),
        "2020-01-01T00:00:00Z" if old else "2026-08-31T00:00:00Z",
    )


def test_repository_search_finds_positive_and_feedback_older_than_2000(tmp_path) -> None:
    database = _database(tmp_path)
    with database.transaction() as connection:
        connection.executemany(
            """
            INSERT INTO learning_examples(
                source_key, learning_source, question_original_masked,
                question_normalized, final_answer, rating, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [_positive_values(index, old=index == 0) for index in range(2001)],
        )
        connection.executemany(
            """
            INSERT INTO learning_feedback(
                source_key, feedback_type, correction_reason, correction_note,
                learning_signal_type, source, question_masked,
                original_answer_masked, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [_feedback_values(index, old=index == 0) for index in range(2001)],
        )

    positive_repository = LearningRepository(database)
    feedback_repository = LearningFeedbackRepository(database)
    assert all(
        "오래된 Positive" not in row["question_original_masked"]
        for row in positive_repository.manager_rows(limit=2000)
    )
    assert all(
        "오래된 Feedback" not in row["correction_note"]
        for row in feedback_repository.manager_rows(limit=2000)
    )

    positive = positive_repository.manager_page(query="오래된Positive삼성감사제")
    feedback = feedback_repository.manager_page(query="오래된 Feedback 벽걸이")
    assert positive.total == 1
    assert positive.rows[0]["source_key"] == "positive-0"
    assert feedback.total == 1
    assert feedback.rows[0]["source_key"] == "feedback-0"

    positive_options = positive_repository.manager_filter_options()
    feedback_options = feedback_repository.manager_filter_options()
    assert "TEMPLATE" in positive_options["sources"]
    assert "OLD_PROVENANCE" in positive_options["provenance"]
    assert "OLD_FEEDBACK_SOURCE" in feedback_options["sources"]
    assert "OLD_FEEDBACK_PROVENANCE" in feedback_options["provenance"]


def test_positive_manager_page_search_fields_filters_count_and_order(tmp_path) -> None:
    database = _database(tmp_path)
    metadata = json.dumps(
        {
            "learning_signal_type": "POSITIVE",
            "answer_provenance": "FIELD_PROVENANCE",
            "answer_reference_id": "FIELD-REFERENCE",
            "human_verified": True,
        },
        ensure_ascii=False,
    )
    with database.transaction() as connection:
        for index in range(5):
            connection.execute(
                """
                INSERT INTO learning_examples(
                    source_key, learning_source, question_original_masked,
                    question_normalized, product_name, seller_answer, gpt_draft,
                    edited_answer, final_answer, rating, metadata_json,
                    validity_type, event_name, valid_from, valid_until,
                    validity_note, condition_json, created_at
                ) VALUES (?, 'APPROVED_EDITED', ?, ?, ?, ?, ?, ?, ?, 5, ?,
                          'TEMPORARY', ?, ?, ?, ?, ?, '2026-08-31T00:00:00Z')
                """,
                (
                    f"field-{index}",
                    f"배송 설치 벽걸이 질문-{index}",
                    f"배송 설치 벽걸이 질문-{index}",
                    f"FIELD-PRODUCT-{index}",
                    f"FIELD-SELLER-{index}",
                    f"FIELD-GPT-{index}",
                    f"FIELD-EDITED-{index}",
                    f"FIELD-ANSWER-{index}",
                    metadata,
                    "삼성 감사제",
                    "2026-08-01T00:00:00+09:00",
                    "2026-08-31T23:59:59+09:00",
                    "FIELD-NOTE",
                    json.dumps({"region": "FIELD-CONDITION"}),
                ),
            )

    repository = LearningRepository(database)
    for query in (
        "질문-0", "FIELD-ANSWER-0", "FIELD-PRODUCT-0", "APPROVED_EDITED",
        "FIELD-REFERENCE", "삼성감사제", "삼성 감사제", "2026-08-01",
        "FIELD-NOTE", "FIELD-CONDITION", "FIELD-SELLER-0", "FIELD-EDITED-0",
        "FIELD-GPT-0", "배송", "설치", "벽걸이",
    ):
        assert repository.manager_page(query=query).total >= 1, query

    first = repository.manager_page(
        source="APPROVED_EDITED", provenance="FIELD_PROVENANCE",
        human_verified="YES", validity_type="TEMPORARY", page=1, page_size=2,
    )
    last = repository.manager_page(
        source="APPROVED_EDITED", provenance="FIELD_PROVENANCE",
        human_verified="YES", validity_type="TEMPORARY", page=3, page_size=2,
    )
    assert first.total == 5
    assert [row["source_key"] for row in first.rows] == ["field-4", "field-3"]
    assert last.page == 3
    assert [row["source_key"] for row in last.rows] == ["field-0"]
    with database.transaction() as connection:
        connection.execute(
            "UPDATE learning_examples SET validity_active=0 WHERE source_key='field-0'"
        )
    disabled = repository.manager_page(
        query="FIELD-ANSWER-0", validity_state="DISABLED"
    )
    assert disabled.total == 1
    assert disabled.rows[0]["source_key"] == "field-0"


def test_feedback_manager_page_filters_and_exact_total(tmp_path) -> None:
    database = _database(tmp_path)
    with database.transaction() as connection:
        connection.executemany(
            """
            INSERT INTO learning_feedback(
                source_key, feedback_type, correction_reason, correction_note,
                learning_signal_type, source, question_masked,
                original_answer_masked, original_answer_source,
                original_answer_reference_id, metadata_json, created_at
            ) VALUES (?, 'STAFF_CORRECTION', 'FACT_ERROR', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    f"filter-{index}", f"메모-{index}",
                    "NEGATIVE" if index < 3 else "EXCLUDED",
                    "FILTER_SOURCE", f"질문-{index}", f"답변-{index}",
                    "PROGRAM_GENERATED", 100 + index,
                    json.dumps({"answer_provenance": "FILTER_PROVENANCE"}),
                    "2026-08-31T00:00:00Z",
                )
                for index in range(5)
            ],
        )

    page = LearningFeedbackRepository(database).manager_page(
        source="FILTER_SOURCE", provenance="PROGRAM_GENERATED",
        signal_type="NEGATIVE", page=2, page_size=2,
    )
    assert page.total == 3
    assert page.page == 2
    assert [row["source_key"] for row in page.rows] == ["filter-0"]


@pytest.mark.parametrize(
    ("stored", "query"),
    (("삼성 감사제", "삼성감사제"), ("삼성감사제", "삼성 감사제")),
)
def test_memory_filter_preserves_substring_and_normalizes_whitespace(
    stored: str, query: str
) -> None:
    row = {
        "id": 1,
        "question_original_masked": f"{stored} 배송 설치 벽걸이",
        "learning_source": "APPROVED_EDITED",
        "active": True,
    }
    assert _filter_rows([row], query=query) == [row]
    for substring in ("삼성", "배송", "설치", "벽걸이"):
        assert _filter_rows([row], query=substring) == [row]
