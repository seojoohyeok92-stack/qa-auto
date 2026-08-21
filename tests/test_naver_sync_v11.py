from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from streamlit.testing.v1 import AppTest

from api.naver_read_client import classified_error, request_json
from config import NaverSyncSettings, StoreConfig
from repositories.answer_repository import AnswerRepository
from repositories.approval_repository import ApprovalRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from repositories.naver_sync_repository import NaverSyncRepository
from services.approval_service import ApprovalService
from services.naver_inquiry_sync_service import NaverInquirySyncService


def _settings(**overrides) -> NaverSyncSettings:
    values = {
        "enabled": True,
        "lookback_days": 7,
        "page_size": 100,
        "max_pages": 10,
        "connect_timeout": 1.0,
        "read_timeout": 2.0,
        "max_retries": 2,
        "retry_backoff_seconds": 0.0,
        "max_runtime_seconds": 30.0,
        "lock_ttl_seconds": 60,
    }
    values.update(overrides)
    return NaverSyncSettings(**values)


def _store() -> StoreConfig:
    return StoreConfig("STORE", "테스트 스토어", "client", "secret", True)


def _product(question_id: str, *, content: str | None = None) -> dict:
    return {
        "questionId": question_id,
        "question": content or f"{question_id} 문의 내용",
        "productId": f"P-{question_id}",
        "productName": "테스트 상품",
        "maskedWriterId": "ma***",
        "answered": False,
        "status": "WAITING",
        "createDate": "2026-07-30T11:00:00+09:00",
        "updateDate": "2026-07-30T11:00:00+09:00",
    }


def _page(
    items: list[dict],
    *,
    page: int = 1,
    total_pages: int = 1,
) -> dict:
    return {
        "contents": items,
        "page": page,
        "totalPages": total_pages,
        "totalElements": len(items),
        "last": page >= total_pages,
    }


@pytest.fixture
def database(tmp_path) -> Database:
    database = Database(tmp_path / "sync-v11.db")
    database.initialize()
    return database


def _service(
    database: Database,
    fetch,
    *,
    token_provider=None,
    settings: NaverSyncSettings | None = None,
) -> NaverInquirySyncService:
    return NaverInquirySyncService(
        database,
        settings=settings or _settings(),
        token_provider=token_provider or (lambda **kwargs: "read-token"),
        product_fetch=fetch,
        customer_fetch=lambda **kwargs: {
            "content": [],
            "totalPages": 1,
            "last": True,
        },
    )


def _run(service: NaverInquirySyncService):
    end = datetime(2026, 7, 31, 0, 0, tzinfo=UTC)
    return service.sync_inquiries(
        stores=[_store()],
        inquiry_types=["PRODUCT_INQUIRY"],
        from_datetime=end - timedelta(days=7),
        to_datetime=end,
    )


def test_new_three_inquiries_are_inserted_and_listed(
    database: Database,
) -> None:
    result = _run(
        _service(
            database,
            lambda **kwargs: _page(
                [_product("Q-1"), _product("Q-2"), _product("Q-3")]
            ),
        )
    )

    assert result.status == "SUCCESS"
    assert result.inserted_count == 3
    assert result.failed_count == 0
    assert InquiryRepository(database).count() == 3
    assert len(InquiryRepository(database).list()) == 3


def test_same_response_twice_is_unchanged_without_duplicates(
    database: Database,
) -> None:
    fetch = lambda **kwargs: _page(
        [_product("Q-1"), _product("Q-2"), _product("Q-3")]
    )
    first = _run(_service(database, fetch))
    second = _run(_service(database, fetch))

    assert first.inserted_count == 3
    assert second.inserted_count == 0
    assert second.unchanged_count == 3
    assert InquiryRepository(database).count() == 3


def test_source_update_preserves_active_draft_and_approval(
    database: Database,
) -> None:
    fetch_payload = [_product("Q-1", content="원본 문의")]
    _run(_service(database, lambda **kwargs: _page(fetch_payload)))
    inquiries = InquiryRepository(database)
    inquiry = inquiries.list()[0]
    answers = AnswerRepository(database)
    from answer.models import AnswerResult, AnswerStatus

    draft = answers.create_program_draft(
        inquiry["id"],
        AnswerResult(
            status=AnswerStatus.GENERATED,
            category="상품",
            reason="test",
            answer="보존할 Program Answer",
            provider="rules",
            auto_answerable=True,
            needs_review=False,
        ),
    )
    ApprovalService(database).approve(
        inquiry_id=inquiry["id"],
        draft_id=draft["id"],
        actor="승인자",
    )
    fetch_payload[0] = _product("Q-1", content="네이버에서 수정된 문의")
    fetch_payload[0]["updateDate"] = "2026-07-31T09:00:00+09:00"

    result = _run(_service(database, lambda **kwargs: _page(fetch_payload)))
    stored = inquiries.get(inquiry["id"])

    assert result.updated_count == 1
    assert stored["content"] == "네이버에서 수정된 문의"
    assert stored["source_content_changed"] == 1
    assert answers.active_for_inquiry(inquiry["id"])["id"] == draft["id"]
    from answer.answer_format import format_final_answer
    assert answers.get(draft["id"])["final_answer"] == format_final_answer(
        "보존할 Program Answer"
    )
    approval = ApprovalRepository(database).get_inquiry_approval(inquiry["id"])
    assert approval["approval_status"] == "APPROVED"
    assert approval["approved_by"] == "승인자"


def test_empty_response_is_success_and_does_not_delete_existing(
    database: Database,
) -> None:
    InquiryRepository(database).upsert_work_item(
        {
            "store_code": "STORE",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": "EXISTING",
        }
    )
    result = _run(
        _service(database, lambda **kwargs: _page([]))
    )
    assert result.status == "SUCCESS"
    assert result.inserted_count == result.failed_count == 0
    assert InquiryRepository(database).count() == 1


def test_401_reauth_is_limited_and_database_is_unchanged(
    database: Database,
) -> None:
    token_calls = []
    fetch_calls = []

    def token_provider(**kwargs):
        token_calls.append(1)
        return f"token-{len(token_calls)}"

    def fetch(**kwargs):
        fetch_calls.append(kwargs["access_token"])
        raise classified_error("AUTH_FAILED", status_code=401)

    result = _run(
        _service(database, fetch, token_provider=token_provider)
    )
    assert result.status == "FAILED"
    assert result.error_code == "AUTH_FAILED"
    assert len(token_calls) == 2
    assert len(fetch_calls) == 2
    assert InquiryRepository(database).count() == 0


def test_403_is_permission_denied_and_has_safe_message(
    database: Database,
) -> None:
    result = _run(
        _service(
            database,
            lambda **kwargs: (_ for _ in ()).throw(
                classified_error("PERMISSION_DENIED", status_code=403)
            ),
        )
    )
    assert result.status == "FAILED"
    assert result.error_code == "PERMISSION_DENIED"
    assert "권한" in result.error_message
    assert "secret" not in str(result.to_dict()).lower()
    assert "read-token" not in str(result.to_dict())


def test_429_uses_bounded_exponential_backoff() -> None:
    class Response:
        status_code = 429

        def json(self):
            return {}

    class Session:
        def __init__(self):
            self.calls = 0

        def request(self, *args, **kwargs):
            self.calls += 1
            return Response()

    session = Session()
    sleeps = []
    with pytest.raises(Exception) as captured:
        request_json(
            "GET",
            "https://api.commerce.naver.com/external/v1/contents/qnas",
            session=session,
            max_retries=2,
            backoff_seconds=0.5,
            sleeper=sleeps.append,
        )
    assert captured.value.code == "RATE_LIMITED"
    assert session.calls == 3
    assert sleeps == [0.5, 1.0]


def test_page_two_timeout_is_partial_and_page_one_is_saved(
    database: Database,
) -> None:
    def fetch(**kwargs):
        if kwargs["page"] == 1:
            return _page([_product("Q-1")], page=1, total_pages=2)
        raise classified_error("API_TIMEOUT")

    result = _run(_service(database, fetch))
    assert result.status == "PARTIAL_SYNC"
    assert result.inserted_count == 1
    assert result.error_code == "API_TIMEOUT"
    assert InquiryRepository(database).count() == 1


@pytest.mark.parametrize(
    "fetch",
    [
        lambda **kwargs: {"contents": {}, "totalPages": 1},
        lambda **kwargs: _page([{"question": "ID 없음"}]),
    ],
)
def test_invalid_response_or_item_does_not_crash_app_service(
    database: Database,
    fetch,
) -> None:
    result = _run(_service(database, fetch))
    assert result.status in {"FAILED", "PARTIAL_SYNC"}
    assert result.error_code in {
        "API_RESPONSE_INVALID",
        "NORMALIZATION_FAILED",
    }
    assert InquiryRepository(database).count() == 0


def test_duplicate_item_in_one_response_is_skipped(
    database: Database,
) -> None:
    result = _run(
        _service(
            database,
            lambda **kwargs: _page([_product("Q-1"), _product("Q-1")]),
        )
    )
    assert result.inserted_count == 1
    assert result.skipped_count == 1
    assert InquiryRepository(database).count() == 1


def test_repeated_page_is_detected_without_infinite_pagination(
    database: Database,
) -> None:
    calls = []

    def fetch(**kwargs):
        calls.append(kwargs["page"])
        return _page([_product("Q-1")], page=kwargs["page"], total_pages=3)

    result = _run(_service(database, fetch))

    assert calls == [1, 2]
    assert result.status == "PARTIAL_SYNC"
    assert result.error_code == "PAGINATION_FAILED"
    assert result.inserted_count == 1
    assert InquiryRepository(database).count() == 1


def test_same_store_lock_blocks_duplicate_api_call(
    database: Database,
) -> None:
    repository = NaverSyncRepository(database)
    assert repository.acquire_lock(
        store_id="STORE", sync_id="already-running", ttl_seconds=60
    )
    fetch_calls = []
    result = _run(
        _service(
            database,
            lambda **kwargs: fetch_calls.append(1) or _page([]),
        )
    )
    assert result.status == "SKIPPED"
    assert result.error_code == "SYNC_IN_PROGRESS"
    assert result.skipped_count == 1
    assert result.failed_count == 0
    assert fetch_calls == []
    persisted = NaverSyncRepository(database).get(result.sync_id)
    assert persisted["status"] == "SKIPPED"
    assert persisted["failed_count"] == 0


def test_product_order_id_is_not_promoted_and_no_dps_is_called(
    database: Database,
) -> None:
    customer = {
        "inquiryNo": "I-1",
        "inquiryContent": "배송 일정 문의",
        "productOrderId": "PRODUCT-ONLY",
        "answered": False,
        "inquiryRegistrationDateTime": "2026-07-30T11:00:00+09:00",
    }
    service = NaverInquirySyncService(
        database,
        settings=_settings(),
        token_provider=lambda **kwargs: "read-token",
        product_fetch=lambda **kwargs: _page([]),
        customer_fetch=lambda **kwargs: {
            "content": [customer],
            "totalPages": 1,
            "last": True,
        },
    )
    end = datetime(2026, 7, 31, tzinfo=UTC)
    result = service.sync_inquiries(
        stores=[_store()],
        inquiry_types=["CUSTOMER_INQUIRY"],
        from_datetime=end - timedelta(days=7),
        to_datetime=end,
    )
    stored = InquiryRepository(database).list()[0]
    assert result.inserted_count == 1
    assert stored["product_order_id"] == "PRODUCT-ONLY"
    assert stored["order_id"] is None
    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM dps_lookup_results"
        ).fetchone()[0] == 0


def test_source_resync_preserves_local_order_snapshot(
    database: Database,
) -> None:
    payload = _product("Q-LOCAL")
    _run(_service(database, lambda **kwargs: _page([payload])))
    repository = InquiryRepository(database)
    inquiry_id = repository.list()[0]["id"]
    repository.update_order_snapshot(
        inquiry_id,
        order_id="ORDER-VERIFIED",
        product_order_id=None,
        order_date="2026-07-30",
        product_name="Local product",
        order_status="PAYED",
        lookup_at="2026-07-31T09:00:00+09:00",
        lookup_type="ORDER_ID",
        cached=False,
    )

    result = _run(_service(database, lambda **kwargs: _page([payload])))
    stored = repository.get(inquiry_id)

    # product_name is source-owned and is refreshed, while the separately
    # verified local order/DPS snapshot remains intact.
    assert result.updated_count == 1
    assert stored["order_id"] == "ORDER-VERIFIED"
    assert stored["raw_json"]["order_lookup"]["lookup_type"] == "ORDER_ID"


def test_sync_is_streamlit_independent_and_read_only_api_only(
    database: Database,
) -> None:
    methods = []

    class Response:
        status_code = 200

        def json(self):
            return _page([])

    class Session:
        def request(self, method, url, **kwargs):
            methods.append((method, url))
            return Response()

    from api.qna import get_qna_list

    def fetch(**kwargs):
        return get_qna_list(**kwargs, session=Session(), sleeper=lambda _: None)

    result = _run(_service(database, fetch))
    assert result.status == "SUCCESS"
    assert methods and {method for method, _ in methods} == {"GET"}
    source = Path("services/naver_inquiry_sync_service.py").read_text(
        encoding="utf-8"
    )
    assert "commentContent" not in source
    assert "requests.put" not in source.lower()


def test_sync_run_and_required_events_are_persisted(
    database: Database,
) -> None:
    result = _run(
        _service(database, lambda **kwargs: _page([_product("Q-1")]))
    )
    persisted = NaverSyncRepository(database).latest()
    assert persisted["sync_id"] == result.sync_id
    assert persisted["status"] == "SUCCESS"
    assert persisted["inserted_count"] == 1
    events = {
        row["event_code"]
        for row in LogRepository(database).recent_system(limit=100)
    }
    assert {
        "NAVER_SYNC_STARTED",
        "NAVER_SYNC_PAGE_FETCHED",
        "NAVER_SYNC_ITEM_INSERTED",
        "NAVER_SYNC_COMPLETED",
    } <= events


def test_v11_migration_preserves_existing_inquiry_and_draft(
    tmp_path,
    monkeypatch,
) -> None:
    import repositories.database as database_module

    path = tmp_path / "migration-preserve.db"
    all_migrations = database_module.MIGRATIONS
    monkeypatch.setattr(database_module, "MIGRATIONS", all_migrations[:10])
    old_database = Database(path)
    old_database.initialize()
    with old_database.transaction() as connection:
        cursor = connection.execute(
            """
            INSERT INTO inquiries(
                store_code, source_type, source_question_id, content
            ) VALUES ('STORE', 'PRODUCT_INQUIRY', 'OLD-1', '기존 문의')
            """
        )
        inquiry_id = int(cursor.lastrowid)
        connection.execute(
            """
            INSERT INTO answer_drafts(
                inquiry_id, original_answer, review_status, is_active
            ) VALUES (?, ?, 'PENDING', 1)
            """,
            (inquiry_id, "기존 Draft"),
        )
    monkeypatch.setattr(database_module, "MIGRATIONS", all_migrations)
    assert old_database.initialize() == [
        11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28
    ]
    stored = InquiryRepository(old_database).get(inquiry_id)
    assert stored["external_inquiry_id"] == "OLD-1"
    assert AnswerRepository(old_database).active_for_inquiry(inquiry_id)[
        "original_answer"
    ] == "기존 Draft"
