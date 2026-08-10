from __future__ import annotations

import streamlit as st

from config import (
    NAVER_AUTO_SYNC_INTERVALS,
    NaverAutoPostSettings,
    NaverPostSettings,
)
from core.time_utils import format_datetime_kst
from repositories.auto_post_repository import AutoPostRepository
from repositories.database import Database
from repositories.post_review_repository import PostReviewRepository
from services.naver_auto_post_scheduler import ensure_auto_post_scheduler


def render_auto_post_controls(database: Database) -> None:
    repository = AutoPostRepository(database)
    settings = repository.settings()
    state = repository.state()
    reviews = PostReviewRepository(database).summary()
    environment = NaverAutoPostSettings.from_environment()
    post_environment = NaverPostSettings.from_environment()
    activation_ready = environment.enabled and post_environment.enabled
    with st.expander("네이버 답변 자동등록", expanded=False):
        columns = st.columns([1.5, 1.5, 1.3, 1.2], gap="small")
        columns[0].checkbox(
            "Runtime 상태",
            value=bool(settings.get("enabled")),
            disabled=True,
            key="naver_auto_post_enabled_widget",
        )
        interval = columns[1].selectbox(
            "자동등록 간격",
            options=list(NAVER_AUTO_SYNC_INTERVALS),
            index=list(NAVER_AUTO_SYNC_INTERVALS).index(
                int(settings.get("interval_minutes") or 10)
            ),
            format_func=lambda value: f"{value}분",
            key="naver_auto_post_interval_widget",
        )
        retries = columns[2].number_input(
            "실패 재시도",
            min_value=0,
            max_value=10,
            value=int(settings.get("max_retries") or 0),
            step=1,
            key="naver_auto_post_retries_widget",
        )
        save = columns[3].button(
            "자동등록 설정 저장",
            width="stretch",
            key="naver_auto_post_save",
        )
        if save:
            repository.save_settings(
                enabled=bool(settings.get("enabled")),
                interval_minutes=int(interval),
                max_retries=int(retries),
            )
            ensure_auto_post_scheduler(database)
            st.session_state["naver_auto_post_saved"] = True
            st.rerun()
        if st.session_state.pop("naver_auto_post_saved", False):
            st.success("자동등록 운영 설정을 저장했습니다.")
        metrics = st.columns(8, gap="small")
        values = (
            ("상태", "ON" if settings.get("enabled") else "OFF"),
            ("처리", state.get("processed_count") or 0),
            ("성공", state.get("succeeded_count") or 0),
            ("실패", state.get("failed_count") or 0),
            ("POST_UNKNOWN", state.get("unknown_count") or 0),
            ("검토 대기", reviews.get("pending_review") or 0),
            ("수정 완료", reviews.get("corrected") or 0),
            ("재시도", settings.get("max_retries") or 0),
        )
        for column, (label, value) in zip(metrics, values):
            column.metric(label, value)
        st.caption(
            "마지막 실행 "
            f"{format_datetime_kst(state.get('last_completed_at'), empty='없음')} · "
            "다음 실행 "
            f"{format_datetime_kst(state.get('next_run_at'), empty='없음')}"
        )
        if not activation_ready:
            st.info(
                "운영 안전 잠금 상태입니다. NAVER_POST_ENABLED와 "
                "NAVER_AUTO_POST_ENABLED가 모두 true일 때만 시작할 수 있습니다."
            )
