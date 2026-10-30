"""The generated answer stays visible after a person answers or edits.

A person answering in Naver's or Coupang's seller centre made the next sync
mark the inquiry answered. On Coupang the panel then turned read-only and the
program answer disappeared from it, although the row was never touched:
``original_answer`` is only ever inserted. These pin both halves -- the record
is kept, and the screen shows it beside the market's reply without claiming
more about who wrote what than the record says.
"""

from __future__ import annotations

import sqlite3

import pytest
from streamlit.testing.v1 import AppTest

from answer.models import AnswerResult, AnswerStatus
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.naver_posted_answer_repository import (
    NaverPostedAnswerRepository,
)
from services.coupang_inquiry_normalizer import CoupangInquiryNormalizer
from services.inquiry_sync_service import normalize_work_item
from ui.answer_history_presenter import (
    FIRST_GENERATED,
    LATEST_GENERATED,
    MARKET_ANSWER,
    MARKET_ANSWER_NOTE,
    POSTED_DRAFT,
    SENT_ANSWER,
    STAFF_EDIT,
    build_answer_history,
    registration_label,
)

A = "GPT 최초 답변 A입니다."
B = "GPT 재생성 답변 B입니다."
C = "직원이 작성한 답변 C입니다."

MARKETS = pytest.mark.parametrize("store_code", ["OJE_PLUS", "COUPANG_OJE_NS"])


def _row(draft_id: int, original: str | None, *, created: str, **fields) -> dict:
    return {
        "id": draft_id, "original_answer": original, "created_at": created,
        "updated_at": created, "edited_answer": None, "final_answer": None,
        "posted": False, "source": "GPT", **fields,
    }


def _labels(history) -> list[str]:
    return [entry.label for entry in history.entries]


def _body(history, label: str) -> str:
    return next(entry.body for entry in history.entries if entry.label == label)


# --- the presenter, both markets -------------------------------------------------

@MARKETS
def test_staff_edit_then_registered_by_staff(store_code) -> None:
    """1. A -> staff edit B -> registered B."""

    inquiry = {"store_code": store_code, "source_answered": 1,
               "posted_draft_id": 1, "post_actor": "local-admin"}
    drafts = [_row(1, A, created="2026-10-30T01:00:00", edited_answer=B, posted=True)]
    history = build_answer_history(inquiry, drafts, market_answer=B)

    assert _labels(history) == [FIRST_GENERATED, STAFF_EDIT, MARKET_ANSWER]
    assert _body(history, FIRST_GENERATED) == A
    assert _body(history, STAFF_EDIT) == B
    assert history.compared_generated.body == A
    assert history.same_text is False
    assert history.registration == "Q&A Auto 직원 등록 (local-admin) · Draft #1"
    # A person's post sends the edit, which is already listed.
    assert history.market_matches_sent is True


@MARKETS
def test_staff_wrote_their_own_answer_in_the_seller_centre(store_code) -> None:
    """2. A -> staff writes C outside Q&A Auto -> market shows C."""

    inquiry = {"store_code": store_code, "source_answered": 1}
    history = build_answer_history(
        inquiry, [_row(1, A, created="2026-10-30T01:00:00")], market_answer=C
    )

    assert _labels(history) == [FIRST_GENERATED, MARKET_ANSWER]
    assert _body(history, FIRST_GENERATED) == A
    assert history.same_text is False
    # The record says only that Q&A Auto did not register it.
    assert history.registration == "Q&A Auto 등록 기록 없음 · 마켓에서 답변완료로 조회됨"
    assert STAFF_EDIT not in _labels(history)


@MARKETS
def test_auto_posted_unchanged(store_code) -> None:
    """3. A -> auto-posted A: the two read the same."""

    inquiry = {"store_code": store_code, "source_answered": 1,
               "posted_draft_id": 1, "post_actor": "SYSTEM_AUTO_POST"}
    drafts = [_row(1, A, created="2026-10-30T01:00:00", posted=True, final_answer=A)]
    history = build_answer_history(inquiry, drafts, market_answer=f"  {A}\n")

    assert _labels(history) == [FIRST_GENERATED, MARKET_ANSWER]
    assert history.same_text is True
    assert history.market_matches_sent is True
    assert history.registration == "Q&A Auto 자동등록 · Draft #1"


@MARKETS
def test_regenerated_then_edited_then_registered(store_code) -> None:
    """4. A -> regenerated B -> staff edits B to C -> registered C."""

    inquiry = {"store_code": store_code, "source_answered": 1,
               "posted_draft_id": 2, "post_actor": "local-admin"}
    drafts = [
        _row(2, B, created="2026-10-30T02:00:00", edited_answer=C, posted=True),
        _row(1, A, created="2026-10-30T01:00:00"),
    ]
    history = build_answer_history(inquiry, drafts, market_answer=C)

    assert _labels(history) == [FIRST_GENERATED, LATEST_GENERATED, STAFF_EDIT, MARKET_ANSWER]
    assert [_body(history, label) for label in _labels(history)] == [A, B, C, C]
    # What went out is compared against the row Q&A Auto registered.
    assert history.compared_generated.body == B
    assert "생성 이력 2건" in history.entries[1].note


@MARKETS
def test_changed_in_the_seller_centre_after_auto_post(store_code) -> None:
    """5. Auto-posted A, later changed outside Q&A Auto: shown, not explained."""

    inquiry = {"store_code": store_code, "source_answered": 1,
               "posted_draft_id": 1, "post_actor": "SYSTEM_AUTO_POST"}
    drafts = [_row(1, A, created="2026-10-30T01:00:00", posted=True, final_answer=A)]
    history = build_answer_history(inquiry, drafts, market_answer=C)

    assert history.same_text is False
    assert history.market_matches_sent is False
    assert history.registration == "Q&A Auto 자동등록 · Draft #1"
    assert STAFF_EDIT not in _labels(history)
    # The registration is Q&A Auto's record, not the author of today's reply.
    market = next(e for e in history.entries if e.label == MARKET_ANSWER)
    assert market.note == MARKET_ANSWER_NOTE and "자동등록" not in market.note


@MARKETS
def test_sent_text_that_differs_from_the_generated_text_is_listed(store_code) -> None:
    """11407's shape: auto-finalisation added a closing before sending, and the
    market now holds a different reply."""

    sent = A + "\n\n안내드린 내용이 문의하신 내용과 다른 경우 문의 남겨주세요."
    inquiry = {"store_code": store_code, "source_answered": 1,
               "posted_draft_id": 1, "post_actor": "SYSTEM_AUTO_POST"}
    drafts = [_row(1, A, created="2026-10-30T01:00:00", posted=True, final_answer=sent)]
    history = build_answer_history(inquiry, drafts, market_answer=C)

    assert _labels(history) == [FIRST_GENERATED, SENT_ANSWER, MARKET_ANSWER]
    assert _body(history, FIRST_GENERATED) == A
    assert _body(history, SENT_ANSWER) == sent
    assert "final_answer" in history.entries[1].note
    assert history.market_matches_sent is False


@MARKETS
def test_held_draft_is_unregistered_and_adds_nothing_to_the_tab(store_code) -> None:
    inquiry = {"store_code": store_code, "source_answered": 0}
    history = build_answer_history(
        inquiry, [_row(1, A, created="2026-10-30T01:00:00")]
    )

    assert history.registration == "미등록"
    assert _labels(history) == [FIRST_GENERATED]
    assert history.same_text is None


@MARKETS
def test_no_generated_answer_is_reported_as_none(store_code) -> None:
    """7. Answered, nothing generated: nothing is made up."""

    history = build_answer_history(
        {"store_code": store_code, "source_answered": 1}, [], market_answer=C
    )
    assert not history.has_generated
    assert history.compared_generated is None
    assert _labels(history) == [MARKET_ANSWER]


def test_first_latest_and_registered_are_picked_by_record_not_by_order() -> None:
    """8. Out-of-order rows, a tie on created_at, a failed generation, and a
    registered row that is neither the first nor the newest."""

    inquiry = {"store_code": "OJE_PLUS", "source_answered": 1,
               "posted_draft_id": 3, "post_actor": "SYSTEM_AUTO_POST"}
    drafts = [
        _row(5, "", created="2026-10-30T05:00:00"),           # failed, no text
        _row(4, "D", created="2026-10-30T04:00:00"),
        _row(3, "R", created="2026-10-30T03:00:00", posted=True, final_answer="R"),
        _row(2, B, created="2026-10-30T01:00:00"),
        _row(1, A, created="2026-10-30T01:00:00"),           # tie -> lower id first
    ]
    history = build_answer_history(inquiry, drafts, market_answer="R")

    assert [(e.label, e.draft_id) for e in history.entries] == [
        (FIRST_GENERATED, 1), (LATEST_GENERATED, 4), (POSTED_DRAFT, 3),
        (MARKET_ANSWER, None),
    ]
    assert history.generated_count == 4
    assert history.compared_generated.draft_id == 3
    assert history.same_text is True


def test_posted_status_alone_is_not_a_registration() -> None:
    """A sync that first sees an answered inquiry writes POSTED with no actor."""

    inquiry = {"store_code": "OJE_PLUS", "source_answered": 1,
               "post_status": "POSTED", "post_actor": None}
    assert registration_label(inquiry, [_row(1, A, created="x")]) == (
        "Q&A Auto 등록 기록 없음 · 마켓에서 답변완료로 조회됨"
    )


# --- the stored record -----------------------------------------------------------

@pytest.fixture
def database(tmp_path) -> Database:
    value = Database(tmp_path / "history.db")
    value.initialize()
    return value


def _draft(database: Database, inquiry_id: int, text: str) -> dict:
    return AnswerRepository(database).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.GENERATED, category="GENERAL", reason="test",
            answer=text, provider="rules", auto_answerable=True, needs_review=False,
        ),
    )


def _coupang_item(inquiry_id: str, reply: str | None) -> dict:
    return normalize_work_item(
        CoupangInquiryNormalizer().online(
            {
                "inquiryId": inquiry_id, "sellerProductId": "15654321531",
                "vendorItemId": "93128932886", "content": "벽걸이 설치 되나요?",
                "inquiryAt": "2026-10-30T10:00:00+09:00", "orderIds": [],
                "commentDtoList": [{
                    "inquiryCommentId": "c-1", "inquiryId": inquiry_id,
                    "content": reply, "inquiryCommentAt": "2026-10-30T11:00:00+09:00",
                }] if reply else [],
            },
            account_code="OJE_NS",
        ).to_work_item()
    )


def _coupang(database: Database, reply: str | None, *, drafts: tuple[str, ...] = (A,)) -> int:
    """A draft made while unanswered, the seller's reply read back later."""

    repo = InquiryRepository(database)
    row_id = repo.upsert_work_item(_coupang_item("170000001", None)).inquiry_id
    for text in drafts:
        _draft(database, row_id, text)
    if reply:
        assert repo.upsert_work_item(_coupang_item("170000001", reply)).inquiry_id == row_id
    return row_id


def _naver_item(answered: bool) -> dict:
    return {
        "store_code": "OJE_PLUS", "source_type": "PRODUCT_INQUIRY",
        "source_question_id": "N-HISTORY-1", "inquiry_type": "PRODUCT_INQUIRY",
        "title": "설치 문의", "content": "벽걸이 설치 되나요?", "product_name": "삼성 TV",
        "source_answered": answered, "raw_json": {},
    }


def _naver(database: Database, reply: str | None, *, drafts: tuple[str, ...] = (A,)) -> int:
    repo = InquiryRepository(database)
    row_id = repo.upsert_work_item(_naver_item(False)).inquiry_id
    for text in drafts:
        _draft(database, row_id, text)
    if reply:
        repo.upsert_work_item(_naver_item(True))
        NaverPostedAnswerRepository(database).observe(
            inquiry_id=row_id, answer_body=reply, answer_id="ANS-1",
            source_api="TEST",
        )
    return row_id


def _originals(database: Database, inquiry_id: int) -> list[str]:
    return [
        row["original_answer"]
        for row in reversed(AnswerRepository(database).history_for_inquiry(inquiry_id))
    ]


@pytest.mark.parametrize("make", [_coupang, _naver], ids=["coupang", "naver"])
def test_sync_after_a_seller_centre_answer_keeps_every_generated_answer(database, make) -> None:
    """6. The answered sync leaves the generated rows exactly as they were."""

    inquiry_id = make(database, None, drafts=(A, B))
    before = _originals(database, inquiry_id)
    make_again = make(database, C, drafts=())
    assert make_again == inquiry_id
    assert _originals(database, inquiry_id) == before
    assert any(A in text for text in before) and any(B in text for text in before)


def test_a_staff_edit_never_overwrites_the_generated_original(database) -> None:
    inquiry_id = _naver(database, None)
    draft = AnswerRepository(database).active_for_inquiry(inquiry_id)
    original = draft["original_answer"]
    AnswerRepository(database).save_edited_answer(int(draft["id"]), C)
    AnswerRepository(database).save_final_answer(int(draft["id"]), C)
    stored = AnswerRepository(database).get(int(draft["id"]))
    assert stored["original_answer"] == original
    assert C in stored["edited_answer"] and C in stored["final_answer"]
    # The only writer for answer text refuses the generated column outright.
    with pytest.raises(ValueError):
        AnswerRepository(database)._update_unposted(
            int(draft["id"]), "original_answer", C
        )


# --- the screen ------------------------------------------------------------------

# The dashboard loads the selected inquiry through ``get_by_source``, which is
# what attaches Coupang's ``seller_answer``; ``get`` alone does not.
_LOAD = """
def load(db, inquiry_id):
    row = InquiryRepository(db).get(inquiry_id)
    return InquiryRepository(db).get_by_source(
        row["store_code"], row["source_type"], row["source_question_id"]
    )
"""


def _render(database: Database, inquiry_id: int) -> AppTest:
    app = AppTest.from_string(f'''
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from ui.review_workspace import _render_answer_panel
{_LOAD}
db = Database(r"{database.path}")
_render_answer_panel(db, load(db, {inquiry_id}))
''').run(timeout=60)
    assert not app.exception, app.exception
    return app


def _history_expander(app: AppTest):
    return next(
        (item for item in app.expander if str(item.label).startswith("답변 이력")),
        None,
    )


def _shown(element) -> str:
    return "\n".join(
        str(item.value)
        for item in (*element.markdown, *element.caption, *element.text)
    )


def _activity_rows(database: Database) -> int:
    connection = sqlite3.connect(str(database.path))
    try:
        return connection.execute("SELECT COUNT(*) FROM activity_logs").fetchone()[0]
    finally:
        connection.close()


def test_coupang_answered_in_wing_still_shows_the_generated_answer(database) -> None:
    inquiry_id = _coupang(database, C)
    before = _activity_rows(database)
    app = _render(database, inquiry_id)

    # The read-only panel is otherwise as it was: one market view, no editing.
    assert [list(control.options) for control in app.segmented_control] == [
        ["쿠팡 실제 판매자 답변"]
    ]
    assert not any(str(b.label) in {"GPT 새 답변 생성", "임시 저장"} for b in app.button)
    expander = _history_expander(app)
    assert expander is not None
    shown = _shown(expander)
    assert A in shown and C in shown
    assert FIRST_GENERATED in shown and MARKET_ANSWER in shown
    assert "Q&A Auto 등록 기록: Q&A Auto 등록 기록 없음" in shown
    assert "차이만으로 직원 수정 여부를 판단하지 않습니다" in shown
    assert _activity_rows(database) == before


def test_naver_answered_in_the_seller_centre_shows_both(database) -> None:
    inquiry_id = _naver(database, C, drafts=(A, B))
    app = _render(database, inquiry_id)

    assert list(app.segmented_control[0].options) == [
        "Program Answer", "직원 수정본", "네이버 실제 등록 답변", "Final Answer"
    ]
    shown = _shown(_history_expander(app))
    for text in (A, B, C, FIRST_GENERATED, LATEST_GENERATED, MARKET_ANSWER):
        assert text in shown, text


def test_answered_without_any_generated_answer_says_so(database) -> None:
    inquiry_id = _coupang(database, C, drafts=())
    app = _render(database, inquiry_id)
    assert _history_expander(app) is None
    assert "생성된 프로그램 답변 없음" in "\n".join(str(c.value) for c in app.caption)


def test_an_unanswered_single_draft_does_not_repeat_the_program_tab(database) -> None:
    app = _render(database, _naver(database, None))
    assert _history_expander(app) is None


def test_the_history_follows_the_inquiry_across_reruns(database) -> None:
    """Moving to another inquiry and back reads the record again, not state."""

    first = _naver(database, C)
    second = _coupang(database, B)
    app = AppTest.from_string(f'''
import streamlit as st
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from ui.review_workspace import _render_answer_panel
{_LOAD}
db = Database(r"{database.path}")
_render_answer_panel(db, load(db, st.session_state.get("pick", {first})))
''').run(timeout=60)
    assert C in _shown(_history_expander(app))
    app.session_state["pick"] = second
    app.run(timeout=60)
    assert B in _shown(_history_expander(app)) and C not in _shown(_history_expander(app))
    app.session_state["pick"] = first
    app.run(timeout=60)
    assert not app.exception
    shown = _shown(_history_expander(app))
    assert A in shown and C in shown
