from __future__ import annotations

import streamlit as st

from services.local_auth_service import Permission
from uat.models import UserRole


def current_identity() -> dict[str, str | int | bool]:
    value = st.session_state.get("local_identity")
    if isinstance(value, dict):
        return value
    return {
        "id": 0,
        "username": "local-admin",
        "display_name": "로컬 관리자",
        "role": UserRole.ADMIN.value,
        "force_password_change": False,
        "auth_enabled": False,
    }


def current_actor() -> str:
    return str(current_identity()["username"])


def current_role() -> UserRole:
    return UserRole(str(current_identity()["role"]))


def can(permission: Permission | str) -> bool:
    from services.local_auth_service import ROLE_PERMISSIONS

    return Permission(permission) in ROLE_PERMISSIONS[current_role()]

