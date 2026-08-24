import os, sys, traceback, logging
from pathlib import Path
from datetime import datetime

SERVICE_ROOT = Path(__file__).resolve().parent

LOG_DIR = SERVICE_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

RUN_LOG = LOG_DIR / "kakao_dispatcher_run.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(RUN_LOG, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)

def _fatal_dump(e: Exception):
    try:
        with open(RUN_LOG, "a", encoding="utf-8", errors="ignore") as f:
            f.write("\n=== FATAL ===\n")
            f.write(traceback.format_exc())
            f.write("\n")
    except Exception:
        pass

logging.info("dispatcher main start pid=%s", os.getpid())

# --- kakao_dispatcher.py (싱글톤/배치전송/하트비트/대기 - 옵션B: 빠른모드) ---
from pathlib import Path
import sys, os, time, json, atexit
from datetime import datetime

from focus_guard import kakao_busy_on, kakao_busy_off, kakao_busy_ping

# ───────── 설정(환경변수로 조절) ─────────
GRACE_BEFORE_OPEN_KAKAO = float(os.getenv("KAKAO_GRACE_BEFORE", "1.8"))
PER_SEND_DELAY          = float(os.getenv("KAKAO_PER_SEND_DELAY", "0.9"))
LINGER_AFTER_SEND       = float(os.getenv("KAKAO_LINGER_SEC", "7.0"))
REUSE_KAKAO             = os.getenv("KAKAO_REUSE", "1") == "1"
NO_RESET                = os.getenv("KAKAO_NO_RESET", "0") == "1"
STAY_OPEN               = os.getenv("KAKAO_STAY_OPEN", "0") == "1"
FORCE_MINIMIZE          = os.getenv("KAKAO_FORCE_MINIMIZE", "0") == "1"
ALWAYS_CLOSE            = os.getenv("KAKAO_ALWAYS_CLOSE", "1") == "1"   # 끝에 무조건 종료(기본 ON)
ALWAYS_OPEN             = os.getenv("KAKAO_ALWAYS_OPEN", "0") == "1"    # 1=항상 방 열기, 0=빠른모드(옵션B)

# ───────── 경로/파일 ─────────
SERVICE_ROOT = Path(__file__).resolve().parent

LOG = SERVICE_ROOT / "kakao_dispatcher.log"
PID_FILE = SERVICE_ROOT / ".dispatcher.pid"
LOCK_FILE = SERVICE_ROOT / ".dispatcher.lock"
HEARTBEAT_FILE = SERVICE_ROOT / ".dispatcher.heartbeat"
OUTBOX = SERVICE_ROOT / "outbox_events.jsonl"
SETTINGS_XLSX = SERVICE_ROOT / "초기설정(경로설정).xlsx"

DEFAULT_RECIPIENT = "테스트"

# (선택) psutil
try:
    import psutil
except Exception:
    psutil = None

# ───────── 로깅/하트비트 ─────────
def dlog(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.open("a", encoding="utf-8").write(f"[{ts}] {msg}\n")

# dlog 아래에 추가
def dlog_exc(prefix: str):
    try:
        etype, evalue, tb = sys.exc_info()
        if tb is None:
            dlog(prefix + "\n(no traceback)")
            return
        dlog(prefix + "\n" + "".join(traceback.format_exception(etype, evalue, tb)))
    except Exception:
        pass

def _beat():
    try:
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.write_text(str(int(time.time())), encoding="utf-8")
    except Exception:
        pass

# ───────── 싱글톤 ─────────
def _acquire_singleton():
    if LOCK_FILE.exists() or PID_FILE.exists():
        try:
            pid = None
            if PID_FILE.exists():
                t = (PID_FILE.read_text(encoding="utf-8") or "").strip()
                if t.isdigit():
                    pid = int(t)
            if pid and psutil and psutil.pid_exists(pid):
                try:
                    process = psutil.Process(pid)
                    command = " ".join(process.cmdline()).lower()

                    current_dispatcher = str(
                        Path(__file__).resolve()
                    ).lower()

                    if current_dispatcher in command:
                        dlog(
                            f"[singleton] another dispatcher running "
                            f"(pid={pid}) → exit"
                        )
                        print("[dispatcher] already running, exit")
                        sys.exit(0)

                    dlog(
                        f"[singleton] stale pid points to another process "
                        f"(pid={pid}) → cleanup"
                    )

                    for path in (LOCK_FILE, PID_FILE):
                        try:
                            path.unlink(missing_ok=True)
                        except Exception:
                            pass

                except Exception:
                    # PID 확인이 불가능하면 오래된 상태 파일로 간주
                    for path in (LOCK_FILE, PID_FILE):
                        try:
                            path.unlink(missing_ok=True)
                        except Exception:
                            pass
            for p in (LOCK_FILE, PID_FILE):
                try: p.unlink(missing_ok=True)
                except Exception: pass
        except Exception:
            pass

    try:
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_RDWR)
    except FileExistsError:
        dlog("[singleton] lock exists → exit")
        print("[dispatcher] lock exists, exit")
        sys.exit(0)

    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")

    def _release():
        try: os.close(fd)
        except Exception: pass
        try: LOCK_FILE.unlink(missing_ok=True)
        except Exception: pass
        try: PID_FILE.unlink(missing_ok=True)
        except Exception: pass
    atexit.register(_release)

_acquire_singleton()

# ───────── 외부 유틸 ─────────
from kakao_lock import acquire_lock, release_lock
from send_kakao_message import (
    open_kakao,
    open_chat,
    send_message,
    send_file,
    reset_to_original_state,
    get_kakao_path,
)

# ───────── 내부 유틸 ─────────
def _read_lines(p: Path) -> list[str]:
    try:
        txt = p.read_text(encoding="utf-8")
        return [ln for ln in (txt or "").splitlines() if ln.strip()]
    except Exception as e:
        dlog(f"read failed: {e}")
        return []

def _drain_atomic(src: Path) -> list[str]:
    work = src.with_suffix(".work")
    try:
        dlog(f"[drain] os.replace: {src} -> {work}")
        os.replace(src, work)
    except FileNotFoundError:
        dlog("[drain] outbox not found (no events)")
        return []
    except Exception:
        dlog_exc("[drain] os.replace failed")
        return []
    try:
        lines = _read_lines(work)
        dlog(f"[drain] read {len(lines)} lines from work")
        return lines
    finally:
        try:
            work.unlink(missing_ok=True)
        except Exception:
            dlog_exc("[drain] work unlink failed")

# ───────── 메인: OUTBOX 처리 ─────────
def _send_body_to_room(kakao_path: str, body: str, recipient: str, *, last_room_ref: dict):
    if ALWAYS_OPEN or (last_room_ref.get("name") != recipient):
        dlog(f"[chat] open_chat('{recipient}')")
        try:
            open_chat(recipient)
            dlog(f"[chat] open_chat OK '{recipient}'")
        except Exception:
            dlog_exc(f"[chat] open_chat FAIL '{recipient}'")
            # open_chat 실패면 send는 의미 없으니 재시도 루틴으로 넘김
            raise
        time.sleep(2)
        last_room_ref["name"] = recipient

    dlog(f"[send] send_message len={len(body)}")
    try:
        send_message(body)
        dlog("[send] send_message OK")
        return True
    except Exception:
        dlog_exc("[send] send_message FAIL (will relaunch & retry)")
        try:
            dlog("[kakao] relaunch open_kakao() for retry")
            open_kakao(kakao_path)
            time.sleep(2)
            dlog(f"[chat] retry open_chat('{recipient}')")
            open_chat(recipient)
            time.sleep(2)
            last_room_ref["name"] = recipient
            dlog("[send] retry send_message()")
            send_message(body)
            dlog("[send] retry OK")
            return True
        except Exception:
            dlog_exc("[send] retry FAIL (give up)")
            return False

def drain_outbox_once() -> int:
    # outbox 자체가 없으면 할 일 없음
    if not OUTBOX.exists():
        return 0

    kakao_busy_on()

    # ✅ 1) 먼저 락부터 잡는다 (락 실패 시 outbox를 건드리지 않음)
    try:
        acquire_lock(timeout=300)
    except Exception:
        dlog_exc("[lock] acquire_lock failed")
        kakao_busy_off()
        return 0  # ✅ raise 하지 말고 다음 루프에서 재시도

    total_sent = 0
    try:
        time.sleep(GRACE_BEFORE_OPEN_KAKAO)

        # ✅ 2) 락을 잡은 후에 outbox를 드레인한다 (유실 방지)
        if not OUTBOX.exists():
            return 0

        lines = _drain_atomic(OUTBOX)
        if not lines:
            return 0

        dlog(f"picked {len(lines)} events")

        # 3) 카카오톡 경로
        try:
            kakao_path = get_kakao_path(str(SETTINGS_XLSX))
            dlog(f"[path] kakao_path='{kakao_path}' exists={os.path.exists(kakao_path) if kakao_path else None}")
        except Exception:
            dlog_exc("[path] get_kakao_path failed")
            return 0

        dlog(f"open kakao (reuse={REUSE_KAKAO}): {kakao_path}")

        # 4) 카카오톡 실행/포커싱
        dlog("[kakao] calling open_kakao()")
        try:
            open_kakao(kakao_path)
            dlog("[kakao] open_kakao() returned")
        except Exception:
            dlog_exc("[kakao] open_kakao failed")
            return 0

        time.sleep(0.6)

        last_room = {"name": None}

        # 5) 배치 전송
        for i, line in enumerate(lines, 1):
            kakao_busy_ping()
            try:
                ev = json.loads(line)
            except Exception as e:
                dlog(f"json parse failed: {e}")
                continue

            title = (
                ev.get("title")
                or "[오제 챗봇 알림]"
            )
            msg = ev.get("message") or ""
            recipient = (
                ev.get("recipient")
                or DEFAULT_RECIPIENT
            )
            file_path = (
                ev.get("file_path")
                or ""
            )

            body = f"{title}\n{msg}"

            dlog(
                f"send {i}/{len(lines)} "
                f"to '{recipient}' "
                f"len={len(body)}"
            )

            ok = _send_body_to_room(
                kakao_path,
                body,
                recipient,
                last_room_ref=last_room,
            )

            if ok:
                total_sent += 1

                if file_path:
                    try:
                        dlog(
                            f"[file] send start: "
                            f"{file_path}"
                        )

                        send_file(file_path)

                        dlog(
                            f"[file] send OK: "
                            f"{file_path}"
                        )

                    except Exception:
                        dlog_exc(
                            f"[file] send FAIL: "
                            f"{file_path}"
                        )
            kakao_busy_ping()
            time.sleep(PER_SEND_DELAY)

        # 6) LINGER: 추가 이벤트 처리
        deadline = time.time() + LINGER_AFTER_SEND
        while time.time() < deadline:
            kakao_busy_ping()
            if OUTBOX.exists():
                extra = _drain_atomic(OUTBOX)
                if extra:
                    dlog(f"got {len(extra)} more events during linger")
                    for line in extra:
                        try:
                            ev = json.loads(line)
                        except Exception as e:
                            dlog(f"json parse failed: {e}")
                            continue

                        title = (
                            ev.get("title")
                            or "[오제 챗봇 알림]"
                        )
                        msg = ev.get("message") or ""
                        recipient = (
                            ev.get("recipient")
                            or DEFAULT_RECIPIENT
                        )
                        file_path = (
                            ev.get("file_path")
                            or ""
                        )

                        body = f"{title}\n{msg}"

                        ok = _send_body_to_room(
                            kakao_path,
                            body,
                            recipient,
                            last_room_ref=last_room,
                        )

                        if ok:
                            total_sent += 1

                            if file_path:
                                try:
                                    dlog(
                                        "[file] send start: "
                                        f"{file_path}"
                                    )

                                    send_file(file_path)

                                    dlog(
                                        "[file] send OK: "
                                        f"{file_path}"
                                    )

                                except Exception:
                                    dlog_exc(
                                        "[file] send FAIL: "
                                        f"{file_path}"
                                    )

                    deadline = time.time() + LINGER_AFTER_SEND
            time.sleep(0.3)

    finally:
        # reset은 락을 잡은 상태에서만 실행되는 게 안전하니 여기 유지
        try:
            if ALWAYS_CLOSE:
                reset_to_original_state(minimize_kakao=False)
                dlog("reset: always close (one-shot)")
            else:
                if NO_RESET:
                    dlog("reset skipped: NO_RESET=1")
                elif STAY_OPEN:
                    dlog("reset skipped: STAY_OPEN=1 (leave kakao open)")
                elif REUSE_KAKAO:
                    if FORCE_MINIMIZE:
                        try:
                            reset_to_original_state(minimize_kakao=True)
                            dlog("reset: minimized kakao (reuse mode)")
                        except TypeError:
                            dlog("reset: legacy signature → skip minimize")
                    else:
                        dlog("reset: reuse mode, leave window as-is")
                else:
                    try:
                        reset_to_original_state(minimize_kakao=True)
                        dlog("reset: minimized kakao")
                    except TypeError:
                        dlog("reset: legacy signature → skip minimize")
        except Exception:
            dlog_exc("reset guard failed (traceback)")

        kakao_busy_off()
        try:
            release_lock()
        except Exception:
            dlog_exc("[lock] release_lock failed")

    return total_sent

# ───────────────── 루프 ─────────────────
def run_forever(poll_interval=2):
    print("[디스패처] 카카오톡 디스패처 시작")
    dlog("dispatcher started")
    _beat()
    while True:
        try:
            processed = drain_outbox_once()
        except Exception:
            dlog_exc("drain error (traceback)")
            processed = 0
        _beat()
        if processed == 0:
            time.sleep(poll_interval)

if __name__ == "__main__":
    try:
        try:
            run_forever()
        except KeyboardInterrupt:
            dlog("dispatcher stopped by KeyboardInterrupt")
            print("\n[dispatcher] stopped")
    except Exception as e:
        logging.exception("DISPATCHER CRASH")
        _fatal_dump(e)
        raise