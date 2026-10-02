"""Naver 상품문의 source-side deletion tracking.

The marketplace's inquiry list carries no deleted flag, so absence from an
otherwise complete response is the only signal available.  That makes two
questions the whole subject of these tests: *which rows are watched at all*,
and *when is absence trustworthy*.

The first answer is deliberately narrow -- only inquiries this build inserted
itself.  Every row that was already in the table keeps
``source_deletion_tracked = 0`` and is never compared against the API, which
is why CASE A and CASE E assert on nothing happening.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

from answer.models import AnswerResult, AnswerStatus
from api.naver_answer_client import NaverAnswerResponse
from api.naver_read_client import classified_error
from config import NaverPostSettings, NaverSyncSettings, StoreConfig
from repositories.answer_repository import AnswerRepository
from repositories.auto_post_repository import AutoPostRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.approval_service import ApprovalService
from services.naver_inquiry_sync_service import (
    SOURCE_DELETION_STREAK_THRESHOLD,
    NaverInquirySyncService,
)
from services.naver_post_service import NaverPostService

WINDOW_TO = datetime(2026, 7, 31, 0, 0, tzinfo=UTC)
WINDOW_FROM = WINDOW_TO - timedelta(days=7)

# Six days inside the lower edge of the requested window, well clear of the
# margin the service keeps there.
INSIDE_WINDOW_CREATED = "2026-07-30T11:00:00+09:00"


@pytest.fixture
def database(tmp_path: Path) -> Database:
    value = Database(tmp_path / "source-deletion.db")
    value.initialize()
    return value


def _settings(**overrides: Any) -> NaverSyncSettings:
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


def _store(code: str = "STORE") -> StoreConfig:
    return StoreConfig(code, f"{code} 스토어", "client", "secret", True)


def _product(question_id: str, *, created: str = INSIDE_WINDOW_CREATED) -> dict:
    return {
        "questionId": question_id,
        "question": f"{question_id} 문의 내용",
        "productId": f"P-{question_id}",
        "productName": "테스트 상품",
        "maskedWriterId": "ma***",
        "answered": False,
        "createDate": created,
        "updateDate": created,
    }


def _page(items: list[dict], *, page: int = 1, total_pages: int = 1) -> dict:
    return {
        "contents": items,
        "page": page,
        "totalPages": total_pages,
        "totalElements": len(items),
        "last": page >= total_pages,
    }


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


def _run(service: NaverInquirySyncService, *, stores=None):
    return service.sync_inquiries(
        stores=stores or [_store()],
        inquiry_types=["PRODUCT_INQUIRY"],
        from_datetime=WINDOW_FROM,
        to_datetime=WINDOW_TO,
    )


def _returning(items: list[dict]):
    return lambda **kwargs: _page(items)


def _state(database: Database, question_id: str) -> dict[str, Any]:
    with database.connection() as connection:
        row = connection.execute(
            """
            SELECT id, source_deletion_tracked, source_deleted,
                   source_missing_streak, source_deleted_detected_at
            FROM inquiries WHERE source_question_id = ?
            """,
            (question_id,),
        ).fetchone()
    assert row is not None, f"{question_id} row missing"
    return dict(row)


def _seed_historical(
    database: Database,
    question_id: str,
    *,
    store_code: str = "STORE",
    source_type: str = "PRODUCT_INQUIRY",
    created: str = INSIDE_WINDOW_CREATED,
) -> int:
    """A row that was already in the table before the feature shipped.

    Written through the ordinary upsert, exactly as the previous build would
    have written it, so it carries the migration defaults.
    """

    raw_json: dict[str, Any] = {"source": source_type}
    if source_type == "PRODUCT_INQUIRY":
        # The same payload shape the collector stores, so the row is a
        # genuinely postable historical inquiry and CASE O is about this
        # feature rather than a missing target id.
        raw_json |= {
            "questionId": question_id,
            "source_payload": {"questionId": question_id},
        }
    return InquiryRepository(database).upsert_work_item(
        {
            "store_code": store_code,
            "source_type": source_type,
            "source_question_id": question_id,
            "external_inquiry_id": question_id,
            "inquiry_type": source_type,
            "title": "과거 문의",
            "content": "기능 배포 전에 수집된 문의",
            "registered_at": created,
            "source_created_at": created,
            "raw_json": raw_json,
        }
    ).inquiry_id


# ----------------------------------------------------------------------
# CASE A / B / C -- what is watched, and from when
# ----------------------------------------------------------------------


def test_case_a_pre_existing_row_is_never_tracked_even_when_api_returns_it(
    database: Database,
) -> None:
    _seed_historical(database, "OLD-1")
    for _ in range(SOURCE_DELETION_STREAK_THRESHOLD + 1):
        result = _run(_service(database, _returning([_product("OLD-1")])))
        assert result.status == "SUCCESS"
    state = _state(database, "OLD-1")
    assert state["source_deletion_tracked"] == 0
    assert state["source_deleted"] == 0
    assert state["source_missing_streak"] == 0
    assert state["source_deleted_detected_at"] is None


def test_case_b_newly_inserted_inquiry_starts_tracked_and_clean(
    database: Database,
) -> None:
    result = _run(_service(database, _returning([_product("NEW-1")])))
    assert (result.status, result.inserted_count) == ("SUCCESS", 1)
    assert _state(database, "NEW-1") | {"id": 0} == {
        "id": 0,
        "source_deletion_tracked": 1,
        "source_deleted": 0,
        "source_missing_streak": 0,
        "source_deleted_detected_at": None,
    }


def test_case_c_tracked_inquiry_that_keeps_appearing_stays_clean(
    database: Database,
) -> None:
    fetch = _returning([_product("NEW-1")])
    for _ in range(4):
        assert _run(_service(database, fetch)).status == "SUCCESS"
    state = _state(database, "NEW-1")
    assert state["source_deletion_tracked"] == 1
    assert state["source_deleted"] == 0
    assert state["source_missing_streak"] == 0


# ----------------------------------------------------------------------
# CASE D / E / F -- the streak, and who it applies to
# ----------------------------------------------------------------------


def test_case_d_three_consecutive_absences_confirm_deletion(
    database: Database,
) -> None:
    both = [_product("GONE-1"), _product("STAYS-1")]
    assert _run(_service(database, _returning(both))).status == "SUCCESS"
    assert _state(database, "GONE-1")["source_deletion_tracked"] == 1

    survivor = _returning([_product("STAYS-1")])
    for expected_streak in (1, 2):
        assert _run(_service(database, survivor)).status == "SUCCESS"
        state = _state(database, "GONE-1")
        assert state["source_missing_streak"] == expected_streak
        assert state["source_deleted"] == 0
        assert state["source_deleted_detected_at"] is None

    assert _run(_service(database, survivor)).status == "SUCCESS"
    confirmed = _state(database, "GONE-1")
    assert confirmed["source_missing_streak"] == 3
    assert confirmed["source_deleted"] == 1
    assert confirmed["source_deleted_detected_at"] is not None

    # The inquiry that stayed is untouched throughout.
    assert _state(database, "STAYS-1")["source_deleted"] == 0
    assert _state(database, "STAYS-1")["source_missing_streak"] == 0


def test_case_d_detected_at_is_not_refreshed_on_later_syncs(
    database: Database,
) -> None:
    both = [_product("GONE-1"), _product("STAYS-1")]
    _run(_service(database, _returning(both)))
    survivor = _returning([_product("STAYS-1")])
    for _ in range(SOURCE_DELETION_STREAK_THRESHOLD):
        _run(_service(database, survivor))
    first = _state(database, "GONE-1")["source_deleted_detected_at"]
    assert first is not None
    for _ in range(2):
        _run(_service(database, survivor))
    assert _state(database, "GONE-1")["source_deleted_detected_at"] == first


def test_case_e_untracked_row_absent_many_times_is_left_alone(
    database: Database,
) -> None:
    _seed_historical(database, "OLD-1")
    # A real inquiry is present, so the source is authoritative and the
    # historical row is genuinely absent from a response that was trusted.
    present = _returning([_product("NEW-1")])
    for _ in range(SOURCE_DELETION_STREAK_THRESHOLD + 2):
        assert _run(_service(database, present)).status == "SUCCESS"
    state = _state(database, "OLD-1")
    assert state["source_deletion_tracked"] == 0
    assert state["source_deleted"] == 0
    assert state["source_missing_streak"] == 0
    assert state["source_deleted_detected_at"] is None


def test_case_f_reappearing_inquiry_is_restored(database: Database) -> None:
    both = [_product("GONE-1"), _product("STAYS-1")]
    _run(_service(database, _returning(both)))
    survivor = _returning([_product("STAYS-1")])
    for _ in range(SOURCE_DELETION_STREAK_THRESHOLD):
        _run(_service(database, survivor))
    assert _state(database, "GONE-1")["source_deleted"] == 1

    _run(_service(database, _returning(both)))
    restored = _state(database, "GONE-1")
    assert restored["source_deleted"] == 0
    assert restored["source_deleted_detected_at"] is None
    assert restored["source_missing_streak"] == 0
    assert restored["source_deletion_tracked"] == 1


def test_partial_absence_streak_resets_before_the_threshold(
    database: Database,
) -> None:
    """Two absences then an appearance must not accumulate towards three."""

    both = [_product("FLICKER-1"), _product("STAYS-1")]
    _run(_service(database, _returning(both)))
    survivor = _returning([_product("STAYS-1")])
    for _ in range(2):
        _run(_service(database, survivor))
    assert _state(database, "FLICKER-1")["source_missing_streak"] == 2
    _run(_service(database, _returning(both)))
    assert _state(database, "FLICKER-1")["source_missing_streak"] == 0
    for _ in range(2):
        _run(_service(database, survivor))
    assert _state(database, "FLICKER-1")["source_deleted"] == 0


# ----------------------------------------------------------------------
# CASE G / H -- absence is only evidence from a clean sync
# ----------------------------------------------------------------------


def _tracked_with_one_absence(database: Database) -> None:
    both = [_product("GONE-1"), _product("STAYS-1")]
    _run(_service(database, _returning(both)))
    _run(_service(database, _returning([_product("STAYS-1")])))
    assert _state(database, "GONE-1")["source_missing_streak"] == 1


def test_case_g_failed_sync_changes_no_deletion_state(
    database: Database,
) -> None:
    _tracked_with_one_absence(database)

    def failing(**kwargs):
        raise classified_error("API_TIMEOUT")

    assert _run(_service(database, failing)).status == "FAILED"
    assert _state(database, "GONE-1")["source_missing_streak"] == 1
    assert _state(database, "GONE-1")["source_deleted"] == 0


def test_case_g_partial_pagination_changes_no_deletion_state(
    database: Database,
) -> None:
    _tracked_with_one_absence(database)

    def fetch(**kwargs):
        if kwargs["page"] == 1:
            return _page([_product("STAYS-1")], page=1, total_pages=2)
        raise classified_error("API_TIMEOUT")

    assert _run(_service(database, fetch)).status == "PARTIAL_SYNC"
    assert _state(database, "GONE-1")["source_missing_streak"] == 1
    assert _state(database, "GONE-1")["source_deleted"] == 0


def test_case_g_normalization_failure_changes_no_deletion_state(
    database: Database,
) -> None:
    _tracked_with_one_absence(database)
    fetch = _returning([_product("STAYS-1"), {"question": "ID 없음"}])
    assert _run(_service(database, fetch)).status == "PARTIAL_SYNC"
    assert _state(database, "GONE-1")["source_missing_streak"] == 1
    assert _state(database, "GONE-1")["source_deleted"] == 0


def test_case_h_empty_response_changes_no_deletion_state(
    database: Database,
) -> None:
    _tracked_with_one_absence(database)
    result = _run(_service(database, _returning([])))
    assert result.status == "SUCCESS"
    assert result.fetched_count == 0
    for question_id in ("GONE-1", "STAYS-1"):
        state = _state(database, question_id)
        assert state["source_deleted"] == 0
    assert _state(database, "GONE-1")["source_missing_streak"] == 1
    assert InquiryRepository(database).count() == 2


def test_row_inside_the_window_margin_is_not_marked(
    database: Database,
) -> None:
    """``fromDate`` filters on creation time, so the edge is not reliable.

    An inquiry created half an hour after the window opens is about to fall
    out of range on its own; its absence says nothing, and the service leaves
    it for another run rather than guessing.
    """

    edge_created = (WINDOW_FROM + timedelta(minutes=30)).isoformat()
    both = [_product("EDGE-1", created=edge_created), _product("STAYS-1")]
    _run(_service(database, _returning(both)))
    assert _state(database, "EDGE-1")["source_deletion_tracked"] == 1
    survivor = _returning([_product("STAYS-1")])
    for _ in range(SOURCE_DELETION_STREAK_THRESHOLD + 1):
        assert _run(_service(database, survivor)).status == "SUCCESS"
    state = _state(database, "EDGE-1")
    assert state["source_missing_streak"] == 0
    assert state["source_deleted"] == 0


# ----------------------------------------------------------------------
# CASE I -- presence is decided per store
# ----------------------------------------------------------------------


def test_case_i_presence_in_one_store_is_not_presence_in_another(
    database: Database,
) -> None:
    stores = [_store("STORE_A"), _store("STORE_B")]

    def token_provider(**kwargs):
        return f"token-{kwargs['store'].code}"

    shared_id = "SHARED-1"

    def both_stores(**kwargs):
        return _page([_product(shared_id)])

    assert _run(
        _service(database, both_stores, token_provider=token_provider),
        stores=stores,
    ).status == "SUCCESS"

    def only_store_a(**kwargs):
        if kwargs["access_token"] == "token-STORE_A":
            return _page([_product(shared_id)])
        # STORE_B's own response is complete and non-empty, and the shared id
        # is not in it.  The id STORE_A returned must not stand in for it.
        return _page([_product("B-ONLY-1")])

    for _ in range(SOURCE_DELETION_STREAK_THRESHOLD):
        assert _run(
            _service(database, only_store_a, token_provider=token_provider),
            stores=stores,
        ).status == "SUCCESS"

    with database.connection() as connection:
        rows = {
            str(row["store_code"]): dict(row)
            for row in connection.execute(
                """
                SELECT store_code, source_deleted, source_missing_streak
                FROM inquiries WHERE source_question_id = ?
                """,
                (shared_id,),
            ).fetchall()
        }
    assert rows["STORE_A"]["source_deleted"] == 0
    assert rows["STORE_A"]["source_missing_streak"] == 0
    assert rows["STORE_B"]["source_deleted"] == 1
    assert rows["STORE_B"]["source_missing_streak"] == 3


# ----------------------------------------------------------------------
# CASE J / K -- the dashboard keeps showing it, and says so
# ----------------------------------------------------------------------


def _deleted_dashboard_row(
    database: Database, question_id: str, *, answered: bool
) -> None:
    both = [_product(question_id), _product("STAYS-1")]
    _run(_service(database, _returning(both)))
    if answered:
        with database.transaction() as connection:
            connection.execute(
                """
                UPDATE inquiries
                SET source_answered = 1, answer_status = 'ANSWERED'
                WHERE source_question_id = ?
                """,
                (question_id,),
            )
    survivor = _returning([_product("STAYS-1")])
    for _ in range(SOURCE_DELETION_STREAK_THRESHOLD):
        _run(_service(database, survivor))
    assert _state(database, question_id)["source_deleted"] == 1


def _render_dashboard(database_path: Path) -> AppTest:
    return AppTest.from_string(
        f'''
import streamlit as st
from app import _dashboard_work_items_from_rows
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from ui.review_workspace import render_review_workspace
db = Database(r"{database_path}")
db.initialize()
st.session_state.setdefault("dashboard_page", 1)
rows, total, total_pages = InquiryRepository(db).dashboard_page(
    store_codes=["STORE"], source="ALL", queues=[], priorities=[],
    answer_status="ALL", delivery_only=False, search_query="",
    start_date="2026-07-30", end_date="2026-07-30", kpi_filter=None,
    page=1, page_size=10,
)
st.write("LISTED", len(rows))
render_review_workspace(
    _dashboard_work_items_from_rows(rows), total, db, page_size=10,
    current_page=1, total_pages=total_pages,
)
'''
    ).run(timeout=60)


def test_case_j_deleted_inquiry_stays_in_the_list_with_its_label(
    tmp_path: Path,
) -> None:
    path = tmp_path / "dashboard-deleted.db"
    database = Database(path)
    database.initialize()
    _deleted_dashboard_row(database, "GONE-1", answered=False)

    at = _render_dashboard(path)
    assert not at.exception
    rendered = "\n".join(item.value for item in at.markdown)
    assert "LISTED `2`" in rendered
    assert "GONE-1" in rendered
    assert 'official-badge">원본 미조회' in rendered


def test_case_k_deleted_label_wins_over_answered(tmp_path: Path) -> None:
    path = tmp_path / "dashboard-deleted-answered.db"
    database = Database(path)
    database.initialize()
    _deleted_dashboard_row(database, "GONE-1", answered=True)

    at = _render_dashboard(path)
    assert not at.exception
    rendered = "\n".join(item.value for item in at.markdown)
    assert "GONE-1" in rendered
    assert 'official-badge">원본 미조회' in rendered
    assert 'official-badge">답변완료' not in rendered


# ----------------------------------------------------------------------
# CASE L -- KPI
# ----------------------------------------------------------------------


def _kpi(database: Database) -> dict[str, Any]:
    return InquiryRepository(database).dashboard_operational_card_counts(
        today_kst=datetime(2026, 7, 30).date(),
        store_codes=["STORE"],
        start_date="2026-07-01",
        end_date="2026-07-31",
    )


def _needs_attention(database: Database, question_id: str) -> None:
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE inquiries
            SET workflow_status = 'NEEDS_ATTENTION',
                approval_status = 'PENDING',
                post_status = 'NOT_POSTED'
            WHERE source_question_id = ?
            """,
            (question_id,),
        )


def test_case_l_stock_kpi_drops_deleted_rows_and_flow_kpi_does_not(
    database: Database,
) -> None:
    both = [_product("GONE-1"), _product("STAYS-1")]
    _run(_service(database, _returning(both)))
    for question_id in ("GONE-1", "STAYS-1"):
        _needs_attention(database, question_id)

    before = _kpi(database)
    assert before["REVIEW"]["value"] == 2
    assert before["ATTENTION"]["value"] == 2

    survivor = _returning([_product("STAYS-1")])
    for _ in range(SOURCE_DELETION_STREAK_THRESHOLD):
        _run(_service(database, survivor))
    _needs_attention(database, "GONE-1")
    assert _state(database, "GONE-1")["source_deleted"] == 1

    after = _kpi(database)
    assert after["REVIEW"]["value"] == 1
    assert after["ATTENTION"]["value"] == 1
    # FLOW counts what happened, and both inquiries did arrive.
    assert after["NEW"]["value"] == before["NEW"]["value"] == 2


def test_case_l_untracked_historical_row_keeps_its_kpi_contribution(
    database: Database,
) -> None:
    _seed_historical(database, "OLD-1")
    _needs_attention(database, "OLD-1")
    present = _returning([_product("NEW-1")])
    for _ in range(SOURCE_DELETION_STREAK_THRESHOLD + 1):
        _run(_service(database, present))
    assert _kpi(database)["REVIEW"]["value"] == 1
    assert _kpi(database)["ATTENTION"]["value"] == 1


# ----------------------------------------------------------------------
# CASE M / N / O -- nothing is posted to a deleted inquiry
# ----------------------------------------------------------------------


class RecordingClient:
    def __init__(self) -> None:
        self.requests: list[Any] = []

    def send(self, request, *, access_token):
        self.requests.append((request, access_token))
        return NaverAnswerResponse(204, "response-1")


def _approved_draft(database: Database, inquiry_id: int) -> None:
    draft = AnswerRepository(database).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.GENERATED,
            category="기타",
            reason="test",
            answer="등록 가능한 답변 본문",
            provider="rules",
            auto_answerable=True,
            needs_review=False,
        ),
    )
    ApprovalService(database).approve(
        inquiry_id=inquiry_id,
        draft_id=int(draft["id"]),
        actor="tester",
    )


def _post_service(database: Database, client) -> NaverPostService:
    return NaverPostService(
        database,
        settings=NaverPostSettings(enabled=True),
        store_resolver=lambda _=None: _store(),
        token_provider=lambda **kwargs: "mock-token",
        client=client,
    )


def _deleted_tracked_inquiry(database: Database, question_id: str) -> int:
    both = [_product(question_id), _product("STAYS-1")]
    _run(_service(database, _returning(both)))
    survivor = _returning([_product("STAYS-1")])
    for _ in range(SOURCE_DELETION_STREAK_THRESHOLD):
        _run(_service(database, survivor))
    state = _state(database, question_id)
    assert state["source_deleted"] == 1
    return int(state["id"])


def test_case_m_deleted_inquiry_leaves_the_auto_post_queue(
    database: Database,
) -> None:
    repository = AutoPostRepository(database)
    both = [_product("GONE-1"), _product("STAYS-1")]
    _run(_service(database, _returning(both)))
    queued = {
        str(row["source_question_id"])
        for row in repository.candidates(max_retries=3)
    }
    assert {"GONE-1", "STAYS-1"} <= queued

    survivor = _returning([_product("STAYS-1")])
    for _ in range(SOURCE_DELETION_STREAK_THRESHOLD):
        _run(_service(database, survivor))

    remaining = {
        str(row["source_question_id"])
        for row in repository.candidates(max_retries=3)
    }
    assert "GONE-1" not in remaining
    assert "STAYS-1" in remaining


@pytest.mark.parametrize("retry_requested", [False, True])
def test_case_n_manual_and_retry_post_are_blocked_with_no_network_call(
    database: Database, retry_requested: bool
) -> None:
    inquiry_id = _deleted_tracked_inquiry(database, "GONE-1")
    _approved_draft(database, inquiry_id)
    client = RecordingClient()
    result = _post_service(database, client).post(
        inquiry_id,
        actor="tester",
        confirmed=True,
        retry_requested=retry_requested,
    )
    assert result.status == "BLOCKED"
    assert result.error_code == "SOURCE_DELETED"
    assert result.network_call_count == 0
    assert client.requests == []


def test_case_n_automatic_post_is_blocked_with_no_network_call(
    database: Database,
) -> None:
    inquiry_id = _deleted_tracked_inquiry(database, "GONE-1")
    _approved_draft(database, inquiry_id)
    client = RecordingClient()
    result = _post_service(database, client).post(
        inquiry_id,
        actor="SYSTEM_AUTO_POST",
        confirmed=True,
        automatic=True,
        auto_post_run_id="run-1",
    )
    assert (result.status, result.error_code) == ("BLOCKED", "SOURCE_DELETED")
    assert client.requests == []


def test_case_o_untracked_historical_row_still_posts_normally(
    database: Database,
) -> None:
    """A row that predates tracking is answerable exactly as before."""

    inquiry_id = _seed_historical(database, "OLD-1")
    _approved_draft(database, inquiry_id)
    # The API keeps returning it, as it does for any live historical inquiry.
    present = _returning([_product("OLD-1"), _product("NEW-1")])
    for _ in range(SOURCE_DELETION_STREAK_THRESHOLD + 1):
        assert _run(_service(database, present)).status == "SUCCESS"
    client = RecordingClient()
    result = _post_service(database, client).post(
        inquiry_id, actor="tester", confirmed=True
    )
    assert (result.status, result.error_code) == ("POSTED", None)
    assert len(client.requests) == 1
    # Appearing in a response does not promote it: tracking starts at insert.
    assert _state(database, "OLD-1")["source_deletion_tracked"] == 0
    assert _state(database, "OLD-1")["source_deleted"] == 0


def test_case_o_absent_historical_row_is_refused_by_the_older_guard(
    database: Database,
) -> None:
    """Absence already had a consequence, and it is not this feature's.

    An inquiry the API has not confirmed since the last sync fails the
    pre-existing remote-snapshot check with ``TARGET_NOT_FOUND``.  What
    matters here is that the reason is still that one -- the historical row
    never acquires deletion state, so ``SOURCE_DELETED`` can never be it.
    """

    inquiry_id = _seed_historical(database, "OLD-1")
    _approved_draft(database, inquiry_id)
    present = _returning([_product("NEW-1")])
    for _ in range(SOURCE_DELETION_STREAK_THRESHOLD + 1):
        _run(_service(database, present))
    client = RecordingClient()
    result = _post_service(database, client).post(
        inquiry_id, actor="tester", confirmed=True
    )
    assert result.error_code == "TARGET_NOT_FOUND"
    assert client.requests == []
    state = _state(database, "OLD-1")
    assert state["source_deletion_tracked"] == 0
    assert state["source_deleted"] == 0
    assert state["source_missing_streak"] == 0


# ----------------------------------------------------------------------
# CASE P -- the record survives
# ----------------------------------------------------------------------


def _history_snapshot(database: Database, inquiry_id: int) -> dict[str, Any]:
    tables = (
        "answer_drafts",
        "answer_versions",
        "approval_history",
        "answer_learning_provenance",
        "workflow_steps",
        "naver_posted_answers",
        "naver_post_attempts",
        "post_reviews",
    )
    snapshot: dict[str, Any] = {}
    with database.connection() as connection:
        existing = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for table in tables:
            if table not in existing:
                continue
            rows = connection.execute(
                f"SELECT * FROM {table} WHERE inquiry_id = ? ORDER BY id",
                (inquiry_id,),
            ).fetchall()
            snapshot[table] = [tuple(row) for row in rows]
        row = connection.execute(
            "SELECT title, content, workflow_status, approval_status,"
            " post_status FROM inquiries WHERE id = ?",
            (inquiry_id,),
        ).fetchone()
        snapshot["inquiry"] = tuple(row)
    return snapshot


def test_case_p_deletion_mark_preserves_every_answer_record(
    database: Database,
) -> None:
    both = [_product("GONE-1"), _product("STAYS-1")]
    _run(_service(database, _returning(both)))
    inquiry_id = int(_state(database, "GONE-1")["id"])
    _approved_draft(database, inquiry_id)
    before = _history_snapshot(database, inquiry_id)
    assert before["answer_drafts"], "fixture must create a draft"
    assert before["approval_history"], "fixture must create an approval"

    survivor = _returning([_product("STAYS-1")])
    for _ in range(SOURCE_DELETION_STREAK_THRESHOLD):
        _run(_service(database, survivor))
    assert _state(database, "GONE-1")["source_deleted"] == 1
    assert _history_snapshot(database, inquiry_id) == before

    # And restoring it does not reset anything either.
    _run(_service(database, _returning(both)))
    assert _state(database, "GONE-1")["source_deleted"] == 0
    assert _history_snapshot(database, inquiry_id) == before


# ----------------------------------------------------------------------
# CASE Q -- Coupang is outside this feature entirely
# ----------------------------------------------------------------------


def test_case_q_coupang_rows_stay_at_the_migration_default(
    database: Database,
) -> None:
    coupang_id = _seed_historical(
        database,
        "COUPANG-1",
        store_code="COUPANG_OJE_NS",
        source_type="COUPANG_ONLINE_INQUIRY",
    )
    present = _returning([_product("NEW-1")])
    for _ in range(SOURCE_DELETION_STREAK_THRESHOLD + 2):
        assert _run(_service(database, present)).status == "SUCCESS"

    state = _state(database, "COUPANG-1")
    assert state["source_deletion_tracked"] == 0
    assert state["source_deleted"] == 0
    assert state["source_missing_streak"] == 0

    # Even handed the id directly, the tracking write refuses it: the source
    # type is checked in SQL, not by the caller.
    repository = InquiryRepository(database)
    assert repository.start_source_deletion_tracking(
        [coupang_id], source_type="PRODUCT_INQUIRY"
    ) == []
    assert _state(database, "COUPANG-1")["source_deletion_tracked"] == 0

    # And it is still an ordinary auto-post candidate.
    queued = {
        str(row["source_question_id"])
        for row in AutoPostRepository(database).candidates(max_retries=3)
    }
    assert "COUPANG-1" in queued


def test_coupang_row_is_not_a_naver_deletion_candidate(
    database: Database,
) -> None:
    _seed_historical(
        database,
        "COUPANG-1",
        store_code="COUPANG_OJE_NS",
        source_type="COUPANG_ONLINE_INQUIRY",
    )
    candidates = InquiryRepository(database).source_deletion_candidates(
        store_code="COUPANG_OJE_NS",
        source_type="COUPANG_ONLINE_INQUIRY",
    )
    assert candidates == []
