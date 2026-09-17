"""Re-read stored Coupang Online Inquiries and refresh their source fields, once.

7,277 Coupang inquiries were written by a backfill that skipped
``normalize_work_item``: ``source_answered`` is NULL and ``raw_json`` is ``{}``,
so an answered inquiry reads as unanswered and nothing joins the product
catalogue.  This re-reads them and lets the ordinary path store them again:

    CoupangReadClient.list_online_inquiries(answered_type="ANSWERED")
      -> CoupangInquiryNormalizer.online(payload, account_code=...)
      -> to_work_item() -> normalize_work_item()
      -> InquiryRepository.upsert_work_item()     (existing rows only)

It repairs rows; it does not collect.  An inquiry with no stored row is counted
as ``skipped_missing`` and never inserted, with or without ``--apply``.

What the repository does not update stays as it is: the row id,
workflow/answer/post/approval status, and everything keyed on the id --
drafts, Learning, Historical Cases.  Nothing here reaches InquirySyncService,
the Historical backfill, AnswerService, drafts, DPS, validation, posting or
Kakao; none of them is imported.

Without ``--apply`` it reads Coupang and the database and writes nothing.

    python scripts/resync_coupang_inquiry_source_fields.py --account OJE_NS
    python scripts/resync_coupang_inquiry_source_fields.py --account ALL --apply
    python scripts/resync_coupang_inquiry_source_fields.py --account OJE_PLUS
        --start 2025-10-19 --end 2025-10-25 --apply
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, fields
from datetime import date, timedelta
import sys
from pathlib import Path
from typing import Any, Callable, Iterator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.coupang_read_client import CoupangReadClient, CoupangReadError
from config import COUPANG_OJE_NS, COUPANG_OJE_PLUS, get_coupang_account
from repositories.database import Database, get_database_path
from repositories.inquiry_repository import SOURCE_OWNED_FIELDS, InquiryRepository
from services.coupang_inquiry_normalizer import (
    CoupangInquiryNormalizer,
    coupang_store_code,
)
from services.inquiry_sync_service import normalize_work_item


# What the server holds, per account (READ-ONLY check, 2026-09-17).
DEFAULT_RANGES: dict[str, tuple[date, date]] = {
    COUPANG_OJE_NS: (date(2024, 9, 1), date(2026, 9, 16)),
    COUPANG_OJE_PLUS: (date(2025, 10, 19), date(2026, 9, 8)),
}
ACCOUNTS: tuple[str, ...] = (COUPANG_OJE_NS, COUPANG_OJE_PLUS)

# Start and end at most six days apart: seven calendar days.  The client's own
# check lets seven apart through, and Coupang answers that with HTTP 400.
WINDOW_SPAN_DAYS = 6
PAGE_SIZE = 50


def date_windows(start: date, end: date) -> Iterator[tuple[date, date]]:
    if end < start:
        raise ValueError("end must not be earlier than start")
    cursor = start
    while cursor <= end:
        window_end = min(cursor + timedelta(days=WINDOW_SPAN_DAYS), end)
        yield cursor, window_end
        cursor = window_end + timedelta(days=1)


class WindowReadError(RuntimeError):
    """A request for a whole window failed; the run stops here."""

    def __init__(
        self, account_code: str, start: date, end: date, page: int, cause: Exception
    ) -> None:
        self.cause = cause
        detail = (
            f"{cause.code} status={cause.status_code} endpoint={cause.endpoint}"
            if isinstance(cause, CoupangReadError)
            else cause.__class__.__name__
        )
        super().__init__(
            f"{account_code} {start}~{end} page={page}: {cause} ({detail})"
        )


@dataclass
class ResyncCounts:
    http_requests: int = 0
    pages: int = 0
    fetched: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped_missing: int = 0
    failed: int = 0

    def add(self, other: "ResyncCounts") -> None:
        for field in fields(self):
            setattr(
                self, field.name,
                getattr(self, field.name) + getattr(other, field.name),
            )


@dataclass
class AccountResult:
    account_code: str
    start: date
    end: date
    apply: bool
    counts: ResyncCounts


def prepare(payload: dict[str, Any], account_code: str) -> dict[str, Any]:
    """The exact row the live path would store for this payload."""

    work_item = CoupangInquiryNormalizer().online(
        payload, account_code=account_code
    ).to_work_item()
    return normalize_work_item(work_item)


def _existing_row(database: Database, ready: dict[str, Any]) -> Any:
    # A plain read of the identity.  ``get_by_source`` would also run the
    # dashboard's catalogue enrichment for every item, which is not needed here.
    with database.connection() as connection:
        return connection.execute(
            "SELECT * FROM inquiries WHERE store_code = ? AND source_type = ? "
            "AND source_question_id = ?",
            (ready["store_code"], ready["source_type"], ready["source_question_id"]),
        ).fetchone()


def _would_change(existing: Any, ready: dict[str, Any]) -> bool:
    """The repository's own updated/unchanged test, without writing.

    Mirrors ``upsert_work_item``: an omitted order identifier keeps the stored
    one, then any difference in a source-owned field is an update.
    """

    candidate = dict(ready)
    for field in ("order_id", "product_order_id"):
        if candidate.get(field) in (None, ""):
            candidate[field] = existing[field]
    return any(
        existing[field] != candidate.get(field) for field in SOURCE_OWNED_FIELDS
    )


def _page(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("Coupang response is missing data object")
    content = data.get("content")
    pagination = data.get("pagination")
    if not isinstance(content, list) or not isinstance(pagination, dict):
        raise ValueError("Coupang response pagination is invalid")
    total_pages = int(pagination.get("totalPages") or 1)
    if total_pages < 1:
        raise ValueError("Coupang response totalPages is invalid")
    return [item for item in content if isinstance(item, dict)], total_pages


def _resync_item(
    database: Database,
    repository: InquiryRepository,
    account_code: str,
    payload: dict[str, Any],
    counts: ResyncCounts,
    apply: bool,
) -> None:
    counts.fetched += 1
    try:
        ready = prepare(payload, account_code)
        if ready["store_code"] != coupang_store_code(account_code):
            raise ValueError("normalized store_code does not match the account")
        existing = _existing_row(database, ready)
        if existing is None:
            # Repair, not collection: a row that is not stored stays unstored.
            counts.skipped_missing += 1
            return
        if not apply:
            if _would_change(existing, ready):
                counts.updated += 1
            else:
                counts.unchanged += 1
            return
        outcome = repository.upsert_work_item(ready)
        if outcome.inquiry_id != int(existing["id"]):
            raise RuntimeError("upsert resolved to a different inquiry id")
        if outcome.outcome == "updated":
            counts.updated += 1
        elif outcome.outcome == "unchanged":
            counts.unchanged += 1
        else:
            raise RuntimeError(f"unexpected upsert outcome: {outcome.outcome}")
    except Exception:
        counts.failed += 1


def resync_account(
    database: Database,
    client: CoupangReadClient,
    account_code: str,
    start: date,
    end: date,
    *,
    apply: bool,
    out: Callable[[str], None] = print,
    totals: ResyncCounts | None = None,
) -> AccountResult:
    """Walk one account's windows.  A failed request raises WindowReadError.

    ``totals`` is filled as the run goes, so a caller still has the partial
    tally when a window fails half-way.
    """

    repository = InquiryRepository(database)
    counts = totals if totals is not None else ResyncCounts()
    verb = "updated" if apply else "would_update"
    for window_start, window_end in date_windows(start, end):
        window = ResyncCounts()
        page = 1
        while True:
            window.http_requests += 1
            try:
                payload = client.list_online_inquiries(
                    inquiry_start_at=window_start,
                    inquiry_end_at=window_end,
                    answered_type="ANSWERED",
                    page_num=page,
                    page_size=PAGE_SIZE,
                )
                content, total_pages = _page(payload)
            except (CoupangReadError, ValueError) as error:
                counts.add(window)
                raise WindowReadError(
                    account_code, window_start, window_end, page, error
                ) from error
            window.pages += 1
            for item in content:
                _resync_item(database, repository, account_code, item, window, apply)
            if page >= total_pages:
                break
            page += 1
        counts.add(window)
        out(
            f"{account_code} {window_start}~{window_end} pages={window.pages} "
            f"fetched={window.fetched} {verb}={window.updated} "
            f"unchanged={window.unchanged} skipped_missing={window.skipped_missing} "
            f"failed={window.failed}"
        )
    return AccountResult(account_code, start, end, apply, counts)


def summary_lines(result: AccountResult) -> list[str]:
    counts = result.counts
    label = "UPDATED" if result.apply else "WOULD_UPDATE"
    return [
        f"ACCOUNT          {result.account_code}",
        f"MODE             {'APPLY' if result.apply else 'DRY_RUN'}",
        f"DATE_RANGE       {result.start} ~ {result.end}",
        f"HTTP_REQUESTS    {counts.http_requests}",
        f"FETCHED          {counts.fetched}",
        f"{label:<16} {counts.updated}",
        f"UNCHANGED        {counts.unchanged}",
        f"SKIPPED_MISSING  {counts.skipped_missing}",
        f"FAILED           {counts.failed}",
    ]


def _client_for(account_code: str) -> CoupangReadClient:
    account = get_coupang_account(account_code)
    return CoupangReadClient(
        access_key=account.access_key,
        secret_key=account.secret_key,
        vendor_id=account.vendor_id,
    )


def run(
    argv: list[str] | None = None,
    *,
    client_factory: Callable[[str], CoupangReadClient] = _client_for,
    out: Callable[[str], None] = print,
    err: Callable[[str], None] = lambda line: print(line, file=sys.stderr),
) -> int:
    parser = argparse.ArgumentParser(
        description="Refresh source fields of stored Coupang Online Inquiries."
    )
    parser.add_argument(
        "--account", required=True, choices=(*ACCOUNTS, "ALL"),
        help="OJE_NS, OJE_PLUS 또는 ALL",
    )
    parser.add_argument(
        "--start", type=date.fromisoformat, default=None,
        help="YYYY-MM-DD (기본: 계정별 저장 범위)",
    )
    parser.add_argument(
        "--end", type=date.fromisoformat, default=None,
        help="YYYY-MM-DD (기본: 계정별 저장 범위)",
    )
    parser.add_argument(
        "--database", type=Path, default=None,
        help="DB 경로 (기본: 운영 설정 경로)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="기존 행을 실제로 갱신한다. 없으면 조회만 하고 아무것도 쓰지 않는다.",
    )
    args = parser.parse_args(argv)

    database_path = get_database_path(args.database)
    if not database_path.exists():
        err(f"DB를 찾을 수 없습니다: {database_path}")
        return 2
    # No initialize(): that runs migrations, and a dry run writes nothing.
    database = Database(database_path)

    accounts = ACCOUNTS if args.account == "ALL" else (args.account,)
    out(f"DB {database_path} / {'APPLY' if args.apply else 'DRY_RUN'}")
    for account_code in accounts:
        start = args.start or DEFAULT_RANGES[account_code][0]
        end = args.end or DEFAULT_RANGES[account_code][1]
        if end < start:
            err(f"{account_code}: --end가 --start보다 빠릅니다 ({start} ~ {end})")
            return 2
        counts = ResyncCounts()
        try:
            result = resync_account(
                database, client_factory(account_code), account_code, start, end,
                apply=args.apply, out=out, totals=counts,
            )
        except (WindowReadError, CoupangReadError, ValueError) as error:
            err(f"ERROR {error}")
            for line in summary_lines(
                AccountResult(account_code, start, end, args.apply, counts)
            ):
                err(line)
            err(
                "중단했습니다. 같은 명령으로 다시 실행하면 "
                "이미 반영된 행은 UNCHANGED로 지나갑니다."
            )
            return 1
        out("")
        for line in summary_lines(result):
            out(line)
        out("")
    if not args.apply:
        out("DRY RUN - DB에 아무것도 쓰지 않았습니다. 실제 반영은 --apply 를 붙이세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
