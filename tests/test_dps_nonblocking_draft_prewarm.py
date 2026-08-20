from __future__ import annotations

from threading import Event
from time import monotonic
from types import SimpleNamespace

from repositories.auto_post_event_repository import AutoPostEventRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.naver_auto_post_scheduler import NaverAutoPostScheduler


def _inquiry(database: Database, source_id: str, content: str) -> int:
    return InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "CUSTOMER_INQUIRY",
            "source_question_id": source_id,
            "inquiry_type": "CUSTOMER_INQUIRY",
            "content": content,
            "product_name": "삼성 TV",
            "order_id": "2026072912345678",
            "raw_json": {},
        }
    ).inquiry_id


def test_slow_dps_draft_does_not_block_unrelated_draft_prewarm(
    tmp_path,
) -> None:
    database = Database(tmp_path / "draft-prewarm.db")
    database.initialize()
    slow_id = _inquiry(database, "SLOW-DPS", "현재 주문은 언제 설치되나요?")
    general_id = _inquiry(database, "GENERAL", "패널 종류가 무엇인가요?")
    learning_id = _inquiry(database, "LEARNING", "제품 기능과 사용법을 알려주세요.")
    policy_id = _inquiry(database, "NO-ORDER", "일반 배송 정책이 궁금합니다.")
    events = AutoPostEventRepository(database)
    for inquiry_id, external_id in (
        (slow_id, "SLOW-DPS"),
        (general_id, "GENERAL"),
        (learning_id, "LEARNING"),
        (policy_id, "NO-ORDER"),
    ):
        events.create(
            inquiry_id=inquiry_id,
            store_code="OJE_PLUS",
            external_id=external_id,
            source_sync_id="SYNC",
            runtime_enabled=True,
        )

    slow_started = Event()
    release_slow = Event()
    ordinary_completed = {
        general_id: Event(), learning_id: Event(), policy_id: Event()
    }
    timestamps: dict[str, float] = {}
    started_at = monotonic()

    class Plans:
        def create(self, inquiry):
            return SimpleNamespace(
                requires_dps_lookup=int(inquiry["id"]) == slow_id
            )

    class Drafts:
        answer_service = SimpleNamespace(
            inquiries=InquiryRepository(database),
            plans=Plans(),
        )

        @staticmethod
        def ensure_for_inquiry(inquiry_id, **kwargs):
            if inquiry_id == slow_id:
                timestamps["A_started"] = monotonic() - started_at
                slow_started.set()
                # This models a lookup that may remain busy for up to 60s.
                assert release_slow.wait(timeout=60)
                timestamps["A_completed"] = monotonic() - started_at
            else:
                assert slow_started.wait(timeout=1)
                timestamps[str(inquiry_id)] = monotonic() - started_at
                ordinary_completed[inquiry_id].set()
            return SimpleNamespace(status="CREATED")

    scheduler = NaverAutoPostScheduler(database)
    scheduler._prewarm_pending_drafts(
        SimpleNamespace(drafts=Drafts()),
        exclude_inquiry_ids=set(),
    )
    try:
        assert slow_started.wait(timeout=1)
        assert all(event.wait(timeout=1) for event in ordinary_completed.values())
        assert not scheduler._draft_futures[slow_id].done()
        assert all(
            timestamps[str(inquiry_id)] >= timestamps["A_started"]
            for inquiry_id in ordinary_completed
        )
    finally:
        release_slow.set()
        scheduler._await_prewarmed_draft(slow_id)
        for inquiry_id in ordinary_completed:
            scheduler._await_prewarmed_draft(inquiry_id)
    assert all(
        timestamps[str(inquiry_id)] < timestamps["A_completed"]
        for inquiry_id in ordinary_completed
    )
    print({
        "A_slow_dps_started_s": round(timestamps["A_started"], 4),
        "B_general_completed_s": round(timestamps[str(general_id)], 4),
        "C_learning_completed_s": round(timestamps[str(learning_id)], 4),
        "D_no_order_completed_s": round(timestamps[str(policy_id)], 4),
        "A_released_completed_s": round(timestamps["A_completed"], 4),
    })
