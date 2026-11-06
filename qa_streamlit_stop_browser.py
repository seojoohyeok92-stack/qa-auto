# -*- coding: utf-8 -*-
"""Safely stop only the scheduled Q&A Streamlit, NAVER sidecar, and Chrome.

Requires psutil. An unrelated application listening on 8504 is never killed.
An unrelated Chrome or Python process is never intentionally terminated.
A nonzero exit status indicates incomplete shutdown.
"""
from __future__ import annotations

from datetime import datetime
import json
import ntpath
from pathlib import Path
import subprocess
import sys
import time

import psutil

ROOT = Path(__file__).resolve().parent
PORT = 8504
STATE = ROOT / '.qa_streamlit_8504.json'
AUTO_STATE = ROOT / '.qa_streamlit_naver_scheduler.json'
CHROME_PROFILE = ROOT / '.qa_streamlit_chrome_profile_8504'
START_SCRIPT = ROOT / 'qa_streamlit_start_browser.py'
LOG = ROOT / 'qa_streamlit_stop.log'


def log(message):
    line = f'[{datetime.now().isoformat(timespec="seconds")}] {message}'
    print(line, flush=True)
    try:
        with LOG.open('a', encoding='utf-8') as fh:
            fh.write(line + '\n')
    except OSError:
        pass


def norm(path):
    return ntpath.normcase(ntpath.normpath(str(path).replace('/', '\\')))


def read_state(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def valid_pid(value):
    try:
        pid = int(value)
        return pid if pid > 0 else None
    except (ValueError, TypeError):
        return None


def proc_args(proc):
    return [str(arg) for arg in proc.cmdline()]


def is_server(proc):
    try:
        args = proc_args(proc)
        cwd = proc.cwd()
        tokens = [a.casefold().replace('\\', '/') for a in args]
        if not any('streamlit' in t for t in tokens) or 'run' not in tokens:
            return False
        for arg in args:
            if arg.startswith('-') or ntpath.basename(arg.replace('/', '\\')).casefold() != 'app.py':
                continue
            script_path = arg if ntpath.isabs(arg) else ntpath.join(cwd, arg)
            if norm(script_path) == norm(ROOT / 'app.py'):
                return True
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        pass
    return False


def server_owner(proc):
    for _ in range(6):
        if is_server(proc):
            return proc
        try:
            proc = proc.parent()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return None
        if proc is None:
            return None
    return None


def listeners():
    try:
        connections = psutil.net_connections(kind='tcp')
    except (psutil.AccessDenied, OSError) as exc:
        raise RuntimeError(f'Cannot inspect TCP {PORT}: {exc}') from exc
    result = {}
    for conn in connections:
        if conn.status != psutil.CONN_LISTEN or not conn.laddr or conn.laddr.port != PORT:
            continue
        if not conn.pid:
            raise RuntimeError(f'TCP {PORT} listener PID is unavailable; refusing unsafe shutdown')
        try:
            result[conn.pid] = psutil.Process(conn.pid)
        except psutil.NoSuchProcess:
            pass
    return list(result.values())


def alive(proc):
    try:
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def terminate(proc, label):
    if not alive(proc):
        return
    log(f'[STOP] {label} PID={proc.pid}')
    try:
        proc.terminate()
        try:
            proc.wait(timeout=4)
        except psutil.TimeoutExpired:
            log(f'[WARN] PID={proc.pid} graceful stop timed out; force-killing')
            proc.kill()
            proc.wait(timeout=4)
    except psutil.NoSuchProcess:
        pass
    except (psutil.AccessDenied, psutil.TimeoutExpired) as exc:
        raise RuntimeError(f'Cannot stop {label} PID={proc.pid}: {exc}') from exc


def stop_server():
    state = read_state(STATE)
    candidates = {}
    for listener in listeners():
        owner = server_owner(listener)
        if owner is None:
            raise RuntimeError(f'TCP {PORT} belongs to unverified process PID={listener.pid}; no termination')
        candidates[owner.pid] = owner
    saved_pid = valid_pid(state.get('pid'))
    if saved_pid:
        try:
            proc = psutil.Process(saved_pid)
            if is_server(proc):
                candidates[proc.pid] = proc
            else:
                log(f'[INFO] saved server PID={saved_pid} is stale or unrelated; skipped')
        except psutil.NoSuchProcess:
            pass
    if not candidates:
        log(f'[OK] TCP {PORT} already closed; no verified server found')
    for proc in candidates.values():
        terminate(proc, 'Q&A Streamlit server')
    # Some launch methods create a second server child; verify the TCP listener.
    for _ in range(20):
        if not listeners():
            break
        time.sleep(0.5)
    remaining = listeners()
    if remaining:
        raise RuntimeError(f'TCP {PORT} still listening: {[p.pid for p in remaining]}')
    if any(alive(p) for p in candidates.values()):
        raise RuntimeError('Verified Q&A server process still running')
    log(f'[OK] Q&A Streamlit stopped; TCP {PORT} closed')


def is_worker(proc):
    try:
        args = proc_args(proc)
        if '--naver-worker' not in args:
            return False
        # Worker is launched as: python -u <absolute start script> --naver-worker PID TOKEN
        return any(norm(arg) == norm(START_SCRIPT) for arg in args)
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return False


def workers():
    found = {}
    saved_pid = valid_pid(read_state(AUTO_STATE).get('worker_pid'))
    if saved_pid:
        try:
            proc = psutil.Process(saved_pid)
            if is_worker(proc):
                found[proc.pid] = proc
            else:
                log(f'[INFO] saved worker PID={saved_pid} is stale or unrelated; skipped')
        except psutil.NoSuchProcess:
            pass
    for proc in psutil.process_iter(['pid', 'cmdline']):
        if is_worker(proc):
            found[proc.pid] = proc
    return found


def stop_workers():
    targets = workers()
    for proc in targets.values():
        terminate(proc, 'Q&A NAVER worker')
    time.sleep(0.3)
    remaining = workers()
    if remaining:
        raise RuntimeError(f'Q&A NAVER worker still running: {list(remaining)}')
    log(f'[OK] Q&A NAVER workers stopped (found {len(targets)})')


def dedicated_chrome_roots():
    found = {}
    marker = norm(CHROME_PROFILE)
    for proc in psutil.process_iter(['name', 'cmdline']):
        try:
            if 'chrome' not in (proc.info['name'] or '').casefold():
                continue
            args = proc.info['cmdline'] or []
            if any(arg.casefold().startswith('--user-data-dir=') and
                   norm(arg.split('=', 1)[1].strip('"')) == marker for arg in args):
                found[proc.pid] = proc
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    return found


def close_browser():
    roots = dedicated_chrome_roots()
    targets = dict(roots)
    for root in roots.values():
        try:
            for child in root.children(recursive=True):
                if 'chrome' in child.name().casefold():
                    targets[child.pid] = child
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            pass
    # Stop descendants before the browser parent, but only verified Chrome descendants.
    for proc in reversed(list(targets.values())):
        terminate(proc, 'Q&A dedicated Chrome')
    time.sleep(0.3)
    remaining_roots = dedicated_chrome_roots()
    remaining_pids = [p.pid for p in targets.values() if alive(p)]
    if remaining_roots or remaining_pids:
        raise RuntimeError(f'Q&A Chrome still running: {sorted(set(remaining_pids + list(remaining_roots)))}')
    log(f'[OK] Q&A dedicated Chrome stopped (found {len(targets)}). Other Chrome untouched')


DPS_CHROME_PROFILE = Path(
    r"C:\Users\oje-server2\.oje-server2\scr\DPS_Daily_Inventory\chrome_profile"
)


def close_dps_browser():
    """DPS 전용 Chrome 프로필을 사용하는 프로세스만 종료."""
    marker = norm(DPS_CHROME_PROFILE)
    targets = {}

    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if (proc.info["name"] or "").casefold() != "chrome.exe":
                continue

            args = proc.info["cmdline"] or []

            for arg in args:
                if not arg.casefold().startswith("--user-data-dir="):
                    continue

                profile = norm(arg.split("=", 1)[1].strip('"'))

                if profile == marker:
                    targets[proc.pid] = proc
                    break

        except (
            psutil.NoSuchProcess,
            psutil.AccessDenied,
            OSError,
        ):
            continue

    if not targets:
        log("[OK] DPS Chrome already stopped")
        return

    for proc in targets.values():
        terminate(proc, "DPS dedicated Chrome")

    time.sleep(0.5)

    remaining = []

    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if (proc.info["name"] or "").casefold() != "chrome.exe":
                continue

            for arg in proc.info["cmdline"] or []:
                if arg.casefold().startswith("--user-data-dir="):
                    profile = norm(arg.split("=", 1)[1].strip('"'))

                    if profile == marker:
                        remaining.append(proc.pid)
                        break

        except (
            psutil.NoSuchProcess,
            psutil.AccessDenied,
            OSError,
        ):
            continue

    if remaining:
        raise RuntimeError(
            f"DPS Chrome still running: {remaining}"
        )

    log(
        f"[OK] DPS dedicated Chrome stopped "
        f"({len(targets)} processes). "
        "Other Chrome untouched"
    )


def cleanup():
    for path in (STATE, AUTO_STATE):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise RuntimeError(f'Failed to clean {path.name}: {exc}') from exc
    log('[OK] Q&A scheduling state files cleaned')


def main():
    log('=== Q&A STOP REQUEST ===')
    # Verify port ownership *before* stopping browser/worker.
    for listener in listeners():
        if server_owner(listener) is None:
            raise RuntimeError(f'TCP {PORT} owned by unrelated PID={listener.pid}; refusing shutdown')
    errors = []
    for name, step in (
        ('SERVER', stop_server),
        ('NAVER WORKER', stop_workers),
        ('CHROME', close_browser),
        ('DPS CHROME', close_dps_browser),
    ):
        try:
            step()
        except Exception as exc:
            errors.append(f'{name}: {exc}')
            log(f'[ERROR] {name}: {exc}')
    if not errors:
        try:
            cleanup()
        except Exception as exc:
            errors.append(str(exc))
            log(f'[ERROR] {exc}')
    if errors:
        log(f'[FAILED] Q&A shutdown incomplete: {len(errors)} issue(s)')
        return 1
    log('[SUCCESS] Q&A shutdown verified: server, NAVER worker, dedicated Chrome')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        log(f'[FAILED] {type(exc).__name__}: {exc}')
        sys.exit(1)
