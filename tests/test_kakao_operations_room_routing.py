"""Where a Q&A operations notification actually lands.

A production notification reached a chat room named "테스트".  Nobody watches
that room for operations, so an alert that goes there is an alert that was
lost -- worse than one that never left, because the outbox recorded it as
sent.

Two halves had to agree that a message may be addressed by default.  The
dispatcher gave any event without a ``recipient`` a hardcoded fallback room,
and the producer would rather borrow some room than none.  Neither guesses
now: no room, no send, and a line in the log saying so.

Nothing here touches KakaoTalk.  The outbox is a file in tmp_path and the
dispatcher is never launched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import kakao_notify

NAVER_ROOM = "오제 네이버 자동답변 확인방"
COUPANG_ROOM = "오제 쿠팡 자동답변 확인방"


@pytest.fixture
def outbox(tmp_path: Path, monkeypatch) -> Path:
    """A private outbox; the real dispatcher folder is never touched."""

    service_dir = tmp_path / "common_service" / "kakao"
    service_dir.mkdir(parents=True)
    (service_dir / "kakao_dispatcher.py").write_text("# marker\n", encoding="utf-8")
    path = service_dir / "outbox_events.jsonl"
    monkeypatch.setenv("KAKAO_NOTIFY_ENABLED", "1")
    monkeypatch.delenv("KAKAO_QNA_RECIPIENT", raising=False)
    monkeypatch.delenv("KAKAO_COUPANG_QNA_RECIPIENT", raising=False)
    monkeypatch.setattr(kakao_notify, "KAKAO_SERVICE_DIR", service_dir)
    monkeypatch.setattr(kakao_notify, "OUTBOX", path)
    monkeypatch.setattr(
        kakao_notify, "NOTIFY_DB", tmp_path / "kakao_notify_history.sqlite3"
    )
    return path


def _events(outbox: Path) -> list[dict]:
    if not outbox.exists():
        return []
    return [
        json.loads(line)
        for line in outbox.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _notify(**overrides) -> bool:
    payload = {
        "title": "[네이버 Q&A 자동등록 완료]",
        "store_code": "OJE_PLUS",
        "product": "삼성 스마트모니터 M5",
        "option_name": "32인치",
        "question": "스피커 있나요?",
        "answer": "네, 내장되어 있습니다.",
        "action": "posted",
        "inquiry_id": "Q-1",
        "notify_key": "posted:1",
    }
    payload.update(overrides)
    return kakao_notify.notify_qna_safely(**payload)


# --- A / B. production notifications go to the operations room -----------------

def test_a_successful_naver_registration_goes_to_the_operations_room(
    outbox,
) -> None:
    assert _notify() is True

    events = _events(outbox)
    assert len(events) == 1
    assert events[0]["recipient"] == NAVER_ROOM


def test_a_held_naver_inquiry_goes_to_the_same_operations_room(outbox) -> None:
    assert _notify(
        title="[Q&A 미등록 / 직원 확인 필요]",
        action="needs_review",
        notify_key="review-required:1",
        answer="-",
    ) is True

    events = _events(outbox)
    assert len(events) == 1
    assert events[0]["recipient"] == NAVER_ROOM
    assert "직원 확인 필요" in events[0]["title"]


def test_the_operations_room_can_be_configured_without_changing_code(
    outbox, monkeypatch,
) -> None:
    monkeypatch.setenv("KAKAO_QNA_RECIPIENT", "오제 네이버 자동답변 확인방2")

    assert _notify() is True
    assert _events(outbox)[0]["recipient"] == "오제 네이버 자동답변 확인방2"


# --- C. no room configured means no send ---------------------------------------

def test_a_market_with_no_room_sends_nothing_and_says_so(outbox, capsys) -> None:
    """Fail closed: no borrowed room, no default room, no event."""

    assert kakao_notify.recipient_for_market("GMARKET") == ""
    with pytest.raises(kakao_notify.KakaoRecipientUnavailable):
        kakao_notify.enqueue_kakao_message(
            title="[운영 알림]", message="본문", market="GMARKET",
        )

    assert _events(outbox) == []


def test_an_unroutable_notification_is_not_recorded_as_sent(
    outbox, monkeypatch, capsys,
) -> None:
    monkeypatch.setattr(kakao_notify, "recipient_for_market", lambda _market: "")

    assert _notify() is False
    assert _events(outbox) == []
    assert "카카오 알림 등록 실패" in capsys.readouterr().out
    # The claim was released, so a later run may retry rather than lose it.
    assert _notify() is False


# --- the dispatcher no longer addresses an unaddressed event -------------------

def test_the_dispatcher_has_no_default_room(outbox) -> None:
    """The one literal that could put operations traffic in "테스트"."""

    source = (
        Path(__file__).resolve().parents[1]
        / "naver_workflow" / "kakao_dispatcher.py"
    ).read_text(encoding="utf-8")

    assert "DEFAULT_RECIPIENT" not in source
    # Both drain loops skip an unaddressed event instead of addressing it.
    assert source.count("no recipient") == 2
    assert source.count('ev.get("recipient")') == 2


# --- D / G. the test path and Coupang ------------------------------------------

def test_an_explicit_recipient_still_addresses_that_room(outbox) -> None:
    """A deliberate test send names its room; production never does."""

    kakao_notify.enqueue_kakao_message(
        title="[테스트]", message="본문", recipient="테스트",
    )

    events = _events(outbox)
    assert len(events) == 1 and events[0]["recipient"] == "테스트"
    # Production callers do not pass one, so they cannot reach it by accident.
    assert _notify() is True
    assert _events(outbox)[1]["recipient"] == NAVER_ROOM


def test_coupang_still_sends_nothing(outbox) -> None:
    """G: the market gate is untouched; a room existing is not a permission."""

    assert kakao_notify.recipient_for_market("COUPANG") == COUPANG_ROOM
    assert _notify(store_code="COUPANG_OJE_NS", notify_key="coupang:1") is False
    assert _events(outbox) == []
