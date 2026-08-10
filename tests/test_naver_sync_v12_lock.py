from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from config import NaverSyncSettings, StoreConfig
from repositories.database import Database
from repositories.naver_sync_repository import NaverSyncRepository
from services.naver_inquiry_sync_service import NaverInquirySyncService


@pytest.fixture
def database(tmp_path: Path) -> Database:
    value = Database(tmp_path / "lock.db")
    value.initialize()
    return value


def _service(database: Database) -> NaverInquirySyncService:
    return NaverInquirySyncService(
        database,
        settings=NaverSyncSettings(enabled=True),
        token_provider=lambda **kwargs: "token",
        product_fetch=lambda **kwargs: {
            "contents": [],
            "totalPages": 1,
            "last": True,
        },
        customer_fetch=lambda **kwargs: {
            "content": [],
            "totalPages": 1,
            "last": True,
        },
    )


def _run(database: Database, *, sync_type: str):
    end = datetime(2026, 7, 31, tzinfo=UTC)
    return _service(database).sync_inquiries(
        stores=[StoreConfig("STORE", "스토어", "id", "secret")],
        inquiry_types=["PRODUCT_INQUIRY"],
        from_datetime=end - timedelta(days=1),
        to_datetime=end,
        sync_type=sync_type,
    )


@pytest.mark.parametrize(
    ("existing_type", "requested_type"),
    [("AUTO", "MANUAL"), ("MANUAL", "AUTO"), ("MANUAL", "MANUAL")],
)
def test_same_store_lock_is_skipped_for_auto_and_manual_combinations(
    database: Database, existing_type: str, requested_type: str
) -> None:
    runs = NaverSyncRepository(database)
    assert runs.acquire_lock(
        store_id="STORE",
        sync_id="existing",
        ttl_seconds=60,
        sync_type=existing_type,
    )
    result = _run(database, sync_type=requested_type)
    assert result.status == "SKIPPED"
    assert result.failed_count == 0
    assert result.skipped_count == 1
    assert result.error_code == "SYNC_IN_PROGRESS"


def test_lock_internal_error_is_a_real_failure(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_lock(**kwargs):
        raise OSError("lock storage unavailable")

    monkeypatch.setattr(
        NaverSyncRepository, "acquire_lock", lambda self, **kwargs: broken_lock(**kwargs)
    )
    result = _run(database, sync_type="MANUAL")
    assert result.status == "FAILED"
    assert result.failed_count == 1
    assert result.skipped_count == 0
    assert result.error_code == "LOCK_FAILED"


def test_expired_lock_is_reclaimed_and_owned_lock_is_released(
    database: Database,
) -> None:
    runs = NaverSyncRepository(database)
    assert runs.acquire_lock(
        store_id="STORE",
        sync_id="expired",
        ttl_seconds=60,
    )
    with database.transaction() as connection:
        connection.execute(
            "UPDATE naver_sync_locks SET expires_at=? WHERE store_id='STORE'",
            ("2000-01-01T00:00:00+00:00",),
        )
    result = _run(database, sync_type="MANUAL")
    assert result.status == "SUCCESS"
    assert runs.active_locks() == []
