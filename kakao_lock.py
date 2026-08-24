from __future__ import annotations

import os
import time
from pathlib import Path


SERVICE_ROOT = Path(__file__).resolve().parent
LOCK_FILE = SERVICE_ROOT / ".kakao_lock"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False

    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _read_lock_pid() -> int | None:
    try:
        text = LOCK_FILE.read_text(
            encoding="utf-8"
        ).strip()

        return int(text) if text.isdigit() else None

    except Exception:
        return None


def _is_stale_lock(stale_sec: int) -> bool:
    if not LOCK_FILE.exists():
        return False

    try:
        pid = _read_lock_pid()
        age = time.time() - LOCK_FILE.stat().st_mtime

        return (
            pid is None
            or not _pid_alive(pid)
            or age >= stale_sec
        )

    except Exception:
        return True


def acquire_lock(
    timeout: int = 300,
    check_interval: float = 3,
    stale_sec: int = 600,
) -> None:
    """
    카카오톡 사용 락을 획득한다.

    - 다른 프로세스가 사용 중이면 대기한다.
    - 락에 기록된 PID가 종료되었으면 오래된 락으로 보고 제거한다.
    - 락 파일이 stale_sec 이상 오래됐으면 제거한다.
    - 원자적 파일 생성을 사용해 동시 락 획득을 방지한다.
    """
    LOCK_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    started_at = time.time()

    while True:
        try:
            file_descriptor = os.open(
                str(LOCK_FILE),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )

            try:
                os.write(
                    file_descriptor,
                    str(os.getpid()).encode("utf-8"),
                )
            finally:
                os.close(file_descriptor)

            return

        except FileExistsError:
            if _is_stale_lock(stale_sec):
                try:
                    LOCK_FILE.unlink(missing_ok=True)
                except Exception:
                    pass

                continue

            if time.time() - started_at >= timeout:
                pid = _read_lock_pid()

                raise TimeoutError(
                    "카카오톡 사용 대기시간을 초과했습니다. "
                    f"현재 락 PID={pid}"
                )

            time.sleep(check_interval)


def release_lock() -> None:
    """
    현재 프로세스가 소유한 카카오톡 락만 해제한다.
    """
    try:
        pid = _read_lock_pid()

        if pid == os.getpid():
            LOCK_FILE.unlink(missing_ok=True)

    except Exception:
        pass