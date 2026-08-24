# qna_auto/kakao_notify.py
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent
SCRIPTS_DIR = PROJECT_ROOT.parent

KAKAO_SERVICE_DIR = SCRIPTS_DIR / "common_service" / "kakao"
OUTBOX = KAKAO_SERVICE_DIR / "outbox_events.jsonl"

# 같은 문의의 카카오 알림 전송 여부를 기록하는 DB
NOTIFY_DB = PROJECT_ROOT / "data" / "kakao_notify_history.sqlite3"

# pending 상태에서 이 시간 이상 멈춘 경우 다시 시도
PENDING_TIMEOUT_MINUTES = 10

# 카카오톡 채팅방 기본 이름
KAKAO_QNA_RECIPIENT = "오제 네이버 자동답변 확인방"


def is_kakao_notify_enabled() -> bool:
    return os.getenv(
        "KAKAO_NOTIFY_ENABLED",
        "1",
    ).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _validate_kakao_service() -> None:
    if not KAKAO_SERVICE_DIR.exists():
        raise RuntimeError(
            f"카카오 공용 폴더가 존재하지 않습니다: "
            f"{KAKAO_SERVICE_DIR}"
        )

    dispatcher_path = KAKAO_SERVICE_DIR / "kakao_dispatcher.py"

    if not dispatcher_path.exists():
        raise RuntimeError(
            f"kakao_dispatcher.py를 찾지 못했습니다: "
            f"{dispatcher_path}"
        )


def _append_json_line(
    path: Path,
    data: dict[str, Any],
) -> None:
    """
    outbox_events.jsonl에 JSON 이벤트를 한 줄씩 추가한다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    line = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    try:
        with path.open("a", encoding="utf-8") as file:
            file.write(line + "\n")
            file.flush()
            os.fsync(file.fileno())

    except FileNotFoundError:
        # 디스패처가 outbox 파일을 이동한 순간과 겹친 경우 재생성
        with path.open("a", encoding="utf-8") as file:
            file.write(line + "\n")
            file.flush()
            os.fsync(file.fileno())


def _connect_notify_db() -> sqlite3.Connection:
    NOTIFY_DB.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    conn = sqlite3.connect(
        NOTIFY_DB,
        timeout=30,
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kakao_notify_history (
            notify_key TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            title TEXT,
            product TEXT,
            question TEXT,
            claimed_at TEXT NOT NULL,
            sent_at TEXT
        )
        """
    )

    conn.commit()
    return conn


def build_notify_key(
    *,
    inquiry_id: str = "",
    product: str = "",
    option_name: str = "",
    question: str = "",
) -> str:
    """
    동일 문의 여부를 판단할 고유 키를 만든다.

    1순위: 네이버 문의 고유 ID
    2순위: 상품명 + 옵션명 + 질문 내용 해시
    """
    normalized_inquiry_id = str(
        inquiry_id or ""
    ).strip()

    if normalized_inquiry_id:
        return f"naver_qna:{normalized_inquiry_id}"

    raw = "\n".join(
        [
            str(product or "").strip(),
            str(option_name or "").strip(),
            str(question or "").strip(),
        ]
    )

    digest = hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()

    return f"naver_qna_hash:{digest}"


def _claim_notification(
    *,
    notify_key: str,
    title: str,
    product: str,
    question: str,
) -> bool:
    """
    알림을 이번 실행에서 전송해도 되는지 확인하고
    pending 상태로 선점한다.

    반환값:
        True  -> 이번 실행에서 전송
        False -> 이미 전송됐거나 다른 프로세스가 처리 중
    """
    now = datetime.now()
    now_text = now.strftime("%Y-%m-%d %H:%M:%S")
    stale_before = now - timedelta(
        minutes=PENDING_TIMEOUT_MINUTES
    )

    conn = _connect_notify_db()

    try:
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute(
            """
            SELECT status, claimed_at
            FROM kakao_notify_history
            WHERE notify_key = ?
            """,
            (notify_key,),
        ).fetchone()

        if row is None:
            conn.execute(
                """
                INSERT INTO kakao_notify_history (
                    notify_key,
                    status,
                    title,
                    product,
                    question,
                    claimed_at,
                    sent_at
                )
                VALUES (?, 'pending', ?, ?, ?, ?, NULL)
                """,
                (
                    notify_key,
                    title,
                    product,
                    question,
                    now_text,
                ),
            )

            conn.commit()
            return True

        status, claimed_at_text = row

        if status == "sent":
            conn.rollback()
            return False

        try:
            claimed_at = datetime.strptime(
                claimed_at_text,
                "%Y-%m-%d %H:%M:%S",
            )
        except (TypeError, ValueError):
            claimed_at = stale_before - timedelta(
                seconds=1
            )

        # 다른 프로세스가 현재 처리 중
        if (
            status == "pending"
            and claimed_at > stale_before
        ):
            conn.rollback()
            return False

        # 오래 멈춘 pending은 이번 실행에서 재시도
        conn.execute(
            """
            UPDATE kakao_notify_history
            SET
                status = 'pending',
                title = ?,
                product = ?,
                question = ?,
                claimed_at = ?,
                sent_at = NULL
            WHERE notify_key = ?
            """,
            (
                title,
                product,
                question,
                now_text,
                notify_key,
            ),
        )

        conn.commit()
        return True

    finally:
        conn.close()


def _mark_notification_sent(
    notify_key: str,
) -> None:
    conn = _connect_notify_db()

    try:
        conn.execute(
            """
            UPDATE kakao_notify_history
            SET
                status = 'sent',
                sent_at = ?
            WHERE notify_key = ?
            """,
            (
                datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                notify_key,
            ),
        )

        conn.commit()

    finally:
        conn.close()


def _release_notification(
    notify_key: str,
) -> None:
    """
    카카오 대기열 등록에 실패한 경우 pending 기록을 제거하여
    다음 실행에서 다시 시도할 수 있게 한다.
    """
    conn = _connect_notify_db()

    try:
        conn.execute(
            """
            DELETE FROM kakao_notify_history
            WHERE notify_key = ?
              AND status = 'pending'
            """,
            (notify_key,),
        )

        conn.commit()

    finally:
        conn.close()


def enqueue_kakao_message(
    *,
    title: str,
    message: str,
    recipient: str | None = None,
) -> Path:
    _validate_kakao_service()

    target_recipient = (
        str(recipient or "").strip()
        or os.getenv("KAKAO_QNA_RECIPIENT", "").strip()
        or KAKAO_QNA_RECIPIENT
    )

    event = {
        "title": title,
        "message": message,
        "recipient": target_recipient,
        "source": "qa_auto",
        "created_at": datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
    }

    _append_json_line(
        OUTBOX,
        event,
    )

    return OUTBOX


def format_qna_message(
    *,
    product: str,
    option_name: str,
    question: str,
    answer: str,
    reason: str = "",
    action: str = "",
) -> str:
    lines = [
        f"상품명: {product or '-'}",
    ]

    if option_name:
        lines.append(
            f"옵션명: {option_name}"
        )

    lines.extend(
        [
            "",
            f"질문: {question or '-'}",
            "",
            f"답변: {answer or '-'}",
        ]
    )

    if (
        reason
        and action not in {"posted", "dry_run"}
    ):
        lines.extend(
            [
                "",
                f"판단 사유: {reason}",
            ]
        )

    return "\n".join(lines)


def notify_qna_safely(
    *,
    title: str,
    product: str,
    option_name: str,
    question: str,
    answer: str,
    reason: str = "",
    action: str = "",
    inquiry_id: str = "",
    notify_key: str = "",
) -> bool:
    """
    동일한 문의는 카카오톡으로 한 번만 알린다.

    inquiry_id:
        네이버 문의 고유 ID. 가능한 경우 반드시 전달 권장.

    notify_key:
        호출부에서 이미 만든 고유 알림 키가 있다면 전달.
        notify_key가 없으면 inquiry_id 또는 질문 내용으로 생성.
    """
    if not is_kakao_notify_enabled():
        return False

    resolved_notify_key = (
        str(notify_key or "").strip()
        or build_notify_key(
            inquiry_id=inquiry_id,
            product=product,
            option_name=option_name,
            question=question,
        )
    )

    claimed = False

    try:
        claimed = _claim_notification(
            notify_key=resolved_notify_key,
            title=title,
            product=product,
            question=question,
        )

        if not claimed:
            print(
                "[KAKAO] 중복 알림 생략: "
                f"{resolved_notify_key}"
            )
            return False

        message = format_qna_message(
            product=product,
            option_name=option_name,
            question=question,
            answer=answer,
            reason=reason,
            action=action,
        )

        outbox = enqueue_kakao_message(
            title=title,
            message=message,
        )

        # outbox 등록이 정상적으로 끝난 후에만 전송 완료 처리
        _mark_notification_sent(
            resolved_notify_key
        )

        print(
            f"[KAKAO] 알림 대기열 등록: {title}"
        )
        print(
            f"[KAKAO] notify_key: {resolved_notify_key}"
        )
        print(
            f"[KAKAO] outbox: {outbox}"
        )

        return True

    except Exception as exc:
        if claimed:
            try:
                _release_notification(
                    resolved_notify_key
                )
            except Exception as release_exc:
                print(
                    "[WARN] 카카오 알림 선점 해제 실패: "
                    f"{release_exc}"
                )

        print(
            f"[WARN] 카카오 알림 등록 실패: {exc}"
        )
        return False
