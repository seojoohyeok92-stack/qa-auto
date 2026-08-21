from __future__ import annotations

from datetime import UTC, datetime

import pytest

from answer.gpt_pricing import ModelPriceKrw, estimate_cost_krw
from repositories.database import Database
from repositories.gpt_provider_run_repository import GptProviderRunRepository
from repositories.inquiry_repository import InquiryRepository


@pytest.fixture
def database(tmp_path) -> Database:
    database = Database(tmp_path / "runs.db")
    database.initialize()
    return database


@pytest.fixture
def inquiry_id(database: Database) -> int:
    return InquiryRepository(database).upsert_work_item(
        {
            "store_code": "STORE",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": "RUN-1",
            "content": "문의",
            "raw_json": {},
        }
    ).inquiry_id


def run_values(inquiry_id: int, correlation: str = "corr-1", **extra):
    now = datetime.now(UTC).isoformat()
    values = {
        "inquiry_id": inquiry_id,
        "correlation_id": correlation,
        "provider": "fake",
        "model": "fake-json-v1",
        "mode": "FAKE",
        "prompt_version": "prompt-v1",
        "policy_version": "governance-v1",
        "privacy_policy_version": "privacy-v1",
        "validator_policy_version": "validator-v1",
        "company_tone_version": "tone-v1",
        "started_at": now,
        "completed_at": now,
        "duration_ms": 12,
        "success": True,
        "input_size": 100,
        "output_size": 50,
        "privacy_removed_count": 2,
        "validator_passed": True,
        "fallback_used": False,
    }
    values.update(extra)
    return values


def test_migration_v5_creates_run_table(database: Database) -> None:
    assert database.migration_versions() == list(range(1, 28))
    with database.connection() as connection:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE name='gpt_provider_runs'"
        ).fetchone()
    assert table is not None
    assert database.initialize() == []


def test_run_repository_create_and_get(
    database: Database, inquiry_id: int
) -> None:
    repository = GptProviderRunRepository(database)
    created = repository.create_run(**run_values(inquiry_id))
    loaded = repository.get(created["id"])
    assert loaded["correlation_id"] == "corr-1"
    assert loaded["success"] is True
    assert loaded["validator_passed"] is True


def test_run_repository_keeps_history(
    database: Database, inquiry_id: int
) -> None:
    repository = GptProviderRunRepository(database)
    repository.create_run(**run_values(inquiry_id, "corr-1"))
    repository.create_run(**run_values(inquiry_id, "corr-2"))
    assert [
        row["correlation_id"]
        for row in repository.recent(inquiry_id=inquiry_id)
    ] == ["corr-2", "corr-1"]


def test_run_can_attach_draft(database: Database, inquiry_id: int) -> None:
    repository = GptProviderRunRepository(database)
    run = repository.create_run(**run_values(inquiry_id))
    with database.transaction() as connection:
        cursor = connection.execute(
            """
            INSERT INTO answer_drafts(inquiry_id, original_answer)
            VALUES (?, ?)
            """,
            (inquiry_id, "답변"),
        )
        draft_id = int(cursor.lastrowid)
    assert repository.attach_draft(run["id"], draft_id)
    assert repository.get(run["id"])["draft_id"] == draft_id


def test_error_message_is_masked(
    database: Database, inquiry_id: int
) -> None:
    repository = GptProviderRunRepository(database)
    run = repository.create_run(
        **run_values(
            inquiry_id,
            success=False,
            error_type="ERROR",
            error_message_masked=(
                "token=secret 010-1234-5678 user@example.com"
            ),
        )
    )
    stored = run["error_message_masked"]
    assert "token=secret" not in stored
    assert "token=<masked-secret>" in stored
    assert "010-1234-5678" not in stored
    assert "user@example.com" not in stored


def test_schema_has_no_prompt_or_response_body_columns(
    database: Database,
) -> None:
    with database.connection() as connection:
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(gpt_provider_runs)"
            )
        }
    assert "prompt" not in columns
    assert "prompt_text" not in columns
    assert "response" not in columns
    assert "response_text" not in columns


def test_count_and_cost_since(database: Database, inquiry_id: int) -> None:
    repository = GptProviderRunRepository(database)
    repository.create_run(
        **run_values(inquiry_id, estimated_cost_krw=12.5)
    )
    assert repository.count_since("2000-01-01", inquiry_id=inquiry_id) == 1
    assert repository.cost_since("2000-01-01") == 12.5


def test_dashboard_stats(database: Database, inquiry_id: int) -> None:
    repository = GptProviderRunRepository(database)
    repository.create_run(**run_values(inquiry_id, "ok"))
    repository.create_run(
        **run_values(
            inquiry_id,
            "failed",
            success=False,
            fallback_used=True,
            error_type="PRIVACY_BLOCKED",
        )
    )
    stats = repository.dashboard_stats()
    assert stats["requests"] == 2
    assert stats["failures"] == 1
    assert stats["fallbacks"] == 1
    assert stats["privacy_blocks"] == 1


def test_duplicate_correlation_id_is_rejected(
    database: Database, inquiry_id: int
) -> None:
    repository = GptProviderRunRepository(database)
    repository.create_run(**run_values(inquiry_id))
    with pytest.raises(Exception):
        repository.create_run(**run_values(inquiry_id))


def test_shadow_comparison_is_json_round_trip(
    database: Database, inquiry_id: int
) -> None:
    run = GptProviderRunRepository(database).create_run(
        **run_values(
            inquiry_id,
            shadow_comparison={"validator_passed": True, "rule_length": 10},
        )
    )
    assert run["shadow_comparison_json"]["rule_length"] == 10


def test_fake_model_cost_is_zero() -> None:
    assert estimate_cost_krw(
        "fake-json-v1", input_tokens=100, output_tokens=50
    ) == 0


def test_unknown_model_cost_is_none() -> None:
    assert estimate_cost_krw(
        "unknown", input_tokens=100, output_tokens=50
    ) is None


def test_missing_usage_cost_is_none() -> None:
    assert estimate_cost_krw(
        "fake-json-v1", input_tokens=None, output_tokens=50
    ) is None


def test_custom_krw_price_calculation() -> None:
    pricing = {"model": ModelPriceKrw(1_000, 2_000)}
    assert estimate_cost_krw(
        "model",
        input_tokens=1_000_000,
        output_tokens=500_000,
        pricing=pricing,
    ) == 2_000
