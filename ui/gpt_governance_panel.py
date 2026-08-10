from __future__ import annotations

from typing import Any

import streamlit as st

from answer.governance_models import GptProviderSettings
from repositories.database import Database
from repositories.gpt_provider_run_repository import GptProviderRunRepository


def build_governance_status(
    settings: GptProviderSettings,
    stats: dict[str, Any],
) -> dict[str, Any]:
    issues = settings.validation_issues()
    return {
        "mode": settings.mode.value,
        "provider": settings.provider_name,
        "model": settings.model or "미설정",
        "company_approved": settings.approved_by_company,
        "api_key_configured": settings.api_key_present,
        "requests": int(stats.get("requests") or 0),
        "failures": int(stats.get("failures") or 0),
        "fallbacks": int(stats.get("fallbacks") or 0),
        "cost": float(stats.get("estimated_cost_krw") or 0),
        "daily_cost_limit": settings.daily_cost_limit_krw,
        "canary_percentage": settings.canary_percentage,
        "average_ms": float(stats.get("average_duration_ms") or 0),
        "privacy_blocks": int(stats.get("privacy_blocks") or 0),
        "rate_limit_status": (
            "주의"
            if int(stats.get("requests") or 0)
            >= settings.daily_request_limit
            else "정상"
        ),
        "configuration_valid": not issues,
        "issues": list(issues),
    }


def render_gpt_governance_panel(database: Database | None) -> None:
    st.markdown(
        '<div class="preparation-page"><h1>GPT Provider Governance</h1>'
        "<p>Provider 실행 모드, 승인 Gate와 운영 한도를 읽기 전용으로 확인합니다.</p></div>",
        unsafe_allow_html=True,
    )
    settings = GptProviderSettings.from_environment()
    stats = (
        GptProviderRunRepository(database).dashboard_stats()
        if database is not None
        else {}
    )
    status = build_governance_status(settings, stats)
    top = st.columns(5, gap="medium")
    top[0].metric("GPT Mode", status["mode"])
    top[1].metric("Provider", status["provider"])
    top[2].metric("Model", status["model"])
    top[3].metric(
        "회사 승인", "승인" if status["company_approved"] else "미승인"
    )
    top[4].metric(
        "API Key", "설정됨" if status["api_key_configured"] else "없음"
    )
    operations = st.columns(5, gap="medium")
    operations[0].metric("오늘 요청", status["requests"])
    operations[1].metric("오늘 실패", status["failures"])
    operations[2].metric("오늘 Fallback", status["fallbacks"])
    operations[3].metric("Privacy 차단", status["privacy_blocks"])
    operations[4].metric("평균 응답", f'{status["average_ms"]:.0f} ms')
    limits = st.columns(4, gap="medium")
    limits[0].metric("오늘 추정 비용", f'{status["cost"]:,.2f} 원')
    limits[1].metric(
        "일일 비용 한도",
        (
            f'{status["daily_cost_limit"]:,.0f} 원'
            if status["daily_cost_limit"] > 0
            else "미설정"
        ),
    )
    limits[2].metric("Canary 비율", f'{status["canary_percentage"]:.1f}%')
    limits[3].metric("Rate Limit", status["rate_limit_status"])
    if status["issues"]:
        st.warning(" · ".join(status["issues"]))
    else:
        st.success("현재 모드의 필수 설정 검증을 통과했습니다.")
    st.info(
        "이 화면은 읽기 전용입니다. API key 입력·저장과 실제 Provider 활성화 "
        "기능은 제공하지 않습니다."
    )
