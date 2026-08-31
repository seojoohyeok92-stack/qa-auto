from __future__ import annotations

import json
from pathlib import Path

import kakao_notify


def test_notify_writes_common_dispatcher_event(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service_dir = tmp_path / "common_service" / "kakao"
    service_dir.mkdir(parents=True)
    (service_dir / "kakao_dispatcher.py").write_text(
        "# dispatcher marker\n",
        encoding="utf-8",
    )
    outbox = service_dir / "outbox_events.jsonl"
    monkeypatch.setenv("KAKAO_NOTIFY_ENABLED", "1")
    monkeypatch.setattr(kakao_notify, "KAKAO_SERVICE_DIR", service_dir)
    monkeypatch.setattr(kakao_notify, "OUTBOX", outbox)
    monkeypatch.setattr(
        kakao_notify,
        "NOTIFY_DB",
        tmp_path / "kakao_notify_history.sqlite3",
    )

    sent = kakao_notify.notify_qna_safely(
        title="[네이버 Q&A 답변 생성 완료]",
        product="테스트 상품",
        option_name="테스트 옵션",
        question="테스트 질문",
        answer="테스트 답변",
        action="generated",
        inquiry_id="Q-1",
        notify_key="answer_draft_created:1:1",
    )

    assert sent is True
    event = json.loads(outbox.read_text(encoding="utf-8").strip())
    assert event["title"] == "[네이버 Q&A 답변 생성 완료]"
    assert event["recipient"] == "오제 네이버 자동답변 확인방"
    assert "질문: 테스트 질문" in event["message"]
    assert "답변: -" in event["message"]
    assert "테스트 답변" not in event["message"]
    assert event["source"] == "qa_auto"
