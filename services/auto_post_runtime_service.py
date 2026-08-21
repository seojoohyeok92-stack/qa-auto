from __future__ import annotations

from typing import Any, Callable

from config import (
    NaverAutoPostSettings,
    NaverPostSettings,
    get_configured_stores,
)
from repositories.auto_post_event_repository import AutoPostEventRepository
from repositories.auto_post_repository import AutoPostRepository
from repositories.database import Database
from repositories.log_repository import LogRepository
from repositories.naver_sync_repository import NaverSyncRepository


class AutoPostRuntimeService:
    """Persistent operator switch for the complete automatic-processing path."""

    def __init__(
        self,
        database: Database,
        *,
        authentication_ready: Callable[[], bool] | None = None,
    ) -> None:
        self.database = database
        self.repository = AutoPostRepository(database)
        self.events = AutoPostEventRepository(database)
        self.logs = LogRepository(database)
        self.authentication_ready = authentication_ready or (
            lambda: bool(get_configured_stores())
        )

    def environment_ready(self) -> bool:
        return (
            NaverPostSettings.from_environment().enabled
            and NaverAutoPostSettings.from_environment().enabled
        )

    def _integrity_ok(self) -> bool:
        try:
            with self.database.connection() as connection:
                return connection.execute(
                    "PRAGMA integrity_check"
                ).fetchone()[0] == "ok"
        except Exception:
            return False

    def preflight(self) -> dict[str, Any]:
        sync_settings = NaverSyncRepository(self.database).auto_settings()
        state = self.repository.state()
        environment_ready = self.environment_ready()
        checks = {
            "naver_post_enabled": NaverPostSettings.from_environment().enabled,
            "naver_auto_post_enabled": NaverAutoPostSettings.from_environment().enabled,
            "authentication_ready": bool(self.authentication_ready()),
            "db_integrity_ok": self._integrity_ok(),
            "post_unknown_count": self.repository.post_unknown_count(),
            "posting_count": self.repository.posting_count(),
            "leader_owner_present": bool(state.get("owner_id")),
            "auto_sync_enabled": bool(sync_settings.get("enabled")),
        }
        blocking = []
        if environment_ready and not checks["authentication_ready"]:
            blocking.append("AUTHENTICATION_NOT_READY")
        if not checks["db_integrity_ok"]:
            blocking.append("DB_INTEGRITY_ERROR")
        if checks["post_unknown_count"]:
            blocking.append("POST_UNKNOWN_PRESENT")
        if checks["posting_count"]:
            blocking.append("POSTING_IN_PROGRESS")
        return {
            **checks,
            "environment_ready": environment_ready,
            "blocking_reasons": blocking,
            "ready": environment_ready and not blocking,
        }

    def enable(self) -> dict[str, Any]:
        self.logs.record_system(
            "AUTO_POST_RUNTIME_ENABLE_REQUESTED",
            "운영자가 자동등록 Runtime 시작을 요청했습니다.",
        )
        settings = self.repository.settings()
        preflight = self.preflight()
        if not preflight["environment_ready"]:
            self.repository.set_state(
                "DISABLED_BY_ENV", error_code="ENVIRONMENT_DISABLED"
            )
            self.logs.record_system(
                "AUTO_POST_RUNTIME_ENABLED",
                "자동등록 Runtime 요청을 저장했지만 환경설정으로 실행하지 않습니다.",
                level="WARNING",
                details={"actual_status": "DISABLED_BY_ENV"},
            )
            return {"status": "DISABLED_BY_ENV", "settings": settings, **preflight}
        if preflight["blocking_reasons"]:
            reason = str(preflight["blocking_reasons"][0])
            status = "PAUSED_POST_UNKNOWN" if reason == "POST_UNKNOWN_PRESENT" else "BLOCKED"
            self.repository.set_pause_reason(reason)
            self.repository.set_state(status, error_code=reason)
            self.logs.record_system(
                "AUTO_POST_PAUSED",
                "시스템 안전 조건으로 자동등록을 일시정지했습니다.",
                level="ERROR",
                details={"reason": reason},
            )
            return {"status": status, "settings": settings, **preflight}

        settings = self.repository.save_settings(
            enabled=True,
            interval_minutes=int(settings.get("interval_minutes") or 10),
            max_retries=int(settings.get("max_retries") or 1),
        )
        unblocked = self.events.unblock_after_runtime_enable()
        sync_enabled = bool(preflight["auto_sync_enabled"])
        self.repository.set_state("STARTING" if sync_enabled else "WAITING_FOR_SYNC")
        self.logs.record_system(
            "AUTO_POST_RUNTIME_ENABLED",
            "자동등록 Runtime을 활성화했습니다.",
            details={
                "actual_status": "STARTING" if sync_enabled else "WAITING_FOR_SYNC",
                "unblocked_event_count": unblocked,
            },
        )
        if unblocked:
            self.logs.record_system(
                "AUTO_POST_EVENTS_UNBLOCKED_ON_ENABLE",
                "자동처리 재개로 OFF 중 대기하던 기존 미처리 문의를 자동처리 "
                "대상으로 복귀시켰습니다.",
                details={"unblocked_event_count": unblocked},
            )
        if not sync_enabled:
            return {"status": "WAITING_FOR_SYNC", "settings": settings, **preflight}
        from services.naver_auto_post_scheduler import ensure_auto_post_scheduler

        scheduler = ensure_auto_post_scheduler(self.database)
        status = "RUNNING" if scheduler.started else "BLOCKED"
        return {"status": status, "settings": settings, **preflight}

    def disable(self) -> dict[str, Any]:
        self.logs.record_system(
            "AUTO_POST_RUNTIME_DISABLE_REQUESTED",
            "운영자가 자동등록 Runtime 중지를 요청했습니다.",
        )
        settings = self.repository.settings()
        self.repository.save_settings(
            enabled=False,
            interval_minutes=int(settings.get("interval_minutes") or 10),
            max_retries=int(settings.get("max_retries") or 1),
        )
        blocked = self.events.block_new_claims()
        self.repository.set_state("STOPPING")
        from services.naver_auto_post_scheduler import ensure_auto_post_scheduler

        scheduler = ensure_auto_post_scheduler(self.database)
        scheduler.stop()
        state = self.repository.state()
        self.logs.record_system(
            "AUTO_POST_RUNTIME_DISABLED",
            "자동등록 Runtime을 비활성화했습니다.",
            details={
                "scheduler_status": state.get("status") or "STOPPED",
                "blocked_event_count": blocked,
                "posting_count": self.repository.posting_count(),
            },
        )
        return {"status": state.get("status") or "STOPPED", "blocked": blocked}

    def pause(self, reason: str) -> None:
        safe_reason = str(reason).upper()[:100]
        settings = self.repository.settings()
        self.repository.save_settings(
            enabled=False,
            interval_minutes=int(settings.get("interval_minutes") or 10),
            max_retries=int(settings.get("max_retries") or 1),
        )
        self.events.block_new_claims()
        self.repository.set_pause_reason(safe_reason)
        self.repository.set_state(
            "PAUSED_POST_UNKNOWN" if "POST_UNKNOWN" in safe_reason else "BLOCKED",
            error_code=safe_reason,
        )
        self.logs.record_system(
            "AUTO_POST_PAUSED",
            "시스템 안전 조건으로 자동등록을 일시정지했습니다.",
            level="ERROR",
            details={"reason": safe_reason},
        )
