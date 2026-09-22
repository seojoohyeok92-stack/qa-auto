"""Production DPS session over CDP: status, Chrome start, and the lookup client.

Naver DPS reads go through ``dps.cdp_backend`` (Chrome DevTools Protocol +
DOM) against the operator's DPS Chrome on port 9222. This module is the thin
production seam around it:

  lookup_dps_order_production  the DpsEnrichmentService default client
  cdp_session_status           what the Dashboard / auto-post gate read
  ensure_cdp_chrome            start that Chrome once if it is not running

Login is always the operator's. Chrome owns the DPS session and its cookies;
nothing here reads, copies or replays them, and a login page is reported as
LOGIN_REQUIRED, never filled.

Which Chrome to start is learned, not configured: while the 9222 Chrome is
running, its own command line (the browser process that has
``--remote-debugging-port=9222``) names its executable and its dedicated
``--user-data-dir``. Those are recorded in ``data/dps_cdp_session.json``
(git-ignored runtime state) and are the only thing a later start uses. No
profile is ever created: without a recorded, existing profile directory
nothing is started, and a profile that is already open without the debugging
port is left alone (starting it again would only open a window in that
process). Nothing is ever closed or killed.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from dps.cdp_backend import CdpBrowser, CdpDpsReader, CdpError, lookup_dps_order_cdp

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
# The port the operations DPS Chrome listens on. Read at call time, so tests
# can point it at a closed port.
PRODUCTION_CDP_PORT = 9222
# Runtime state (git-ignored by data/*session*.json).
STATE_FILE = PROJECT_ROOT / "data" / "dps_cdp_session.json"
# A start request is not repeated within this window -- Streamlit reruns and a
# second process must not open a second Chrome while the first is coming up.
LAUNCH_GUARD_SECONDS = 90
_LOCK = threading.Lock()
_SPEC_RECORDED_THIS_PROCESS = False


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _browser(port: int | None = None) -> CdpBrowser:
    return CdpBrowser(port=int(port or PRODUCTION_CDP_PORT), timeout=3.0)


# ---------------------------------------------------------------------------
# Lookup (DpsEnrichmentService default client)
# ---------------------------------------------------------------------------


def lookup_dps_order_production(naver_order_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
    """``lookup_dps_order`` for production: the CDP reader on port 9222.

    Same keyword contract the enrichment service already passes. There is no
    fallback to the pywinauto agent: a CDP failure is returned as it is and
    takes the existing review/hold path.
    """

    from dps.keepalive_runtime import run_with_dps_lookup_priority

    reader = CdpDpsReader(_browser())
    return run_with_dps_lookup_priority(
        lambda: lookup_dps_order_cdp(naver_order_id, reader=reader, **kwargs)
    )


# ---------------------------------------------------------------------------
# Session status (Dashboard, auto-post DPS gate)
# ---------------------------------------------------------------------------

_DIAGNOSTIC = {
    "READY": "OK", "LOGIN_REQUIRED": "AUTH_ERROR", "CHROME_NOT_FOUND": "CONFIG_ERROR",
    "DPS_PAGE_NOT_FOUND": "CONFIG_ERROR", "CONNECTION_FAILED": "NETWORK_ERROR",
}
_MESSAGES = {
    "READY": "DPS 구매요청리스트 조회 가능",
    "LOGIN_REQUIRED": "DPS 로그인이 필요합니다. DPS Chrome에서 직접 로그인해 주세요.",
    "CHROME_NOT_FOUND": "DPS Chrome(CDP)이 실행 중이 아닙니다.",
    "DPS_PAGE_NOT_FOUND": "DPS Chrome에서 판매 > 온라인판매 > 구매요청리스트 화면을 열어 주세요.",
    "CONNECTION_FAILED": "DPS Chrome에 연결하지 못했습니다.",
}


def cdp_session_status(*, port: int | None = None, browser: Any | None = None) -> dict[str, Any]:
    """The DPS session as the CDP lookup itself would find it -- read only.

    Uses the lookup's own tab finder (``CdpDpsReader._purchase_page``), so
    READY means exactly "a lookup could start now". The vocabulary is the one
    the Dashboard and the auto-post gate already read (``session_status``).
    """

    target_port = int(port or PRODUCTION_CDP_PORT)
    browser = browser or _browser(target_port)
    base: dict[str, Any] = {
        "backend": "CDP", "port": target_port, "last_checked_at": _now(),
        "chrome_running": False, "dps_tab_found": False, "purchase_list_ready": False,
    }
    try:
        browser.version()
    except CdpError:
        session = "CHROME_NOT_FOUND"
    else:
        base["chrome_running"] = True
        try:
            reader = CdpDpsReader(browser)
            base["dps_tab_found"] = any(reader._allowed(str(p.get("url") or "")) for p in browser.pages())
            _, page, state = reader._purchase_page()
        except CdpError:
            session = "CONNECTION_FAILED"
        else:
            if page is not None:
                page.close()
                session = "READY"
                base["purchase_list_ready"] = True
            elif state.get("code") == "DPS_LOGIN_REQUIRED":
                session = "LOGIN_REQUIRED"
            else:
                session = "DPS_PAGE_NOT_FOUND"
    ready = session == "READY"
    from dps.keepalive_runtime import dps_keepalive_runtime_status

    return {
        **dps_keepalive_runtime_status(),
        **base, "ok": ready, "success": ready, "session_status": session,
        "login_status": "LOGGED_IN" if ready else session,
        "browser_connected": base["chrome_running"],
        "code": None if ready else session,
        "diagnostic_code": _DIAGNOSTIC[session],
        "message": _MESSAGES[session],
    }


# ---------------------------------------------------------------------------
# Chrome start
# ---------------------------------------------------------------------------


def _read_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _split_command_line(command_line: str) -> list[str]:
    """Windows argv rules (CommandLineToArgvW); plain split elsewhere."""

    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        argc = ctypes.c_int()
        shell32 = ctypes.windll.shell32
        shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
        argv = shell32.CommandLineToArgvW(command_line, ctypes.byref(argc))
        if not argv:
            return []
        try:
            return [argv[index] for index in range(argc.value)]
        finally:
            ctypes.windll.kernel32.LocalFree(argv)
    return command_line.split()


def chrome_browser_processes() -> list[dict[str, Any]]:
    """Running chrome.exe browser processes: executable and argv. Read only."""

    if os.name != "nt":
        return []
    script = ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
              "Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress")
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        raw = json.loads(completed.stdout or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    rows = raw if isinstance(raw, list) else [raw]
    processes = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("CommandLine"):
            continue
        argv = _split_command_line(str(row["CommandLine"]))
        if any(arg.startswith("--type=") for arg in argv):
            continue  # renderer/gpu/utility children, not the browser
        processes.append({"pid": row.get("ProcessId"), "executable": row.get("ExecutablePath"),
                          "argv": argv})
    return processes


def cdp_chrome_process_ids(*, port: int | None = None) -> set[int]:
    """Return only browser PIDs serving the production CDP port."""

    target_port = int(port or PRODUCTION_CDP_PORT)
    result: set[int] = set()
    for process in chrome_browser_processes():
        argv = list(process.get("argv") or [])
        if _flag(argv, "remote-debugging-port") != str(target_port):
            continue
        try:
            process_id = int(process.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if process_id > 0:
            result.add(process_id)
    return result


def _flag(argv: list[str], name: str) -> str | None:
    prefix = f"--{name}="
    for arg in argv:
        if arg.startswith(prefix):
            return arg[len(prefix):].strip().strip('"')
    return None


def launch_spec_from_processes(processes: list[dict[str, Any]], port: int) -> dict[str, Any] | None:
    """The executable + dedicated profile of the Chrome serving ``port``."""

    for process in processes:
        argv = list(process.get("argv") or [])
        if any(arg.startswith("--type=") for arg in argv):
            continue  # a child process carries the parent's flags; not the browser
        if _flag(argv, "remote-debugging-port") != str(port):
            continue
        user_data_dir = _flag(argv, "user-data-dir")
        executable = process.get("executable") or (argv[0] if argv else None)
        if not user_data_dir or not executable:
            return None  # a default profile is not ours to restart
        spec = {"executable": str(executable), "user_data_dir": user_data_dir, "port": int(port)}
        profile_directory = _flag(argv, "profile-directory")
        if profile_directory:
            spec["profile_directory"] = profile_directory
        return spec
    return None


def _default_launcher(argv: list[str]) -> Any:
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True)


def ensure_cdp_chrome(
    *, port: int | None = None, browser: Any | None = None,
    state_path: Path | None = None,
    process_lister: Callable[[], list[dict[str, Any]]] = chrome_browser_processes,
    launcher: Callable[[list[str]], Any] = _default_launcher,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Make sure the DPS Chrome is up on the CDP port; start it at most once.

      running on the port   reuse it; remember its executable and profile
      not running           start the remembered executable + profile with
                            --remote-debugging-port, unless no profile is
                            remembered, it no longer exists, it is already
                            open without the port, or a start was requested
                            moments ago
    """

    global _SPEC_RECORDED_THIS_PROCESS
    target_port = int(port or PRODUCTION_CDP_PORT)
    path = state_path or STATE_FILE
    browser = browser or _browser(target_port)
    with _LOCK:
        try:
            browser.version()
        except CdpError:
            running = False
        else:
            running = True
        state = _read_state(path)
        if running:
            if not _SPEC_RECORDED_THIS_PROCESS or not state.get("chrome"):
                spec = launch_spec_from_processes(process_lister(), target_port)
                if spec:
                    state.update(chrome=spec, recorded_at=_now())
                    _write_state(path, state)
                _SPEC_RECORDED_THIS_PROCESS = True
            return {"action": "REUSED", "port": target_port,
                    "profile_recorded": bool(state.get("chrome"))}
        spec = dict(state.get("chrome") or {})
        if not spec.get("executable") or not spec.get("user_data_dir"):
            return {"action": "NOT_LAUNCHED", "reason": "PROFILE_UNKNOWN", "port": target_port}
        if not Path(spec["executable"]).is_file():
            return {"action": "NOT_LAUNCHED", "reason": "CHROME_EXECUTABLE_MISSING", "port": target_port}
        if not Path(spec["user_data_dir"]).is_dir():
            return {"action": "NOT_LAUNCHED", "reason": "PROFILE_PATH_MISSING", "port": target_port}
        last = float(state.get("last_launch_epoch") or 0)
        if clock() - last < LAUNCH_GUARD_SECONDS:
            return {"action": "NOT_LAUNCHED", "reason": "LAUNCH_ALREADY_REQUESTED", "port": target_port}
        wanted = os.path.normcase(os.path.normpath(spec["user_data_dir"]))
        for process in process_lister():
            other = _flag(list(process.get("argv") or []), "user-data-dir")
            if other and os.path.normcase(os.path.normpath(other)) == wanted:
                return {"action": "NOT_LAUNCHED", "reason": "PROFILE_IN_USE_WITHOUT_CDP",
                        "port": target_port}
        argv = [spec["executable"], f"--remote-debugging-port={target_port}",
                "--remote-debugging-address=127.0.0.1",
                f"--user-data-dir={spec['user_data_dir']}"]
        if spec.get("profile_directory"):
            argv.append(f"--profile-directory={spec['profile_directory']}")
        state.update(last_launch_epoch=clock(), last_launch_at=_now())
        _write_state(path, state)
        try:
            process = launcher(argv)
        except OSError as error:
            LOGGER.warning("DPS Chrome 시작 실패: %s", error.__class__.__name__)
            return {"action": "NOT_LAUNCHED", "reason": "LAUNCH_FAILED", "port": target_port}
        return {"action": "LAUNCHED", "port": target_port,
                "pid": getattr(process, "pid", None)}


def ensure_cdp_chrome_on_start() -> dict[str, Any]:
    """App start hook: only when DPS lookups are on, only on Windows."""

    from services.dps_lookup_policy import DpsSettings

    if os.name != "nt" or not DpsSettings.from_environment().automatic_lookup_enabled:
        return {"action": "SKIPPED"}
    try:
        return ensure_cdp_chrome()
    except Exception as error:  # noqa: BLE001 - never block the app
        LOGGER.warning("DPS Chrome 확인 실패: %s", error.__class__.__name__)
        return {"action": "SKIPPED", "reason": error.__class__.__name__}
