from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from streamlit.testing.v1 import AppTest

from config import StoreConfig
from repositories.database import Database
from services.inquiry_sync_orchestrator import InquirySyncOrchestrator
from services.work_queue_service import (
    load_customer_inquiries,
    load_product_inquiries,
)


def _store() -> StoreConfig:
    return StoreConfig("STORE", "테스트 스토어", "client", "secret", True)


def _item(source: str, question_id: str) -> dict:
    return {
        "store_code": "STORE",
        "store_name": "테스트 스토어",
        "source": source,
        "inquiry_id": question_id,
        "title": "문의",
        "content": "내용",
        "registered_at": "2026-07-30T11:00:00+09:00",
        "answered": False,
    }


def test_product_inquiry_loader_fetches_every_page(monkeypatch) -> None:
    calls: list[tuple[int, datetime]] = []

    def fake_get_qna_list(**kwargs):
        calls.append((kwargs["page"], kwargs["to_date"]))
        page = kwargs["page"]
        return {
            "contents": [
                {
                    "questionId": f"Q-{page}",
                    "question": "일반 문의",
                    "createDate": "2026-07-30T11:00:00+09:00",
                }
            ],
            "totalPages": 2,
            "totalElements": 2,
            "last": page == 2,
            "_request": {"page": page},
        }

    monkeypatch.setattr(
        "services.work_queue_service.get_qna_list",
        fake_get_qna_list,
    )
    monkeypatch.setattr(
        "services.work_queue_service.analyze_product_inquiry",
        lambda qna, **kwargs: _item("PRODUCT_INQUIRY", qna["questionId"]),
    )

    items, errors = load_product_inquiries(_store(), "token")

    assert not errors
    assert [page for page, _ in calls] == [1, 2]
    assert calls[0][1] == calls[1][1]
    assert [item["inquiry_id"] for item in items] == ["Q-1", "Q-2"]


def test_customer_inquiry_loader_fetches_every_page(monkeypatch) -> None:
    calls: list[int] = []

    def fake_get_customer_inquiries(**kwargs):
        page = kwargs["page"]
        calls.append(page)
        return {
            "content": [
                {
                    "inquiryNo": f"C-{page}",
                    "inquiryContent": "일반 문의",
                    "inquiryRegistrationDateTime": (
                        "2026-07-30T11:00:00+09:00"
                    ),
                }
            ],
            "totalPages": 3,
            "totalElements": 3,
            "last": page == 3,
            "_request": {"page": page},
        }

    monkeypatch.setattr(
        "services.work_queue_service.get_customer_inquiries",
        fake_get_customer_inquiries,
    )
    monkeypatch.setattr(
        "services.work_queue_service.analyze_customer_inquiry",
        lambda inquiry, **kwargs: _item(
            "CUSTOMER_INQUIRY", inquiry["inquiryNo"]
        ),
    )

    items, errors = load_customer_inquiries(_store(), "token")

    assert not errors
    assert calls == [1, 2, 3]
    assert len(items) == 3


def test_incremental_product_sync_stops_when_page_reaches_watermark(
    monkeypatch,
) -> None:
    calls: list[int] = []

    def fake_get_qna_list(**kwargs):
        page = kwargs["page"]
        calls.append(page)
        return {
            "contents": [
                {
                    "questionId": f"Q-{page}-NEW",
                    "question": "일반 문의",
                    "createDate": "2026-07-30T12:00:00+09:00",
                },
                {
                    "questionId": f"Q-{page}-OLD",
                    "question": "일반 문의",
                    "createDate": "2026-07-30T10:00:00+09:00",
                },
            ],
            "totalPages": 10,
            "totalElements": 20,
            "last": False,
            "_request": {"page": page},
        }

    monkeypatch.setattr(
        "services.work_queue_service.get_qna_list",
        fake_get_qna_list,
    )
    monkeypatch.setattr(
        "services.work_queue_service.analyze_product_inquiry",
        lambda qna, **kwargs: _item("PRODUCT_INQUIRY", qna["questionId"]),
    )

    items, errors = load_product_inquiries(
        _store(),
        "token",
        since_registered_at="2026-07-30T10:30:00+09:00",
    )

    assert not errors
    assert calls == [1]
    assert len(items) == 2


def test_repository_sync_watermarks_are_isolated_by_store_and_source(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "watermarks.db")
    database.initialize()
    from repositories.inquiry_repository import InquiryRepository

    repository = InquiryRepository(database)
    repository.upsert_work_item(
        {
            "store_code": "STORE_A",
            "source_type": "CUSTOMER_INQUIRY",
            "source_question_id": "A-1",
            "registered_at": "2026-07-30T12:00:00+09:00",
        }
    )
    repository.upsert_work_item(
        {
            "store_code": "STORE_B",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": "B-1",
            "registered_at": "2026-07-30T13:00:00+09:00",
        }
    )

    assert repository.sync_watermarks() == {
        ("STORE_A", "CUSTOMER_INQUIRY"): "2026-07-30T12:00:00+09:00",
        ("STORE_B", "PRODUCT_INQUIRY"): "2026-07-30T13:00:00+09:00",
    }


def test_orchestrator_persists_source_failure_stage_and_correlation(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "sync.db")
    database.initialize()

    def loader(**kwargs):
        kwargs["event_callback"](
            "NAVER_SYNC_API_REQUEST_STARTED",
            {"store_code": "STORE", "source": "CUSTOMER_INQUIRY", "page": 1},
        )
        return [], [
            {
                "store_code": "STORE",
                "store_name": "테스트 스토어",
                "stage": "고객문의 조회",
                "source": "CUSTOMER_INQUIRY",
                "inquiry_id": None,
                "message": "ReadTimeout",
            }
        ]

    result = InquirySyncOrchestrator(database, loader=loader).run(
        stores=[_store()]
    )

    assert result.failed_count == 1
    with database.connection() as connection:
        rows = connection.execute(
            """
            SELECT event_code, details_json FROM activity_logs
            WHERE event_code IN (
                'NAVER_SYNC_API_REQUEST_STARTED',
                'NAVER_SYNC_SOURCE_FAILED'
            )
            ORDER BY id
            """
        ).fetchall()
    assert [row["event_code"] for row in rows] == [
        "NAVER_SYNC_API_REQUEST_STARTED",
        "NAVER_SYNC_SOURCE_FAILED",
    ]
    assert all(result.correlation_id in row["details_json"] for row in rows)


def test_partial_sync_is_not_presented_as_success() -> None:
    at = AppTest.from_string(
        """
import streamlit as st
from app import _render_sync_result
st.session_state["dashboard_sync_result"] = {
    "requested_store_count": 1,
    "fetched_count": 0,
    "created_count": 0,
    "updated_count": 0,
    "failed_count": 1,
    "completed_at": "2026-07-30T11:00:00+09:00",
    "correlation_id": "trace-id",
    "errors": [{
        "store_name": "테스트 스토어",
        "stage": "고객문의 조회",
        "source": "CUSTOMER_INQUIRY",
        "message": "ReadTimeout",
    }],
}
_render_sync_result()
"""
    ).run(timeout=20)

    assert not at.exception
    assert at.warning
    assert not at.success
    assert "일부 실패" in at.warning[0].value
