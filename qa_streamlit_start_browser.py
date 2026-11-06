# -*- coding: utf-8 -*-
"""Start Q&A Streamlit + dedicated Chrome; scheduled starts also enable NAVER.

Only this launcher enables scheduled auto-post. The existing app.py is unchanged.
Requires psutil and the application's normal dependencies.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
PORT = 8504
URL = f'http://127.0.0.1:{PORT}'
STATE = ROOT / '.qa_streamlit_8504.json'
AUTO_STATE = ROOT / '.qa_streamlit_naver_scheduler.json'
LOG = ROOT / 'qa_streamlit_scheduler.log'
CHROME_PROFILE = ROOT / '.qa_streamlit_chrome_profile_8504'


def note(message):
    print(message, flush=True)
    with LOG.open('a', encoding='utf-8') as log:
        log.write(f'[{datetime.now().isoformat(timespec="seconds")}] {message}\n')


def read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return {}


def write_json(path, payload):
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def is_qna_process(pid):
    import psutil
    try:
        p = psutil.Process(int(pid))
        args = ' '.join(p.cmdline()).casefold().replace('/', '\\')
        folder = str(ROOT).casefold().replace('/', '\\')
        cwd = str(Path(p.cwd())).casefold().replace('/', '\\')
        return p if p.is_running() and 'streamlit' in args and 'app.py' in args and (
            cwd == folder or folder in args
        ) else None
    except (ValueError, psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return None


def listeners():
    """Return all TCP listeners, including Windows connections with PID 0.

    A PID of zero is not a valid server PID, but still indicates that the port
    is occupied. Never launch a second server while it is listening.
    """
    import psutil
    return {c.pid for c in psutil.net_connections(kind='tcp')
            if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == PORT}


def server_ready():
    try:
        with urlopen(f'{URL}/_stcore/health', timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


def find_chrome():
    candidates = [
        shutil.which('chrome.exe'),
        Path(os.environ.get('PROGRAMFILES', r'C:\Program Files')) / 'Google/Chrome/Application/chrome.exe',
        Path(os.environ.get('PROGRAMFILES(X86)', r'C:\Program Files (x86)')) / 'Google/Chrome/Application/chrome.exe',
        Path(os.environ.get('LOCALAPPDATA', '')) / 'Google/Chrome/Application/chrome.exe',
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise FileNotFoundError('Chrome not found; Q&A server may still be active.')


def open_qa_browser():
    import psutil
    marker = ('--user-data-dir=' + str(CHROME_PROFILE)).casefold()
    for p in psutil.process_iter(['name', 'cmdline']):
        try:
            if 'chrome' in (p.info['name'] or '').casefold() and marker in [
                str(a).casefold() for a in p.info['cmdline'] or []
            ]:
                note('Q&A dedicated Chrome already open.')
                return
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    CHROME_PROFILE.mkdir(parents=True, exist_ok=True)
    p = subprocess.Popen(
        [find_chrome(), f'--user-data-dir={CHROME_PROFILE}', f'--app={URL}',
         '--start-maximized', '--window-size=1600,950',
         '--no-first-run', '--no-default-browser-check'],
        cwd=str(ROOT), stdin=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    note(f'Q&A Chrome open requested: PID {p.pid}')


def db_post_state():
    """Read only; main() changes this after the first UI Streamlit run.

    Import the existing app database path without modifying its schema or app.
    """
    from repositories.database import Database
    db_path = Database().path
    try:
        with sqlite3.connect(f'file:{Path(db_path).as_posix()}?mode=ro', uri=True, timeout=3) as conn:
            row = conn.execute('SELECT updated_at FROM naver_auto_post_state WHERE id=1').fetchone()
            return row[0] if row else None
    except sqlite3.Error:
        return None


def wait_for_ui_start(baseline, process, timeout=90):
    """Wait for ensure_auto_post_scheduler() in app.main(), after startup OFF reset.

    It writes naver_auto_post_state.updated_at on initial browser UI run.
    """
    stop_at = time.monotonic() + timeout
    while time.monotonic() < stop_at:
        if process.poll() is not None:
            raise RuntimeError(f'Streamlit exited during UI initialization: {process.returncode}')
        current = db_post_state()
        if current and current != baseline:
            note(f'Q&A UI initialized; auto-post state updated ({current}).')
            return
        time.sleep(1)
    raise RuntimeError('Streamlit server started but UI startup was not confirmed; NAVER auto-post NOT enabled. Open the Q&A browser window in the logged-in Windows session.')


def worker(server_pid, token):
    """Keep the application's existing timer scheduler alive outside Streamlit.

    A transient launcher cannot host Python Timer threads: they die on exit.
    This sidecar exits when the Q&A server exits and does NOT re-enable if an
    operator switches NAVER off during this session.
    """
    import psutil
    from repositories.database import Database
    from repositories.auto_post_repository import AutoPostRepository
    from services.auto_post_runtime_service import AutoPostRuntimeService
    from services.naver_auto_post_scheduler import stop_all_auto_post_schedulers

    try:
        db = Database()
        service = AutoPostRuntimeService(db)
        result = service.enable_market('NAVER')
        status = str(result.get('status', 'UNKNOWN')).upper()
        write_json(AUTO_STATE, {'token': token, 'worker_pid': os.getpid(),
                                'server_pid': server_pid, 'status': status,
                                'started_at': datetime.now().isoformat(timespec='seconds'),
                                'blocking_reasons': result.get('blocking_reasons', [])})
        note(f'[QA_SCHEDULED_NAVER] start result: {status}')
        if status != 'RUNNING':
            return 2
        repository = AutoPostRepository(db)
        while True:
            if not is_qna_process(server_pid) or not server_ready():
                note('[QA_SCHEDULED_NAVER] server gone; worker exiting.')
                return 0
            # Respect a manual OFF. Do not silently turn posting back on.
            if not repository.platform_enabled('NAVER'):
                note('[QA_SCHEDULED_NAVER] manually disabled; worker exiting.')
                return 0
            time.sleep(2)
    except Exception as exc:
        note(f'[QA_SCHEDULED_NAVER] worker ERROR: {type(exc).__name__}: {exc}')
        write_json(AUTO_STATE, {'token': token, 'server_pid': server_pid,
                                'status': 'ERROR', 'error': f'{type(exc).__name__}: {exc}'})
        return 2
    finally:
        try:
            stop_all_auto_post_schedulers()
        except Exception:
            pass


def main():
    import psutil
    if not (ROOT / 'app.py').is_file():
        raise FileNotFoundError(f'Missing existing app.py: {ROOT}')
    # A state file records an old launcher PID, NOT server readiness. On Windows
    # that process can still exist after its TCP listener has disappeared, or a
    # PID may have been recycled. Never skip startup solely because it exists.
    prior = read_json(STATE)
    recorded_pid = prior.get('pid')
    healthy = server_ready()
    bound = listeners()
    if healthy or bound:
        # A scheduled START must never enable automatic posting on a session
        # that someone already started manually.
        note(f'Q&A already listening on TCP {PORT} (health={healthy}, listener PIDs={sorted(bound, key=str)}). '
             'NAVER auto-start skipped for existing session.')
        if healthy:
            open_qa_browser()
        return 0
    if recorded_pid:
        note(f'Ignoring previous PID {recorded_pid}: TCP {PORT} is not listening and health check failed.')
        try:
            STATE.unlink(missing_ok=True)
        except OSError as exc:
            note(f'[WARN] Could not remove stale PID record: {exc}')

    baseline = db_post_state()
    with LOG.open('a', encoding='utf-8') as log:
        log.write(f'\n[{datetime.now().isoformat(timespec="seconds")}] START REQUEST\n')
        log.flush()
        proc = subprocess.Popen(
            [sys.executable, '-m', 'streamlit', 'run', 'app.py',
             '--server.port', str(PORT), '--server.headless', 'true'],
            cwd=str(ROOT), stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    write_json(STATE, {'pid': proc.pid, 'port': PORT,
                       'started_at': datetime.now().isoformat(timespec='seconds'),
                       'chrome_profile': str(CHROME_PROFILE)})
    note(f'Streamlit launched: PID {proc.pid}')
    for _ in range(35):
        if server_ready():
            break
        if proc.poll() is not None:
            raise RuntimeError(f'Streamlit exited with {proc.returncode}. Check {LOG}')
        time.sleep(1)
    else:
        raise RuntimeError(f'Streamlit health check timeout. Check {LOG}')

    # Rendering the page runs app.main(), including its once-per-process
    # automatic OFF reset. Health endpoint alone does not execute app.main().
    open_qa_browser()
    wait_for_ui_start(baseline, proc)

    token = uuid.uuid4().hex
    try:
        AUTO_STATE.unlink(missing_ok=True)
    except OSError:
        pass
    with LOG.open('a', encoding='utf-8') as log:
        log.flush()
        sidecar = subprocess.Popen(
            [sys.executable, '-u', str(Path(__file__).resolve()), '--naver-worker',
             str(proc.pid), token],
            cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    note(f'[QA_SCHEDULED_NAVER] worker spawned: PID {sidecar.pid}')
    for _ in range(50):
        result = read_json(AUTO_STATE)
        if result.get('token') == token:
            status = result.get('status')
            if status != 'RUNNING':
                raise RuntimeError(f'NAVER auto-start blocked: {status}; details: {result}')
            note('Q&A running; NAVER auto-post enabled. COUPANG unchanged.')
            return 0
        if sidecar.poll() is not None:
            raise RuntimeError(f'NAVER worker exited: {sidecar.returncode}; check {LOG}')
        time.sleep(1)
    raise RuntimeError(f'NAVER worker timed out. Check {LOG}')


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--naver-worker':
        sys.exit(worker(int(sys.argv[2]), sys.argv[3]))
    if len(sys.argv) != 1:
        raise SystemExit('Unsupported arguments')
    main()
