from __future__ import annotations

import os
from typing import Any

import streamlit as st

from services.dps_agent_client import get_dps_agent_status
from ui.session_identity import current_identity


PAGE_LABELS = {
    "dashboard": "Dashboard",
    "inquiries": "전체 문의",
    "progress": "진행 카드",
    "dps": "DPS 관리",
    "settings": "설정 관리",
    "uat": "관리자 진단",
    "activity": "활동 로그",
    "learning": "Learning Manager",
}


def _change_page(page_code: str, *, kpi_filter: str | None = None) -> None:
    st.session_state["current_page"] = page_code
    if kpi_filter is not None:
        st.session_state["dashboard_kpi_filter"] = kpi_filter
    st.session_state.pop("selected_inquiry_id", None)


@st.cache_data(ttl=20, show_spinner=False)
def _cached_dps_status() -> dict[str, Any]:
    try:
        return get_dps_agent_status()
    except Exception as error:
        return {"agent_running": False, "error_code": error.__class__.__name__}


def sidebar_system_status(
    *,
    db_status: dict[str, Any] | None,
    configured_store_count: int,
    dps_status: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    dps = dps_status if dps_status is not None else _cached_dps_status()
    provider = os.getenv("QNA_GPT_PROVIDER", "fake").strip().upper()
    has_openai = bool(os.getenv("OPENAI_API_KEY", "").strip())
    login_status = str(dps.get("login_status") or "").upper()
    browser_connected = bool(
        dps.get("browser_connected")
        or dps.get("chrome_connected")
        or login_status in {"LOGGED_IN", "READY"}
    )
    return [
        {"label": "DB 상태", "status": "정상" if (db_status or {}).get("ok") else "오류", "tone": "ok" if (db_status or {}).get("ok") else "error"},
        {"label": "DPS Agent", "status": "정상" if dps.get("agent_running") else "미시작", "tone": "ok" if dps.get("agent_running") else "muted"},
        {"label": "네이버 API", "status": f"설정됨 {configured_store_count}" if configured_store_count else "오류", "tone": "warning" if configured_store_count else "error"},
        {"label": "GPT Provider", "status": provider if provider not in {"OPENAI", "REAL"} or has_openai else "키 확인", "tone": "ok" if provider in {"OPENAI", "REAL"} and has_openai else "warning"},
        {"label": "Chrome 연결", "status": "정상" if browser_connected else "미시작", "tone": "ok" if browser_connected else "muted"},
    ]


def _menu_button(
    label: str,
    page_code: str,
    current_page: str,
    *,
    key: str,
    kpi_filter: str | None = None,
) -> None:
    active = current_page == page_code and (
        kpi_filter is None
        or st.session_state.get("dashboard_kpi_filter") == kpi_filter
    )
    if st.button(
        label,
        key=key,
        width="stretch",
        type="primary" if active else "secondary",
    ):
        _change_page(page_code, kpi_filter=kpi_filter)
        st.rerun()


def render_sidebar(
    active_store_names: list[str],
    *,
    db_status: dict[str, Any] | None = None,
    dps_status: dict[str, Any] | None = None,
) -> tuple[str, bool]:
    st.session_state.setdefault("current_page", "dashboard")
    current_page = str(st.session_state["current_page"])
    with st.sidebar:
        st.markdown(
            '<div class="sidebar-brand"><strong>OJE</strong>'
            '<span>Q&amp;A auto</span></div>',
            unsafe_allow_html=True,
        )
        _menu_button(
            "Dashboard", "dashboard", current_page,
            key="sidebar_page_dashboard",
        )
        identity = current_identity()
        can_admin = str(identity.get("role") or "").upper() == "ADMIN"
        admin_mode = st.toggle(
            "관리자 모드",
            value=bool(st.session_state.get("production_admin_mode", False)),
            disabled=not can_admin,
            key="production_admin_mode",
        )
        if admin_mode:
            st.markdown(
                '<div class="sidebar-group-label">관리자 도구</div>',
                unsafe_allow_html=True,
            )
            for label, page_code, key_suffix in (
                ("Learning Manager", "learning", "learning"),
                ("Historical Cases", "historical", "historical"),
                ("Migration", "uat", "migration"),
                ("Debug", "uat", "debug"),
                ("Scheduler", "dashboard", "scheduler"),
                ("Activity", "activity", "activity"),
                ("UAT", "uat", "uat"),
                ("Settings", "settings", "settings"),
            ):
                _menu_button(
                    label, page_code, current_page,
                    key=f"sidebar_admin_{key_suffix}",
                )
            with st.expander("시스템 상태", expanded=False):
                for item in sidebar_system_status(
                    db_status=db_status,
                    configured_store_count=len(active_store_names),
                    dps_status=dps_status,
                ):
                    st.write(f"{item['label']}: {item['status']}")
    return current_page, False
