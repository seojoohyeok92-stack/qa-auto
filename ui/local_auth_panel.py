from __future__ import annotations

import streamlit as st

from repositories.database import Database
from repositories.local_user_repository import LocalUserRepository
from services.local_auth_service import (
    AuthenticationError,
    LocalAuthService,
    local_auth_enabled,
)


def ensure_local_identity(database: Database) -> None:
    if not local_auth_enabled():
        st.session_state.setdefault(
            "local_identity",
            {
                "id": 0,
                "username": "local-admin",
                "display_name": "로컬 관리자",
                "role": "ADMIN",
                "force_password_change": False,
                "auth_enabled": False,
            },
        )
        return
    if isinstance(st.session_state.get("local_identity"), dict):
        return
    users = LocalUserRepository(database)
    st.title("Q&A auto 로컬 로그인")
    if not users.list_users():
        st.warning(
            "로컬 사용자가 없습니다. scripts/manage_local_user.py를 실행해 "
            "초기 ADMIN을 생성한 뒤 로그인하세요."
        )
        st.stop()
    with st.form("local_login_form"):
        username = st.text_input("사용자명")
        password = st.text_input("비밀번호", type="password")
        submitted = st.form_submit_button("로그인", type="primary")
    if submitted:
        try:
            user = LocalAuthService(database).authenticate(username, password)
        except AuthenticationError as error:
            st.error(str(error))
        else:
            st.session_state["local_identity"] = {
                "id": user.id,
                "username": user.username,
                "display_name": user.display_name,
                "role": user.role.value,
                "force_password_change": user.force_password_change,
                "auth_enabled": True,
            }
            st.rerun()
    st.stop()

