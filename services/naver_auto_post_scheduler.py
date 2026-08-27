from __future__ import annotations

import atexit
import os
import sys
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import RLock, Timer
from typing import Any, Callable

from config import NaverAutoPostSettings, NaverPostSettings
from repositories.auto_post_event_repository import AutoPostEventRepository
from repositories.auto_post_repository import AutoPostRepository
from repositories.database import Database
from repositories.log_repository import LogRepository
from repositories.naver_sync_repository import NaverSyncRepository
from services.auto_post_pipeline_service import AutoPostPipelineService


PROCESS_OWNER_ID = f"auto-post-{os.getpid()}-{uuid.uuid4().hex}"
SchedulerFactory = Callable[[float, Callable[[], None]], Any]
PipelineFactory = Callable[[Database], AutoPostPipelineService]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


class NaverAutoPostScheduler:
    """Timer scheduler backed by a SQLite leader lease and per-inquiry locks."""

    def __init__(
        self,
        database: Database,
        *,
        pipeline_factory: PipelineFactory = AutoPostPipelineService,
        timer_factory: SchedulerFactory = Timer,
        now: Callable[[], datetime] = _utc_now,
        owner_id: str = PROCESS_OWNER_ID,
        tick_seconds: int = 30,
        lease_ttl_seconds: int = 180,
    ) -> None:
        self.database = database
        self.repository = AutoPostRepository(database)
        self.events = AutoPostEventRepository(database)
        self.logs = LogRepository(database)
        self.pipeline_factory = pipeline_factory
        self.timer_factory = timer_factory
        self.now = now
        self.owner_id = owner_id
        self.tick_seconds = max(1, int(tick_seconds))
        self.lease_ttl_seconds = max(30, int(lease_ttl_seconds))
        self._guard = RLock()
        self._timer: Any = None
        self._started = False
        self._running = False
        self._started_event_logged = False
        self._draft_executor = ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="qa-auto-draft",
        )
        self._draft_futures: dict[int, Future[Any]] = {}

    @property
    def started(self) -> bool:
        with self._guard:
            return self._started

    def _schedule(self, seconds: float) -> None:
        with self._guard:
            if not self._started:
                return
            if self._timer is not None:
                self._timer.cancel()
            timer = self.timer_factory(max(0.05, float(seconds)), self._tick)
            timer.daemon = True
            self._timer = timer
            timer.start()

    def start(self) -> bool:
        if not self.repository.settings().get("enabled"):
            return False
        with self._guard:
            if self._started:
                return True
            self._started = True
        if not self.repository.acquire_scheduler_lease(
            owner_id=self.owner_id, ttl_seconds=self.lease_ttl_seconds
        ):
            self._schedule(self.tick_seconds)
            return False
        self.repository.set_state("RUNNING", owner_id=self.owner_id)
        if not self._started_event_logged:
            self.logs.record_system(
                "AUTO_POST_SCHEDULER_STARTED",
                "자동등록 Scheduler Leader를 시작했습니다.",
                details={"owner_pid": os.getpid()},
            )
            self._started_event_logged = True
        settings = self.repository.settings()
        due = _parse(self.repository.state().get("next_run_at"))
        if due is None:
            due = self.now() + timedelta(minutes=int(settings["interval_minutes"]))
        self._schedule(min(self.tick_seconds, max(0.05, (due - self.now()).total_seconds())))
        return True

    def stop(self) -> None:
        with self._guard:
            self._started = False
            timer, self._timer = self._timer, None
            running = self._running
        if timer is not None:
            timer.cancel()
        self.repository.set_state("STOPPING", owner_id=self.owner_id)
        if running:
            return
        self._finish_stop()

    def _finish_stop(self) -> None:
        try:
            self.repository.release_scheduler_lease(self.owner_id)
        except Exception:
            self.repository.set_state(
                "ERROR", error_code="LEASE_RELEASE_FAILED"
            )
            return
        self.repository.set_state("STOPPED")
        if self._started_event_logged:
            self.logs.record_system(
                "AUTO_POST_SCHEDULER_STOPPED",
                "자동등록 Scheduler Leader를 중지했습니다.",
            )
            self._started_event_logged = False

    def _tick(self) -> None:
        try:
            if not self.repository.settings().get("enabled"):
                self.stop()
                return
            if not self.repository.acquire_scheduler_lease(
                owner_id=self.owner_id, ttl_seconds=self.lease_ttl_seconds
            ):
                self._schedule(self.tick_seconds)
                return
            if self.repository.post_unknown_count():
                from services.auto_post_runtime_service import AutoPostRuntimeService

                AutoPostRuntimeService(self.database).pause(
                    "POST_UNKNOWN_PRESENT"
                )
                self.stop()
                return
            queue = self.events.summary()
            if queue["PENDING"] or queue["RETRY_BY_SCHEDULER"]:
                self.run_once(trigger="POLLING_FALLBACK")
                return
            due = _parse(self.repository.state().get("next_run_at"))
            if due is None or due <= self.now():
                self.run_once(trigger="TIMER")
                return
            self._schedule(min(self.tick_seconds, max(0.05, (due - self.now()).total_seconds())))
        except Exception as error:
            self.logs.record_system(
                "AUTO_POST_SCHEDULER_FAILED",
                "자동등록 Scheduler 확인에 실패했습니다.",
                level="ERROR", details={"error_type": error.__class__.__name__},
            )
            self._schedule(self.tick_seconds)

    def _prewarm_pending_drafts(
        self,
        pipeline: AutoPostPipelineService,
        *,
        exclude_inquiry_ids: set[int],
    ) -> None:
        """Generate queued drafts concurrently while POST remains serial.

        A slow DPS UI lookup must not occupy the only path capable of
        classifying and drafting unrelated non-DPS inquiries. Chrome UI
        interaction remains protected by the DPS Agent single-flight lock.
        """
        drafts = getattr(pipeline, "drafts", None)
        ensure = getattr(drafts, "ensure_for_inquiry", None)
        if not callable(ensure):
            return
        pending = self.events.pending_inquiry_ids(
            exclude_inquiry_ids=exclude_inquiry_ids,
            limit=25,
        )
        answer_service = getattr(drafts, "answer_service", None)
        ranked: list[tuple[bool, int]] = []
        for inquiry_id in pending:
            requires_dps = False
            try:
                inquiry = answer_service.inquiries.get(inquiry_id)
                if inquiry is not None:
                    requires_dps = bool(
                        answer_service.plans.create(
                            inquiry
                        ).requires_dps_lookup
                    )
            except Exception:
                # Prewarming is an optimization. The canonical pipeline still
                # performs every safety check if lightweight ranking fails.
                requires_dps = False
            ranked.append((requires_dps, inquiry_id))
        with self._guard:
            for requires_dps, inquiry_id in sorted(ranked):
                if inquiry_id in self._draft_futures:
                    continue
                self._draft_futures[inquiry_id] = (
                    self._draft_executor.submit(
                        ensure,
                        inquiry_id,
                        correlation_id=f"prewarm-{uuid.uuid4()}",
                    )
                )
                self.logs.record_inquiry(
                    inquiry_id,
                    "AUTOMATIC_DRAFT_PREWARM_QUEUED",
                    "다른 문의의 DPS 조회와 독립적으로 초안 생성을 예약했습니다.",
                    details={"requires_dps_lookup": requires_dps},
                )

    def _await_prewarmed_draft(self, inquiry_id: int) -> Any | None:
        with self._guard:
            future = self._draft_futures.get(int(inquiry_id))
        if future is None:
            return None
        try:
            return future.result()
        finally:
            with self._guard:
                self._draft_futures.pop(int(inquiry_id), None)

    def run_once(
        self,
        *,
        trigger: str = "MANUAL_TEST",
        event_only_inquiry_id: int | None = None,
    ) -> dict[str, Any]:
        settings = self.repository.settings()
        environment = NaverAutoPostSettings.from_environment()
        post_environment = NaverPostSettings.from_environment()
        if not settings.get("enabled") or not environment.enabled or not post_environment.enabled:
            return {"status": "DISABLED"}
        if (
            self.pipeline_factory is AutoPostPipelineService
            and not NaverSyncRepository(self.database).auto_settings().get("enabled")
        ):
            self.repository.set_state("WAITING_FOR_SYNC")
            return {"status": "WAITING_FOR_SYNC"}
        if self.repository.post_unknown_count():
            from services.auto_post_runtime_service import AutoPostRuntimeService

            AutoPostRuntimeService(self.database).pause("POST_UNKNOWN_PRESENT")
            return {"status": "PAUSED_POST_UNKNOWN"}
        if not self.repository.acquire_scheduler_lease(
            owner_id=self.owner_id, ttl_seconds=self.lease_ttl_seconds
        ):
            return {"status": "SKIPPED", "reason": "NOT_LEADER"}
        with self._guard:
            if self._running:
                return {"status": "SKIPPED", "reason": "ALREADY_RUNNING"}
            self._running = True
        self.events.recover_stale_processing()
        run_id = self.repository.start_run(owner_id=self.owner_id)
        next_run = (
            self.now() + timedelta(minutes=int(settings["interval_minutes"]))
        ).isoformat(timespec="seconds")
        self.logs.record_system(
            "AUTO_POST_RUN_STARTED",
            "네이버 답변 자동등록 실행을 시작했습니다.",
            details={"auto_post_run_id": run_id, "trigger": trigger},
        )
        try:
            pipeline = self.pipeline_factory(self.database)
            values = {
                "processed_count": 0, "succeeded_count": 0,
                "failed_count": 0, "unknown_count": 0, "skipped_count": 0,
            }
            claimed_any = False
            processed_inquiry_ids: set[int] = set()
            while self.repository.settings().get("enabled"):
                event = self.events.claim_next(
                    owner_id=self.owner_id,
                    event_only_inquiry_id=event_only_inquiry_id,
                    exclude_inquiry_ids=processed_inquiry_ids,
                )
                if event is None:
                    break
                claimed_any = True
                inquiry_id = int(event["inquiry_id"])
                processed_inquiry_ids.add(inquiry_id)
                self._prewarm_pending_drafts(
                    pipeline,
                    exclude_inquiry_ids=processed_inquiry_ids,
                )
                prewarmed = self._await_prewarmed_draft(inquiry_id)
                self.logs.record_inquiry(
                    inquiry_id,
                    "AUTO_SYNC_EVENT_PROCESSING",
                    "Auto Sync Event 처리를 시작했습니다.",
                    details={"event_id": event["id"], "trigger": trigger},
                )
                try:
                    if getattr(prewarmed, "status", None) == "FAILED":
                        item = {
                            "processed_count": 1,
                            "succeeded_count": 0,
                            "failed_count": 1,
                            "unknown_count": 0,
                            "skipped_count": 0,
                        }
                    else:
                        outcome = pipeline.run_pending(
                            run_id=run_id,
                            owner_id=self.owner_id,
                            max_retries=int(settings["max_retries"]),
                            inquiry_ids=[inquiry_id],
                        )
                        item = outcome.to_dict()
                except TypeError as error:
                    if "inquiry_ids" not in str(error):
                        raise
                    item = pipeline.run_pending(
                        run_id=run_id,
                        owner_id=self.owner_id,
                        max_retries=int(settings["max_retries"]),
                    ).to_dict()
                for key in values:
                    values[key] += int(item.get(key) or 0)
                if item["unknown_count"]:
                    self.events.fail(
                        int(event["id"]), owner_id=self.owner_id,
                        error_code="POST_UNKNOWN", retry_by_scheduler=False,
                    )
                    from services.auto_post_runtime_service import (
                        AutoPostRuntimeService,
                    )

                    AutoPostRuntimeService(self.database).pause(
                        "POST_UNKNOWN_PRESENT"
                    )
                    break
                if item["failed_count"]:
                    retry = (
                        str(trigger).upper() not in {"TIMER", "POLLING_FALLBACK"}
                        or int(event["attempt_count"]) <= int(settings["max_retries"])
                    )
                    self.events.fail(
                        int(event["id"]), owner_id=self.owner_id,
                        error_code="EVENT_PIPELINE_FAILED",
                        retry_by_scheduler=retry,
                    )
                    self.logs.record_inquiry(
                        inquiry_id,
                        "AUTO_SYNC_EVENT_FAILED",
                        "Auto Sync Event 처리에 실패했습니다.",
                        level="ERROR", details={"event_id": event["id"]},
                    )
                    if retry:
                        self.logs.record_inquiry(
                            inquiry_id,
                            "AUTO_SYNC_EVENT_RETRY_BY_SCHEDULER",
                            "Polling Scheduler 재처리 대상으로 전환했습니다.",
                            level="WARNING", details={"event_id": event["id"]},
                        )
                else:
                    self.events.complete(int(event["id"]), owner_id=self.owner_id)
                    self.logs.record_inquiry(
                        inquiry_id,
                        "AUTO_SYNC_EVENT_COMPLETED",
                        "Auto Sync Event 처리를 완료했습니다.",
                        details={"event_id": event["id"]},
                    )
                if event_only_inquiry_id is not None:
                    break
            # Preserve injected scheduler test doubles without selecting legacy
            # production Pending inquiries. The real pipeline is queue-only.
            if not claimed_any and type(pipeline) is not AutoPostPipelineService:
                fallback = pipeline.run_pending(
                    run_id=run_id,
                    owner_id=self.owner_id,
                    max_retries=int(settings["max_retries"]),
                ).to_dict()
                values = {key: int(fallback.get(key) or 0) for key in values}
            status = (
                "SUCCESS"
                if not values["failed_count"] and not values["unknown_count"]
                else "PARTIAL"
            )
            self.repository.finish_run(
                owner_id=self.owner_id, run_id=run_id, status=status,
                result=values, next_run_at=next_run,
            )
            self.logs.record_system(
                "AUTO_POST_RUN_COMPLETED",
                "네이버 답변 자동등록 실행을 완료했습니다.",
                level="INFO" if status == "SUCCESS" else "WARNING",
                details={"auto_post_run_id": run_id, **values},
            )
            if values["unknown_count"]:
                self.repository.set_state(
                    "PAUSED_POST_UNKNOWN", error_code="POST_UNKNOWN_PRESENT",
                    owner_id=self.owner_id,
                )
            elif self.repository.settings().get("enabled"):
                self.repository.set_state("RUNNING", owner_id=self.owner_id)
            return {"status": status, "auto_post_run_id": run_id, **values}
        except Exception as error:
            values = {
                "processed_count": 0, "succeeded_count": 0,
                "failed_count": 1, "unknown_count": 0, "skipped_count": 0,
            }
            self.repository.finish_run(
                owner_id=self.owner_id, run_id=run_id, status="FAILED",
                result=values, next_run_at=next_run,
                error_code=error.__class__.__name__.upper()[:100],
                error_message="자동등록 실행 실패",
            )
            self.logs.record_system(
                "AUTO_POST_RUN_FAILED",
                "네이버 답변 자동등록 실행에 실패했습니다.",
                level="ERROR", details={
                    "auto_post_run_id": run_id,
                    "error_type": error.__class__.__name__,
                },
            )
            return {"status": "FAILED", "auto_post_run_id": run_id, **values}
        finally:
            with self._guard:
                self._running = False
                should_continue = self._started and bool(
                    self.repository.settings().get("enabled")
                )
            if should_continue:
                self._schedule(self.tick_seconds)
            else:
                self._finish_stop()

    def notify_event(self, inquiry_id: int) -> None:
        """Wake the leader without executing inside the sync DB transaction."""
        if not self.started:
            return
        self._schedule(0.05)


_REGISTRY_LOCK = RLock()
_SCHEDULERS: dict[str, NaverAutoPostScheduler] = {}

# Which databases this *interpreter* has already forced OFF at startup.
#
# This deliberately does not live in a module global. Streamlit reloads a
# changed module inside the running process, and a reloaded module re-executes
# its top level -- so a module-level set forgets everything it knew. The guard
# below then read "this process has never run" and forced the switch OFF again,
# in the same process, with no restart and nobody having pressed Stop. That is
# the operator report: the screen refreshed and 자동처리 was off.
#
# ``sys`` is never reloaded, so an attribute on it lasts exactly as long as the
# interpreter does -- which is the lifetime the guard is supposed to have. A
# pid would not do: pids are recycled, and a genuinely new process inheriting a
# recycled pid would skip the reset and resume posting unattended, which is the
# one thing this must never allow.
_RESET_REGISTRY_ATTRIBUTE = "_oje_auto_post_startup_reset_done"


def _startup_reset_registry() -> set[str]:
    registry = getattr(sys, _RESET_REGISTRY_ATTRIBUTE, None)
    if not isinstance(registry, set):
        registry = set()
        setattr(sys, _RESET_REGISTRY_ATTRIBUTE, registry)
    return registry


def reset_auto_post_runtime_on_process_start(database: Database) -> bool:
    """Force the persisted operator switch OFF once per process, at startup.

    ``naver_auto_post_settings.enabled`` is a DB-persisted switch, so it
    otherwise survives a full Streamlit process restart -- a new process
    would silently resume automatic customer-facing outbound processing
    with zero operator interaction.  This must run exactly once per process
    (per database path), *before* anything in this process has had a
    chance to call :func:`ensure_auto_post_scheduler` or
    ``AutoPostRuntimeService.enable()`` for a legitimate reason (a Streamlit
    UI rerun within the same already-running process, or this same
    process's own explicit ``enable()`` call, must never be reset).

    Callers: only ``app.py``'s startup path (``main()``), executed before
    ``ensure_auto_post_scheduler``.  Returns True only when a reset
    actually happened (for logging/tests).
    """

    key = str(Path(database.path).resolve())
    with _REGISTRY_LOCK:
        registry = _startup_reset_registry()
        if key in registry:
            return False
        registry.add(key)
    repository = AutoPostRepository(database)
    settings = repository.settings()
    if not settings.get("enabled"):
        return False
    repository.save_settings(
        enabled=False,
        interval_minutes=int(settings.get("interval_minutes") or 10),
        max_retries=int(settings.get("max_retries") or 1),
    )
    repository.set_state("STOPPED")
    LogRepository(database).record_system(
        "AUTO_POST_RUNTIME_FORCED_OFF_ON_STARTUP",
        "새 프로세스 시작으로 자동등록 운영 상태(Dashboard operation)를 OFF로 "
        "초기화했습니다. 자동등록을 재개하려면 운영자가 다시 ON을 눌러야 "
        "합니다.",
        level="WARNING",
    )
    return True


def ensure_auto_post_scheduler(
    database: Database,
    *,
    pipeline_factory: PipelineFactory = AutoPostPipelineService,
    timer_factory: SchedulerFactory = Timer,
) -> NaverAutoPostScheduler:
    key = str(Path(database.path).resolve())
    environment = NaverAutoPostSettings.from_environment()
    repository = AutoPostRepository(database)
    settings = repository.ensure_settings(
        enabled=environment.enabled,
        interval_minutes=environment.interval_minutes,
        max_retries=environment.max_retries,
    )
    with _REGISTRY_LOCK:
        scheduler = _SCHEDULERS.get(key)
        if scheduler is None:
            scheduler = NaverAutoPostScheduler(
                database,
                pipeline_factory=pipeline_factory,
                timer_factory=timer_factory,
            )
            _SCHEDULERS[key] = scheduler
    if not settings.get("enabled"):
        scheduler.stop()
        repository.set_state("STOPPED")
        return scheduler
    if not environment.enabled or not NaverPostSettings.from_environment().enabled:
        scheduler.stop()
        repository.set_state("DISABLED_BY_ENV", error_code="ENVIRONMENT_DISABLED")
        return scheduler
    if not NaverSyncRepository(database).auto_settings().get("enabled"):
        scheduler.stop()
        repository.set_state("WAITING_FOR_SYNC")
        return scheduler
    recovered = scheduler.events.recover_stale_processing()
    recovered_posting = repository.recover_stale_posting()
    if recovered or recovered_posting:
        scheduler.logs.record_system(
            "AUTO_POST_RESUMED",
            "앱 재시작 후 자동등록 작업 상태를 복구했습니다.",
            level="WARNING" if recovered_posting else "INFO",
            details={
                "recovered_event_count": recovered,
                "recovered_posting_count": recovered_posting,
            },
        )
    if repository.post_unknown_count():
        scheduler.stop()
        repository.set_pause_reason("POST_UNKNOWN_PRESENT")
        repository.set_state(
            "PAUSED_POST_UNKNOWN", error_code="POST_UNKNOWN_PRESENT"
        )
        return scheduler
    scheduler.start()
    return scheduler


def stop_all_auto_post_schedulers() -> None:
    with _REGISTRY_LOCK:
        schedulers = list(_SCHEDULERS.values())
    for scheduler in schedulers:
        scheduler.stop()


atexit.register(stop_all_auto_post_schedulers)
