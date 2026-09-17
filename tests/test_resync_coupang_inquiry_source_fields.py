"""The one-off repair of Coupang inquiries stored without their source fields.

The broken rows are reproduced the way they were made -- the old backfill
handed ``to_work_item()`` straight to the repository, skipping
``normalize_work_item`` -- and the script is run against a fake transport
behind the real ``CoupangReadClient``.  No request leaves the process.

What is pinned: the repair only updates rows that already exist, only through
``upsert_work_item``, never under another account's identity, and no table but
``inquiries`` changes.
"""

from __future__ import annotations

import ast
from datetime import date
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from api.coupang_read_client import CoupangReadClient
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.coupang_inquiry_normalizer import CoupangInquiryNormalizer

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "resync_coupang_inquiry_source_fields.py"
_spec = importlib.util.spec_from_file_location("resync_coupang_inquiry_source_fields", SCRIPT)
resync = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = resync  # dataclasses look the module up by name
_spec.loader.exec_module(resync)  # type: ignore[union-attr]

SOURCE_TYPE = "COUPANG_ONLINE_INQUIRY"
REPLY = "안녕하세요 고객님 삼성서비스센터로 연락주셔야 할 부분입니다."


def payload(inquiry_id: str, *, spid: str = "15654321531", vendor_item: str = "93128932886") -> dict:
    """The shape of 160668556 as Coupang returned it."""

    return {
        "inquiryId": inquiry_id,
        "sellerProductId": spid,
        "vendorItemId": vendor_item,
        "content": "와이파이가안됩니다",
        "inquiryAt": "2026-09-16T11:00:00+09:00",
        "orderIds": [],
        "commentDtoList": [{
            "inquiryCommentId": f"c-{inquiry_id}",
            "inquiryId": inquiry_id,
            "content": REPLY,
            "inquiryCommentAt": "2026-09-16T11:23:00+09:00",
        }],
    }


# --- fixtures -------------------------------------------------------------

@pytest.fixture
def database(tmp_path) -> Database:
    db = Database(tmp_path / "resync.db")
    db.initialize()
    return db


def store_legacy(database: Database, item: dict, account: str) -> int:
    """Write a row exactly as the pre-fix backfill did: no normalize_work_item."""

    work_item = CoupangInquiryNormalizer().online(item, account_code=account).to_work_item()
    return InquiryRepository(database).upsert_work_item(work_item).inquiry_id


def set_local_state(database: Database, inquiry_id: int) -> None:
    with database.transaction() as connection:
        connection.execute(
            "UPDATE inquiries SET workflow_status='REVIEW_PENDING', "
            "answer_status='UNANSWERED', post_status='NOT_POSTED', "
            "approval_status='APPROVED' WHERE id=?",
            (inquiry_id,),
        )
        connection.execute(
            "INSERT INTO answer_drafts (inquiry_id, original_answer) VALUES (?, ?)",
            (inquiry_id, "기존 draft"),
        )


def row(database: Database, inquiry_id: int) -> dict:
    with database.connection() as connection:
        found = connection.execute("SELECT * FROM inquiries WHERE id=?", (inquiry_id,)).fetchone()
    return dict(found)


def count_inquiries(database: Database) -> int:
    with database.connection() as connection:
        return connection.execute("SELECT COUNT(*) FROM inquiries").fetchone()[0]


def snapshot(database: Database, *, skip: tuple[str, ...] = ()) -> dict[str, list]:
    """Every row of every table, for a write-nothing comparison."""

    connection = sqlite3.connect(str(database.path))
    try:
        tables = [
            name for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            if name not in skip
        ]
        return {
            table: sorted(
                (tuple(r) for r in connection.execute(f'SELECT * FROM "{table}"')),
                key=repr,
            )
            for table in tables
        }
    finally:
        connection.close()


class Response:
    def __init__(self, status_code: int, body: Any = None) -> None:
        self.status_code = status_code
        self.body = body

    def json(self) -> Any:
        return self.body


class Transport:
    """Serves pages by (window start, page), and records every request."""

    def __init__(self, pages: dict[tuple[str, int], tuple[list[dict], int]] | None = None,
                 *, status: int = 200) -> None:
        self.pages = pages or {}
        self.status = status
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> Response:
        query = {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}
        self.calls.append({"method": method, "url": url, "query": query})
        if self.status != 200:
            return Response(self.status, {})
        content, total_pages = self.pages.get(
            (query["inquiryStartAt"], int(query["pageNum"])), ([], 1)
        )
        return Response(200, {
            "code": 200,
            "data": {"content": content, "pagination": {"totalPages": total_pages}},
        })


def factory(transports: dict[str, Transport]):
    def make(account_code: str) -> CoupangReadClient:
        return CoupangReadClient(
            access_key="test-access", secret_key="test-secret", vendor_id="A00000000",
            transport=transports[account_code], min_request_interval_seconds=0,
            max_retries=0,
        )
    return make


def run(database: Database, transports: dict[str, Transport], *args: str) -> tuple[int, list[str], list[str]]:
    out: list[str] = []
    err: list[str] = []
    code = resync.run(
        ["--database", str(database.path), *args],
        client_factory=factory(transports), out=out.append, err=err.append,
    )
    return code, out, err


def summary(lines: list[str], label: str) -> int:
    return int(next(line for line in lines if line.startswith(label + " ")).split()[-1])


ONE_WINDOW = ("--start", "2026-09-10", "--end", "2026-09-16")


# --- 1-3 identity and windows ---------------------------------------------

@pytest.mark.parametrize("account,store", [
    ("OJE_NS", "COUPANG_OJE_NS"), ("OJE_PLUS", "COUPANG_OJE_PLUS"),
])
def test_normalized_identity_keeps_the_account(account, store) -> None:
    ready = resync.prepare(payload("160668556"), account)
    assert (ready["store_code"], ready["source_type"], ready["source_question_id"]) == (
        store, SOURCE_TYPE, "160668556",
    )
    assert ready["source_metadata_json"]["account_code"] == account


@pytest.mark.parametrize("account", ["OJE_NS", "OJE_PLUS"])
def test_windows_are_at_most_six_days_apart_and_cover_the_range(account) -> None:
    start, end = resync.DEFAULT_RANGES[account]
    windows = list(resync.date_windows(start, end))
    assert windows[0][0] == start and windows[-1][1] == end
    for (window_start, window_end), following in zip(windows, windows[1:] + [None]):
        assert 0 <= (window_end - window_start).days <= 6
        if following is not None:
            assert (following[0] - window_end).days == 1


def test_default_ranges_are_the_stored_ones() -> None:
    assert resync.DEFAULT_RANGES == {
        "OJE_NS": (date(2024, 9, 1), date(2026, 9, 16)),
        "OJE_PLUS": (date(2025, 10, 19), date(2026, 9, 8)),
    }


def test_requests_sent_are_answered_online_inquiries_within_seven_days(database) -> None:
    transport = Transport()
    code, _, _ = run(database, {"OJE_PLUS": transport}, "--account", "OJE_PLUS",
                     "--start", "2026-08-01", "--end", "2026-08-20")
    assert code == 0
    assert len(transport.calls) == 3
    for call in transport.calls:
        assert call["method"] == "GET"
        assert "/onlineInquiries" in call["url"]
        assert call["query"]["answeredType"] == "ANSWERED"
        span = date.fromisoformat(call["query"]["inquiryEndAt"]) - date.fromisoformat(
            call["query"]["inquiryStartAt"])
        assert span.days <= 6


# --- 4-6 dry run and missing rows ------------------------------------------

def test_dry_run_writes_nothing(database) -> None:
    inquiry_id = store_legacy(database, payload("160668556"), "OJE_NS")
    set_local_state(database, inquiry_id)
    before = snapshot(database)
    transport = Transport({("2026-09-10", 1): ([payload("160668556"), payload("999")], 1)})

    code, out, _ = run(database, {"OJE_NS": transport}, "--account", "OJE_NS", *ONE_WINDOW)

    assert code == 0
    assert snapshot(database) == before
    assert summary(out, "WOULD_UPDATE") == 1
    assert summary(out, "SKIPPED_MISSING") == 1
    assert summary(out, "FETCHED") == 2
    assert any("DRY RUN" in line for line in out)


def test_missing_identity_is_skipped_even_with_apply(database) -> None:
    store_legacy(database, payload("160668556"), "OJE_NS")
    transport = Transport({("2026-09-10", 1): ([payload("160000001"), payload("160000002")], 1)})

    code, out, _ = run(database, {"OJE_NS": transport}, "--account", "OJE_NS", *ONE_WINDOW, "--apply")

    assert code == 0
    assert count_inquiries(database) == 1
    assert summary(out, "SKIPPED_MISSING") == 2
    assert summary(out, "UPDATED") == 0


# --- 7-12, 20 apply on the 160668556-shaped row -----------------------------

def test_the_legacy_fixture_matches_the_server_state(database) -> None:
    stored = row(database, store_legacy(database, payload("160668556"), "OJE_NS"))
    assert stored["source_answered"] is None
    assert json.loads(stored["raw_json"]) == {}
    assert json.loads(stored["source_metadata_json"])["account_code"] == "OJE_NS"


def test_apply_updates_the_same_row_and_keeps_local_state(database) -> None:
    inquiry_id = store_legacy(database, payload("160668556"), "OJE_NS")
    set_local_state(database, inquiry_id)
    transport = Transport({("2026-09-10", 1): ([payload("160668556")], 1)})

    code, out, _ = run(database, {"OJE_NS": transport}, "--account", "OJE_NS", *ONE_WINDOW, "--apply")

    assert code == 0
    assert summary(out, "UPDATED") == 1
    assert count_inquiries(database) == 1
    after = row(database, inquiry_id)
    assert after["id"] == inquiry_id
    assert after["store_code"] == "COUPANG_OJE_NS"
    assert after["workflow_status"] == "REVIEW_PENDING"
    assert after["answer_status"] == "UNANSWERED"
    assert after["approval_status"] == "APPROVED"
    assert after["post_status"] == "NOT_POSTED"

    assert after["source_answered"] == 1
    raw = json.loads(after["raw_json"])
    assert raw["sellerProductId"] == "15654321531"
    assert raw["vendorItemId"] == "93128932886"
    assert raw["commentDtoList"][0]["content"] == REPLY
    metadata = json.loads(after["source_metadata_json"])
    assert metadata["account_code"] == "OJE_NS"
    assert metadata["market"] == "COUPANG"
    assert metadata["seller_answer_selection"] == "SINGLE_COMMENT"

    with database.connection() as connection:
        drafts = connection.execute(
            "SELECT inquiry_id, original_answer FROM answer_drafts"
        ).fetchall()
    assert [tuple(d) for d in drafts] == [(inquiry_id, "기존 draft")]

    detail = InquiryRepository(database).get_by_source("COUPANG_OJE_NS", SOURCE_TYPE, "160668556")
    assert detail["seller_answer"] == REPLY


def test_dry_run_predicts_what_apply_does(database) -> None:
    store_legacy(database, payload("160668556"), "OJE_NS")
    transport = Transport({("2026-09-10", 1): ([payload("160668556")], 1)})
    _, dry, _ = run(database, {"OJE_NS": transport}, "--account", "OJE_NS", *ONE_WINDOW)
    _, applied, _ = run(database, {"OJE_NS": transport}, "--account", "OJE_NS", *ONE_WINDOW, "--apply")
    _, dry_again, _ = run(database, {"OJE_NS": transport}, "--account", "OJE_NS", *ONE_WINDOW)

    assert summary(dry, "WOULD_UPDATE") == summary(applied, "UPDATED") == 1
    assert summary(dry_again, "WOULD_UPDATE") == 0
    assert summary(dry_again, "UNCHANGED") == 1


# --- 13 account isolation --------------------------------------------------

def test_another_accounts_row_is_never_borrowed(database) -> None:
    ns_id = store_legacy(database, payload("160668556"), "OJE_NS")
    before = row(database, ns_id)
    plus = Transport({("2026-09-10", 1): ([payload("160668556")], 1)})

    code, out, _ = run(database, {"OJE_PLUS": plus}, "--account", "OJE_PLUS", *ONE_WINDOW, "--apply")

    assert code == 0
    assert summary(out, "SKIPPED_MISSING") == 1
    assert count_inquiries(database) == 1
    assert row(database, ns_id) == before


def test_all_runs_each_account_under_its_own_identity(database) -> None:
    ns_id = store_legacy(database, payload("1001"), "OJE_NS")
    plus_id = store_legacy(database, payload("1001"), "OJE_PLUS")
    transports = {
        "OJE_NS": Transport({("2026-09-10", 1): ([payload("1001", spid="111")], 1)}),
        "OJE_PLUS": Transport({("2026-09-10", 1): ([payload("1001", spid="222")], 1)}),
    }

    code, _, _ = run(database, transports, "--account", "ALL", *ONE_WINDOW, "--apply")

    assert code == 0
    assert json.loads(row(database, ns_id)["raw_json"])["sellerProductId"] == "111"
    assert json.loads(row(database, plus_id)["raw_json"])["sellerProductId"] == "222"
    assert json.loads(row(database, plus_id)["source_metadata_json"])["account_code"] == "OJE_PLUS"


# --- 14-18 side effects ----------------------------------------------------

def test_apply_changes_no_table_but_inquiries(database) -> None:
    inquiry_id = store_legacy(database, payload("160668556"), "OJE_NS")
    set_local_state(database, inquiry_id)
    before = snapshot(database, skip=("inquiries",))
    transport = Transport({("2026-09-10", 1): ([payload("160668556"), payload("404")], 1)})

    code, _, _ = run(database, {"OJE_NS": transport}, "--account", "OJE_NS", *ONE_WINDOW, "--apply")

    assert code == 0
    # Historical cases, Learning, drafts, posted answers, workflow steps, logs.
    assert snapshot(database, skip=("inquiries",)) == before
    assert all(call["method"] == "GET" for call in transport.calls)


def test_no_production_path_is_entered(database, monkeypatch) -> None:
    from answer.answer_validator import AnswerValidator
    import kakao_notify
    from repositories.historical_case_repository import HistoricalCaseRepository
    from repositories.naver_posted_answer_repository import NaverPostedAnswerRepository
    from services.answer_service import AnswerService
    from services.automatic_draft_service import AutomaticDraftService
    from services.dps_enrichment_service import DpsEnrichmentService
    from services.inquiry_sync_service import InquirySyncService
    from services.learning_service import LearningService

    entered: list[str] = []

    def trap(name: str):
        def called(*args: Any, **kwargs: Any) -> None:
            entered.append(name)
            raise AssertionError(name)
        return called

    for owner, attribute in (
        (InquirySyncService, "sync"),
        (AutomaticDraftService, "ensure_for_inquiry"),
        (NaverPostedAnswerRepository, "observe"),
        (HistoricalCaseRepository, "upsert"),
        (LearningService, "__init__"),
        (AnswerService, "__init__"),
        (AnswerValidator, "__init__"),
        (DpsEnrichmentService, "__init__"),
        (kakao_notify, "notify_qna_safely"),
    ):
        monkeypatch.setattr(owner, attribute, trap(f"{getattr(owner, '__name__', owner)}.{attribute}"))

    store_legacy(database, payload("160668556"), "OJE_NS")
    transport = Transport({("2026-09-10", 1): ([payload("160668556")], 1)})
    code, out, _ = run(database, {"OJE_NS": transport}, "--account", "OJE_NS", *ONE_WINDOW, "--apply")

    assert code == 0
    assert entered == []
    assert summary(out, "FAILED") == 0
    assert summary(out, "UPDATED") == 1


def test_the_script_imports_only_the_read_path() -> None:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
        for alias in node.names
    }
    project = {name for name in imported if name.split(".")[0] in {
        "api", "config", "repositories", "services", "answer", "dps", "kakao_notify", "workflow",
    }}
    assert project == {
        "api.coupang_read_client",
        "config",
        "repositories.database",
        "repositories.inquiry_repository",
        "services.coupang_inquiry_normalizer",
        "services.inquiry_sync_service",
    }
    # Identifiers the code uses -- the docstring names these services on purpose.
    used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    } | {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert used.isdisjoint({
        "InquirySyncService", "sync", "CoupangInquirySyncService",
        "CoupangHistoricalInquiryBackfillService", "HistoricalCaseService",
        "HistoricalCaseRepository", "LearningService", "AnswerService",
        "AutomaticDraftService", "ensure_for_inquiry", "observe", "AnswerValidator",
        "DpsEnrichmentService", "list_contact_center_inquiries", "contact_center",
        "notify_qna_safely", "post",
    })
    for identifier in used:
        for fragment in ("Backfill", "Historical", "Learning", "Kakao", "kakao", "Post", "Dps"):
            assert fragment not in identifier, identifier
    called = {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "upsert_work_item" in called and "list_online_inquiries" in called


# --- 19 rerun --------------------------------------------------------------

def test_rerun_creates_no_duplicate_and_reports_unchanged(database) -> None:
    ids = [store_legacy(database, payload(str(n)), "OJE_NS") for n in (1, 2, 3)]
    transport = Transport({("2026-09-10", 1): ([payload(str(n)) for n in (1, 2, 3)], 1)})

    _, first, _ = run(database, {"OJE_NS": transport}, "--account", "OJE_NS", *ONE_WINDOW, "--apply")
    _, second, _ = run(database, {"OJE_NS": transport}, "--account", "OJE_NS", *ONE_WINDOW, "--apply")

    assert summary(first, "UPDATED") == 3
    assert summary(second, "UPDATED") == 0
    assert summary(second, "UNCHANGED") == 3
    assert count_inquiries(database) == 3
    with database.connection() as connection:
        assert sorted(r[0] for r in connection.execute("SELECT id FROM inquiries")) == sorted(ids)


# --- paging, failures, CLI --------------------------------------------------

def test_every_page_of_a_window_is_read(database) -> None:
    for n in (1, 2, 3):
        store_legacy(database, payload(str(n)), "OJE_NS")
    transport = Transport({
        ("2026-09-10", 1): ([payload("1"), payload("2")], 2),
        ("2026-09-10", 2): ([payload("3")], 2),
    })
    code, out, _ = run(database, {"OJE_NS": transport}, "--account", "OJE_NS", *ONE_WINDOW, "--apply")
    assert code == 0
    assert summary(out, "HTTP_REQUESTS") == 2
    assert summary(out, "UPDATED") == 3


def test_a_bad_item_is_counted_and_the_rest_continue(database) -> None:
    store_legacy(database, payload("1"), "OJE_NS")
    broken = payload("2")
    del broken["inquiryId"]
    transport = Transport({("2026-09-10", 1): ([broken, payload("1")], 1)})
    code, out, _ = run(database, {"OJE_NS": transport}, "--account", "OJE_NS", *ONE_WINDOW, "--apply")
    assert code == 0
    assert summary(out, "FAILED") == 1
    assert summary(out, "UPDATED") == 1


@pytest.mark.parametrize("status", [400, 401, 403])
def test_a_failed_window_stops_with_a_non_zero_exit(database, status) -> None:
    store_legacy(database, payload("1"), "OJE_NS")
    before = snapshot(database)
    transport = Transport(status=status)
    code, _, err = run(database, {"OJE_NS": transport}, "--account", "OJE_NS",
                       "--start", "2026-09-01", "--end", "2026-09-20", "--apply")
    assert code == 1
    assert len(transport.calls) == 1
    assert any(line.startswith("ERROR OJE_NS 2026-09-01~2026-09-07 page=1") for line in err)
    assert snapshot(database) == before


def test_all_stops_before_the_second_account_when_the_first_fails(database) -> None:
    transports = {"OJE_NS": Transport(status=401), "OJE_PLUS": Transport()}
    code, _, _ = run(database, transports, "--account", "ALL", *ONE_WINDOW)
    assert code == 1
    assert transports["OJE_PLUS"].calls == []


def test_account_is_required_and_limited(database) -> None:
    with pytest.raises(SystemExit):
        resync.run(["--database", str(database.path)], client_factory=factory({}))
    with pytest.raises(SystemExit):
        resync.run(["--database", str(database.path), "--account", "COUPANG"],
                   client_factory=factory({}))


def test_a_missing_database_is_not_created(tmp_path) -> None:
    target = tmp_path / "absent.db"
    err: list[str] = []
    code = resync.run(["--database", str(target), "--account", "OJE_NS"],
                      client_factory=factory({}), out=lambda _: None, err=err.append)
    assert code == 2
    assert not target.exists()
