"""Bulk promotion of the stored Historical Candidate backlog.

The backlog is 1337 rows that are all ``active=0``, and the single-case gate
requires a case to be active before it will promote.  The danger this suite
exists for is the halfway state that falls out of that: a case made active so
it can be promoted, whose promotion is then refused, left behind as a reviewed
answer nobody reviewed.
"""

from __future__ import annotations

import pytest

from repositories.database import Database
from repositories.historical_case_repository import HistoricalCaseRepository
from repositories.learning_repository import LearningRepository, is_market_applicable
from services.historical_case_service import (
    CHATBOT_EXCLUSION_REASON,
    HistoricalCaseService,
)

COUPANG_SOURCE = "COUPANG_ONLINE_HISTORY"
CHATBOT_ANSWER = (
    "안녕하세요. ⚙ 오제 챗봇입니다.\n"
    "삼성 S32DM501 모델은 VESA 100x100 규격을 지원합니다."
)
GOOD_ANSWER = (
    "해당 제품은 RF 안테나 단자가 없어 직접 연결은 불가하며 "
    "셋톱박스를 HDMI로 연결하여 사용하셔야 합니다."
)


def _database(tmp_path) -> Database:
    database = Database(tmp_path / "bulk-promotion.db")
    database.initialize()
    return database


def _seed_case(
    database: Database,
    *,
    external_id: str,
    answer: str = GOOD_ANSWER,
    quality: float = 0.80,
    policy_risk: str = "NONE",
    decision: str = "KEEP",
    canonical_model: str = "32DM501",
    store_code: str = "COUPANG_OJE_NS",
) -> dict:
    """One stored Coupang candidate, shaped the way the backfill writes them."""

    service = HistoricalCaseService(database)
    case = service.prepare_case(
        {
            "title": "Coupang 상품문의",
            "content": f"{external_id} 문의 내용입니다. 유선 잭으로 TV 시청이 되나요?",
            "seller_answer": answer,
            "external_inquiry_id": external_id,
            "source_type": "COUPANG_ONLINE_INQUIRY",
            "store_code": store_code,
            "source_answered": True,
            "historical_metadata": {
                "candidate_only": True,
                "market": "COUPANG",
                "origin_market": "COUPANG",
                "market_applicability": "COUPANG_ONLY",
                "shared_cross_market_learning": False,
                "account_code": "OJE_NS",
                "canonical_model": canonical_model,
                "model_code": canonical_model,
                "historical_candidate_decision": decision,
            },
        },
        source_reference=f"COUPANG_ONLINE_API:OJE_NS:{external_id}",
    )
    # The backfill's own quality scoring is not what this suite is about; the
    # gate values are set directly so each case exercises one branch.
    case["quality_score"] = quality
    case["confidence"] = quality
    case["policy_risk"] = policy_risk
    stored, _ = HistoricalCaseRepository(database).upsert(case)
    assert stored["active"] is False
    assert stored["source"] == COUPANG_SOURCE
    return stored


def _row(database: Database, case_id: int) -> dict:
    return HistoricalCaseRepository(database).get(int(case_id)) or {}


def _promoted_learning(database: Database, case_id: int) -> dict:
    learning_id = _row(database, case_id)["promoted_learning_id"]
    assert learning_id is not None
    return LearningRepository(database).get(int(learning_id))


def _assert_untouched_candidate(database: Database, case_id: int) -> None:
    """No promotion, and nothing left active behind it."""

    row = _row(database, case_id)
    assert row["active"] is False
    assert row["promoted_learning_id"] is None


# --- chatbot answers --------------------------------------------------------

def test_chatbot_answer_is_excluded_and_never_reaches_learning(tmp_path) -> None:
    database = _database(tmp_path)
    case = _seed_case(database, external_id="bot-1", answer=CHATBOT_ANSWER)
    service = HistoricalCaseService(database)

    summary = service.bulk_promote_candidates(actor="tester", apply=True)

    assert summary["chatbot_excluded"] == 1
    assert summary["promoted"] == 0
    assert summary["promotion_attempted"] == 0
    _assert_untouched_candidate(database, case["id"])
    row = _row(database, case["id"])
    assert row["metadata_json"]["learning_exclusion_reason"] == CHATBOT_EXCLUSION_REASON
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM learning_examples").fetchone()[0] == 0
    # The stored answer itself is preserved, exclusion is a label on top of it.
    assert row["seller_answer"] == CHATBOT_ANSWER


def test_chatbot_marker_is_read_from_the_answer_not_the_question(tmp_path) -> None:
    database = _database(tmp_path)
    case = _seed_case(database, external_id="bot-2")
    with database.transaction() as connection:
        connection.execute(
            "UPDATE historical_cases SET question=? WHERE id=?",
            ("챗봇으로 문의하면 되나요?", int(case["id"])),
        )
    summary = HistoricalCaseService(database).bulk_promote_candidates(
        actor="tester", apply=True
    )
    assert summary["chatbot_excluded"] == 0
    assert summary["promoted"] == 1


# --- the ordinary promotion path -------------------------------------------

def test_clean_candidate_is_promoted_through_the_existing_path(tmp_path) -> None:
    database = _database(tmp_path)
    case = _seed_case(database, external_id="ok-1")

    summary = HistoricalCaseService(database).bulk_promote_candidates(
        actor="tester", apply=True
    )

    assert summary["promotion_attempted"] == 1
    assert summary["promoted"] == 1
    assert summary["failed"] == 0
    row = _row(database, case["id"])
    assert row["promoted_learning_id"] is not None
    learning = LearningRepository(database).get(int(row["promoted_learning_id"]))
    assert learning["active"] is True
    assert learning["generation_mode"] == "HISTORICAL_ADMIN_PROMOTION"
    assert learning["metadata_json"]["source_origin"] == "HISTORICAL_PROMOTED"
    assert learning["metadata_json"]["historical_case_id"] == int(case["id"])


def test_promoted_learning_keeps_coupang_market_and_model(tmp_path) -> None:
    database = _database(tmp_path)
    case = _seed_case(database, external_id="market-1", canonical_model="32FM500")

    HistoricalCaseService(database).bulk_promote_candidates(actor="tester", apply=True)

    learning = _promoted_learning(database, case["id"])
    metadata = learning["metadata_json"]
    assert metadata["origin_market"] == "COUPANG"
    assert metadata["market_applicability"] == "COUPANG_ONLY"
    assert metadata["shared_cross_market_learning"] is False
    assert metadata["canonical_model"] == "32FM500"
    assert learning["model_code"] == "32FM500"
    # Account-independent: the Coupang scope is carried by metadata, not store.
    assert learning["store_code"] is None


@pytest.mark.parametrize(
    "market,expected",
    [("COUPANG", True), ("NAVER", False)],
    ids=["coupang-inquiry-may-use-it", "naver-inquiry-may-not"],
)
def test_promoted_coupang_learning_is_market_isolated(tmp_path, market, expected) -> None:
    database = _database(tmp_path)
    case = _seed_case(database, external_id="iso-1")
    HistoricalCaseService(database).bulk_promote_candidates(actor="tester", apply=True)

    learning = _promoted_learning(database, case["id"])
    assert is_market_applicable(learning["metadata_json"], market) is expected


@pytest.mark.parametrize(
    "decision", ["KEEP", "MANUAL_REVIEW", None],
    ids=["keep", "manual-review", "legacy-null"],
)
def test_candidate_decision_is_not_a_bulk_promotion_filter(tmp_path, decision) -> None:
    database = _database(tmp_path)
    _seed_case(database, external_id=f"decision-{decision}", decision=decision)

    summary = HistoricalCaseService(database).bulk_promote_candidates(
        actor="tester", apply=True
    )
    assert summary["promoted"] == 1


# --- the existing gate, unchanged ------------------------------------------

@pytest.mark.parametrize(
    "quality,policy_risk,reason",
    [
        pytest.param(0.40, "NONE", "QUALITY_SCORE", id="below-quality-floor"),
        pytest.param(0.80, "HIGH", "POLICY_RISK", id="policy-risk-high"),
        pytest.param(0.80, "BLOCKED", "POLICY_RISK", id="policy-risk-blocked"),
    ],
)
def test_gated_candidates_are_left_exactly_as_found(
    tmp_path, quality, policy_risk, reason
) -> None:
    database = _database(tmp_path)
    case = _seed_case(
        database, external_id="gate-1", quality=quality, policy_risk=policy_risk
    )

    summary = HistoricalCaseService(database).bulk_promote_candidates(
        actor="tester", apply=True
    )

    assert summary["quality_or_policy_blocked"] == 1
    assert summary["blocked_reasons"][reason] == 1
    assert summary["promotion_attempted"] == 0
    _assert_untouched_candidate(database, case["id"])
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM learning_examples").fetchone()[0] == 0


def test_medium_policy_risk_still_promotes(tmp_path) -> None:
    """MEDIUM is 319 of the stored rows and the gate never blocked it."""

    database = _database(tmp_path)
    _seed_case(database, external_id="medium-1", policy_risk="MEDIUM")
    summary = HistoricalCaseService(database).bulk_promote_candidates(
        actor="tester", apply=True
    )
    assert summary["promoted"] == 1


# --- the halfway state this suite exists for -------------------------------

def test_failed_promotion_never_leaves_a_case_active(tmp_path, monkeypatch) -> None:
    """A case made active for promotion must not survive the failure."""

    database = _database(tmp_path)
    case = _seed_case(database, external_id="boom-1")
    service = HistoricalCaseService(database)

    def _explode(*_args, **_kwargs):
        raise RuntimeError("Learning 저장 실패")

    monkeypatch.setattr(service, "promote", _explode)
    summary = service.bulk_promote_candidates(actor="tester", apply=True)

    assert summary["failed"] == 1
    assert summary["promoted"] == 0
    assert summary["blocked_reasons"]["OTHER_GATE"] == 1
    _assert_untouched_candidate(database, case["id"])
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM learning_examples").fetchone()[0] == 0


def test_no_case_is_left_active_without_a_promotion(tmp_path) -> None:
    """The whole-run invariant, over a mixed backlog."""

    database = _database(tmp_path)
    _seed_case(database, external_id="mix-ok")
    _seed_case(database, external_id="mix-bot", answer=CHATBOT_ANSWER)
    _seed_case(database, external_id="mix-low", quality=0.40)
    _seed_case(database, external_id="mix-risk", policy_risk="BLOCKED")

    HistoricalCaseService(database).bulk_promote_candidates(actor="tester", apply=True)

    with database.connection() as connection:
        dangling = connection.execute(
            "SELECT COUNT(*) FROM historical_cases "
            "WHERE active=1 AND promoted_learning_id IS NULL"
        ).fetchone()[0]
    assert dangling == 0


# --- re-running ------------------------------------------------------------

def test_bulk_promotion_is_idempotent(tmp_path) -> None:
    database = _database(tmp_path)
    _seed_case(database, external_id="again-1")
    _seed_case(database, external_id="again-bot", answer=CHATBOT_ANSWER)
    service = HistoricalCaseService(database)

    first = service.bulk_promote_candidates(actor="tester", apply=True)
    second = service.bulk_promote_candidates(actor="tester", apply=True)

    assert first["promoted"] == 1
    assert second["promoted"] == 0
    assert second["already_promoted"] == 1
    assert second["chatbot_excluded"] == 1
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM learning_examples").fetchone()[0] == 1


def test_already_promoted_case_creates_no_second_learning(tmp_path) -> None:
    database = _database(tmp_path)
    case = _seed_case(database, external_id="dup-1")
    service = HistoricalCaseService(database)
    service.bulk_promote_candidates(actor="tester", apply=True)
    learning_id = _row(database, case["id"])["promoted_learning_id"]

    service.bulk_promote_candidates(actor="tester", apply=True)

    assert _row(database, case["id"])["promoted_learning_id"] == learning_id
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM learning_examples").fetchone()[0] == 1


def test_dry_run_counts_without_writing_anything(tmp_path) -> None:
    database = _database(tmp_path)
    case = _seed_case(database, external_id="dry-1")
    bot = _seed_case(database, external_id="dry-bot", answer=CHATBOT_ANSWER)

    summary = HistoricalCaseService(database).bulk_promote_candidates(actor="tester")

    assert summary["applied"] is False
    assert summary["promotion_attempted"] == 1
    assert summary["promoted"] == 0
    assert summary["chatbot_excluded"] == 1
    _assert_untouched_candidate(database, case["id"])
    _assert_untouched_candidate(database, bot["id"])
    assert "learning_exclusion_reason" not in (_row(database, bot["id"])["metadata_json"])
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM learning_examples").fetchone()[0] == 0


def test_source_filter_limits_the_backlog(tmp_path) -> None:
    database = _database(tmp_path)
    _seed_case(database, external_id="src-1")
    summary = HistoricalCaseService(database).bulk_promote_candidates(
        actor="tester", source="NAVER_HISTORY", apply=True
    )
    assert summary["total_candidates"] == 0
    assert summary["promoted"] == 0
