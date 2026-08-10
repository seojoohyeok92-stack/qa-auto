from __future__ import annotations

import argparse
import ctypes
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from urllib.request import urlopen

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

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
from streamlit.testing.v1 import AppTest
from ui.dps_presenter import build_dps_display
from ui.review_workspace import program_answer_widget_key


def _masked(value: str) -> str:
    text = str(value or "").strip()
    return (
        f"{text[:4]}****{text[-4:]}"
        if len(text) > 8
        else "<masked-order>"
    )


def _process_alive(pid: int) -> bool:
    process = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
    if not process:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(
            ctypes.windll.kernel32.GetExitCodeProcess(
                process, ctypes.byref(code)
            )
            and code.value == 259
        )
    finally:
        ctypes.windll.kernel32.CloseHandle(process)


def _streamlit_health() -> bool:
    try:
        with urlopen(
            "http://127.0.0.1:8501/_stcore/health", timeout=5
        ) as response:
            return response.status == 200 and response.read().strip() == b"ok"
    except Exception:
        return False


def _rendered(database: Database, inquiry_id: int, draft: dict) -> dict:
    os.environ["PHASE86_INQUIRY_ID"] = str(inquiry_id)
    os.environ["PHASE86_PANEL"] = "answer"
    os.environ.pop("PHASE86_FAKE_ANSWER", None)
    app = AppTest.from_file(
        str(PROJECT_ROOT / "uat" / "phase86_streamlit_probe.py")
    ).run(timeout=30)
    if app.exception:
        return {
            "rendered_draft_id": None,
            "ui_exception": app.exception[0].message,
        }
    if app.segmented_control:
        app.segmented_control[0].set_value("Program Answer")
        app.run(timeout=30)
    expected_key = program_answer_widget_key(inquiry_id, draft["id"])
    widgets = [
        area
        for area in app.text_area
        if area.label == "Program Answer" and area.key == expected_key
    ]
    answer = str(widgets[0].value or "") if widgets else ""
    return {
        "rendered_draft_id": (
            int(draft["id"])
            if answer == str(draft.get("original_answer") or "")
            else None
        ),
        "widget_key": expected_key,
        "answer_rendered": bool(answer),
        "ui_exception": None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("dps", "gpt", "combined"), required=True)
    parser.add_argument("--inquiry-id", type=int, required=True)
    parser.add_argument("--case", type=int, required=True)
    parser.add_argument("--streamlit-pid", type=int, required=True)
    arguments = parser.parse_args()

    database = Database()
    database.initialize()
    inquiry = InquiryRepository(database).get(arguments.inquiry_id)
    if inquiry is None:
        raise SystemExit("INQUIRY_NOT_FOUND")
    order_id = str(inquiry.get("order_id") or "")
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    streamlit_before = _process_alive(arguments.streamlit_pid)
    health_before = _streamlit_health()
    agent_before = get_dps_agent_status()
    previous_draft = AnswerRepository(database).active_for_inquiry(
        arguments.inquiry_id
    )
    dps_report = {}
    gpt_report = {}

    if arguments.mode in {"dps", "combined"}:
        outcome = DpsLookupOrchestrator(database).lookup(
            arguments.inquiry_id,
            force_refresh=True,
            correlation_id=f"phase86-{arguments.mode}-{arguments.case}",
        )
        row = DpsRepository(
            database
        ).get_latest_success_by_inquiry_and_order(
            arguments.inquiry_id, order_id
        )
        if row is None:
            raise SystemExit("DPS_SUCCESS_NOT_PERSISTED")
        normalized = row["normalized_result_json"]
        detail = (
            row.get("raw_result_json") or {}
        ).get("detail_lookup") or {}
        display = build_dps_display(
            lookup_required=True,
            order_id=order_id,
            latest_row=row,
        )
        dps_report = {
            "dps_lookup_id": row["id"],
            "lookup_status": outcome.metadata.get("lookup_status"),
            "detail_opened": bool(detail.get("opened")),
            "detail_parsed": bool(detail.get("parsed")),
            "detail_closed": bool(detail.get("closed")),
            "detail_hwnd": detail.get("detail_hwnd"),
            "purchase_hwnd": detail.get("purchase_hwnd"),
            "db_saved": bool(row["id"]),
            "ui_refreshed": bool(
                display.get("installation_date_value")
                == row.get("installation_date")
            ),
            "installation_date": row.get("installation_date"),
        }

    if arguments.mode in {"gpt", "combined"}:
        generation = AnswerService(database).generate_for_inquiry(
            arguments.inquiry_id
        )
        active = AnswerRepository(database).active_for_inquiry(
            arguments.inquiry_id
        )
        if active is None:
            raise SystemExit("ACTIVE_DRAFT_NOT_FOUND")
        run = GptProviderRunRepository(database).latest_for_draft(
            int(generation.draft["id"])
        )
        rendered = _rendered(database, arguments.inquiry_id, active)
        gpt_report = {
            "provider_run_id": run.get("id") if run else None,
            "draft_id": generation.draft.get("id"),
            "active_draft_id": active.get("id"),
            "rendered_draft_id": rendered.get("rendered_draft_id"),
            "validator_passed": (
                run.get("validator_passed") if run else None
            ),
            "provider": run.get("provider") if run else None,
            "model": run.get("model") if run else None,
            "duration_ms": run.get("duration_ms") if run else None,
            "estimated_cost_krw": (
                run.get("estimated_cost_krw") if run else None
            ),
            "program_answer_changed": bool(
                active.get("original_answer")
                and (
                    previous_draft is None
                    or int(previous_draft["id"]) != int(active["id"])
                )
            ),
            "metadata_matches": bool(
                run
                and int(run.get("draft_id") or 0)
                == int(generation.draft["id"])
                == int(active["id"])
            ),
            "widget_key": rendered.get("widget_key"),
            "ui_exception": rendered.get("ui_exception"),
        }

    agent_after = get_dps_agent_status()
    ended_at = datetime.now().astimezone().isoformat(timespec="seconds")
    report = {
        "case": arguments.case,
        "mode": arguments.mode,
        "inquiry_id": arguments.inquiry_id,
        "masked_order_id": _masked(order_id),
        "started_at": started_at,
        "ended_at": ended_at,
        "streamlit_pid_before": arguments.streamlit_pid,
        "streamlit_pid_after": arguments.streamlit_pid,
        "streamlit_alive_before": streamlit_before,
        "streamlit_alive_after": _process_alive(arguments.streamlit_pid),
        "streamlit_health_before": health_before,
        "streamlit_health_after": _streamlit_health(),
        "agent_pid_before": agent_before.get("agent_pid"),
        "agent_pid_after": agent_after.get("agent_pid"),
        "agent_restore_warning": agent_after.get(
            "last_window_restore_warning"
        ),
        "dps": dps_report,
        "gpt": gpt_report,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
