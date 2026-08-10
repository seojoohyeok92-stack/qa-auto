from __future__ import annotations

from html import escape
from typing import Any

import streamlit as st

from config import NaverAutoPostSettings, NaverPostSettings, NaverSyncSettings
from core.time_utils import format_datetime_kst
from repositories.auto_post_repository import AutoPostRepository
from repositories.database import Database
from repositories.dashboard_preferences_repository import (
    DashboardPreferencesRepository,
)
from services.dashboard_operations_service import DashboardOperationsService
from services.auto_post_runtime_service import AutoPostRuntimeService
from services.dps_agent_client import get_dps_session_status
from ui.session_identity import current_identity


def _status(value: Any, *, enabled: bool) -> str:
    state = str(value or "STOPPED").upper()
    if not enabled:
        return "STOPPED"
    if state in {"FAILED", "PARTIAL", "PARTIAL_SYNC"}:
        return "ERROR"
    if state == "RUNNING":
        return "RUNNING"
    return "RUNNING" if state in {"IDLE", "SUCCESS"} else "STOPPED"


def _auto_post_status(
    *, runtime_enabled: bool, environment_ready: bool,
    sync_enabled: bool, state: dict[str, Any],
) -> str:
    persisted = str(state.get("status") or "STOPPED").upper()
    if runtime_enabled and not environment_ready:
        return "DISABLED_BY_ENV"
    if not runtime_enabled:
        return "OFF"
    if persisted in {
        "STARTING", "RUNNING", "WAITING_FOR_SYNC", "STOPPING", "STOPPED",
        "BLOCKED", "ERROR", "PAUSED_POST_UNKNOWN", "DISABLED_BY_ENV",
    }:
        return persisted
    if not sync_enabled:
        return "WAITING_FOR_SYNC"
    return "RUNNING"


@st.cache_data(ttl=15, show_spinner=False)
def _cached_dps_session_status() -> dict[str, Any]:
    return get_dps_session_status()


def _dps_session_label(status: object) -> str:
    return {
        "READY": "정상",
        "LOGIN_REQUIRED": "로그인 필요",
        "CHROME_NOT_FOUND": "Chrome 미실행",
        "DPS_PAGE_NOT_FOUND": "DPS 페이지 없음",
        "CONNECTION_FAILED": "연결 실패",
        "UNKNOWN": "확인 필요",
    }.get(str(status or "UNKNOWN").upper(), "확인 필요")


@st.dialog("자동등록 시작 확인")
def _confirm_auto_post_start(database_path: str) -> None:
    st.write("새로운 미답변 문의에 생성된 답변이 네이버에 자동 등록됩니다.")
    cancel, start = st.columns(2)
    if cancel.button("취소", width="stretch", key="auto_post_start_cancel"):
        st.session_state["production_auto_post_runtime"] = False
        st.rerun()
    if start.button(
        "자동등록 시작", type="primary", width="stretch",
        key="auto_post_start_confirm",
    ):
        result = AutoPostRuntimeService(Database(database_path)).enable()
        st.session_state["production_auto_post_runtime"] = bool(
            AutoPostRepository(Database(database_path)).settings().get(
                "runtime_auto_post_enabled"
            )
        )
        st.session_state["auto_post_runtime_result"] = result["status"]
        st.rerun()


def _render_admin_mode(database: Database) -> None:
    identity = current_identity()
    username = str(identity.get("username") or "local-admin")
    can_admin = str(identity.get("role") or "").upper() == "ADMIN"
    repository = DashboardPreferencesRepository(database)
    loaded_key = "production_admin_mode_loaded_for"
    if st.session_state.get(loaded_key) != username:
        st.session_state["production_admin_mode"] = repository.admin_mode(username)
        st.session_state[loaded_key] = username
    enabled = st.toggle(
        "관리자 모드",
        key="production_admin_mode",
        disabled=not can_admin,
        help="관리자 상세와 내부 운영 도구를 표시합니다.",
    )
    if enabled != repository.admin_mode(username):
        repository.save_admin_mode(username, enabled)


def render_realtime_operations(database: Database) -> dict[str, Any]:
    data = DashboardOperationsService(database).snapshot()
    dps_session = _cached_dps_session_status()
    data["dps_session"] = dps_session
    sync_settings = data["auto_sync_settings"]
    sync_state = data["auto_sync_state"]
    post_settings = data["auto_post_settings"]
    post_state = data["auto_post_state"]
    post_env = NaverPostSettings.from_environment()
    auto_env = NaverAutoPostSettings.from_environment()
    sync_env = NaverSyncSettings.from_environment()
    environment_ready = post_env.enabled and auto_env.enabled
    persisted_runtime_enabled = bool(
        post_settings.get("runtime_auto_post_enabled")
    )
    sync_enabled = bool(sync_settings.get("enabled"))
    post_status = _auto_post_status(
        runtime_enabled=persisted_runtime_enabled,
        environment_ready=environment_ready,
        sync_enabled=sync_enabled,
        state=post_state,
    )

    # The compact status strip and the emergency ON/OFF control always remain
    # visible.  Everything else belongs to the native expander below so it
    # consumes no vertical workspace while collapsed.
    status_columns = st.columns([1.05, 1.25, 1.05, 0.75, 1.35, 1.25, 1.05], gap="small")
    values = (
        ("Auto Sync", _status(sync_state.get("status"), enabled=sync_enabled)),
        ("Runtime 자동등록", "ON" if persisted_runtime_enabled else "OFF"),
        ("Scheduler", str(post_state.get("status") or "STOPPED")),
        ("Pending", data["pending"]),
        ("최근 등록", format_datetime_kst(
            (data.get("recent_post") or {}).get("completed_at"), empty="없음"
        )),
        ("최근 오류", (data.get("recent_error") or {}).get("event_code") or "없음"),
        ("환경 허용", "ON" if environment_ready and sync_env.enabled else "OFF"),
    )
    for column, (label, value) in zip(status_columns, values):
        column.metric(label, value)

    control, admin, explanation = st.columns(
        [1.5, 1.4, 5.1], gap="medium", vertical_alignment="center"
    )
    requested_runtime = control.toggle(
        "자동등록 ON/OFF",
        value=persisted_runtime_enabled,
        disabled=not environment_ready and not persisted_runtime_enabled,
        key="production_auto_post_runtime",
        help="환경변수와 Dashboard Runtime이 모두 ON일 때만 실제 등록합니다.",
    )
    with admin:
        _render_admin_mode(database)
    if requested_runtime != persisted_runtime_enabled:
        if requested_runtime:
            _confirm_auto_post_start(str(database.path))
        else:
            result = AutoPostRuntimeService(database).disable()
            st.session_state["auto_post_runtime_result"] = result["status"]
            st.rerun()
    changed = st.session_state.pop("auto_post_runtime_result", None)
    if changed:
        st.toast(f"자동등록 Runtime 상태: {changed}")
    explanation.caption(
        "OFF여도 Auto Sync와 답변 생성은 계속됩니다. 진행 중인 POSTING만 완료되고 "
        "Scheduler는 새 등록을 시작하지 않습니다."
    )
    if not environment_ready:
        explanation.warning(
            "운영 환경 잠금 상태입니다. NAVER_POST_ENABLED와 "
            "NAVER_AUTO_POST_ENABLED가 모두 true일 때만 ON이 유효합니다."
        )

    summary = (
        "실시간 운영 상태 · "
        f"Auto Sync {_status(sync_state.get('status'), enabled=sync_enabled)} · "
        f"Auto Post {post_status} · Pending {data['pending']} · "
        f"DPS {_dps_session_label(dps_session.get('session_status'))}"
    )
    with st.expander(summary, expanded=False):
        st.caption(
            "DPS 세션 "
            f"{_dps_session_label(dps_session.get('session_status'))} · "
            "최근 확인 "
            f"{format_datetime_kst(dps_session.get('last_checked_at'), empty='없음')} · "
            "최근 Keepalive "
            f"{format_datetime_kst(dps_session.get('last_keepalive_at'), empty='없음')}"
        )
        columns = st.columns(7, gap="small")
        details = (
            ("마지막 Sync", format_datetime_kst(sync_state.get("last_completed_at"), empty="없음")),
            ("다음 Sync", format_datetime_kst(sync_state.get("next_run_at"), empty="없음")),
            ("최근 성공", format_datetime_kst(sync_state.get("last_success_at"), empty="없음")),
            ("Runtime", "ON" if persisted_runtime_enabled else "OFF"),
            ("Sync 환경 허용", "ON" if sync_env.enabled else "OFF"),
            ("등록 환경 허용", "ON" if environment_ready else "OFF"),
            ("Event", ", ".join(
                f"{key} {value}" for key, value in data.get("event_summary", {}).items()
                if value
            ) or "대기 없음"),
        )
        for column, (label, value) in zip(columns, details):
            column.metric(label, value)
    return data

def render_sync_status(data: dict[str, Any]) -> None:
    settings = data["auto_sync_settings"]
    state = data["auto_sync_state"]
    recent_error = data.get("recent_error") or {}
    recent_failure = (
        f"{format_datetime_kst(recent_error.get('created_at'), empty='없음')} "
        f"{state.get('error_code') or recent_error.get('event_code') or ''}"
    ).strip()
    with st.expander("운영 상세", expanded=False):
        columns = st.columns(6, gap="small")
        values = (
            ("마지막 Sync", format_datetime_kst(state.get("last_completed_at"), empty="없음")),
            ("다음 Sync", format_datetime_kst(state.get("next_run_at"), empty="없음")),
            ("최근 성공", format_datetime_kst(state.get("last_success_at"), empty="없음")),
            ("최근 실패", recent_failure or "없음"),
            ("Event 상태", ", ".join(
                f"{key} {value}" for key, value in data.get("event_summary", {}).items()
                if value
            ) or "대기 없음"),
            ("조회 스토어", state.get("store_count") or "-"),
        )
        for column, (label, value) in zip(columns, values):
            column.metric(label, value)
        columns = st.columns(6, gap="small")
        for column, (label, value) in zip(columns, (
            ("조회 문의", state.get("fetched_count") or 0),
            ("신규 저장", state.get("inserted_count") or 0),
            ("갱신", state.get("updated_count") or 0),
            ("변경 없음", state.get("unchanged_count") or 0),
            ("건너뜀", state.get("skipped_count") or 0),
            ("실패", state.get("failed_count") or 0),
        )):
            column.metric(label, value)
        pending_columns = st.columns(4, gap="small")
        for column, (label, value) in zip(pending_columns, (
            ("기존 Pending", data.get("existing_pending", 0)),
            ("ON 이후 신규 Pending", data.get("new_pending", 0)),
            ("자동등록 대기", data.get("automatic_waiting", 0)),
            ("수동검토 대기", data.get("manual_waiting", 0)),
        )):
            column.metric(label, value)


def render_operations_statistics(data: dict[str, Any]) -> None:
    with st.expander("오늘 운영 통계", expanded=False):
        columns = st.columns(6, gap="small")
        first = (
            ("오늘 문의", data["today_inquiries"]),
            ("자동답변", data["auto_answers"]),
            ("자동등록 성공", data["auto_posted"]),
            ("자동등록 실패", data["auto_failed"]),
            ("직원 수정", data["staff_corrections"]),
            ("Learning", data["learning_today"]),
        )
        for column, (label, value) in zip(columns, first):
            column.metric(label, value)
        columns = st.columns(5, gap="small")
        second = (
            ("Pending", data["pending"]),
            ("Scheduler", str(data["auto_post_state"].get("status") or "STOPPED")),
            ("Auto Sync", str(data["auto_sync_state"].get("status") or "STOPPED")),
            ("최근 오류", (data.get("recent_error") or {}).get("event_code") or "없음"),
            ("최근 등록", format_datetime_kst((data.get("recent_post") or {}).get("completed_at"), empty="없음")),
        )
        for column, (label, value) in zip(columns, second):
            column.metric(label, value)


def render_learning_status(data: dict[str, Any]) -> None:
    learning = data["learning"]
    with st.expander("Learning Repository", expanded=False):
        columns = st.columns(5, gap="small")
        latest = learning.get("latest") or {}
        values = (
            ("총 개수", learning.get("total", 0)),
            ("오늘 증가", learning.get("today", 0)),
            ("오늘 사용", learning.get("used_today", 0)),
            ("최근 학습", format_datetime_kst(latest.get("created_at"), empty="없음")),
            ("최근 수정", format_datetime_kst(latest.get("updated_at"), empty="없음")),
        )
        for column, (label, value) in zip(columns, values):
            column.metric(label, value)
        distribution = learning.get("quality_distribution") or {}
        quality = " · ".join(
            f"{'★' * rating} {distribution.get(rating, 0)}"
            for rating in range(5, 0, -1)
        )
        st.caption("품질 분포: " + escape(quality))
