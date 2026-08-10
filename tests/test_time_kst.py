from __future__ import annotations

from datetime import UTC, datetime

import pytest
from streamlit.testing.v1 import AppTest

from core.time_utils import format_datetime_kst, to_kst
from services.work_queue_service import parse_registered_at


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-07-31T07:46:50.745+00:00", "2026-07-31 16:46:50"),
        ("2026-07-31T07:46:50Z", "2026-07-31 16:46:50"),
        ("2026-07-31T07:46:50+00:00", "2026-07-31 16:46:50"),
        ("2026-07-31T16:46:50+09:00", "2026-07-31 16:46:50"),
        ("2026-07-31T18:30:00+00:00", "2026-08-01 03:30:00"),
        (datetime(2026, 7, 31, 7, 46, 50, tzinfo=UTC), "2026-07-31 16:46:50"),
        (datetime(2026, 7, 31, 7, 46, 50), "2026-07-31 16:46:50"),
        (None, "-"),
        ("", "-"),
        ("not-a-date", "-"),
    ],
)
def test_format_datetime_kst(value, expected: str) -> None:
    assert format_datetime_kst(value) == expected


def test_kst_conversion_is_timezone_aware_and_legacy_parser_uses_kst() -> None:
    value = to_kst("2026-07-31T07:46:50Z")
    assert value is not None
    assert value.tzinfo is not None
    assert value.utcoffset().total_seconds() == 9 * 3600
    assert parse_registered_at("2026-07-31T07:46:50Z") == datetime(
        2026, 7, 31, 16, 46, 50
    )


def test_dashboard_sync_result_renders_kst(tmp_path) -> None:
    path = tmp_path / "time-ui.db"
    app = AppTest.from_string(
        f'''
import streamlit as st
from app import _render_sync_result
from repositories.database import Database
db=Database(r"{path}")
db.initialize()
st.session_state["dashboard_sync_result"]={{
 "status":"SUCCESS","requested_store_count":1,"fetched_count":0,
 "created_count":0,"updated_count":0,"unchanged_count":0,
 "skipped_count":0,"failed_count":0,"errors":[],
 "requested_from":"2026-07-31T07:00:00Z",
 "requested_to":"2026-07-31T07:46:50Z"
}}
_render_sync_result(db)
'''
    ).run(timeout=30)
    assert not app.exception
    captions = "\n".join(item.value for item in app.caption)
    assert "2026-07-31 16:00:00" in captions
    assert "2026-07-31 16:46:50" in captions


def test_activity_log_panel_renders_kst(tmp_path) -> None:
    path = tmp_path / "activity-time.db"
    app = AppTest.from_string(
        f'''
from repositories.database import Database
from ui.activity_log_panel import render_activity_log_panel
db=Database(r"{path}")
db.initialize()
with db.transaction() as connection:
    connection.execute(
      "INSERT INTO activity_logs(level,event_code,message,created_at) "
      "VALUES ('INFO','TIME_TEST','ok','2026-07-31T07:46:50+00:00')"
    )
render_activity_log_panel(db)
'''
    ).run(timeout=30)
    assert not app.exception
    assert app.dataframe
    assert "2026-07-31 16:46:50" in app.dataframe[0].value.to_string()
