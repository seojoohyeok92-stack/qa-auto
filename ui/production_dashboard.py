from __future__ import annotations

from html import escape
from typing import Any

import streamlit as st

from config import (
    DpsSessionSettings,
    NaverAutoPostSettings,
    NaverPostSettings,
    NaverSyncSettings,
)
from core.time_utils import format_datetime_kst
from repositories.database import Database
from repositories.dashboard_preferences_repository import (
    DashboardPreferencesRepository,
)
from services.dashboard_operations_service import DashboardOperationsService
from services.auto_post_runtime_service import AutoPostRuntimeService
from services.dps_agent_client import get_dps_session_status
from services.dps_lookup_policy import DpsSettings
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
        st.rerun()
    if start.button(
        "자동등록 시작", type="primary", width="stretch",
        key="auto_post_start_confirm",
    ):
        result = AutoPostRuntimeService(Database(database_path)).enable()
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
    admin_enabled = bool(st.session_state.get("production_admin_mode", False))
    st.caption("관리자 모드: **ON**" if admin_enabled else "관리자 모드: **OFF**")
    start_col, stop_col = st.columns(2, gap="small")
    start_clicked = start_col.button(
        "관리자 모드 시작",
        width="stretch",
        key="production_admin_mode_start",
        disabled=not can_admin or admin_enabled,
        help="관리자 상세와 내부 운영 도구를 표시합니다. Auto Sync/자동처리와 무관합니다.",
    )
    stop_clicked = stop_col.button(
        "관리자 모드 종료",
        width="stretch",
        key="production_admin_mode_stop",
        disabled=not can_admin or not admin_enabled,
        help="관리자 상세와 내부 운영 도구를 숨깁니다. Auto Sync/자동처리와 무관합니다.",
    )
    if start_clicked:
        st.session_state["production_admin_mode"] = True
        repository.save_admin_mode(username, True)
        st.rerun()
    if stop_clicked:
        st.session_state["production_admin_mode"] = False
        repository.save_admin_mode(username, False)
        st.rerun()


@st.fragment(run_every="30s")
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
    dps_env = DpsSessionSettings.from_environment()
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
    status_columns = st.columns(9, gap="small")
    values = (
        ("Auto Sync", _status(sync_state.get("status"), enabled=sync_enabled)),
        ("Auto Processing", "ON" if persisted_runtime_enabled else "OFF"),
        ("Auto Post", post_status),
        ("DPS Agent", "ON" if dps_session.get("agent_running") else "OFF"),
        ("DPS Keepalive", "ON" if dps_env.keepalive_enabled else "OFF"),
        ("최근 Sync", format_datetime_kst(
            sync_state.get("last_completed_at"), empty="없음"
        )),
        ("최근 Auto Process", format_datetime_kst(
            (data.get("recent_auto_process") or {}).get("created_at"), empty="없음"
        )),
        ("최근 Auto Post", format_datetime_kst(
            (data.get("recent_post") or {}).get("completed_at"), empty="없음"
        )),
        ("직원 검토 필요", data.get("review_required", 0)),
    )
    for column, (label, value) in zip(status_columns, values):
        column.metric(label, value)

    control, admin, explanation = st.columns(
        [1.5, 1.4, 5.1], gap="medium", vertical_alignment="center"
    )
    missing_env_flags = [
        name
        for name, ready in (
            ("NAVER_POST_ENABLED", post_env.enabled),
            ("NAVER_AUTO_POST_ENABLED", auto_env.enabled),
        )
        if not ready
    ]
    with control:
        control.caption(
            "자동처리: **ON**" if persisted_runtime_enabled else "자동처리: **OFF**"
        )
        start_col, stop_col = st.columns(2, gap="small")
        start_help = "DB에 저장되는 서버 공용 스위치를 ON으로 전환합니다."
        if missing_env_flags:
            start_help = (
                "다음 환경변수가 true가 아니어서 시작할 수 없습니다: "
                + ", ".join(missing_env_flags)
            )
        start_clicked = start_col.button(
            "자동처리 시작",
            width="stretch",
            key="production_auto_processing_start",
            disabled=persisted_runtime_enabled or not environment_ready,
            help=start_help,
        )
        stop_clicked = stop_col.button(
            "자동처리 중지",
            width="stretch",
            key="production_auto_processing_stop",
            disabled=not persisted_runtime_enabled,
            help="DB에 저장되는 서버 공용 스위치를 OFF로 전환합니다. Auto Sync와 수동 기능은 유지됩니다.",
        )
    with admin:
        _render_admin_mode(database)
    if start_clicked:
        _confirm_auto_post_start(str(database.path))
    if stop_clicked:
        result = AutoPostRuntimeService(database).disable()
        st.session_state["auto_post_runtime_result"] = result["status"]
        st.rerun()
    changed = st.session_state.pop("auto_post_runtime_result", None)
    if changed:
        st.toast(f"자동처리 Runtime 상태: {changed}")
    explanation.caption(
        "OFF에서는 Auto Sync와 문의 저장만 계속됩니다. 자동 답변·GPT·DPS·POST는 새로 시작하지 않으며, "
        "진행 중 POST는 안전하게 완료됩니다."
    )
    if not environment_ready:
        explanation.warning(
            "운영 환경 잠금 상태로 자동처리를 시작할 수 없습니다. "
            "다음 환경변수가 true로 설정되어 있지 않습니다: "
            + ", ".join(missing_env_flags)
        )

    summary = (
        "실시간 운영 상태 · "
        f"Auto Sync {_status(sync_state.get('status'), enabled=sync_enabled)} · "
        f"Auto Post {post_status} · Pending {data['pending']} · "
        f"DPS {_dps_session_label(dps_session.get('session_status'))}"
    )
    with st.expander(summary, expanded=False):
        dps_lookup_settings = DpsSettings.from_environment()
        st.caption(
            "DPS 세션 "
            f"{_dps_session_label(dps_session.get('session_status'))} · "
            "최근 확인 "
            f"{format_datetime_kst(dps_session.get('last_checked_at'), empty='없음')} · "
            "최근 Keepalive "
            f"{format_datetime_kst(dps_session.get('last_keepalive_at'), empty='없음')}"
        )
        st.caption(
            "DPS 진단 "
            f"{dps_session.get('diagnostic_code') or 'UNKNOWN_ERROR'} · "
            "최근 실제 DPS 조작 "
            f"{format_datetime_kst(dps_session.get('last_dps_activity_at'), empty='없음')} · "
            f"Idle {dps_session.get('dps_idle_minutes') if dps_session.get('dps_idle_minutes') is not None else '-'}분 · "
            f"세션 유지 기준 {dps_env.keepalive_interval_minutes}분 · "
            f"DPS 데이터 갱신 {dps_lookup_settings.refresh_interval_minutes}분"
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

        diagnostics = DashboardOperationsService(database).queue_diagnostics()
        queue = diagnostics["queue"]
        st.markdown("**Queue 진단** (claim 가능 여부 기준, Pending/직원 검토 필요 KPI와는 다른 모집단)")
        queue_columns = st.columns(6, gap="small")
        for column, (label, value) in zip(queue_columns, (
            ("Claim 가능 PENDING", queue["claimable_pending"]),
            ("Processing", queue["processing"]),
            ("Retry 대기", queue["retry_scheduled"]),
            ("Blocked(OFF)", queue["blocked_auto_post_off"]),
            ("Failed", queue["failed"]),
            ("DPS 확인 필요", diagnostics["dps_required_count"]),
        )):
            column.metric(label, value)
        if diagnostics["review_required_reasons"]:
            st.caption(
                "직원 검토 필요 사유별 집계: " + " · ".join(
                    f"{reason} {count}건"
                    for reason, count in diagnostics["review_required_reasons"].items()
                )
            )
        recent_events = diagnostics["recent_events"]
        if recent_events:
            st.caption(f"최근 처리 이벤트 (최근 {len(recent_events)}건, 네이버 원본 문의번호 기준)")
            st.dataframe(
                [
                    {
                        "문의번호(네이버)": event["external_inquiry_id"],
                        "내부 ID": event["inquiry_id"],
                        "Queue 상태": event["queue_status"],
                        "결과": event["result"],
                        "자동등록": "Y" if event["auto_posted"] else "N",
                        "사유": ", ".join(event["reasons"]) or "-",
                        "최근 처리시간": format_datetime_kst(
                            event["updated_at"], empty="없음"
                        ),
                    }
                    for event in recent_events
                ],
                width="stretch", hide_index=True,
            )
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
            ("오늘 생성/승격", learning.get("today", 0)),
            ("오늘 답변 생성에서 참조", learning.get("used_today", 0)),
            ("최근 학습", format_datetime_kst(latest.get("created_at"), empty="없음")),
            ("최근 수정", format_datetime_kst(latest.get("updated_at"), empty="없음")),
        )
        for column, (label, value) in zip(columns, values):
            column.metric(label, value)
        st.caption(
            "생성/승격은 Learning row 생성 건수(판매자답변 style-only 포함), "
            "참조는 답변 생성 검색에서 선택된 고유 Learning 항목 수입니다. Naver POST 건수와는 독립적입니다."
        )
        distribution = learning.get("quality_distribution") or {}
        quality = " · ".join(
            f"{'★' * rating} {distribution.get(rating, 0)}"
            for rating in range(5, 0, -1)
        )
        st.caption("품질 분포: " + escape(quality))
