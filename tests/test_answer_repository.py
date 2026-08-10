from __future__ import annotations

import pytest

from answer.exceptions import AnswerAlreadyPostedError
from answer.models import AnswerResult, AnswerStatus
from answer.answer_format import format_final_answer
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository


@pytest.fixture
def database(tmp_path) -> Database:
    database = Database(tmp_path / "answers.db")
    database.initialize()
    return database


@pytest.fixture
def inquiry_id(database: Database) -> int:
    return InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": "ANSWER-1",
            "title": "상품 문의",
            "content": "배송은 얼마나 걸리나요?",
            "product_name": "삼성 스마트모니터 M5",
            "raw_json": {},
        }
    ).inquiry_id


def result(answer: str = "프로그램 답변") -> AnswerResult:
    return AnswerResult(
        status=AnswerStatus.GENERATED,
        category="배송/택배",
        reason="택배 규칙",
        answer=answer,
        provider="rules",
        auto_answerable=True,
        needs_review=False,
    )


def test_program_draft_is_saved(
    database: Database,
    inquiry_id: int,
) -> None:
    draft = AnswerRepository(database).create_program_draft(
        inquiry_id,
        result(),
    )
    assert draft["program_status"] == "GENERATED"
    assert draft["original_answer"] == format_final_answer("프로그램 답변")
    assert draft["edited_answer"] is None
    assert draft["final_answer"] is None
    assert draft["review_status"] == "PENDING"
    assert draft["posted"] is False


def test_latest_draft_and_history_keep_multiple_versions(
    database: Database,
    inquiry_id: int,
) -> None:
    repository = AnswerRepository(database)
    first = repository.create_program_draft(inquiry_id, result("첫 답변"))
    second = repository.create_program_draft(inquiry_id, result("둘째 답변"))
    assert repository.latest_for_inquiry(inquiry_id)["id"] == second["id"]
    history = repository.history_for_inquiry(inquiry_id)
    assert [draft["id"] for draft in history] == [second["id"], first["id"]]


def test_edited_and_final_answer_fields_are_ready(
    database: Database,
    inquiry_id: int,
) -> None:
    repository = AnswerRepository(database)
    draft = repository.create_program_draft(inquiry_id, result())
    edited = repository.save_edited_answer(draft["id"], "직원 수정 답변")
    final = repository.save_final_answer(draft["id"], "최종 답변")
    reviewed = repository.update_review_status(draft["id"], "APPROVED")
    assert edited["edited_answer"] == format_final_answer("직원 수정 답변")
    assert final["final_answer"] == format_final_answer("최종 답변")
    assert reviewed["review_status"] == "APPROVED"


def test_posted_draft_cannot_be_changed_or_replaced(
    database: Database,
    inquiry_id: int,
) -> None:
    repository = AnswerRepository(database)
    draft = repository.create_program_draft(inquiry_id, result())
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE answer_drafts SET posted = 1, posted_at = ?
            WHERE id = ?
            """,
            ("2026-07-29T12:00:00Z", draft["id"]),
        )
    assert repository.is_inquiry_posted(inquiry_id) is True
    with pytest.raises(AnswerAlreadyPostedError):
        repository.save_edited_answer(draft["id"], "변경 금지")
    with pytest.raises(AnswerAlreadyPostedError):
        repository.create_program_draft(inquiry_id, result("새 답변"))


def test_generated_empty_answer_is_rejected(
    database: Database,
    inquiry_id: int,
) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        AnswerRepository(database).create_program_draft(
            inquiry_id,
            result(""),
        )


def test_inquiry_delete_cascades_answer_history(
    database: Database,
    inquiry_id: int,
) -> None:
    repository = AnswerRepository(database)
    repository.create_program_draft(inquiry_id, result())
    InquiryRepository(database).delete(inquiry_id)
    with database.connection() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM answer_drafts WHERE inquiry_id = ?",
            (inquiry_id,),
        ).fetchone()[0]
    assert count == 0
