from __future__ import annotations

from types import SimpleNamespace

import pytest

from answer.answer_format import format_final_answer
from config import StoreConfig
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.post_review_repository import PostReviewRepository
from services.auto_post_confirmation_service import (
    AutoPostConfirmationError,
    AutoPostConfirmationService,
)
from tests.test_auto_post_pipeline import make_draft, make_inquiry


def _database(tmp_path) -> Database:
    database = Database(tmp_path / "confirmation.db")
    database.initialize()
    return database


class FakeSync:
    def __init__(self, normalizer, *, answer: str, answered: bool = True) -> None:
        self.normalizer = normalizer
        self.answer = answer
        self.answered = answered

    def sync_inquiries(self, **kwargs):
        self.normalizer.product(
            {
                "questionId": "Q-1",
                "question": "문의",
                "answered": self.answered,
                "commentContent": self.answer,
            },
            store_code="OJE_PLUS",
        )
        return SimpleNamespace(status="SUCCESS")


def _ready(database: Database) -> tuple[int, str]:
    inquiry_id = make_inquiry(database)
    draft = make_draft(database, inquiry_id, route="TEMPLATE")
    PostReviewRepository(database).finalize_auto(
        inquiry_id=inquiry_id,
        draft_id=int(draft["id"]),
        run_id="RUN-1",
    )
    final = AnswerRepository(database).active_for_inquiry(inquiry_id)["final_answer"]
    return inquiry_id, final


def test_confirmation_requires_fresh_answered_target_and_exact_body(tmp_path) -> None:
    database = _database(tmp_path)
    inquiry_id, final = _ready(database)
    service = AutoPostConfirmationService(
        database,
        store_resolver=lambda _: StoreConfig("OJE_PLUS", "테스트", "id", "secret"),
        sync_factory=lambda _db, normalizer: FakeSync(normalizer, answer=final),
    )
    result = service.confirm(inquiry_id, run_id="RUN-1")
    assert result.source_answered is True
    assert result.body_matched is True


@pytest.mark.parametrize(
    ("answered", "answer", "code"),
    [
        (False, "same", "SOURCE_ANSWERED_MISMATCH"),
        (True, "different", "REMOTE_ANSWER_MISMATCH"),
    ],
)
def test_confirmation_blocks_unconfirmed_remote_state(
    tmp_path, answered: bool, answer: str, code: str,
) -> None:
    database = _database(tmp_path)
    inquiry_id, final = _ready(database)
    remote_answer = final if answer == "same" else format_final_answer(answer)
    service = AutoPostConfirmationService(
        database,
        store_resolver=lambda _: StoreConfig("OJE_PLUS", "테스트", "id", "secret"),
        sync_factory=lambda _db, normalizer: FakeSync(
            normalizer, answer=remote_answer, answered=answered
        ),
    )
    with pytest.raises(AutoPostConfirmationError, match=code):
        service.confirm(inquiry_id, run_id="RUN-1")
