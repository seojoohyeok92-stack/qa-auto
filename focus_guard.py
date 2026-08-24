# common/focus_guard.py
import os, time, atexit
from pathlib import Path
try:
    import psutil
except Exception:
    psutil = None

SERVICE_ROOT = Path(__file__).resolve().parent
BUSY_FILE = SERVICE_ROOT / ".kakao_busy"

def kakao_busy_on():
    BUSY_FILE.parent.mkdir(parents=True, exist_ok=True)
    BUSY_FILE.write_text(f"{os.getpid()}", encoding="utf-8")
    atexit.register(kakao_busy_off)

def kakao_busy_ping():
    try:
        os.utime(BUSY_FILE, None)
    except Exception:
        pass

def kakao_busy_off():
    try:
        # 내가 만든 busy만 지우고 싶으면 여기서 pid 비교해도 됨
        BUSY_FILE.unlink(missing_ok=True)
    except Exception:
        pass

def is_kakao_busy(ttl: int = 20) -> bool:
    """busy 파일이 있고, 최근 갱신(ttl초) 내면 busy로 간주"""
    if not BUSY_FILE.exists():
        return False
    try:
        age = time.time() - BUSY_FILE.stat().st_mtime
        if age > ttl:
            # 스테일 방지: 너무 오래되면 정리
            BUSY_FILE.unlink(missing_ok=True)
            return False
    except Exception:
        return False
    return True

def wait_kakao_idle(max_wait: int = 120, poll: float = 0.2):
    """카톡 전송 중이면 끝날 때까지 대기"""
    start = time.time()
    while is_kakao_busy():
        time.sleep(poll)
        if time.time() - start > max_wait:
            break