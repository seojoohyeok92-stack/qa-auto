from __future__ import annotations

from threading import Event
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
    events = AutoPostEventRepository(database)
    for inquiry_id, external_id in (
        (slow_id, "SLOW-DPS"),
        (general_id, "GENERAL"),
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
    general_completed = Event()

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
                slow_started.set()
                assert release_slow.wait(timeout=3)
            else:
                general_completed.set()
            return SimpleNamespace(status="CREATED")

    scheduler = NaverAutoPostScheduler(database)
    scheduler._prewarm_pending_drafts(
        SimpleNamespace(drafts=Drafts()),
        exclude_inquiry_ids=set(),
    )
    try:
        assert slow_started.wait(timeout=1)
        assert general_completed.wait(timeout=1)
        assert not scheduler._draft_futures[slow_id].done()
    finally:
        release_slow.set()
        scheduler._await_prewarmed_draft(slow_id)
        scheduler._await_prewarmed_draft(general_id)
