"""The review workspace is read-only for a market production only collects.

Every control on this screen writes: drafts through AutomaticDraftService and
AnswerService, DPS lookups, approval state, Learning rows, registration.  The
data-layer boundaries added earlier stop the queue and the notifications, but
a person clicking in the dashboard went straight past them -- merely selecting
a Coupang inquiry created a draft for it.

So the gate is the inquiry's own store, and it is applied twice on purpose:
the widget is disabled, and the intent it produces is cleared.  A disabled
button is a UI state, and a rerun can replay a click that a fresh render would
have refused.
"""

from __future__ import annotations

import pytest

from ui.review_workspace import _is_read_only_inquiry

NAVER = {"store_code": "OJE_PLUS"}
COUPANG_NS = {"store_code": "COUPANG_OJE_NS"}
COUPANG_PLUS = {"store_code": "COUPANG_OJE_PLUS"}


# --- the gate --------------------------------------------------------------

@pytest.mark.parametrize(
    "inquiry", [COUPANG_NS, COUPANG_PLUS], ids=["ns", "plus"]
)
def test_coupang_inquiries_are_read_only(inquiry) -> None:
    assert _is_read_only_inquiry(inquiry) is True


def test_naver_inquiries_are_not() -> None:
    assert _is_read_only_inquiry(NAVER) is False


def test_the_dashboard_filter_does_not_decide() -> None:
    """Whatever the market picker is set to, the store answers the question."""

    for _ in ("ALL", "NAVER", "COUPANG"):
        assert _is_read_only_inquiry(COUPANG_NS) is True
        assert _is_read_only_inquiry(NAVER) is False


# --- selecting an inquiry must not write ------------------------------------

def test_selecting_a_coupang_inquiry_creates_no_draft(monkeypatch) -> None:
    """The call that made merely opening a question generate an answer."""

    import ui.review_workspace as workspace

    calls: list[int] = []
    monkeypatch.setattr(
        workspace, "_ensure_initial_program_answer",
        lambda database, inquiry: calls.append(int(inquiry["id"])) or True,
    )

    for inquiry in (dict(COUPANG_NS, id=1), dict(COUPANG_PLUS, id=2)):
        if not workspace._is_read_only_inquiry(inquiry):
            workspace._ensure_initial_program_answer(None, inquiry)
    assert calls == []

    naver = dict(NAVER, id=3)
    if not workspace._is_read_only_inquiry(naver):
        workspace._ensure_initial_program_answer(None, naver)
    assert calls == [3]


# --- the source is the same one the rest of the boundary uses ---------------

def test_the_gate_reads_the_shared_market_policy() -> None:
    from services.market_policy import is_store_answer_enabled

    for inquiry in (NAVER, COUPANG_NS, COUPANG_PLUS):
        expected = not is_store_answer_enabled(inquiry["store_code"])
        assert _is_read_only_inquiry(inquiry) is expected


def test_a_row_with_no_store_is_read_only() -> None:
    """No store means no market, and no market may be written to.

    ``market_from_store_code`` returns None for an empty code rather than
    guessing Naver, so the unknown case fails closed here.
    """

    assert _is_read_only_inquiry({}) is True
    assert _is_read_only_inquiry({"store_code": ""}) is True


def test_an_unrecognised_non_empty_code_is_still_treated_as_naver() -> None:
    """The other half of the same rule, recorded rather than endorsed.

    Anything that is not the COUPANG_ prefix is Naver -- the corpus predating
    marketplaces is Naver.  The Kakao gate and the auto-post queue read the
    same rule, so this screen must not disagree with it; when a third
    marketplace arrives, that rule is what has to learn about it.
    """

    assert _is_read_only_inquiry({"store_code": "GMARKET_MAIN"}) is False


# --- the list label --------------------------------------------------------

def _status_label(item: dict) -> str:
    """The same ladder ``_render_list`` walks, kept in one place to assert."""

    if item.get("answered"):
        return "답변완료"
    if _is_read_only_inquiry(item):
        return "조회전용"
    if item.get("queue") == "AUTO_PROCESSABLE":
        return "생성가능"
    return "검토대기"


@pytest.mark.parametrize(
    "answered,expected",
    [
        pytest.param(True, "답변완료", id="answered"),
        pytest.param(False, "조회전용", id="unanswered"),
        pytest.param(None, "조회전용", id="unknown"),
    ],
)
def test_a_coupang_card_never_says_it_is_awaiting_review(answered, expected) -> None:
    label = _status_label(dict(COUPANG_NS, answered=answered))
    assert label == expected
    assert label != "검토대기"


def test_a_coupang_card_is_not_offered_as_generatable() -> None:
    """Auto-processable is about a queue Coupang inquiries never enter."""

    label = _status_label(
        dict(COUPANG_NS, answered=False, queue="AUTO_PROCESSABLE")
    )
    assert label == "조회전용"


@pytest.mark.parametrize(
    "item,expected",
    [
        pytest.param({**NAVER, "answered": True}, "답변완료", id="answered"),
        pytest.param(
            {**NAVER, "answered": False, "queue": "AUTO_PROCESSABLE"},
            "생성가능", id="auto",
        ),
        pytest.param({**NAVER, "answered": False}, "검토대기", id="review"),
    ],
)
def test_naver_card_labels_are_unchanged(item, expected) -> None:
    assert _status_label(item) == expected


def test_the_learning_badge_is_untouched() -> None:
    """Second badge is Learning lifecycle and keeps its own vocabulary."""

    from services.learning_lifecycle_service import LEARNING_STATUS_LABELS

    assert LEARNING_STATUS_LABELS == {
        "APPROVED": "승인",
        "AUTO": "자동",
        "EXCLUDED": "제외",
        "CORRECTED": "교정",
        "NONE": "-",
    }
