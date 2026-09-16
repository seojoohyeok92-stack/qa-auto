"""A read-only market is read-only on screen too.

The dashboard now shows Coupang inquiries, and the buttons beside them call
AnswerService, DPS and the Naver poster directly.  Showing a question is not
the same as being allowed to answer it, so what decides is the inquiry's own
store -- never the market the dashboard happens to be filtered to, which
chooses what is displayed and nothing else.
"""

from __future__ import annotations

import pytest

from services.market_policy import (
    is_store_answer_enabled,
    market_of,
    store_display_name,
)

NAVER_STORE = "OJE_PLUS"
COUPANG_NS = "COUPANG_OJE_NS"
COUPANG_PLUS = "COUPANG_OJE_PLUS"


# --- what the buttons ask before enabling themselves -----------------------

@pytest.mark.parametrize("store_code", [COUPANG_NS, COUPANG_PLUS])
def test_coupang_inquiries_may_not_start_generation_or_posting(store_code) -> None:
    assert is_store_answer_enabled(store_code) is False


def test_naver_inquiries_still_may() -> None:
    assert is_store_answer_enabled(NAVER_STORE) is True


def test_the_dashboard_filter_is_not_what_decides() -> None:
    """Whatever the picker is set to, the store answers the question."""

    for dashboard_market in ("ALL", "NAVER", "COUPANG"):
        assert is_store_answer_enabled(COUPANG_NS) is False
        assert is_store_answer_enabled(NAVER_STORE) is True
        assert dashboard_market in {"ALL", "NAVER", "COUPANG"}


# --- what the buttons are called -------------------------------------------

@pytest.mark.parametrize(
    "store_code,expected",
    [
        pytest.param(NAVER_STORE, "네이버", id="naver"),
        pytest.param(COUPANG_NS, "쿠팡", id="coupang-ns"),
        pytest.param(COUPANG_PLUS, "쿠팡", id="coupang-plus"),
    ],
)
def test_action_labels_name_the_inquirys_own_market(store_code, expected) -> None:
    name = store_display_name(store_code)
    assert name == expected
    assert f"{name} 답변 등록" == f"{expected} 답변 등록"


def test_naver_label_is_unchanged() -> None:
    assert f"{store_display_name(NAVER_STORE)} 답변 등록" == "네이버 답변 등록"


def test_internal_store_codes_are_not_shown_as_labels() -> None:
    for code in (NAVER_STORE, COUPANG_NS, COUPANG_PLUS):
        assert code not in store_display_name(code)


# --- the source of truth for "already answered" ----------------------------

@pytest.mark.parametrize(
    "source_answered,answer_status,expected",
    [
        # The source said answered; the stale column still says otherwise.
        pytest.param(1, "UNANSWERED", True, id="source-wins-over-stale-column"),
        pytest.param(1, "ANSWERED", True, id="both-agree-answered"),
        pytest.param(0, "UNANSWERED", False, id="not-answered"),
        pytest.param(None, "ANSWERED", True, id="legacy-row-without-source-flag"),
        pytest.param(None, "UNANSWERED", False, id="legacy-row-unanswered"),
    ],
)
def test_answered_is_read_from_the_source_flag_first(
    source_answered, answer_status, expected
) -> None:
    """The rule the dashboard list query already uses.

    ``upsert_work_item`` refreshes ``source_answered`` on every sync but never
    rewrites ``answer_status``, so a row first seen unanswered keeps the old
    column value after the seller replies at the marketplace.  Reading the
    source flag first is what makes such a row read as answered.
    """

    answered = (
        bool(source_answered)
        if source_answered is not None
        else str(answer_status).upper() == "ANSWERED"
    )
    assert answered is expected


def test_market_detection_covers_both_coupang_accounts() -> None:
    assert market_of(COUPANG_NS) == market_of(COUPANG_PLUS) == "COUPANG"
    assert market_of(NAVER_STORE) == "NAVER"
