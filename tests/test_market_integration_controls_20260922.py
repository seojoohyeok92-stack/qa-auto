from __future__ import annotations

from dataclasses import dataclass

from answer.answer_provenance import AnswerProvenance
from answer.learning_signal import SignalKind
from config import COUPANG_OJE_NS, COUPANG_OJE_PLUS
from repositories.auto_post_event_repository import AutoPostEventRepository
from repositories.auto_post_repository import AutoPostRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_feedback_repository import LearningFeedbackRepository
from repositories.naver_sync_repository import NaverSyncRepository
from services.coupang_inquiry_normalizer import CoupangInquiryNormalizer
from services.inquiry_sync_service import normalize_work_item
from services.learning_feedback_service import LearningFeedbackService
from services.learning_signal_service import LearningSignalService
from services.manual_inquiry_sync_service import ManualInquirySyncService
from ui.market_labels import ALL_MARKETS, shows_market_badge, stores_in_markets


NAVER = "OJE_PLUS"
NS = "COUPANG_OJE_NS"
PLUS = "COUPANG_OJE_PLUS"


def database(tmp_path) -> Database:
    value = Database(tmp_path / "integration-20260922.db")
    value.initialize()
    return value


def test_platform_migration_inherits_the_previous_effective_state(
    tmp_path, monkeypatch,
) -> None:
    import repositories.database as database_module

    migrations = database_module.MIGRATIONS
    monkeypatch.setattr(database_module, "MIGRATIONS", migrations[:-1])
    db = Database(tmp_path / "platform-migration.db")
    db.initialize()
    with db.transaction() as connection:
        connection.execute(
            "UPDATE naver_auto_post_settings SET enabled=1 WHERE id=1"
        )
    monkeypatch.setattr(database_module, "MIGRATIONS", migrations)
    assert db.initialize() == [34]
    assert AutoPostRepository(db).settings()["platform_enabled"] == {
        "NAVER": True, "COUPANG": True,
    }


def test_multi_market_scope_and_badge() -> None:
    stores = [NAVER, NS, PLUS]
    assert stores_in_markets(stores, [ALL_MARKETS]) == stores
    assert stores_in_markets(stores, ["NAVER"]) == [NAVER]
    assert stores_in_markets(stores, ["COUPANG"]) == [NS, PLUS]
    assert stores_in_markets(stores, ["NAVER", "COUPANG"]) == stores
    assert shows_market_badge(["NAVER", "COUPANG"])
    assert not shows_market_badge(["NAVER"])


@dataclass
class CoupangResult:
    account_code: str
    fetched: int = 3
    new: int = 1
    updated: int = 1
    unchanged: int = 1
    failed: int = 0
    error: str | None = None


def test_manual_sync_calls_all_three_and_preserves_partial_results(tmp_path) -> None:
    db = database(tmp_path)
    calls: list[object] = []

    class Naver:
        def __init__(self, _database):
            pass

        def run(self, **kwargs):
            calls.append(("NAVER", kwargs["sync_type"]))
            return {"status": "SUCCESS", "fetched_count": 2, "new": 1}

    class Coupang:
        def __init__(self, _database):
            pass

        def sync_accounts(self, accounts):
            calls.extend(accounts)
            return [
                CoupangResult(COUPANG_OJE_NS),
                CoupangResult(COUPANG_OJE_PLUS, error="READ_FAILED"),
            ]

    result = ManualInquirySyncService(
        db, naver_factory=Naver, coupang_factory=Coupang
    ).run(stores=[object()])
    assert calls == [
        ("NAVER", "MANUAL"), COUPANG_OJE_NS, COUPANG_OJE_PLUS
    ]
    by_key = {item["key"]: item for item in result["platforms"]}
    assert by_key["NAVER"]["new"] == 1
    assert by_key["COUPANG_OJE_NS"]["status"] == "SUCCESS"
    assert by_key["COUPANG_OJE_PLUS"]["status"] == "FAILED"
    assert by_key["COUPANG_OJE_NS"]["fetched"] == 3
    assert result["status"] == "PARTIAL"


def _inquiry(db: Database, store: str, source_id: str) -> int:
    return InquiryRepository(db).upsert_work_item({
        "store_code": store,
        "source_type": "COUPANG_ONLINE_INQUIRY" if store.startswith("COUPANG_") else "PRODUCT_INQUIRY",
        "source_question_id": source_id,
        "content": "터치되나요?",
        "raw_json": {},
    }).inquiry_id


def test_platform_switches_gate_events_independently(tmp_path) -> None:
    db = database(tmp_path)
    repo = AutoPostRepository(db)
    events = AutoPostEventRepository(db)
    NaverSyncRepository(db).save_auto_settings(enabled=True, interval_minutes=10)
    repo.save_settings(enabled=True, interval_minutes=10, max_retries=1)
    assert repo.settings()["platform_enabled"] == {"NAVER": True, "COUPANG": True}

    naver_id = _inquiry(db, NAVER, "N-1")
    coupang_id = _inquiry(db, NS, "C-1")
    events.create(inquiry_id=naver_id, store_code=NAVER, external_id="N-1", source_sync_id=None, runtime_enabled=True)
    events.create(inquiry_id=coupang_id, store_code=NS, external_id="C-1", source_sync_id=None, runtime_enabled=True)

    repo.save_platform_enabled("COUPANG", False)
    assert repo.settings()["platform_enabled"] == {"NAVER": True, "COUPANG": False}
    assert NaverSyncRepository(db).auto_settings()["enabled"] is True
    events.block_new_claims(store_codes=[NS, PLUS])
    assert events.claim_next(owner_id="n", store_codes=[NAVER])["inquiry_id"] == naver_id
    assert events.claim_next(owner_id="c", store_codes=[NS, PLUS]) is None

    repo.save_platform_enabled("NAVER", False)
    assert repo.settings()["enabled"] is False
    assert repo.settings()["platform_enabled"] == {"NAVER": False, "COUPANG": False}
    repo.save_platform_enabled("COUPANG", True)
    assert repo.settings()["platform_enabled"] == {"NAVER": False, "COUPANG": True}
    events.unblock_after_runtime_enable(store_codes=[NS, PLUS])
    assert events.claim_next(owner_id="c", store_codes=[NS, PLUS])["inquiry_id"] == coupang_id


def _coupang_answered(db: Database, account: str, inquiry_id: str) -> int:
    payload = {
        "inquiryId": inquiry_id,
        "sellerProductId": "15654321531",
        "vendorItemId": "93128932886",
        "content": "터치되나요?",
        "inquiryAt": "2026-09-16T10:00:00+09:00",
        "orderIds": [],
        "commentDtoList": [{
            "inquiryCommentId": f"c-{inquiry_id}",
            "inquiryId": inquiry_id,
            "content": "삼성 S32DM501 모델은 터치 호환 가능합니다.",
            "inquiryCommentAt": "2026-09-16T11:00:00+09:00",
        }],
    }
    ready = normalize_work_item(
        CoupangInquiryNormalizer().online(
            payload, account_code=account
        ).to_work_item()
    )
    return InquiryRepository(db).upsert_work_item(ready).inquiry_id


def test_coupang_seller_negative_is_market_scoped_deduped_and_shared(tmp_path) -> None:
    db = database(tmp_path)
    inquiry_id = _coupang_answered(db, "OJE_NS", "160900001")
    service = LearningFeedbackService(db)
    kwargs = {
        "inquiry_id": inquiry_id,
        "original_answer_source": AnswerProvenance.HISTORICAL_VERIFIED,
        "original_answer_reference_id": inquiry_id,
        "correction_reason": "FACT_ERROR",
        "correction_note": "32DM501은 터치 불가능함.",
        "signal_kind": SignalKind.CORRECTION.value,
        "signal_content": "S32DM501은 터치를 지원하지 않습니다.",
        "fact_scope": "MODEL",
    }
    first = service.capture_dashboard_negative(**kwargs)
    second = service.capture_dashboard_negative(**kwargs)
    assert [row["id"] for row in first] == [row["id"] for row in second]
    metadata = first[0]["metadata_json"]
    assert metadata["market_applicability"] == "COUPANG_ONLY"
    assert metadata["origin_market"] == "COUPANG"
    assert metadata["shared_cross_market_learning"] is False
    assert metadata["account_code"] == "OJE_NS"
    assert metadata["seller_answer_provenance"] == "MARKETPLACE_SELLER_ANSWER"

    signals = LearningSignalService(db)
    plus = signals.retrieve(
        "터치되나요?", store_code=PLUS, model_code="S32DM501",
        minimum_relevance=0,
    )
    naver = signals.retrieve(
        "터치되나요?", store_code=NAVER, model_code="S32DM501",
        minimum_relevance=0,
    )
    assert plus["corrections"]
    assert naver["corrections"] == []
    feedback = LearningFeedbackRepository(db).for_inquiry(inquiry_id)
    assert len([row for row in feedback if row["learning_signal_type"] == "NEGATIVE"]) == 1


def test_naver_negative_existing_path_stays_naver_only(tmp_path) -> None:
    db = database(tmp_path)
    inquiry_id = _inquiry(db, NAVER, "N-NEG")
    from repositories.answer_repository import AnswerRepository
    from answer.models import AnswerResult, AnswerStatus

    draft = AnswerRepository(db).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.GENERATED,
            category="GENERAL",
            reason="test",
            answer="터치가 가능합니다.",
            provider="rules",
            auto_answerable=True,
            needs_review=False,
        ),
    )
    saved = LearningFeedbackService(db).capture_dashboard_negative(
        inquiry_id=inquiry_id,
        original_answer_source=AnswerProvenance.PROGRAM_GENERATED,
        original_answer_reference_id=int(draft["id"]),
        correction_reason="FACT_ERROR",
        correction_note="터치를 지원하지 않습니다.",
    )
    assert saved[0]["metadata_json"]["market_applicability"] == "NAVER_ONLY"
    assert saved[0]["metadata_json"]["origin_market"] == "NAVER"
