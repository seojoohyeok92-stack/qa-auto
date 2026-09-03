"""The dashboard's date filter across a KST midnight.

The operations PC leaves the dashboard open. The default range was applied once
per session behind ``dashboard_full_range_v1``, so after midnight "오늘" still
meant yesterday and inquiries arriving on the new day sat outside the filter
until somebody noticed and pressed 초기화.

Following the calendar is only half of it. A range the operator picked is an
instruction, and a date change is not a reason to discard it -- so these pin
both directions, plus the reset that has to keep working either way.
"""
from __future__ import annotations

from datetime import date

from streamlit.testing.v1 import AppTest


# The script re-executes on every rerun, so the "current day" has to come from
# somewhere that survives one: session_state is what the test drives.
SCRIPT = """
import datetime
import streamlit as st
import ui.dashboard as dash

day = st.session_state.get("fake_kst_day", 2)
dash.today_kst = lambda: datetime.date(2026, 9, day)

if st.session_state.get("simulate_reset"):
    for key in (
        "dashboard_date_range",
        "dashboard_full_range_v1",
        dash.AUTO_RANGE_STATE_KEY,
    ):
        st.session_state.pop(key, None)
    st.session_state["simulate_reset"] = False

dash.render_header(items=[], database=None, states=[])
"""


def _run(**session):
    app = AppTest.from_string(SCRIPT)
    for key, value in session.items():
        app.session_state[key] = value
    return app.run(timeout=30)


def _range(app):
    return app.session_state["dashboard_date_range"]


def test_a_same_kst_day_keeps_the_existing_range() -> None:
    """No date change, no reassignment: reruns must be inert."""

    app = _run()
    first = _range(app)
    assert first == (date(2026, 8, 3), date(2026, 9, 2))

    app = app.run(timeout=30)

    assert _range(app) == first


def test_b_kst_midnight_moves_the_automatic_range_to_the_new_today() -> None:
    """The bug: the end date stayed on the previous day for the whole session."""

    app = _run()
    assert _range(app)[1] == date(2026, 9, 2)

    app.session_state["fake_kst_day"] = 3
    app = app.run(timeout=30)

    assert _range(app) == (date(2026, 8, 4), date(2026, 9, 3))


def test_c_a_manually_chosen_range_survives_midnight() -> None:
    """An operator looking at one past day keeps looking at it."""

    app = _run()
    chosen = (date(2026, 8, 10), date(2026, 8, 12))
    app.session_state["dashboard_date_range"] = chosen
    app = app.run(timeout=30)
    assert _range(app) == chosen

    app.session_state["fake_kst_day"] = 3
    app = app.run(timeout=30)

    assert _range(app) == chosen


def test_c2_a_manual_range_is_not_reclaimed_on_a_later_day_either() -> None:
    """Once it is theirs it stays theirs -- not just for the first rollover."""

    app = _run()
    chosen = (date(2026, 8, 10), date(2026, 8, 12))
    app.session_state["dashboard_date_range"] = chosen
    app.session_state["fake_kst_day"] = 3
    app = app.run(timeout=30)
    app.session_state["fake_kst_day"] = 4
    app = app.run(timeout=30)

    assert _range(app) == chosen


def test_d_reset_restores_the_default_range_for_the_current_kst_day() -> None:
    """초기화 has to hand back today's range, not the day the session began."""

    app = _run()
    app.session_state["dashboard_date_range"] = (
        date(2026, 8, 10), date(2026, 8, 12),
    )
    app.session_state["fake_kst_day"] = 5
    app = app.run(timeout=30)

    app.session_state["simulate_reset"] = True
    app = app.run(timeout=30)

    assert _range(app) == (date(2026, 8, 6), date(2026, 9, 5))


def test_the_automatic_range_is_tracked_with_the_day_it_was_derived_for() -> None:
    """The state the rollover reads, so a later change cannot silently break it."""

    app = _run()
    from ui.dashboard import AUTO_RANGE_STATE_KEY

    applied = app.session_state[AUTO_RANGE_STATE_KEY]

    assert applied == (date(2026, 9, 2), (date(2026, 8, 3), date(2026, 9, 2)))
