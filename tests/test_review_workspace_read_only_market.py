"""When the review workspace is read-only, and what selecting an inquiry may do.

Phase 2-1 opens Coupang answer *generation* for a person to review.  Two
things stay closed on this screen:

* Selecting an inquiry is a read.  On Naver it lazily creates the first draft;
  on a market whose answers are only generated on request, it creates nothing.
* An inquiry the marketplace already holds a seller reply for, on a market
  nothing here can post to, has nothing left to write -- it is view-only.

The gate is the inquiry's own store, never the dashboard's market picker.
"""

from __future__ import annotations

import pytest

from ui.review_workspace import _is_read_only_inquiry

NAVER = {"store_code": "OJE_PLUS"}
COUPANG_NS = {"store_code": "COUPANG_OJE_NS"}
COUPANG_PLUS = {"store_code": "COUPANG_OJE_PLUS"}


# --- the gate --------------------------------------------------------------

@pytest.mark.parametrize("inquiry", [COUPANG_NS, COUPANG_PLUS], ids=["ns", "plus"])
def test_an_unanswered_coupang_inquiry_is_reviewable(inquiry) -> None:
    assert _is_read_only_inquiry(dict(inquiry, source_answered=0)) is False


@pytest.mark.parametrize("inquiry", [COUPANG_NS, COUPANG_PLUS], ids=["ns", "plus"])
@pytest.mark.parametrize("flag", ["source_answered", "answered"])
def test_an_answered_coupang_inquiry_is_view_only(inquiry, flag) -> None:
    """Detail rows carry ``source_answered``; list items carry ``answered``."""

    assert _is_read_only_inquiry(dict(inquiry, **{flag: True})) is True


@pytest.mark.parametrize("answered", [False, True])
def test_naver_inquiries_are_never_read_only(answered) -> None:
    assert _is_read_only_inquiry(dict(NAVER, source_answered=answered)) is False


def test_the_dashboard_filter_does_not_decide() -> None:
    for _ in ("ALL", "NAVER", "COUPANG"):
        assert _is_read_only_inquiry(dict(COUPANG_NS, answered=True)) is True
        assert _is_read_only_inquiry(dict(COUPANG_NS, answered=False)) is False
        assert _is_read_only_inquiry(NAVER) is False


def test_a_row_with_no_store_is_read_only() -> None:
    """No store means no market, and no market may be written to."""

    assert _is_read_only_inquiry({}) is True
    assert _is_read_only_inquiry({"store_code": ""}) is True


def test_an_unrecognised_non_empty_code_is_still_treated_as_naver() -> None:
    """Recorded rather than endorsed: the shared rule reads non-COUPANG_ as Naver."""

    assert _is_read_only_inquiry({"store_code": "GMARKET_MAIN"}) is False


# --- selecting an inquiry must not generate ----------------------------------

def test_selection_generates_only_where_automatic_generation_is_open() -> None:
    """Both answerable markets draft on selection now, and an unanswerable
    one never would.

    This is the visible consequence of opening automatic generation for
    Coupang: selecting a Coupang inquiry drafts for it exactly as selecting a
    Naver one does, instead of waiting for a person to ask.  The read-only
    rule above is what keeps that away from inquiries Coupang has already
    answered.
    """

    from services.market_policy import is_store_automatic_generation_enabled

    assert is_store_automatic_generation_enabled("OJE_PLUS") is True
    assert is_store_automatic_generation_enabled("COUPANG_OJE_NS") is True
    assert is_store_automatic_generation_enabled("COUPANG_OJE_PLUS") is True
    assert _is_read_only_inquiry(
        {"store_code": "COUPANG_OJE_NS", "source_answered": True}
    ) is True


def test_the_selection_call_is_gated_by_automatic_generation() -> None:
    """The one call site that drafts on selection reads the automatic gate."""

    import inspect

    import ui.review_workspace as workspace

    source = inspect.getsource(workspace.render_review_workspace)
    gate = "if is_store_automatic_generation_enabled(inquiry.get(\"store_code\")):"
    assert gate in source
    after_gate = source[source.index(gate):]
    assert after_gate.index("_ensure_initial_program_answer(database, inquiry)") < 120


# --- the list label ----------------------------------------------------------

def _status_label(item: dict) -> str:
    """The same ladder ``_render_list`` walks."""

    if item.get("answered"):
        return "답변완료"
    if _is_read_only_inquiry(item):
        return "조회전용"
    if item.get("queue") == "AUTO_PROCESSABLE":
        return "생성가능"
    return "검토대기"


def test_an_answered_coupang_card_says_answered() -> None:
    assert _status_label(dict(COUPANG_NS, answered=True)) == "답변완료"


def test_an_unanswered_coupang_card_is_waiting_for_review() -> None:
    assert _status_label(dict(COUPANG_NS, answered=False)) == "검토대기"


@pytest.mark.parametrize(
    "item,expected",
    [
        pytest.param({**NAVER, "answered": True}, "답변완료", id="answered"),
        pytest.param({**NAVER, "answered": False, "queue": "AUTO_PROCESSABLE"}, "생성가능", id="auto"),
        pytest.param({**NAVER, "answered": False}, "검토대기", id="review"),
    ],
)
def test_naver_card_labels_are_unchanged(item, expected) -> None:
    assert _status_label(item) == expected


def test_the_learning_badge_is_untouched() -> None:
    from services.learning_lifecycle_service import LEARNING_STATUS_LABELS

    assert LEARNING_STATUS_LABELS == {
        "APPROVED": "승인", "AUTO": "자동", "EXCLUDED": "제외",
        "CORRECTED": "교정", "NONE": "-",
    }
