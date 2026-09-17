"""Operational Coupang Online Inquiry sync: GET -> normalize -> inquiries, nothing else.

Runs the real ``CoupangReadClient`` over a fake transport against a temporary
database, so every request the service builds is inspected and none leaves the
process.  What is pinned: both accounts under their own identity, inserts for
new inquiries and updates when one is answered later, a bounded lookback, no
table but ``inquiries`` touched, and a cycle that survives an account failing.
"""

from __future__ import annotations

import ast
from datetime import UTC, date, datetime
from pathlib import Path
import sqlite3
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from api.coupang_read_client import CoupangReadClient
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.naver_sync_repository import NaverSyncRepository
from services import coupang_inquiry_sync_service as sync_module
from services.coupang_inquiry_sync_service import (
    CoupangInquirySyncService,
    date_windows,
    run_operational_sync,
)
from services.naver_auto_sync_scheduler import (
    NaverAutoSyncScheduler,
    ensure_auto_sync_scheduler,
    run_coupang_inquiry_sync,
)

TODAY = date(2026, 9, 17)
SOURCE_TYPE = "COUPANG_ONLINE_INQUIRY"
SECRET = "SECRET-KEY-MUST-NOT-PRINT"
ACCESS = "ACCESS-KEY-MUST-NOT-PRINT"
VENDOR = "A00VENDOR99"
REPLY = "안녕하세요 고객님, 해당 모델은 벽걸이 설치가 가능합니다."


def inquiry(
    inquiry_id: str,
    *,
    answered: bool = False,
    asked: str = "2026-09-16T10:00:00+09:00",
    spid: str = "15654321531",
) -> dict:
    return {
        "inquiryId": inquiry_id,
        "sellerProductId": spid,
        "vendorItemId": "93128932886",
        "content": "벽걸이 설치 되나요?",
        "inquiryAt": asked,
        "orderIds": [],
        "commentDtoList": [{
            "inquiryCommentId": f"c-{inquiry_id}",
            "inquiryId": inquiry_id,
            "content": REPLY,
            "inquiryCommentAt": "2026-09-17T09:00:00+09:00",
        }] if answered else [],
    }


class Response:
    def __init__(self, status_code: int, body: Any = None) -> None:
        self.status_code = status_code
        self.body = body

    def json(self) -> Any:
        return self.body


class Transport:
    """Pages keyed by (window start, page); records every request."""

    def __init__(self, pages: dict | None = None, *, status: int = 200) -> None:
        self.pages = pages or {}
        self.status = status
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> Response:
        query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        self.calls.append({"method": method, "url": url, "query": query})
        if self.status != 200:
            return Response(self.status, {})
        content, total = self.pages.get(
            (query["inquiryStartAt"], int(query["pageNum"])), ([], 1)
        )
        return Response(200, {"code": 200, "data": {
            "content": content, "pagination": {"totalPages": total},
        }})


def recent(items: list[dict], total: int = 1, page: int = 1) -> dict:
    """The default single window of the last seven days."""

    return {("2026-09-11", page): (items, total)}


@pytest.fixture
def database(tmp_path) -> Database:
    value = Database(tmp_path / "coupang-ops.db")
    value.initialize()
    return value


def service(database: Database, transports: dict[str, Transport]) -> CoupangInquirySyncService:
    def make(account_code: str) -> CoupangReadClient:
        return CoupangReadClient(
            access_key=ACCESS, secret_key=SECRET, vendor_id=VENDOR,
            transport=transports[account_code],
            min_request_interval_seconds=0, max_retries=0,
        )
    return CoupangInquirySyncService(database, client_factory=make, today=lambda: TODAY)


def rows(database: Database) -> list[dict]:
    with database.connection() as connection:
        return [dict(r) for r in connection.execute("SELECT * FROM inquiries ORDER BY id")]


def snapshot(database: Database, *, skip: tuple[str, ...] = ()) -> dict[str, list]:
    connection = sqlite3.connect(str(database.path))
    try:
        tables = [n for (n,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ) if n not in skip]
        return {t: sorted(map(tuple, connection.execute(f'SELECT * FROM "{t}"')), key=repr)
                for t in tables}
    finally:
        connection.close()


def by_source(database: Database, store: str, inquiry_id: str) -> dict:
    return InquiryRepository(database).get_by_source(store, SOURCE_TYPE, inquiry_id)


# --- 1, 2, 6 new inquiries --------------------------------------------------

@pytest.mark.parametrize("account,store", [
    ("OJE_NS", "COUPANG_OJE_NS"), ("OJE_PLUS", "COUPANG_OJE_PLUS"),
])
def test_a_new_unanswered_inquiry_is_inserted_under_its_account(database, account, store) -> None:
    transport = Transport(recent([inquiry("500")]))

    result = service(database, {account: transport}).sync_account(account)

    assert (result.new, result.updated, result.failed) == (1, 0, 0)
    row = by_source(database, store, "500")
    assert row["source_answered"] in (0, False)
    assert row["source_metadata_json"]["account_code"] == account
    assert row["source_metadata_json"]["market"] == "COUPANG"
    assert row["raw_json"]["sellerProductId"] == "15654321531"
    assert row["workflow_status"] == "NEW"
    assert row["answer_status"] == "UNANSWERED"


# --- 3 idempotence ----------------------------------------------------------

def test_the_same_inquiry_twice_is_one_row(database) -> None:
    transport = Transport(recent([inquiry("500"), inquiry("501", answered=True)]))
    sync = service(database, {"OJE_NS": transport})

    first = sync.sync_account("OJE_NS")
    second = sync.sync_account("OJE_NS")

    assert first.new == 2
    assert (second.new, second.updated, second.unchanged) == (0, 0, 2)
    assert len(rows(database)) == 2


# --- 4, 5 an answer arrives later -------------------------------------------

def test_a_later_answer_updates_the_same_row(database) -> None:
    service(database, {"OJE_NS": Transport(recent([inquiry("500")]))}).sync_account("OJE_NS")
    before = by_source(database, "COUPANG_OJE_NS", "500")
    with database.transaction() as connection:
        connection.execute(
            "UPDATE inquiries SET workflow_status='REVIEW_PENDING', approval_status='APPROVED' WHERE id=?",
            (before["id"],),
        )

    result = service(
        database, {"OJE_NS": Transport(recent([inquiry("500", answered=True)]))}
    ).sync_account("OJE_NS")

    after = by_source(database, "COUPANG_OJE_NS", "500")
    assert result.updated == 1
    assert after["id"] == before["id"]
    assert after["source_answered"] == 1
    assert after["raw_json"]["commentDtoList"][0]["content"] == REPLY
    assert after["seller_answer"] == REPLY
    assert after["source_metadata_json"]["seller_answer_selection"] == "SINGLE_COMMENT"
    # Local state is not the sync's to change.
    assert after["workflow_status"] == "REVIEW_PENDING"
    assert after["approval_status"] == "APPROVED"
    assert after["answer_status"] == "UNANSWERED"
    assert len(rows(database)) == 1


# --- 7 two accounts, one inquiry id -----------------------------------------

def test_the_same_inquiry_id_in_both_accounts_stays_two_inquiries(database) -> None:
    transports = {
        "OJE_NS": Transport(recent([inquiry("777", spid="111")])),
        "OJE_PLUS": Transport(recent([inquiry("777", spid="222")])),
    }
    results = service(database, transports).sync_accounts()

    assert [r.new for r in results] == [1, 1]
    ns = by_source(database, "COUPANG_OJE_NS", "777")
    plus = by_source(database, "COUPANG_OJE_PLUS", "777")
    assert ns["id"] != plus["id"]
    assert (ns["raw_json"]["sellerProductId"], plus["raw_json"]["sellerProductId"]) == ("111", "222")
    assert ns["source_metadata_json"]["account_code"] == "OJE_NS"
    assert plus["source_metadata_json"]["account_code"] == "OJE_PLUS"
    assert not any(r["store_code"] == "COUPANG" for r in rows(database))


# --- 8, 9, 10 the requests ----------------------------------------------------

def test_requests_are_online_all_answer_states_within_seven_days(database) -> None:
    transport = Transport()
    service(database, {"OJE_NS": transport}).sync_account(
        "OJE_NS", start_date=date(2026, 8, 1), end_date=TODAY
    )

    assert transport.calls
    for call in transport.calls:
        assert call["method"] == "GET"
        assert "/onlineInquiries" in call["url"]
        assert "callCenterInquiries" not in call["url"]
        assert call["query"]["answeredType"] == "ALL"
        span = date.fromisoformat(call["query"]["inquiryEndAt"]) - date.fromisoformat(
            call["query"]["inquiryStartAt"])
        assert 0 <= span.days <= 6


def test_windows_cover_the_range_without_gaps() -> None:
    windows = list(date_windows(date(2026, 8, 19), TODAY))
    assert windows[0][0] == date(2026, 8, 19) and windows[-1][1] == TODAY
    for (start, end), following in zip(windows, windows[1:]):
        assert (end - start).days <= 6
        assert (following[0] - end).days == 1


def test_every_page_is_read(database) -> None:
    transport = Transport({
        ("2026-09-11", 1): ([inquiry("1"), inquiry("2")], 2),
        ("2026-09-11", 2): ([inquiry("3")], 2),
    })
    result = service(database, {"OJE_NS": transport}).sync_account("OJE_NS")
    assert result.http_requests == 2
    assert result.new == 3


# --- lookback ---------------------------------------------------------------

def _store(database: Database, account: str, inquiry_id: str, asked: str, *, answered: bool) -> None:
    service(database, {account: Transport({
        (asked[:10], 1): ([inquiry(inquiry_id, answered=answered, asked=asked)], 1),
    })}).sync_account(account, start_date=date.fromisoformat(asked[:10]),
                      end_date=date.fromisoformat(asked[:10]))


def test_with_nothing_open_a_cycle_is_the_last_seven_days(database) -> None:
    _store(database, "OJE_NS", "old-answered", "2026-08-25T10:00:00+09:00", answered=True)
    sync = service(database, {"OJE_NS": Transport()})
    assert sync.lookback_range("OJE_NS") == (date(2026, 9, 11), TODAY)
    assert sync.sync_account("OJE_NS").http_requests == 1


def test_an_open_inquiry_older_than_a_week_is_re_read(database) -> None:
    """Asked 23 days ago, answered since: seven days alone would never see it."""

    _store(database, "OJE_NS", "900", "2026-08-25T10:00:00+09:00", answered=False)
    sync = service(database, {"OJE_NS": Transport({
        ("2026-08-25", 1): ([inquiry("900", answered=True, asked="2026-08-25T10:00:00+09:00")], 1),
    })})

    assert sync.lookback_range("OJE_NS") == (date(2026, 8, 25), TODAY)
    result = sync.sync_account("OJE_NS")

    assert result.updated == 1
    assert by_source(database, "COUPANG_OJE_NS", "900")["source_answered"] == 1


def test_the_reach_back_is_capped_and_per_account(database) -> None:
    _store(database, "OJE_NS", "ancient", "2026-06-01T10:00:00+09:00", answered=False)
    _store(database, "OJE_PLUS", "plus-open", "2026-09-01T10:00:00+09:00", answered=False)
    sync = service(database, {})

    assert sync.lookback_range("OJE_NS") == (date(2026, 9, 11), TODAY)
    assert sync.lookback_range("OJE_PLUS") == (date(2026, 9, 1), TODAY)
    start, end = sync.lookback_range("OJE_NS")
    assert (end - start).days < sync_module.UNANSWERED_RECHECK_DAYS


def test_a_row_of_unknown_answer_state_does_not_extend_the_reach(database) -> None:
    """NULL is the pre-normalization shape; only a known 0 is an open inquiry."""

    _store(database, "OJE_NS", "901", "2026-09-01T10:00:00+09:00", answered=False)
    with database.transaction() as connection:
        connection.execute("UPDATE inquiries SET source_answered = NULL")
    assert service(database, {}).lookback_range("OJE_NS") == (date(2026, 9, 11), TODAY)


# --- 11, 12 accounts, failures, the scheduler ----------------------------------

def test_a_cycle_syncs_both_accounts(database, capsys) -> None:
    transports = {"OJE_NS": Transport(recent([inquiry("1")])),
                  "OJE_PLUS": Transport(recent([inquiry("2")]))}
    results = service(database, transports).sync_accounts()

    assert [r.account_code for r in results] == ["OJE_NS", "OJE_PLUS"]
    assert transports["OJE_NS"].calls and transports["OJE_PLUS"].calls
    lines = [l for l in capsys.readouterr().out.splitlines() if l.startswith("COUPANG_SYNC")]
    assert lines == [
        "COUPANG_SYNC OJE_NS range=2026-09-11~2026-09-17 requests=1 fetched=1 new=1 updated=0 unchanged=0 failed=0",
        "COUPANG_SYNC OJE_PLUS range=2026-09-11~2026-09-17 requests=1 fetched=1 new=1 updated=0 unchanged=0 failed=0",
    ]


@pytest.mark.parametrize("status", [401, 504])
def test_one_account_failing_does_not_stop_the_other(database, capsys, status) -> None:
    transports = {"OJE_NS": Transport(status=status),
                  "OJE_PLUS": Transport(recent([inquiry("2")]))}
    results = service(database, transports).sync_accounts()

    assert results[0].error is not None and results[0].new == 0
    assert results[1].error is None and results[1].new == 1
    assert by_source(database, "COUPANG_OJE_PLUS", "2") is not None
    printed = capsys.readouterr().out
    assert "COUPANG_SYNC OJE_NS" in printed and "error=" in printed
    assert "endpoint=/v2/providers/openapi/apis/api/v5/vendors/{vendor_id}/onlineInquiries" in printed
    for secret in (SECRET, ACCESS, VENDOR):
        assert secret not in printed


def test_a_bad_item_is_counted_and_the_rest_are_stored(database) -> None:
    broken = inquiry("x")
    del broken["inquiryId"]
    result = service(database, {"OJE_NS": Transport(recent([broken, inquiry("1")]))}).sync_account("OJE_NS")
    assert (result.failed, result.new) == (1, 1)


def test_the_operational_entry_never_raises(database, monkeypatch) -> None:
    monkeypatch.setattr(
        CoupangInquirySyncService, "lookback_range",
        lambda self, account: (_ for _ in ()).throw(sqlite3.OperationalError("locked")),
    )
    results = run_operational_sync(database)
    assert [r.error for r in results] == ["OperationalError", "OperationalError"]


class _FakeTimer:
    def __init__(self, delay, callback) -> None:
        self.daemon = False

    def start(self) -> None:
        pass

    def cancel(self) -> None:
        pass


class _Naver:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def run(self, **kwargs):
        if self.error:
            raise self.error
        return {"status": "SUCCESS", "fetched_count": 0, "failed_count": 0}


def _scheduler(database: Database, coupang, *, naver_error: Exception | None = None):
    NaverSyncRepository(database).save_auto_settings(enabled=True, interval_minutes=10)
    return NaverAutoSyncScheduler(
        database, service_factory=lambda db: _Naver(naver_error),
        timer_factory=_FakeTimer, owner_id="owner",
        now=lambda: datetime(2026, 9, 17, 3, 0, tzinfo=UTC),
        coupang_sync=coupang,
    )


@pytest.mark.parametrize("naver_error", [None, RuntimeError("naver down")])
def test_the_naver_cycle_runs_coupang_once_whatever_naver_did(database, naver_error) -> None:
    calls: list[Database] = []
    value = _scheduler(database, calls.append, naver_error=naver_error).run_once()
    assert calls == [database]
    assert value["status"] == ("SUCCESS" if naver_error is None else "FAILED")


def test_a_coupang_failure_leaves_the_naver_result_alone(database) -> None:
    def boom(db):
        raise RuntimeError("coupang exploded")

    value = _scheduler(database, boom).run_once()
    assert value["status"] == "SUCCESS"
    assert NaverSyncRepository(database).auto_state()["status"] == "SUCCESS"


def test_a_directly_built_scheduler_runs_no_coupang(database) -> None:
    assert NaverAutoSyncScheduler(database).coupang_sync is None


def test_the_production_scheduler_is_wired_to_the_coupang_sync(database, monkeypatch) -> None:
    import services.naver_auto_sync_scheduler as scheduler_module

    monkeypatch.setattr(scheduler_module, "_SCHEDULERS", {})
    monkeypatch.setenv("NAVER_SYNC_ENABLED", "false")
    scheduler = ensure_auto_sync_scheduler(database, timer_factory=_FakeTimer)
    assert scheduler.coupang_sync is run_coupang_inquiry_sync


# --- 13 dashboard and the read-only gate -------------------------------------

def test_the_dashboard_reads_a_new_row_and_keeps_it_read_only(database) -> None:
    service(database, {"OJE_PLUS": Transport(recent([inquiry("42")]))}).sync_account("OJE_PLUS")
    repository = InquiryRepository(database)

    assert "COUPANG_OJE_PLUS" in repository.dashboard_store_codes()
    listed, total, _ = repository.dashboard_page(
        store_codes=["COUPANG_OJE_PLUS"], source="ALL", queues=[], priorities=[],
        answer_status="ALL", delivery_only=False, search_query="",
        start_date="2026-09-01", end_date="2026-09-30",
        kpi_filter=None, page=1, page_size=30,
    )
    assert total == 1
    assert str(listed[0].get("source_question_id") or listed[0].get("inquiry_id")) == "42"

    from ui.review_workspace import _is_read_only_inquiry
    assert _is_read_only_inquiry(by_source(database, "COUPANG_OJE_PLUS", "42")) is True


# --- 14, 15 nothing but inquiries ----------------------------------------------

def test_a_cycle_writes_no_table_but_inquiries(database) -> None:
    service(database, {"OJE_NS": Transport(recent([inquiry("1")]))}).sync_account("OJE_NS")
    before = snapshot(database, skip=("inquiries",))
    transports = {
        "OJE_NS": Transport(recent([inquiry("1", answered=True), inquiry("2")])),
        "OJE_PLUS": Transport(recent([inquiry("3", answered=True)])),
    }

    results = service(database, transports).sync_accounts()

    assert sum(r.new for r in results) == 2 and sum(r.updated for r in results) == 1
    after = snapshot(database, skip=("inquiries",))
    # SQLite's own AUTOINCREMENT counter for inquiries moves with an INSERT;
    # that entry, and only that entry, may differ.
    for state in (before, after):
        state["sqlite_sequence"] = [r for r in state["sqlite_sequence"] if r[0] != "inquiries"]
    # historical_cases, Learning, answer_drafts, workflow_steps,
    # naver_posted_answers, activity_logs, auto_sync_events ...
    assert after == before


def test_no_production_path_is_entered(database, monkeypatch) -> None:
    from answer.answer_validator import AnswerValidator
    import kakao_notify
    from repositories.historical_case_repository import HistoricalCaseRepository
    from repositories.naver_posted_answer_repository import NaverPostedAnswerRepository
    from repositories.workflow_repository import WorkflowRepository
    from services.answer_service import AnswerService
    from services.automatic_draft_service import AutomaticDraftService
    from services.dps_enrichment_service import DpsEnrichmentService
    from services.inquiry_sync_service import InquirySyncService
    from services.learning_service import LearningService

    entered: list[str] = []

    def trap(name: str):
        def called(*args, **kwargs):
            entered.append(name)
            raise AssertionError(name)
        return called

    for owner, attribute in (
        (InquirySyncService, "sync"),
        (AutomaticDraftService, "ensure_for_inquiry"),
        (NaverPostedAnswerRepository, "observe"),
        (WorkflowRepository, "initialize_steps"),
        (HistoricalCaseRepository, "upsert"),
        (LearningService, "__init__"),
        (AnswerService, "__init__"),
        (AnswerValidator, "__init__"),
        (DpsEnrichmentService, "__init__"),
        (kakao_notify, "notify_qna_safely"),
    ):
        monkeypatch.setattr(owner, attribute, trap(f"{owner}.{attribute}"))

    transports = {"OJE_NS": Transport(recent([inquiry("1", answered=True)])),
                  "OJE_PLUS": Transport(recent([inquiry("2")]))}
    results = service(database, transports).sync_accounts()

    assert entered == []
    assert [r.failed for r in results] == [0, 0]
    assert [r.new for r in results] == [1, 1]
    assert all(c["method"] == "GET" for t in transports.values() for c in t.calls)


def test_the_service_imports_only_the_read_path() -> None:
    source = Path(sync_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert {m for m in imported if m.split(".")[0] in {"api", "config", "repositories", "services"}} == {
        "api.coupang_read_client", "config", "repositories.database",
        "repositories.inquiry_repository", "services.coupang_inquiry_normalizer",
        "services.inquiry_sync_service",
    }
    names = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert (names | used).isdisjoint({
        "InquirySyncService", "sync", "list_contact_center_inquiries", "contact_center",
        "AutomaticDraftService", "AnswerService", "LearningService", "HistoricalCaseService",
        "HistoricalCaseRepository", "NaverPostedAnswerRepository", "observe",
        "WorkflowRepository", "LogRepository", "notify_qna_safely", "post",
    })
