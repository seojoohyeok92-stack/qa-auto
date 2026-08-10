from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import re
import sys

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

from answer.facts import build_answer_facts
from answer.models import AnswerResult, AnswerStatus
from answer.prompt_builder import PromptBuilder
from answer.source_adapter import answer_request_from_inquiry
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.dps_repository import DpsRepository
from repositories.gpt_provider_run_repository import (
    GptProviderRunRepository,
)
from repositories.inquiry_repository import InquiryRepository
from services.answer_service import AnswerService
from services.dps_agent_client import get_dps_agent_status
from services.dps_lookup_orchestrator import DpsLookupOrchestrator
from ui.dps_presenter import build_dps_display


SOURCE = "DPS_ITEM_DETAIL_REQUIRED_DELIVERY_DATE"
DATE_PATTERN = re.compile(
    r"(?<!\d)(20\d{2})\s*(?:년|[-./])\s*(\d{1,2})\s*"
    r"(?:월|[-./])\s*(\d{1,2})\s*(?:일)?(?!\d)"
)


def _eligible(database: Database) -> list[dict]:
    with database.connection() as connection:
        rows = connection.execute(
            """
            SELECT i.*
            FROM inquiries i
            WHERE i.order_id IS NOT NULL
              AND length(trim(i.order_id)) > 0
              AND i.order_date IS NOT NULL
              AND length(trim(i.order_date)) > 0
              AND upper(COALESCE(i.post_status, '')) <> 'POSTED'
              AND upper(COALESCE(i.approval_status, '')) <> 'APPROVED'
              AND NOT EXISTS (
                  SELECT 1 FROM answer_drafts a
                  WHERE a.inquiry_id = i.id
                    AND (
                        a.posted = 1
                        OR length(trim(COALESCE(a.edited_answer, ''))) > 0
                        OR length(trim(COALESCE(a.final_answer, ''))) > 0
                    )
              )
            ORDER BY i.updated_at DESC, i.id DESC
            """
        ).fetchall()
    distinct: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        value = dict(row)
        order_id = str(value.get("order_id") or "").strip()
        if order_id in seen:
            continue
        seen.add(order_id)
        distinct.append(value)
    return distinct


def _masked(value: str) -> str:
    text = str(value or "").strip()
    return (
        f"{text[:4]}****{text[-4:]}"
        if len(text) > 8
        else "<masked-order>"
    )


def _answer_dates(answer: str) -> list[str]:
    values: list[str] = []
    for match in DATE_PATTERN.finditer(answer):
        try:
            values.append(
                date(*(int(value) for value in match.groups())).isoformat()
            )
        except ValueError:
            continue
    return list(dict.fromkeys(values))


def _rule() -> AnswerResult:
    return AnswerResult(
        status=AnswerStatus.GENERATED,
        category="validation",
        reason="validation",
        answer="validation",
        provider="rules",
        auto_answerable=True,
        needs_review=False,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--case-index", type=int)
    parser.add_argument("--skip-gpt", action="store_true")
    arguments = parser.parse_args()

    database = Database()
    database.initialize()
    cases = _eligible(database)
    agent = get_dps_agent_status()
    if arguments.list:
        print(
            json.dumps(
                {
                    "agent_running": bool(agent.get("agent_running")),
                    "login_status": agent.get("login_status"),
                    "eligible_distinct_orders": len(cases),
                    "cases": [
                        {
                            "case_index": index,
                            "inquiry_id": value["id"],
                            "masked_order_id": _masked(
                                value["order_id"]
                            ),
                        }
                        for index, value in enumerate(cases[:10])
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if arguments.case_index is None:
        parser.error("--case-index is required unless --list is used")
    if not 0 <= arguments.case_index < len(cases):
        raise SystemExit("CASE_INDEX_OUT_OF_RANGE")

    inquiry = cases[arguments.case_index]
    inquiry_id = int(inquiry["id"])
    order_id = str(inquiry["order_id"])
    dps_outcome = DpsLookupOrchestrator(database).lookup(
        inquiry_id,
        force_refresh=True,
        correlation_id=f"phase8-5-case-{arguments.case_index}",
    )
    dps_row = DpsRepository(
        database
    ).get_latest_success_by_inquiry_and_order(inquiry_id, order_id)
    if dps_row is None:
        raise SystemExit("DPS_SUCCESS_NOT_PERSISTED")
    normalized = dict(dps_row["normalized_result_json"])
    display = build_dps_display(
        lookup_required=True,
        order_id=None,
        latest_row=dps_row,
    )
    request = answer_request_from_inquiry(
        InquiryRepository(database).get(inquiry_id)
    )
    request.metadata["dps"] = {
        **normalized,
        "dps_lookup_id": dps_row["id"],
        "lookup_timestamp": dps_row["queried_at"],
    }
    facts = build_answer_facts(request, _rule())
    prompt = json.loads(
        PromptBuilder().build(task="DRAFT", facts=facts)
    )

    draft = None
    provider_run = None
    if not arguments.skip_gpt:
        generation = AnswerService(database).generate_for_inquiry(
            inquiry_id
        )
        draft = AnswerRepository(database).active_for_inquiry(inquiry_id)
        if (
            draft is None
            or int(draft["id"]) != int(generation.draft["id"])
        ):
            raise SystemExit("GPT_DRAFT_NOT_ACTIVE")
        provider_run = GptProviderRunRepository(
            database
        ).latest_for_draft(int(draft["id"]))

    required = normalized.get("required_delivery_date")
    answer_dates = (
        _answer_dates(str(draft.get("original_answer") or ""))
        if draft
        else []
    )
    report = {
        "case_index": arguments.case_index,
        "inquiry_id": inquiry_id,
        "masked_order_id": _masked(order_id),
        "lookup_status": dps_outcome.metadata.get("lookup_status"),
        "dps_lookup_id": dps_row["id"],
        "dps_raw_required_delivery_date": normalized.get(
            "raw_required_delivery_date"
        ),
        "dps_required_delivery_date": required,
        "db_installation_date": dps_row.get("installation_date"),
        "dashboard_installation_date": display.get(
            "installation_date_value"
        ),
        "answer_facts_installation_date": facts.installation.get("date"),
        "prompt_installation_date": (
            prompt.get("confirmed_facts") or {}
        ).get("installation_date"),
        "gpt_answer_dates": answer_dates,
        "draft_id": draft.get("id") if draft else None,
        "draft_source": draft.get("source") if draft else None,
        "validation_status": (
            draft.get("validation_status") if draft else None
        ),
        "provider": (
            provider_run.get("provider") if provider_run else None
        ),
        "model": provider_run.get("model") if provider_run else None,
        "validator_passed": (
            provider_run.get("validator_passed")
            if provider_run
            else None
        ),
        "all_dates_match": bool(
            required
            and required == dps_row.get("installation_date")
            == display.get("installation_date_value")
            == facts.installation.get("date")
            == (prompt.get("confirmed_facts") or {}).get(
                "installation_date"
            )
            and (
                arguments.skip_gpt
                or required in answer_dates
            )
        ),
        "source_is_required_delivery_date": (
            normalized.get("installation_date_source") == SOURCE
        ),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
