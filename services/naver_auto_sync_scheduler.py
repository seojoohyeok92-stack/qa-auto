from __future__ import annotations

import atexit
import logging
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import RLock, Timer
from typing import Any, Callable
import uuid

from config import NaverAutoSyncSettings, NaverSyncSettings
from repositories.database import Database
from repositories.log_repository import LogRepository
from repositories.naver_sync_repository import NaverSyncRepository
from services.inquiry_sync_orchestrator import InquirySyncOrchestrator
from api.naver_read_client import NaverSyncError


LOGGER = logging.getLogger(__name__)
PROCESS_OWNER_ID = f"{os.getpid()}-{uuid.uuid4().hex}"
SchedulerFactory = Callable[[float, Callable[[], None]], Any]
ServiceFactory = Callable[[Database], Any]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _error_code(error: Exception) -> str:
    if isinstance(error, NaverSyncError):
        return str(error.code or "UNKNOWN_ERROR")[:100]
    if isinstance(error, TimeoutError):
        return "API_TIMEOUT"
    if isinstance(error, ConnectionError):
        return "NETWORK_ERROR"
    if isinstance(error, sqlite3.OperationalError) and "lock" in str(
        error
    ).lower():
        return "DB_LOCK"
    if isinstance(error, (ValueError, TypeError)):
        return "INTERNAL_TRANSFORMATION_ERROR"
    return "UNKNOWN_ERROR"


class NaverAutoSyncScheduler:
    """Single-process timer with a cross-process SQLite leader lease."""

    def __init__(
        self,
        database: Database,
        *,
        service_factory: ServiceFactory = InquirySyncOrchestrator,
        timer_factory: SchedulerFactory = Timer,
        now: Callable[[], datetime] = _utc_now,
        owner_id: str = PROCESS_OWNER_ID,
        lease_ttl_seconds: int = 180,
        tick_seconds: int = 30,
    ) -> None:
        self.database = database
        self.runs = NaverSyncRepository(database)
        self.logs = LogRepository(database)
        self.service_factory = service_factory
        self.timer_factory = timer_factory
        self.now = now
        self.owner_id = owner_id
        self.lease_ttl_seconds = max(30, int(lease_ttl_seconds))
        self.tick_seconds = max(1, int(tick_seconds))
        self._guard = RLock()
        self._timer: Any = None
        self._started = False

    @property
    def started(self) -> bool:
        with self._guard:
            return self._started

    def _settings(self) -> dict[str, Any]:
        return self.runs.auto_settings()

    def _next_after(self, interval_minutes: int) -> datetime:
        return self.now() + timedelta(minutes=int(interval_minutes))

    def _schedule(self, delay_seconds: float) -> None:
        with self._guard:
            if not self._started:
                return
            if self._timer is not None:
                self._timer.cancel()
            timer = self.timer_factory(
                max(0.05, float(delay_seconds)), self._tick
            )
            timer.daemon = True
            self._timer = timer
            timer.start()

    def start(self) -> bool:
        settings = self._settings()
        if not settings.get("enabled"):
            return False
        with self._guard:
            if self._started:
                return True
            self._started = True
        if not self.runs.acquire_scheduler_lease(
            owner_id=self.owner_id,
            ttl_seconds=self.lease_ttl_seconds,
        ):
            self._schedule(self.tick_seconds)
            return False
        state = self.runs.auto_state()
        next_run = _parse_datetime(state.get("next_run_at"))
        if next_run is None:
            next_run = self._next_after(settings["interval_minutes"])
            self.runs.set_auto_next_run(
                owner_id=self.owner_id,
                next_run_at=next_run.isoformat(timespec="seconds"),
            )
        delay = max(0.05, (next_run - self.now()).total_seconds())
        self._schedule(min(delay, self.tick_seconds))
        return True

    def stop(self) -> None:
        with self._guard:
            self._started = False
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
        try:
            self.runs.release_scheduler_lease(self.owner_id)
        except Exception:
            LOGGER.exception("자동 동기화 scheduler lease 해제 실패")

    def _tick(self) -> None:
        try:
            settings = self._settings()
            if not settings.get("enabled"):
                self.stop()
                return
            if not self.runs.acquire_scheduler_lease(
                owner_id=self.owner_id,
                ttl_seconds=self.lease_ttl_seconds,
            ):
                self._schedule(self.tick_seconds)
                return
            state = self.runs.auto_state()
            due = _parse_datetime(state.get("next_run_at"))
            if due is None or due <= self.now():
                self.run_once()
                return
            self._schedule(
                min(
                    self.tick_seconds,
                    max(0.05, (due - self.now()).total_seconds()),
                )
            )
        except Exception as error:
            LOGGER.exception("자동 동기화 scheduler tick 실패")
            self.logs.record_system(
                "NAVER_AUTO_SYNC_SCHEDULER_FAILED",
                "자동 동기화 예약 확인에 실패했습니다.",
                level="ERROR",
                details={"error_type": error.__class__.__name__},
            )
            self._schedule(self.tick_seconds)

    def run_once(self) -> dict[str, Any]:
        settings = self._settings()
        interval = int(settings.get("interval_minutes") or 10)
        next_run = self._next_after(interval).isoformat(timespec="seconds")
        if not settings.get("enabled"):
            return {"status": "DISABLED"}
        if not self.runs.acquire_scheduler_lease(
            owner_id=self.owner_id,
            ttl_seconds=self.lease_ttl_seconds,
        ):
            return {"status": "SKIPPED", "reason": "NOT_LEADER"}
        active_locks = self.runs.active_locks()
        if active_locks:
            self.runs.finish_auto_run(
                owner_id=self.owner_id,
                status="SKIPPED",
                next_run_at=next_run,
                error_code="SYNC_IN_PROGRESS",
                error_message="이미 동기화가 진행 중입니다.",
            )
            self.logs.record_system(
                "NAVER_AUTO_SYNC_SKIPPED",
                "이미 동기화가 진행 중이어서 자동 실행을 건너뛰었습니다.",
                details={"active_lock_count": len(active_locks)},
            )
            LOGGER.info("NAVER_SYNC_SKIPPED_LOCKED")
            self._schedule(self.tick_seconds)
            return {"status": "SKIPPED", "reason": "SYNC_IN_PROGRESS"}

        self.runs.mark_auto_running(owner_id=self.owner_id)
        self.logs.record_system(
            "NAVER_AUTO_SYNC_STARTED",
            "예약된 네이버 문의 자동 동기화를 시작했습니다.",
            details={"interval_minutes": interval},
        )
        LOGGER.info("NAVER_SYNC_START interval_minutes=%s", interval)
        try:
            outcome = self.service_factory(self.database).run(
                sync_type="AUTO",
                owner_id=self.owner_id,
            )
            value = (
                outcome.to_dict()
                if hasattr(outcome, "to_dict")
                else dict(outcome)
            )
            if str(value.get("status") or "").upper() == "SKIPPED":
                self.runs.finish_auto_run(
                    owner_id=self.owner_id,
                    status="SKIPPED",
                    next_run_at=next_run,
                    result=value,
                    error_code="SYNC_IN_PROGRESS",
                    error_message=value.get("error_message"),
                )
                self.logs.record_system(
                    "NAVER_AUTO_SYNC_SKIPPED",
                    "이미 진행 중인 동일 스토어 동기화로 자동 실행을 건너뛰었습니다.",
                    details={
                        "reason": "SYNC_IN_PROGRESS",
                        "skipped_count": int(value.get("skipped_count") or 0),
                    },
                )
                return {"status": "SKIPPED", **value}
            success = (
                str(value.get("status") or "SUCCESS") == "SUCCESS"
                and int(value.get("failed_count") or 0) == 0
            )
            status = "SUCCESS" if success else "PARTIAL_SYNC"
            self.runs.finish_auto_run(
                owner_id=self.owner_id,
                status=status,
                next_run_at=next_run,
                result=value,
                error_code=value.get("error_code"),
                error_message=value.get("error_message"),
            )
            self.logs.record_system(
                (
                    "NAVER_AUTO_SYNC_COMPLETED"
                    if success
                    else "NAVER_AUTO_SYNC_PARTIAL"
                ),
                (
                    "네이버 문의 자동 동기화를 완료했습니다."
                    if success
                    else "네이버 문의 자동 동기화가 일부 실패했습니다."
                ),
                level="INFO" if success else "WARNING",
                details={
                    "sync_id": value.get("sync_id")
                    or value.get("correlation_id"),
                    "fetched_count": int(
                        value.get("fetched_count") or 0
                    ),
                    "inserted_count": int(
                        value.get("inserted_count")
                        or value.get("created_count")
                        or 0
                    ),
                    "updated_count": int(
                        value.get("updated_count") or 0
                    ),
                    "unchanged_count": int(
                        value.get("unchanged_count") or 0
                    ),
                    "failed_count": int(
                        value.get("failed_count") or 0
                    ),
                },
            )
            inserted_count = int(
                value.get("inserted_count") or value.get("created_count") or 0
            )
            LOGGER.info(
                "NAVER_SYNC_SUCCESS inserted_count=%s", inserted_count
            )
            if inserted_count == 0:
                LOGGER.info("NAVER_SYNC_NO_NEW_INQUIRY")
            try:
                from services.naver_auto_post_scheduler import (
                    ensure_auto_post_scheduler,
                )
                ensure_auto_post_scheduler(self.database).run_once(
                    trigger="AUTO_SYNC_COMPLETED"
                )
            except Exception as auto_post_error:
                self.logs.record_system(
                    "AUTO_POST_TRIGGER_FAILED",
                    "동기화 후 자동등록 Trigger를 시작하지 못했습니다.",
                    level="ERROR",
                    details={"error_type": auto_post_error.__class__.__name__},
                )
            return {"status": status, **value}
        except Exception as error:
            error_code = _error_code(error)
            if "AUTH" in error_code.upper():
                LOGGER.warning("NAVER_SYNC_AUTH_ERROR error_type=%s", error_code)
            elif "TIMEOUT" in error_code.upper():
                LOGGER.warning("NAVER_SYNC_TIMEOUT error_type=%s", error_code)
            else:
                LOGGER.warning("NAVER_SYNC_API_ERROR error_type=%s", error_code)
            self.runs.finish_auto_run(
                owner_id=self.owner_id,
                status="FAILED",
                next_run_at=next_run,
                error_code=error_code,
                error_message="자동 동기화를 완료하지 못했습니다.",
            )
            self.logs.record_system(
                "NAVER_AUTO_SYNC_FAILED",
                "네이버 문의 자동 동기화를 완료하지 못했습니다.",
                level="ERROR",
                details={"error_type": error.__class__.__name__},
            )
            return {"status": "FAILED", "error_code": error_code}
        finally:
            self._schedule(self.tick_seconds)


_REGISTRY_LOCK = RLock()
_SCHEDULERS: dict[str, NaverAutoSyncScheduler] = {}


def ensure_auto_sync_scheduler(
    database: Database,
    *,
    service_factory: ServiceFactory = InquirySyncOrchestrator,
    timer_factory: SchedulerFactory = Timer,
) -> NaverAutoSyncScheduler:
    key = str(Path(database.path).resolve())
    environment = NaverAutoSyncSettings.from_environment()
    runs = NaverSyncRepository(database)
    settings = runs.ensure_auto_settings(
        enabled=environment.enabled,
        interval_minutes=environment.interval_minutes,
    )
    with _REGISTRY_LOCK:
        scheduler = _SCHEDULERS.get(key)
        if scheduler is None:
            scheduler = NaverAutoSyncScheduler(
                database,
                service_factory=service_factory,
                timer_factory=timer_factory,
            )
            _SCHEDULERS[key] = scheduler
    if settings.get("enabled") and NaverSyncSettings.from_environment().enabled:
        scheduler.start()
    else:
        scheduler.stop()
    return scheduler


def stop_all_auto_sync_schedulers() -> None:
    with _REGISTRY_LOCK:
        schedulers = list(_SCHEDULERS.values())
    for scheduler in schedulers:
        scheduler.stop()


atexit.register(stop_all_auto_sync_schedulers)
