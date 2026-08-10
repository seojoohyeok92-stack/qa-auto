from __future__ import annotations

import streamlit as st

from core.time_utils import format_datetime_kst
from repositories.database import Database
from repositories.inquiry_repository import deserialize_json
from services.local_auth_service import Permission
from ui.session_identity import can, current_role


def render_activity_log_panel(database: Database | None) -> None:
    st.title("활동 로그")
    st.caption("저장 시 마스킹된 최근 활동만 표시합니다.")
    if database is None:
        st.error("DB를 사용할 수 없어 활동 로그를 표시할 수 없습니다.")
        return
    if not can(Permission.ACTIVITY_LOG_FULL):
        st.warning("현재 역할에는 전체 Activity Log 조회 권한이 없습니다.")
        return
    limit = st.selectbox("표시 건수", (50, 100, 200), index=1)
    with database.connection() as connection:
        rows = connection.execute(
            """
            SELECT id, inquiry_id, level, event_code, message,
                   details_json, created_at
            FROM activity_logs
            ORDER BY created_at DESC, id DESC LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    table = []
    for row in rows:
        item = dict(row)
        item["created_at"] = format_datetime_kst(item.get("created_at"))
        details = deserialize_json(item.pop("details_json"))
        item["actor"] = details.get("actor") if isinstance(details, dict) else None
        item["error_code"] = (
            details.get("error_code") if isinstance(details, dict) else None
        )
        table.append(item)
    st.dataframe(table, hide_index=True, width="stretch")
    if current_role().value == "MANAGER":
        st.caption("MANAGER는 최근 처리 활동만 확인할 수 있으며 설정 비교 이력은 제외됩니다.")
