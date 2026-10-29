# qna_auto/kakao_notify.py
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from answer.hold_reasons import staff_headline, staff_reason_summary


PROJECT_ROOT = Path(__file__).resolve().parent
SCRIPTS_DIR = PROJECT_ROOT.parent

KAKAO_SERVICE_DIR = SCRIPTS_DIR / "common_service" / "kakao"
OUTBOX = KAKAO_SERVICE_DIR / "outbox_events.jsonl"

# 같은 문의의 카카오 알림 전송 여부를 기록하는 DB
NOTIFY_DB = PROJECT_ROOT / "data" / "kakao_notify_history.sqlite3"

# pending 상태에서 이 시간 이상 멈춘 경우 다시 시도
PENDING_TIMEOUT_MINUTES = 10

# 카카오톡 채팅방 기본 이름 #"테스트" #"오제 네이버 자동답변 확인방"
KAKAO_QNA_RECIPIENT = "오제 네이버 자동답변 확인방"
# One room per marketplace, never per seller account: both Coupang accounts
# report into the same room.  The Naver variable keeps its name and its
# meaning because Naver production is already reading it.
#
# A room being configured is still not the same as a market being notified.
# Nothing here sends anything; what may be notified at all is decided once, in
# ``services.market_policy`` (``KAKAO_MARKETS``).  Coupang is in that set now
# that a person can register a Coupang answer, and both Coupang seller
# accounts report into the one room above -- the room is chosen by market, not
# by account, so OJE_NS and OJE_PLUS never split into two chat rooms.
KAKAO_RECIPIENT_ENV_BY_MARKET = {
    "NAVER": "KAKAO_QNA_RECIPIENT",
    "COUPANG": "KAKAO_COUPANG_QNA_RECIPIENT",
}
KAKAO_RECIPIENT_DEFAULT_BY_MARKET = {
    "NAVER": KAKAO_QNA_RECIPIENT,
    "COUPANG": "오제 쿠팡 자동답변 확인방",
}


class KakaoRecipientUnavailable(RuntimeError):
    """No operations room is configured for this market, so nothing is sent."""


def recipient_for_market(market: object) -> str:
    """The chat room for one marketplace's Q&A notifications.

    ``""`` when this market has no room of its own.  It deliberately does not
    borrow another market's room, and there is no default: a notification with
    nowhere to go is not sent, and the caller says so in the log.  Falling back
    put operations messages somewhere nobody watches.
    """

    code = str(market or "NAVER").strip().upper() or "NAVER"
    env_name = KAKAO_RECIPIENT_ENV_BY_MARKET.get(code)
    configured = os.getenv(env_name, "").strip() if env_name else ""
    return configured or KAKAO_RECIPIENT_DEFAULT_BY_MARKET.get(code, "")


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
    market: str | None = None,
) -> Path:
    _validate_kakao_service()

    target_recipient = (
        str(recipient or "").strip()
        or recipient_for_market(market)
    )
    if not target_recipient:
        # Fail closed.  The dispatcher used to give an unaddressed event a
        # default room, which is how an operations notification could arrive
        # somewhere it was never meant to go.  Neither side guesses now.
        raise KakaoRecipientUnavailable(
            "카카오 운영 채팅방이 설정되지 않아 알림을 발송하지 않습니다: "
            f"market={str(market or 'NAVER').strip().upper() or 'NAVER'}"
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


def _market_display_name(market: object) -> str:
    """The marketplace name for the message body, defaulting to Naver.

    Imported lazily: ``kakao_notify`` is imported by the answer pipeline, and
    a module-level import back into services would close a cycle.
    """

    from services.market_policy import market_display_name

    return market_display_name(market) or "네이버"


def format_qna_message(
    *,
    market: str | None = None,
    product: str,
    option_name: str,
    question: str,
    answer: str,
    reason: str = "",
    action: str = "",
    hold_reason: str = "",
    hold_codes: tuple = (),
    generation_skipped: bool = False,
    store_label: str = "",
    inquiry_id: str = "",
    draft: str = "",
    failure_reason: str = "",
) -> str:
    """The message an operator reads.

    Only ``action='posted'`` is proof that the answer reached Naver. Drafts,
    holds, dry-runs and failed/API-error attempts therefore render ``답변: -``;
    a successful post keeps the registered answer text. Outbox routing and
    diagnostic reasons are deliberately independent of this display rule.
    """

    held = bool(hold_reason or hold_codes)
    failed = str(action or "").strip().lower() in {"post_failed", "post_unknown"}
    uncertain = str(action or "").strip().lower() == "post_unknown"

    # Which shop and which inquiry, on the two messages an operator has to act
    # on. A hold and a failure both end with "open the marketplace and do this
    # by hand", and the message did not say which store to open or which
    # inquiry to find: the id lived only in ``notify_key``, which is not sent,
    # and the seller account appeared nowhere. Both are omitted from the
    # success message, which is a receipt rather than a task, so that message
    # stays byte-for-byte what it has always been.
    lines: list[str] = []
    if held or failed:
        if store_label:
            lines.append(f"스토어: {store_label}")
        if inquiry_id:
            lines.append(f"문의 ID: {inquiry_id}")
    lines.append(f"상품명: {product or '-'}")

    if option_name:
        lines.append(
            f"옵션명: {option_name}"
        )

    lines.extend(
        [
            "",
            f"질문: {question or '-'}",
        ]
    )

    if held:
        # ``hold_reason`` is the dashboard's sentence for this same hold, and it
        # names the pipeline's parts: GPT①, Validator, DPS, payload. It is not
        # changed anywhere -- it stays in the draft metadata, the activity log
        # and the console -- but what reaches the chat room is the
        # operator-facing sentence for the same codes. One boundary, one
        # vocabulary: ``answer.hold_reasons.staff_headline``.
        lines.extend(
            [
                "",
                f"미등록 사유: {staff_headline(hold_codes, default=hold_reason)}",
            ]
        )
        # Internal reason codes are how the pipeline talks to itself. A staff
        # member reading this on their phone gets the same reasons as short
        # Korean phrases instead; the codes themselves are unchanged and stay
        # in the activity log for whoever needs to trace one.
        detail = staff_reason_summary(hold_codes)
        if detail:
            lines.append(f"세부 사유: {detail}")
        lines.extend(
            [
                "",
                "답변: -",
            ]
        )
        # The text waiting in the dashboard, labelled as the draft it is.
        #
        # ``답변`` above stays "-" because only a registered answer may appear
        # under that name -- that is the rule this function is built on and it
        # is unchanged. What an operator needs in the chat room is different:
        # whether there is anything to approve at all, and whether it is close
        # enough to send. So the draft is shown under its own name, where it
        # cannot be read as proof that anything was published.
        if draft:
            lines.extend(["", f"답변 초안(미등록): {draft}"])
        lines.extend(
            [
                "",
                f"답변 생성: {'생략됨' if generation_skipped else '완료'}",
                # Which marketplace the answer was not posted to.  A staff
                # member reading this on their phone needs to know which shop
                # to open; "등록: 안 됨" on its own does not say.
                f"{_market_display_name(market)} 등록: 안 됨",
                "처리: 답변 초안을 확인한 뒤 직접 등록해 주세요.",
            ]
        )
    elif failed:
        # A registration that was attempted and did not succeed. This is not
        # the hold above and must not read like it: the hold means the pipeline
        # decided not to send, while this means it sent and the marketplace
        # did not accept -- or, for ``post_unknown``, that whether it arrived
        # is not knowable here, which is why that case never says 실패.
        lines.extend(
            [
                "",
                f"등록 실패 사유: {failure_reason or '-'}"
                if not uncertain
                else f"확인 필요 사유: {failure_reason or '-'}",
                "",
                "답변: -",
                "",
                (
                    f"{_market_display_name(market)} 등록 결과: 확인 필요"
                    if uncertain
                    else f"{_market_display_name(market)} 등록: 실패"
                ),
                (
                    "처리: 등록 여부를 마켓에서 직접 확인한 뒤 조치해 주세요."
                    if uncertain
                    else "처리: 운영자 확인 후 재시도가 필요합니다."
                ),
            ]
        )
    else:
        lines.extend(
            [
                "",
                f"답변: {answer or '-'}" if action == "posted" else "답변: -",
            ]
        )

    # "판단 사유" is gone from the message, not from the record.
    #
    # It rendered ``AnswerResult.reason``, which is how the pipeline explains its
    # own routing to itself: RULE_ENGINE_MATCH, TEMPLATE_RENDER_FAILED,
    # CURRENT_ORDER_ACTION_REQUIRED, "적용 가능한 고정 템플릿이 없어 GPT 안전
    # 답변을 생성합니다". None of it is something an operator acts on, and a held
    # inquiry already carries 미등록 사유 and 세부 사유 above. The value itself is
    # unchanged in the draft, the activity log and the dashboard.
    _ = reason

    return "\n".join(lines)


def notify_qna_safely(
    *,
    title: str,
    market: str | None = None,
    store_code: str | None = None,
    product: str,
    option_name: str,
    question: str,
    answer: str,
    reason: str = "",
    action: str = "",
    inquiry_id: str = "",
    notify_key: str = "",
    hold_reason: str = "",
    hold_codes: tuple = (),
    generation_skipped: bool = False,
    draft: str = "",
    failure_reason: str = "",
    on_error: Any = None,
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

    # Which marketplace this inquiry belongs to, and whether production may
    # notify about it at all.
    #
    # This gate is here rather than in each caller because a Coupang inquiry
    # once reached a real chat room by accident: it entered the auto-post
    # queue, which was not scoped by market, generated a draft, was held for
    # review, and the hold notification went out reading "네이버 등록: 안 됨"
    # for a question nobody had asked on Naver.  The queue scoping was fixed
    # at its source, and this stayed: a market is notified because it is in
    # ``KAKAO_MARKETS``, never because some caller forgot to check.  A market
    # that is only collected and displayed still sends nothing.
    resolved_market = market
    if resolved_market is None and store_code is not None:
        from services.market_policy import market_of

        resolved_market = market_of(store_code)
    if resolved_market is not None:
        from services.market_policy import is_kakao_market_enabled

        if not is_kakao_market_enabled(resolved_market):
            print(
                "[KAKAO] 발송 대상 마켓이 아니어서 생략: "
                f"{resolved_market}"
            )
            return False
    market = resolved_market

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

        # The seller account this inquiry belongs to, resolved here rather
        # than at each call site: every caller already passes ``store_code``
        # for the room routing above, and one resolution means the dashboard
        # and the chat room name the same store the same way.
        store_label = ""
        if store_code:
            from services.market_policy import store_label as _store_label

            store_label = _store_label(store_code)
        message = format_qna_message(
            market=market,
            product=product,
            option_name=option_name,
            question=question,
            answer=answer,
            reason=reason,
            action=action,
            hold_reason=hold_reason,
            hold_codes=tuple(hold_codes or ()),
            generation_skipped=generation_skipped,
            store_label=store_label,
            inquiry_id=str(inquiry_id or ""),
            draft=draft,
            failure_reason=failure_reason,
        )

        outbox = enqueue_kakao_message(
            title=title,
            message=message,
            market=market,
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
        # The console line above is the only record this module can keep: it
        # has no database and deliberately no logger. A send that failed was
        # therefore invisible the moment the server restarted, while a *held*
        # inquiry it failed to announce stayed held with nothing saying the
        # operator had never been told. Callers that do have a log pass
        # ``on_error`` and record it; the return value stays a bool, so no
        # existing caller changes.
        if callable(on_error):
            try:
                on_error(exc)
            except Exception as hook_error:  # noqa: BLE001
                print(
                    "[WARN] 카카오 알림 실패 기록 실패: "
                    f"{hook_error}"
                )
        return False
