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


class ScriptedSync:
    """One scripted remote read per confirmation attempt.

    ``reads`` is a list of ``(answered, commentContent)`` pairs consumed in
    order; the last entry repeats once exhausted. This is how the read-after-
    write gap is reproduced: the first read shows the answer missing, a later
    one shows it published.
    """

    calls = 0

    def __init__(self, normalizer, *, reads) -> None:
        self.normalizer = normalizer
        self.reads = list(reads)

    def sync_inquiries(self, **kwargs):
        index = min(type(self).calls, len(self.reads) - 1)
        answered, body = self.reads[index]
        type(self).calls += 1
        self.normalizer.product(
            {
                "questionId": "Q-1",
                "question": "문의",
                "answered": answered,
                "commentContent": body,
            },
            store_code="OJE_PLUS",
        )
        return SimpleNamespace(status="SUCCESS")


def _scripted_service(database, reads):
    """Confirmation service whose remote reads follow ``reads``, without waits."""

    ScriptedSync.calls = 0
    waits: list[float] = []
    service = AutoPostConfirmationService(
        database,
        store_resolver=lambda _: StoreConfig("OJE_PLUS", "테스트", "id", "secret"),
        sync_factory=lambda _db, normalizer: ScriptedSync(normalizer, reads=reads),
        sleeper=waits.append,
    )
    return service, waits


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
        # The retry that P0-1 added would otherwise make this case wait through
        # the real backoff before reporting the same verdict.
        sleeper=lambda _seconds: None,
    )
    with pytest.raises(AutoPostConfirmationError, match=code):
        service.confirm(inquiry_id, run_id="RUN-1")


def test_body_not_yet_published_is_not_reported_as_a_mismatch(tmp_path) -> None:
    """A 204 whose answer has not appeared yet must not look like wrong content.

    This is the production incident: the read fired ~1s after the PUT, the
    body came back empty, and REMOTE_ANSWER_MISMATCH paused every inquiry's
    auto-post. The body was correct all along.
    """

    database = _database(tmp_path)
    inquiry_id, final = _ready(database)
    service, _ = _scripted_service(database, [(True, ""), (True, "")])
    with pytest.raises(AutoPostConfirmationError) as excinfo:
        service.confirm(inquiry_id, run_id="RUN-1")
    assert str(excinfo.value) == "REMOTE_ANSWER_NOT_VISIBLE"
    assert str(excinfo.value) != "REMOTE_ANSWER_MISMATCH"


def test_not_yet_visible_does_not_pause_auto_post(tmp_path) -> None:
    """The unconfirmed-but-not-wrong codes stay out of the immediate pause set."""

    from services.auto_post_pipeline_service import AutoPostPipelineService

    source = AutoPostPipelineService._pause_for_system_error.__code__.co_consts
    immediate = next(
        const for const in source
        if isinstance(const, frozenset) and "REMOTE_ANSWER_MISMATCH" in const
    )
    assert "REMOTE_ANSWER_NOT_VISIBLE" not in immediate
    assert "REMOTE_TARGET_NOT_FOUND" not in immediate
    # A genuinely different body still pauses.
    assert "REMOTE_ANSWER_MISMATCH" in immediate


def test_confirmation_succeeds_once_the_body_appears(tmp_path) -> None:
    """Retry: invisible on the first read, published on the second."""

    database = _database(tmp_path)
    inquiry_id, final = _ready(database)
    service, waits = _scripted_service(database, [(True, ""), (True, final)])
    result = service.confirm(inquiry_id, run_id="RUN-1")
    assert result.body_matched is True
    assert result.source_answered is True
    assert waits, "the second attempt must be preceded by a backoff wait"


def test_real_mismatch_still_blocks_without_retrying(tmp_path) -> None:
    """A present-but-different body keeps the original safety, and fails fast."""

    database = _database(tmp_path)
    inquiry_id, final = _ready(database)
    service, waits = _scripted_service(
        database, [(True, format_final_answer("완전히 다른 답변")), (True, final)],
    )
    with pytest.raises(AutoPostConfirmationError, match="REMOTE_ANSWER_MISMATCH"):
        service.confirm(inquiry_id, run_id="RUN-1")
    assert waits == [], "a real mismatch must not be retried into a pass"


def test_bounded_retry_does_not_loop_forever(tmp_path) -> None:
    """Attempts are capped, so one stuck inquiry cannot stall a run."""

    database = _database(tmp_path)
    inquiry_id, _ = _ready(database)
    service, waits = _scripted_service(database, [(True, "")])
    with pytest.raises(AutoPostConfirmationError, match="REMOTE_ANSWER_NOT_VISIBLE"):
        service.confirm(inquiry_id, run_id="RUN-1")
    assert ScriptedSync.calls == service.attempts
    assert len(waits) == service.attempts - 1
