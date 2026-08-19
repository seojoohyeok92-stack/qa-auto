from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from repositories.database import Database
from repositories.learning_repository import LearningRepository
from services.learning_validity_service import (
    is_learning_usable,
    validity_status,
)


def _example(source_key: str, answer: str = "삼성 감사제 안내") -> dict:
    return {
        "source_key": source_key,
        "learning_source": "APPROVED_UNEDITED",
        "question_original_masked": "85인치 주문했는데 언제 오나요?",
        "question_normalized": "85인치 주문 언제",
        "final_answer": answer,
        "posted": False,
        "auto_posted": False,
        "rating": 5,
        "edit_ratio": 0.0,
        "quality_score": 1.0,
        "style_only": False,
        "version": 1,
        "style_features_json": {},
        "metadata_json": {"learning_signal_type": "POSITIVE"},
        "active": True,
        "usage_count": 0,
    }


@pytest.fixture
def repository(tmp_path) -> LearningRepository:
    database = Database(tmp_path / "validity.db")
    database.initialize()
    return LearningRepository(database)


def test_migration_is_idempotent_and_existing_learning_defaults_permanent(
    repository: LearningRepository,
) -> None:
    row = repository.upsert(_example("legacy"))
    assert row["validity_type"] == "PERMANENT"
    assert row["validity_active"] is True
    assert row["validity_status"] == "ACTIVE"
    assert 24 in repository.database.migration_versions()
    assert repository.database.initialize() == []


def test_temporary_learning_is_usable_only_inside_kst_window() -> None:
    now = datetime(2026, 8, 19, 3, 0, tzinfo=UTC)
    base = {
        "active": True,
        "validity_type": "TEMPORARY",
        "validity_active": True,
        "valid_from": now - timedelta(days=1),
        "valid_until": now + timedelta(days=1),
    }
    assert validity_status(base, now=now) == "ACTIVE"
    assert is_learning_usable(base, now=now)
    assert validity_status({**base, "valid_from": now + timedelta(minutes=1)}, now=now) == "SCHEDULED"
    assert not is_learning_usable({**base, "valid_from": now + timedelta(minutes=1)}, now=now)
    assert validity_status({**base, "valid_until": now - timedelta(minutes=1)}, now=now) == "EXPIRED"
    assert not is_learning_usable({**base, "valid_until": now - timedelta(minutes=1)}, now=now)
    assert validity_status({**base, "validity_active": False}, now=now) == "DISABLED"


def test_expired_and_manually_disabled_samsung_event_never_reaches_candidates(
    repository: LearningRepository,
) -> None:
    permanent = repository.upsert(_example("permanent", "영구 설치 안내"))
    active = repository.upsert(_example("event-active"))
    expired = repository.upsert(_example("event-expired"))
    disabled = repository.upsert(_example("event-disabled"))
    repository.update_validity(
        active["id"], validity_type="TEMPORARY", event_name="삼성 감사제",
        valid_from="2020-01-01", valid_until="2099-12-31",
    )
    repository.update_validity(
        expired["id"], validity_type="TEMPORARY", event_name="삼성 감사제",
        valid_from="2020-01-01", valid_until="2020-12-31",
    )
    repository.update_validity(
        disabled["id"], validity_type="TEMPORARY", event_name="삼성 감사제",
        valid_from="2020-01-01", valid_until="2099-12-31",
        validity_active=False,
    )
    candidates = repository.candidates(store_code=None)
    assert {row["source_key"] for row in candidates} == {
        permanent["source_key"], active["source_key"]
    }
    assert "삼성 감사제" not in " ".join(
        row["final_answer"] for row in candidates if row["source_key"] != "event-active"
    )


def test_validity_update_preserves_learning_identity_and_is_optimistic(
    repository: LearningRepository,
) -> None:
    original = repository.upsert(_example("identity"))
    updated = repository.update_validity(
        original["id"],
        validity_type="TEMPORARY",
        event_name="삼성 감사제",
        valid_from="2026-08-01",
        valid_until="2026-08-31",
        validity_note="행사 물량 증가",
        expected_updated_at=original["updated_at"],
    )
    assert updated["id"] == original["id"]
    assert updated["source_key"] == original["source_key"]
    assert updated["learning_source"] == original["learning_source"]
    with pytest.raises(RuntimeError, match="다른 사용자"):
        repository.update_validity(
            original["id"], validity_type="PERMANENT",
            expected_updated_at="2000-01-01T00:00:00Z",
        )


def test_temporary_learning_requires_complete_validity_condition(
    repository: LearningRepository,
) -> None:
    row = repository.upsert(_example("invalid"))
    with pytest.raises(ValueError):
        repository.update_validity(
            row["id"], validity_type="TEMPORARY", event_name="삼성 감사제",
            valid_from="2026-08-01", valid_until=None,
        )
    with pytest.raises(ValueError):
        repository.update_validity(
            row["id"], validity_type="TEMPORARY", event_name="삼성 감사제",
            valid_from="2026-09-01", valid_until="2026-08-01",
        )
