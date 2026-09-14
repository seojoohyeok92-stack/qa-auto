from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest


def _admin_details_app(database_path: Path, *, admin_enabled: bool) -> AppTest:
    return AppTest.from_string(
        f'''
import streamlit as st
from app import _render_admin_details
from repositories.database import Database

db = Database(r"{database_path}")
db.initialize()
st.session_state["production_admin_mode"] = {admin_enabled!r}
_render_admin_details(db, {{"auto_post_state": {{}}}})
'''
    ).run(timeout=40)


def test_coupang_admin_entry_is_hidden_when_admin_mode_is_off(tmp_path: Path) -> None:
    app = _admin_details_app(tmp_path / "admin-off.db", admin_enabled=False)

    assert not app.exception
    assert "쿠팡 관리" not in {button.label for button in app.button}


def test_coupang_admin_entry_sets_the_coupang_route_when_admin_mode_is_on(
    tmp_path: Path,
) -> None:
    app = _admin_details_app(tmp_path / "admin-on.db", admin_enabled=True)

    assert not app.exception
    entry = next(button for button in app.button if button.label == "쿠팡 관리")
    entry.click().run(timeout=40)

    assert app.session_state["current_page"] == "coupang"


def test_coupang_route_renders_without_constructing_a_read_client(tmp_path: Path) -> None:
    app = AppTest.from_string(
        f'''
from repositories.database import Database
import ui.coupang_management as management

class UnexpectedReadClient:
    def __init__(self, *args, **kwargs):
        raise AssertionError("normal render must not construct a Coupang client")

management.CoupangReadClient = UnexpectedReadClient
db = Database(r"{tmp_path / 'coupang-route.db'}")
db.initialize()
management.render_coupang_management(db)
'''
    ).run(timeout=40)

    assert not app.exception
    assert any(title.value == "쿠팡 관리" for title in app.title)
