from __future__ import annotations

import os
import uuid
from pathlib import Path

import streamlit as st

from answer.governance_models import GptProviderSettings
from core.time_utils import format_datetime_kst
from config import get_configured_stores
from repositories.database import Database
from repositories.gpt_provider_run_repository import GptProviderRunRepository
from repositories.local_user_repository import LocalUserRepository
from repositories.uat_repository import UatRepository
from services.env_comparison_service import EnvComparisonService, EnvParseError
from services.environment_validation_service import EnvironmentValidationService
from services.local_auth_service import Permission
from services.uat_diagnostic_service import UatDiagnosticService
from services.uat_sync_service import UatInquirySyncService
from ui.session_identity import can, current_actor
from uat.models import UatStatus


STATUS_ICONS = {
    "NORMAL": "●",
    "WARNING": "▲",
    "FAILED": "✕",
    "NOT_CONFIGURED": "○",
    "NOT_RUN": "–",
    "BLOCKED": "■",
}


def operator_card_items(report: dict) -> list[dict[str, str]]:
    """개발자 진단을 운영자용 5개 상태 카드로 축약합니다."""

    items = {
        str(item.get("code")): item for item in report.get("items", [])
    }

    def card(
        code: str,
        title: str,
        *,
        action: str,
        fallback: str,
    ) -> dict[str, str]:
        item = items.get(code, {})
        status = str(item.get("status") or "NOT_RUN")
        return {
            "code": code,
            "title": title,
            "status": status,
            "status_label": UatStatus(status).label,
            "message": str(item.get("message") or fallback),
            "action": str(item.get("action") or action),
        }

    return [
        card(
            "STORE_LOOKUP",
            "네이버 연결",
            action="문의 동기화로 실제 연결을 확인하세요.",
            fallback="스토어 연결 상태를 확인하지 않았습니다.",
        ),
        card(
            "DPS_AGENT",
            "DPS 연결",
            action="DPS Agent와 Chrome 로그인을 확인하세요.",
            fallback="DPS Agent 상태를 확인하지 않았습니다.",
        ),
        card(
            "GPT_GOVERNANCE",
            "GPT 연결",
            action="Provider와 승인 Gate를 확인하세요.",
            fallback="GPT 설정을 확인하지 않았습니다.",
        ),
        card(
            "DATABASE",
            "DB",
            action="DB 경로와 파일 권한을 확인하세요.",
            fallback="DB 상태를 확인하지 않았습니다.",
        ),
        {
            "code": "NAVER_POST_LOCK",
            "title": "등록 잠금",
            "status": "NORMAL",
            "status_label": "잠금 유지",
            "message": "승인 후에도 네이버 실제 등록은 실행되지 않습니다.",
            "action": "현재 단계에서는 별도 조치가 필요 없습니다.",
        },
    ]


def _render_operator_cards(report: dict) -> tuple[bool, bool]:
    any_recheck = False
    dps_recheck = False
    columns = st.columns(5, gap="medium")
    for column, item in zip(columns, operator_card_items(report)):
        with column:
            with st.container(border=True, key=f"uat_card_{item['code'].lower()}"):
                st.markdown(
                    f"### {item['title']}\n"
                    f"**{STATUS_ICONS.get(item['status'], '·')} "
                    f"{item['status_label']}**"
                )
                st.write(item["message"])
                st.caption(f"조치: {item['action']}")
                clicked = st.button(
                    "다시 확인",
                    key=f"uat_card_recheck_{item['code'].lower()}",
                    width="stretch",
                    disabled=item["code"] == "NAVER_POST_LOCK",
                )
                any_recheck = any_recheck or clicked
                dps_recheck = dps_recheck or (
                    clicked and item["code"] == "DPS_AGENT"
                )
    return any_recheck, dps_recheck


def _render_report(report: dict) -> None:
    rows = []
    for item in report.get("items", []):
        status = str(item.get("status"))
        rows.append(
            {
                "상태": f"{STATUS_ICONS.get(status, '·')} "
                f"{UatStatus(status).label}",
                "점검 항목": item.get("label"),
                "설명": item.get("message"),
                "마지막 확인": item.get("checked_at"),
                "실패 유형": item.get("failure_type") or "",
                "사용자 조치": item.get("action") or "",
            }
        )
    st.dataframe(rows, hide_index=True, width="stretch")


def _render_environment(database: Database) -> None:
    result = EnvironmentValidationService().validate()
    rows = [
        {
            "변수명": item.name,
            "분류": item.requirement.value,
            "영역": item.scope,
            "상태": item.status.label,
            "등록": "등록됨" if item.present else "미등록",
            "유효": "유효" if item.valid else "확인 필요",
            "설명": item.description,
            "해결 방법": item.resolution,
        }
        for item in result.checks
    ]
    st.dataframe(rows, hide_index=True, width="stretch")
    counts = {
        "valid": sum(item.status is UatStatus.NORMAL for item in result.checks),
        "warning": sum(
            item.status in {UatStatus.WARNING, UatStatus.NOT_CONFIGURED}
            for item in result.checks
        ),
        "failure": sum(item.status is UatStatus.FAILED for item in result.checks),
    }
    UatRepository(database).create_environment_check(
        correlation_id=str(uuid.uuid4()),
        actor=current_actor(),
        status=result.status.value,
        valid_count=counts["valid"],
        warning_count=counts["warning"],
        failure_count=counts["failure"],
        summary={
            "checked_names": [item.name for item in result.checks],
            "statuses": {item.name: item.status.value for item in result.checks},
        },
    )


def render_uat_panel(database: Database | None) -> None:
    st.title("운영 연결 상태")
    st.caption(
        "현재 개발 PC의 조회·답변·검토 흐름을 점검합니다. "
        "서버 배포와 네이버 실제 답변 등록은 이 화면에서 수행하지 않습니다."
    )
    if database is None:
        st.error("DB를 사용할 수 없어 UAT 진단을 실행할 수 없습니다.")
        return

    settings = GptProviderSettings.from_environment()
    stats = GptProviderRunRepository(database).dashboard_stats()
    with database.connection() as connection:
        approval_pending = int(
            connection.execute(
                "SELECT COUNT(*) FROM inquiries WHERE approval_status='PENDING'"
            ).fetchone()[0]
        )
        posted_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM inquiries WHERE post_status='POSTED'"
            ).fetchone()[0]
        )
        dps_success = int(
            connection.execute(
                "SELECT COUNT(*) FROM dps_lookup_results WHERE lookup_status='SUCCESS'"
            ).fetchone()[0]
        )
        dps_failed = int(
            connection.execute(
                "SELECT COUNT(*) FROM dps_lookup_results WHERE lookup_status<>'SUCCESS'"
            ).fetchone()[0]
        )
    if "uat_report" not in st.session_state:
        report = UatDiagnosticService(database).run(
            actor=current_actor(), check_dps=False
        )
        st.session_state["uat_report"] = report.to_dict()
    recheck, dps_check = _render_operator_cards(st.session_state["uat_report"])
    if recheck:
        report = UatDiagnosticService(database).run(
            actor=current_actor(), check_dps=dps_check
        )
        st.session_state["uat_report"] = report.to_dict()
        st.rerun()

    with st.expander("개발자용 상세보기", expanded=False):
        summary = st.columns(5)
        summary[0].metric(
            "DB / Migration", f"정상 / v{max(database.migration_versions())}"
        )
        summary[1].metric("활성 스토어", len(get_configured_stores()))
        summary[2].metric("DPS 성공 / 실패", f"{dps_success} / {dps_failed}")
        summary[3].metric(
            "승인 대기 / posted", f"{approval_pending} / {posted_count}"
        )
        summary[4].metric(
            "GPT / fallback", f"{settings.mode.value} / {stats['fallbacks']}"
        )
        st.caption(
            "프로세스 health와 외부 접속 가능 여부는 /_stcore/health에서 "
            "별도로 확인합니다."
        )
        _render_report(st.session_state["uat_report"])

    with st.expander("네이버 문의 동기화", expanded=True):
        st.warning(
            "버튼을 누르면 활성화되고 인증정보가 완성된 스토어만 실제 조회합니다. "
            "한 스토어 실패는 다른 스토어와 기존 DB 데이터를 중단시키지 않습니다."
        )
        columns = st.columns(3)
        days = columns[0].number_input(
            "조회 기간(일)", min_value=1, max_value=90, value=30
        )
        answered_label = columns[1].selectbox(
            "답변 상태", ("전체", "미답변", "답변 완료")
        )
        run_sync = columns[2].button(
            "문의 동기화", type="primary", width="stretch",
            disabled=not can(Permission.SETTINGS_DIAGNOSTICS),
        )
        if run_sync:
            answered = {"전체": None, "미답변": False, "답변 완료": True}[
                answered_label
            ]
            with st.spinner("네이버 스토어별 문의를 동기화하고 있습니다."):
                result = UatInquirySyncService(database).run(
                    days=int(days), answered=answered
                )
            st.session_state["uat_sync_result"] = result.to_dict()
        if isinstance(st.session_state.get("uat_sync_result"), dict):
            result = st.session_state["uat_sync_result"]
            st.write(
                {
                    "마지막 동기화": format_datetime_kst(
                        result["completed_at"]
                    ),
                    "조회": result["fetched_count"],
                    "신규": result["created_count"],
                    "갱신": result["updated_count"],
                    "변경 없음": result["unchanged_count"],
                    "실패": result["failed_count"],
                }
            )
            if result["errors"]:
                st.warning("일부 스토어 조회에 실패했습니다. 오류에는 인증값이 포함되지 않습니다.")
                st.dataframe(result["errors"], hide_index=True, width="stretch")

    with st.expander("환경설정 Validator", expanded=False):
        st.caption("환경변수 값은 표시하지 않습니다.")
        if st.button(
            "환경설정 검사", disabled=not can(Permission.SETTINGS_DIAGNOSTICS)
        ):
            _render_environment(database)

    with st.expander(".env 읽기 전용 비교", expanded=False):
        st.caption(
            "값은 표시하지 않고 EMPTY/PRESENT/SAME/DIFFERENT 상태만 비교합니다. "
            "AnySign4PC 아이콘이나 파일 연결 상태로 암호화 여부를 판단하지 않습니다."
        )
        current = Path(__file__).resolve().parents[1] / ".env"
        compared = st.text_input(
            "비교할 기존 .env 절대 경로",
            placeholder=r"C:\path\to\existing\.env",
            disabled=not can(Permission.ENV_COMPARE),
        )
        if st.button(
            "비교 보고서 생성",
            disabled=not compared or not can(Permission.ENV_COMPARE),
        ):
            try:
                report = EnvComparisonService().compare(current, compared)
            except (EnvParseError, OSError) as error:
                st.error(str(error))
            else:
                rows = [item.to_dict() for item in report.items]
                st.dataframe(rows, hide_index=True, width="stretch")
                same = sum(item.comparison == "SAME" for item in report.items)
                different = sum(
                    item.comparison == "DIFFERENT" for item in report.items
                )
                missing = sum(
                    item.comparison in {"CURRENT_ONLY", "COMPARED_ONLY"}
                    for item in report.items
                )
                UatRepository(database).create_env_comparison(
                    correlation_id=str(uuid.uuid4()),
                    actor=current_actor(),
                    current_file_name=report.current_file_name,
                    compared_file_name=report.compared_file_name,
                    status="COMPLETED",
                    same_count=same,
                    different_count=different,
                    missing_count=missing,
                    summary={
                        "names": [item.name for item in report.items],
                        "comparisons": {
                            item.name: item.comparison for item in report.items
                        },
                    },
                )

    with st.expander("로컬 사용자·권한", expanded=False):
        auth_enabled = os.getenv(
            "QNA_LOCAL_AUTH_ENABLED", "false"
        ).strip().lower() in {"1", "true", "yes", "on"}
        st.write(
            {
                "로컬 인증": "활성" if auth_enabled else "비활성(개발 PC ADMIN 문맥)",
                "현재 actor": current_actor(),
                "사용자 수": len(LocalUserRepository(database).list_users()),
            }
        )
        if can(Permission.USER_MANAGE):
            st.dataframe(
                LocalUserRepository(database).list_users(),
                hide_index=True,
                width="stretch",
            )
        st.caption(
            "초기 ADMIN은 scripts/manage_local_user.py로 생성합니다. "
            "비밀번호는 bcrypt hash만 DB에 저장됩니다."
        )

    st.error(
        "네이버 실제 답변 등록은 Phase 7에서 잠겨 있습니다. "
        "승인된 Final Answer가 있어도 이 화면에서는 등록하지 않습니다."
    )
