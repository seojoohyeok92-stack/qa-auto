from __future__ import annotations

from repositories.database import Database
from repositories.learning_repository import LearningRepository
from services.learning_service import LearningService
from services.similar_answer_service import SimilarAnswerService


def _database(tmp_path) -> Database:
    database = Database(tmp_path / "learning-market-applicability.db")
    database.initialize()
    return database


def _save_learning(
    repository: LearningRepository,
    *,
    source_key: str,
    origin_market: str | None,
    market_applicability: str | None,
    store_code: str | None = None,
) -> dict:
    metadata = {"human_verified": True, "learning_signal_type": "POSITIVE"}
    if origin_market is not None:
        metadata["origin_market"] = origin_market
    if market_applicability is not None:
        metadata["market_applicability"] = market_applicability
    return repository.upsert(
        {
            "source_key": source_key,
            "inquiry_id": None,
            "answer_draft_id": None,
            "approval_history_id": None,
            "learning_source": "APPROVED_EDITED",
            "question_original_masked": "monitor setup guide",
            "question_normalized": "monitor setup guide",
            # Applicability, not an individual store, controls new shared
            # historical Learning.  Legacy store-scoped Learning remains
            # supported by the existing repository predicate.
            "store_code": store_code,
            "inquiry_type": "PRODUCT_INQUIRY",
            "intent": "GENERAL",
            "product_name": "monitor",
            "model_code": "32DM501",
            "generation_mode": "TEST",
            "template_id": None,
            "processing_route": "TEST",
            "validator_result": "PASSED",
            "seller_answer": None,
            "gpt_draft": None,
            "edited_answer": "Use the monitor setup guide.",
            "final_answer": "Use the monitor setup guide.",
            "posted": True,
            "posted_at": None,
            "auto_posted": False,
            "rating": 5,
            "edit_ratio": 0.0,
            "quality_score": 1.0,
            "style_only": False,
            "version": 1,
            "style_features_json": {},
            "metadata_json": metadata,
            "active": True,
        }
    )


def test_market_applicability_filters_runtime_candidates_and_preserves_legacy_naver(tmp_path) -> None:
    repository = LearningRepository(_database(tmp_path))
    _save_learning(
        repository,
        source_key="coupang-only",
        origin_market="COUPANG",
        market_applicability="COUPANG_ONLY",
    )
    _save_learning(
        repository,
        source_key="naver-only",
        origin_market="NAVER",
        market_applicability="NAVER_ONLY",
    )
    _save_learning(
        repository,
        source_key="coupang-common",
        origin_market="COUPANG",
        market_applicability="COMMON",
    )
    _save_learning(
        repository,
        source_key="legacy-naver",
        origin_market=None,
        market_applicability=None,
        store_code="OJE_PLUS",
    )

    coupang = {
        row["source_key"]
        for row in repository.candidates(store_code="COUPANG_OJE_NS", limit=20)
    }
    naver = {
        row["source_key"]
        for row in repository.candidates(store_code="OJE_PLUS", limit=20)
    }

    assert {"coupang-only", "coupang-common"} <= coupang
    assert "naver-only" not in coupang
    assert "legacy-naver" not in coupang
    assert {"naver-only", "coupang-common", "legacy-naver"} <= naver
    assert "coupang-only" not in naver

    legacy_row = next(
        row for row in repository.candidates(store_code="OJE_PLUS")
        if row["source_key"] == "legacy-naver"
    )
    legacy_context = SimilarAnswerService(repository).context(
        "monitor setup guide",
        store_code="OJE_PLUS",
        inquiry_type="PRODUCT_INQUIRY",
        product_name="monitor",
        minimum_relevance=0.1,
        candidate_pool=[legacy_row],
    )
    legacy_reference = legacy_context["similar_approved_answers"][0]
    assert legacy_reference["origin_market"] == "NAVER"


def test_common_promotion_keeps_coupang_origin_and_reference_payload(tmp_path) -> None:
    database = _database(tmp_path)
    promoted = LearningService(database).capture_historical_promotion(
        case={
            "id": 7,
            "fingerprint": "coupang-common-case",
            "inquiry_id": None,
            "store_code": "COUPANG_OJE_NS",
            "inquiry_type": "PRODUCT_INQUIRY",
            "classification": "GENERAL",
            "product_id": None,
            "product_name": "monitor",
            "question": "monitor setup guide",
            "seller_answer": "Use the monitor setup guide.",
            "answer_updated_at": None,
            "quality_score": 0.8,
            "policy_risk": "NONE",
            "metadata_json": {
                "market": "COUPANG",
                "canonical_model": "32DM501",
            },
        },
        actor="tester",
        market_applicability="COMMON",
    )

    metadata = promoted["metadata_json"]
    assert promoted["store_code"] is None
    assert metadata["origin_market"] == "COUPANG"
    assert metadata["market_applicability"] == "COMMON"
    assert metadata["shared_cross_market_learning"] is True

    context = SimilarAnswerService(LearningRepository(database)).context(
        "monitor setup guide",
        store_code="OJE_PLUS",
        inquiry_type="PRODUCT_INQUIRY",
        product_name="monitor",
        minimum_relevance=0.1,
    )
    references = context["similar_approved_answers"]
    assert references
    assert references[0]["origin_market"] == "COUPANG"
