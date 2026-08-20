from __future__ import annotations

from datetime import UTC, datetime, timedelta

from repositories.database import Database
from repositories.dps_repository import DpsRepository
from repositories.inquiry_repository import InquiryRepository


def inquiry(database: Database) -> int:
    return InquiryRepository(database).upsert_work_item(
        {
            "store_code": "STORE",
            "source_type": "CUSTOMER_INQUIRY",
            "source_question_id": "DPS-REPO",
            "content": "배송은 언제 오나요?",
        }
    ).inquiry_id


def test_migration_v2_is_reentrant_and_creates_lookup_table(tmp_path) -> None:
    database = Database(tmp_path / "migration.db")
    assert database.initialize() == list(range(1, 27))
    assert database.initialize() == []
    assert database.migration_versions() == list(range(1, 27))
    with database.connection() as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = ?",
            ("dps_lookup_results",),
        ).fetchone()
    assert table is not None


def test_repository_crud_and_latest_queries(tmp_path) -> None:
    database = Database(tmp_path / "dps.db")
    database.initialize()
    inquiry_id = inquiry(database)
    repository = DpsRepository(database)
    row = repository.create_lookup_result(
        inquiry_id=inquiry_id,
        order_id="ORDER-1",
        lookup_status="SUCCESS",
        raw_result={"success": True},
        normalized_result={"lookup_status": "SUCCESS"},
        queried_at="2026-07-29T10:00:00+00:00",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    assert repository.get(row["id"])["order_id"] == "ORDER-1"
    assert repository.get_latest_by_order_id("ORDER-1")["id"] == row["id"]
    assert repository.get_latest_by_inquiry_id(inquiry_id)["id"] == row["id"]
    assert repository.get_latest_success_by_order_id("ORDER-1")["id"] == row["id"]


def test_valid_success_cache_query(tmp_path) -> None:
    database = Database(tmp_path / "cache.db")
    database.initialize()
    inquiry_id = inquiry(database)
    expires = (datetime.now(UTC) + timedelta(minutes=30)).isoformat()
    repository = DpsRepository(database)
    repository.create_lookup_result(
        inquiry_id=inquiry_id,
        order_id="ORDER-1",
        lookup_status="SUCCESS",
        raw_result={},
        normalized_result={"lookup_status": "SUCCESS"},
        expires_at=expires,
    )
    assert repository.get_latest_success_by_order_id(
        "ORDER-1", valid_only=True
    ) is not None


def test_failure_does_not_replace_latest_success_query(tmp_path) -> None:
    database = Database(tmp_path / "history.db")
    database.initialize()
    inquiry_id = inquiry(database)
    repository = DpsRepository(database)
    success = repository.create_lookup_result(
        inquiry_id=inquiry_id,
        order_id="ORDER-1",
        lookup_status="SUCCESS",
        raw_result={},
        normalized_result={"lookup_status": "SUCCESS"},
    )
    failure = repository.mark_or_store_failure(
        inquiry_id=inquiry_id,
        order_id="ORDER-1",
        lookup_status="TIMEOUT",
        error_code="AGENT_READ_TIMEOUT",
        error_message="시간 초과",
    )
    assert repository.get_latest_by_order_id("ORDER-1")["id"] == failure["id"]
    assert repository.get_latest_success_by_order_id("ORDER-1")["id"] == success["id"]
    assert len(repository.list_history_by_inquiry_id(inquiry_id)) == 2


def test_inquiry_delete_cascades_lookup_history(tmp_path) -> None:
    database = Database(tmp_path / "cascade.db")
    database.initialize()
    inquiry_id = inquiry(database)
    repository = DpsRepository(database)
    repository.create_lookup_result(
        inquiry_id=inquiry_id,
        order_id="ORDER-1",
        lookup_status="NOT_FOUND",
        raw_result={},
        normalized_result={"lookup_status": "NOT_FOUND"},
    )
    with database.transaction() as connection:
        connection.execute("DELETE FROM inquiries WHERE id = ?", (inquiry_id,))
    assert repository.list_history_by_inquiry_id(inquiry_id) == []
