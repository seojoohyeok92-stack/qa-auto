"""Promote the stored Historical Candidate backlog, once, by hand.

Run explicitly from a shell.  Nothing schedules this and the dashboard never
calls it: a Streamlit rerun that promoted a thousand answers would be a very
expensive accident.  Without ``--apply`` it only counts, so the usual order is
one plain run to read the numbers and a second with ``--apply``.

Every Learning row is written by the ordinary single-case path
(``HistoricalCaseService.promote`` -> ``LearningService.capture_historical_promotion``);
this script picks the cases and prints the tally.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from repositories.database import Database
from services.historical_case_service import HistoricalCaseService

COUPANG_HISTORY_SOURCE = "COUPANG_ONLINE_HISTORY"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bulk-promote Historical Candidates through the existing path"
    )
    parser.add_argument(
        "database", nargs="?", default="data/oje_automation.db", type=Path
    )
    parser.add_argument(
        "--actor", default="관리자", help="승격 기록에 남길 실행자"
    )
    parser.add_argument(
        "--source", default=COUPANG_HISTORY_SOURCE,
        help=f"historical_cases.source 필터 (기본 {COUPANG_HISTORY_SOURCE}, ALL이면 전체)",
    )
    parser.add_argument("--limit", type=int, default=None, help="처리할 최대 건수")
    parser.add_argument(
        "--apply", action="store_true",
        help="실제로 승격한다. 없으면 집계만 하고 아무것도 쓰지 않는다.",
    )
    args = parser.parse_args()

    if not args.database.exists():
        raise SystemExit(f"DB를 찾을 수 없습니다: {args.database}")

    service = HistoricalCaseService(Database(args.database))
    summary = service.bulk_promote_candidates(
        actor=str(args.actor),
        source=None if str(args.source).upper() == "ALL" else str(args.source),
        limit=args.limit,
        apply=bool(args.apply),
    )
    # Counts only.  Customer questions and seller answers stay out of stdout.
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    if not args.apply:
        print("\nDRY RUN — 아무것도 쓰지 않았습니다. 실제 승격은 --apply 를 붙이세요.")


if __name__ == "__main__":
    main()
