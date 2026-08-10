from __future__ import annotations

import logging
from threading import RLock, Timer
from typing import Any, Callable

from config import DpsSessionSettings


Monitor = Callable[..., dict[str, Any]]
TimerFactory = Callable[[float, Callable[[], None]], Any]


class DpsSessionMonitorScheduler:
    """Timer-based DPS monitor used by the long-lived Windows Agent.

    The scheduler has no loop of its own.  Each tick schedules exactly one
    daemon Timer, following the existing Q&A Auto scheduler convention.
    """

    def __init__(
        self,
        monitor: Monitor,
        *,
        settings: DpsSessionSettings | None = None,
        timer_factory: TimerFactory = Timer,
        logger: logging.Logger | None = None,
    ) -> None:
        self.monitor = monitor
        self.settings = settings or DpsSessionSettings.from_environment()
        self.timer_factory = timer_factory
        self.logger = logger or logging.getLogger(__name__)
        self._guard = RLock()
        self._timer: Any = None
        self._started = False
        self._running = False
        self._failure_count = 0

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
        if not self.settings.monitor_enabled:
            return False
        with self._guard:
            if self._started:
                return True
            self._started = True
        self._schedule(0.1)
        return True

    def stop(self) -> None:
        with self._guard:
            self._started = False
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()

    def _tick(self) -> None:
        with self._guard:
            if not self._started:
                return
            if self._running:
                self._schedule(self.settings.monitor_interval_seconds)
                return
            self._running = True
        delay = self.settings.monitor_interval_seconds
        try:
            self.monitor(
                keepalive_enabled=self.settings.keepalive_enabled,
                keepalive_interval_seconds=(
                    self.settings.keepalive_interval_minutes * 60
                ),
                trigger="SCHEDULER",
            )
            self._failure_count = 0
        except Exception as error:  # Agent must remain available after a tick.
            self._failure_count += 1
            delay = min(
                self.settings.keepalive_interval_minutes * 60,
                self.settings.monitor_interval_seconds
                * (2 ** min(self._failure_count, 3)),
            )
            self.logger.warning(
                "DPS session monitor tick failed: error_type=%s retry_seconds=%s",
                error.__class__.__name__,
                delay,
            )
        finally:
            with self._guard:
                self._running = False
                should_continue = self._started
            if should_continue:
                self._schedule(delay)
