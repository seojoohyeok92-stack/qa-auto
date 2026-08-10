from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest


def run(code: str) -> AppTest:
    return AppTest.from_string(code).run(timeout=20)


def test_uat_page_renders_without_exception(tmp_path: Path) -> None:
    path = tmp_path / "uat.db"
    at = run(
        f'''
from repositories.database import Database
from ui.uat_panel import render_uat_panel
db=Database(r"{path}")
db.initialize()
render_uat_panel(db)
'''
    )
    assert not at.exception
    assert len(at.button) >= 3


def test_uat_page_agent_role_disables_admin_controls(tmp_path: Path) -> None:
    path = tmp_path / "uat-agent.db"
    at = run(
        f'''
import streamlit as st
from repositories.database import Database
from ui.uat_panel import render_uat_panel
st.session_state["local_identity"]={{"id":1,"username":"agent","display_name":"상담원","role":"AGENT","force_password_change":False,"auth_enabled":True}}
db=Database(r"{path}")
db.initialize()
render_uat_panel(db)
'''
    )
    assert not at.exception
    compare_buttons = [
        item for item in at.button if item.label == "비교 보고서 생성"
    ]
    assert compare_buttons and compare_buttons[0].disabled


def test_uat_page_database_failure_is_user_message() -> None:
    at = run(
        """
from ui.uat_panel import render_uat_panel
render_uat_panel(None)
"""
    )
    assert not at.exception
    assert at.error


def test_sidebar_contains_uat_menu() -> None:
    at = run(
        """
from ui.sidebar import render_sidebar
render_sidebar([])
"""
    )
    assert not at.exception
    assert not any("UAT" in button.label for button in at.button)
    assert {button.label for button in at.button} == {"Dashboard"}


def test_inquiry_detail_masks_customer() -> None:
    at = run(
        """
from ui.review_workspace import _render_inquiry_detail
_render_inquiry_detail({"source_question_id":"q1","registered_at":"2026-07-29T00:00:00","inquiry_type":"배송","order_id":"2026072912345678","product_name":"TV","customer_display":"홍길동","content":"문의","answer_status":"UNANSWERED"})
"""
    )
    assert not at.exception
    assert "홍길동" not in str(at)


def test_gpt_source_message_distinguishes_fake() -> None:
    at = run(
        """
import streamlit as st
from ui.uat_presenters import answer_source_label, external_ai_called
draft={"provider":"fake_gpt_hybrid"}
st.write(answer_source_label(draft))
st.write("외부 호출" if external_ai_called(draft) else "실제 외부 AI 호출 없음")
"""
    )
    assert not at.exception
    assert any("FAKE_PROVIDER" in item.value for item in at.markdown)
    assert any("실제 외부 AI 호출 없음" in item.value for item in at.markdown)


def test_settings_governance_still_renders(tmp_path: Path) -> None:
    path = tmp_path / "settings.db"
    at = run(
        f'''
from repositories.database import Database
from ui.gpt_governance_panel import render_gpt_governance_panel
db=Database(r"{path}")
db.initialize()
render_gpt_governance_panel(db)
'''
    )
    assert not at.exception
    assert len(at.metric) >= 10


def test_local_auth_disabled_uses_development_admin(tmp_path: Path) -> None:
    path = tmp_path / "auth.db"
    at = run(
        f'''
import os
import streamlit as st
from repositories.database import Database
from ui.local_auth_panel import ensure_local_identity
os.environ["QNA_LOCAL_AUTH_ENABLED"]="false"
db=Database(r"{path}")
db.initialize()
ensure_local_identity(db)
st.write(st.session_state["local_identity"]["role"])
'''
    )
    assert not at.exception
    assert any("ADMIN" in item.value for item in at.markdown)
