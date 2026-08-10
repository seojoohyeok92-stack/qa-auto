from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Callable

from answer.governance_models import GptMode, GptProviderSettings
from config import ALL_STORES, get_configured_stores
from repositories.database import Database
from repositories.gpt_provider_run_repository import GptProviderRunRepository
from repositories.log_repository import LogRepository
from repositories.uat_repository import UatRepository
from services.dps_agent_client import get_dps_agent_status
from services.environment_validation_service import EnvironmentValidationService
from services.uat_error_mapper import classify_dps_uat_error
from uat.models import UatDiagnosticItem, UatDiagnosticReport, UatStatus


DpsStatusChecker = Callable[[], dict[str, Any]]


def _now() -> str:
    return datetime.now(UTC).isoformat()


class UatDiagnosticService:
    def __init__(
        self,
        database: Database,
        *,
        dps_status_checker: DpsStatusChecker = get_dps_agent_status,
    ) -> None:
        self.database = database
        self.dps_status_checker = dps_status_checker
        self.logs = LogRepository(database)
        self.runs = UatRepository(database)

    @staticmethod
    def _item(
        code: str,
        label: str,
        status: UatStatus,
        message: str,
        *,
        failure_type: str | None = None,
        action: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> UatDiagnosticItem:
        return UatDiagnosticItem(
            code, label, status, message, _now(), failure_type, action,
            details or {},
        )

    def run(
        self, *, actor: str = "local-admin", check_dps: bool = False
    ) -> UatDiagnosticReport:
        started = _now()
        correlation_id = str(uuid.uuid4())
        items: list[UatDiagnosticItem] = []

        environment = EnvironmentValidationService().validate()
        items.append(
            self._item(
                "ENVIRONMENT", "환경설정", environment.status,
                "환경변수 값은 숨기고 존재 여부와 형식만 확인했습니다.",
                action="설정 화면에서 누락 항목의 해결 방법을 확인하세요.",
                details={
                    "normal": sum(
                        item.status is UatStatus.NORMAL
                        for item in environment.checks
                    ),
                    "issues": sum(
                        item.status is not UatStatus.NORMAL
                        for item in environment.checks
                    ),
                },
            )
        )

        health = self.database.health()
        items.append(
            self._item(
                "DATABASE", "DB",
                UatStatus.NORMAL if health.get("ok") else UatStatus.FAILED,
                "SQLite 연결과 migration을 확인했습니다."
                if health.get("ok") else "SQLite 상태를 확인하지 못했습니다.",
                failure_type=health.get("error"),
                action="DB 경로 권한과 파일 상태를 확인하세요.",
                details={
                    "migration_version": max(
                        self.database.migration_versions(), default=0
                    ),
                    "journal_mode": health.get("journal_mode"),
                },
            )
        )

        configured = get_configured_stores()
        items.append(
            self._item(
                "NAVER_AUTH", "네이버 인증",
                UatStatus.NOT_RUN if configured else UatStatus.NOT_CONFIGURED,
                "인증정보 존재 여부만 확인했습니다. 토큰 발급은 아직 실행하지 않았습니다."
                if configured else "활성화되고 인증정보가 완성된 스토어가 없습니다.",
                action="문의 동기화 버튼으로 실제 인증을 확인하세요.",
                details={"configured_store_count": len(configured)},
            )
        )
        items.append(
            self._item(
                "STORE_LOOKUP", "스토어 조회",
                UatStatus.NORMAL if configured else UatStatus.NOT_CONFIGURED,
                f"조회 가능한 스토어 {len(configured)}개를 확인했습니다.",
                action="스토어별 enabled와 인증정보를 확인하세요.",
                details={
                    "active_store_codes": [store.code for store in configured],
                    "defined_store_count": len(ALL_STORES),
                },
            )
        )

        recent_system = self.logs.recent_system(limit=100)
        sync_log = next(
            (
                row for row in recent_system
                if str(row.get("event_code", "")).startswith(
                    "NAVER_INQUIRY_SYNC_"
                )
            ),
            None,
        )
        items.append(
            self._item(
                "INQUIRY_SYNC", "문의 동기화",
                UatStatus.NORMAL
                if sync_log and str(sync_log["event_code"]).endswith("SUCCEEDED")
                else UatStatus.WARNING if sync_log else UatStatus.NOT_RUN,
                str(sync_log["message"]) if sync_log else "UAT 동기화 실행 기록이 없습니다.",
                action="문의 동기화를 실행하고 스토어별 결과를 확인하세요.",
                details={
                    "last_checked_at": sync_log.get("created_at") if sync_log else None
                },
            )
        )
        order_log = next(
            (
                row for row in recent_system
                if str(row.get("event_code", "")).startswith(
                    "NAVER_ORDER_LOOKUP_"
                )
            ),
            None,
        )
        items.append(
            self._item(
                "ORDER_LOOKUP", "주문 조회",
                UatStatus.NOT_RUN if order_log is None else UatStatus.NORMAL,
                "선택 문의에서 주문 조회 버튼으로 확인합니다."
                if order_log is None else str(order_log["message"]),
                action="주문 식별자가 있는 문의를 선택하세요.",
            )
        )

        dps_payload: dict[str, Any] | None = None
        if check_dps:
            try:
                dps_payload = self.dps_status_checker()
            except Exception as error:
                dps_payload = {
                    "ok": False,
                    "code": "AGENT_CONNECTION_FAILED",
                    "exception_type": error.__class__.__name__,
                }
        dps_error = classify_dps_uat_error(dps_payload)
        dps_status = (
            UatStatus.NORMAL
            if dps_payload and (dps_payload.get("ok") or dps_payload.get("success"))
            else UatStatus.FAILED if dps_payload
            else UatStatus.NOT_RUN
        )
        items.append(
            self._item(
                "DPS_AGENT", "DPS Agent", dps_status,
                "DPS Agent 상태 확인을 완료했습니다."
                if dps_payload else "DPS Agent 상태를 아직 확인하지 않았습니다.",
                failure_type=dps_error,
                action="Agent 실행과 127.0.0.1 포트를 확인하세요.",
            )
        )
        chrome_found = bool(
            dps_payload
            and (
                dps_payload.get("chrome_found")
                or dps_payload.get("chrome_window_count")
            )
        )
        items.append(
            self._item(
                "DPS_CHROME", "DPS Chrome 연결",
                UatStatus.NORMAL if chrome_found
                else UatStatus.WARNING if dps_payload
                else UatStatus.NOT_RUN,
                "Chrome 연결 가능 여부는 Agent 상태 응답 범위에서만 판단합니다.",
                failure_type=(
                    None if chrome_found else dps_error or "CHROME_NOT_FOUND"
                    if dps_payload else None
                ),
                action="기존 Chrome에서 DPS 탭과 로그인 상태를 확인하세요.",
            )
        )
        items.append(
            self._item(
                "DPS_LOOKUP", "DPS 조회", UatStatus.NOT_RUN,
                "실제 주문 조회는 문의별 DPS 조회 버튼에서만 실행합니다.",
                action="일반 order_id가 있는 배송·설치 문의를 선택하세요.",
            )
        )

        with self.database.connection() as connection:
            draft_count = int(
                connection.execute("SELECT COUNT(*) FROM answer_drafts").fetchone()[0]
            )
            approved_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM inquiries WHERE approval_status='APPROVED'"
                ).fetchone()[0]
            )
            log_count = int(
                connection.execute("SELECT COUNT(*) FROM activity_logs").fetchone()[0]
            )
        items.append(
            self._item(
                "RULE_ANSWER", "Rule Answer",
                UatStatus.NORMAL if draft_count else UatStatus.NOT_RUN,
                f"저장된 답변 초안 {draft_count}건을 확인했습니다.",
                action="문의 상세에서 답변 생성을 실행하세요.",
            )
        )

        settings = GptProviderSettings.from_environment()
        issues = settings.validation_issues()
        gpt_status = (
            UatStatus.NORMAL if not issues
            else UatStatus.BLOCKED if settings.is_real_provider
            else UatStatus.WARNING
        )
        items.append(
            self._item(
                "GPT_GOVERNANCE", "GPT Governance", gpt_status,
                "실제 외부 AI 호출 없음"
                if settings.mode in {GptMode.FAKE, GptMode.DISABLED}
                else "Provider Gate 상태를 확인했습니다.",
                failure_type="CONFIGURATION_INVALID" if issues else None,
                action="설정 화면에서 승인 Gate를 확인하세요.",
                details={
                    "mode": settings.mode.value,
                    "provider": settings.provider_name,
                    "model": settings.model,
                    "external_call_available": bool(
                        settings.is_real_provider and not issues
                    ),
                },
            )
        )
        provider_stats = GptProviderRunRepository(self.database).dashboard_stats()
        items.append(
            self._item(
                "VALIDATOR", "Validator",
                UatStatus.NORMAL
                if provider_stats.get("requests", 0)
                else UatStatus.NOT_RUN,
                "Validator 결과는 Provider 실행 기록과 답변 metadata에서 확인합니다.",
                details={"today_requests": provider_stats.get("requests", 0)},
            )
        )
        items.append(
            self._item(
                "APPROVAL", "Approval",
                UatStatus.NORMAL if approved_count else UatStatus.NOT_RUN,
                f"현재 승인 완료 문의 {approved_count}건입니다.",
                action="Staff Edit 저장 후 MANAGER 또는 ADMIN으로 승인하세요.",
            )
        )
        items.append(
            self._item(
                "ACTIVITY_LOG", "Activity Log",
                UatStatus.NORMAL if log_count else UatStatus.NOT_RUN,
                f"마스킹된 활동 로그 {log_count}건을 확인했습니다.",
            )
        )

        statuses = {item.status for item in items}
        overall = (
            UatStatus.FAILED if UatStatus.FAILED in statuses
            else UatStatus.WARNING
            if statuses.intersection(
                {UatStatus.WARNING, UatStatus.NOT_CONFIGURED, UatStatus.BLOCKED}
            )
            else UatStatus.NORMAL
        )
        report = UatDiagnosticReport(
            tuple(items), overall, correlation_id, _now()
        )
        self.runs.create_run(
            correlation_id=correlation_id,
            actor=actor,
            status=overall.value,
            started_at=started,
            completed_at=report.checked_at,
            summary=report.to_dict(),
        )
        self.logs.record_system(
            "UAT_DIAGNOSTIC_COMPLETED",
            "개발 PC UAT 진단을 완료했습니다.",
            level="INFO" if overall is UatStatus.NORMAL else "WARNING",
            details={
                "actor": actor,
                "correlation_id": correlation_id,
                "status": overall.value,
                "item_count": len(items),
            },
        )
        return report
