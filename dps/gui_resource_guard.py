from __future__ import annotations

import ctypes
import fnmatch
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable

from config import DpsGuiGuardSettings


GUI_RESOURCE_STATES = {"FREE", "BUSY", "COOLDOWN", "TIMEOUT", "UNKNOWN"}


@dataclass(frozen=True, slots=True)
class ForegroundActivity:
    hwnd: int | None = None
    window_title: str = ""
    process_name: str = ""
    error: str | None = None


@dataclass(frozen=True, slots=True)
class GUIResourceState:
    available: bool
    state: str
    reason: str
    detected_source: str
    wait_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.state not in GUI_RESOURCE_STATES:
            raise ValueError(f"Unsupported GUI resource state: {self.state}")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _matches(value: str, patterns: Iterable[str]) -> str | None:
    folded = str(value or "").casefold()
    for raw_pattern in patterns:
        pattern = str(raw_pattern or "").strip().casefold()
        if not pattern:
            continue
        if fnmatch.fnmatch(folded, pattern) or pattern in folded:
            return raw_pattern
    return None


def read_foreground_activity() -> ForegroundActivity:
    """Read only the current foreground owner; never enumerate resident processes."""

    if os.name != "nt":
        return ForegroundActivity(error="NON_WINDOWS")
    process_handle = None
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = int(user32.GetForegroundWindow() or 0)
        if hwnd <= 0:
            return ForegroundActivity(error="FOREGROUND_WINDOW_NOT_FOUND")

        length = int(user32.GetWindowTextLengthW(hwnd) or 0)
        title_buffer = ctypes.create_unicode_buffer(max(1, length + 1))
        user32.GetWindowTextW(hwnd, title_buffer, len(title_buffer))

        process_id = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        process_name = ""
        if process_id.value:
            process_handle = kernel32.OpenProcess(
                0x1000,  # PROCESS_QUERY_LIMITED_INFORMATION
                False,
                process_id.value,
            )
            if process_handle:
                size = ctypes.c_ulong(32768)
                path_buffer = ctypes.create_unicode_buffer(size.value)
                if kernel32.QueryFullProcessImageNameW(
                    process_handle, 0, path_buffer, ctypes.byref(size)
                ):
                    process_name = os.path.basename(path_buffer.value)
        return ForegroundActivity(
            hwnd=hwnd,
            window_title=title_buffer.value,
            process_name=process_name,
        )
    except Exception as error:  # pragma: no cover - Windows API failure
        return ForegroundActivity(error=error.__class__.__name__)
    finally:
        if process_handle:
            try:
                ctypes.windll.kernel32.CloseHandle(process_handle)
            except Exception:
                pass


class GUIResourceGuard:
    """Low-priority gate for DPS foreground UI automation."""

    def __init__(
        self,
        settings: DpsGuiGuardSettings | None = None,
        *,
        project_root: Path | None = None,
        foreground_reader: Callable[[], ForegroundActivity] = read_foreground_activity,
        monotonic: Callable[[], float] = time.monotonic,
        wall_time: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings or DpsGuiGuardSettings.from_environment()
        self.project_root = (project_root or Path.cwd()).resolve()
        self.foreground_reader = foreground_reader
        self.monotonic = monotonic
        self.wall_time = wall_time
        self.sleep = sleep
        self.logger = logger or logging.getLogger(__name__)
        self._last_unavailable_at: float | None = None

    def _activity_path(self, configured: str) -> Path:
        path = Path(os.path.expandvars(configured)).expanduser()
        return path if path.is_absolute() else self.project_root / path

    def _active_marker(self) -> GUIResourceState | None:
        now = self.wall_time()
        for configured in self.settings.activity_paths:
            path = self._activity_path(configured)
            try:
                age = max(0.0, now - path.stat().st_mtime)
            except FileNotFoundError:
                continue
            except OSError as error:
                self._last_unavailable_at = self.monotonic()
                return GUIResourceState(
                    False,
                    "UNKNOWN",
                    f"ACTIVITY_MARKER_UNREADABLE:{error.__class__.__name__}",
                    "activity_marker",
                )
            if age <= self.settings.activity_grace_seconds:
                self._last_unavailable_at = self.monotonic()
                return GUIResourceState(
                    False,
                    "BUSY",
                    "RECENT_GUI_ACTIVITY_MARKER",
                    f"activity_marker:{path.name}",
                )
        return None

    def check(self) -> GUIResourceState:
        if not self.settings.enabled:
            return GUIResourceState(True, "FREE", "GUARD_DISABLED", "configuration")

        marker = self._active_marker()
        if marker is not None:
            return marker

        foreground = self.foreground_reader()
        if foreground.error not in {None, "NON_WINDOWS", "FOREGROUND_WINDOW_NOT_FOUND"}:
            self._last_unavailable_at = self.monotonic()
            return GUIResourceState(
                False,
                "UNKNOWN",
                f"FOREGROUND_DETECTION_FAILED:{foreground.error}",
                "foreground_window",
            )

        title_pattern = _matches(
            foreground.window_title, self.settings.window_patterns
        )
        process_pattern = _matches(
            foreground.process_name, self.settings.process_patterns
        )
        if title_pattern or process_pattern:
            self._last_unavailable_at = self.monotonic()
            reason = "FOREGROUND_WINDOW_ACTIVE" if title_pattern else "FOREGROUND_PROCESS_ACTIVE"
            source = (
                f"foreground_window:{title_pattern}"
                if title_pattern
                else f"foreground_process:{process_pattern}"
            )
            return GUIResourceState(False, "BUSY", reason, source)

        now = self.monotonic()
        if self._last_unavailable_at is not None:
            remaining = self.settings.cooldown_seconds - (
                now - self._last_unavailable_at
            )
            if remaining > 0:
                return GUIResourceState(
                    False,
                    "COOLDOWN",
                    "POST_ACTIVITY_COOLDOWN",
                    "guard_history",
                    wait_seconds=remaining,
                )
        return GUIResourceState(True, "FREE", "NO_COMPETING_GUI_ACTIVITY", "signals")

    def wait_for_available(self) -> GUIResourceState:
        started = self.monotonic()
        last = self.check()
        while not last.available:
            elapsed = self.monotonic() - started
            remaining = self.settings.max_wait_seconds - elapsed
            if remaining <= 0:
                return GUIResourceState(
                    False,
                    "TIMEOUT",
                    f"GUI_RESOURCE_WAIT_TIMEOUT:{last.reason}",
                    last.detected_source,
                )
            delay = min(float(self.settings.recheck_seconds), remaining)
            if last.state == "COOLDOWN" and last.wait_seconds > 0:
                delay = min(delay, last.wait_seconds)
            self.sleep(max(0.001, delay))
            last = self.check()
        return last
