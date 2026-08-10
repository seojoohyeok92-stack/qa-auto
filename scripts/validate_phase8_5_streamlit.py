from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.dps_repository import DpsRepository
from repositories.inquiry_repository import InquiryRepository
from streamlit.testing.v1 import AppTest


DATE_PATTERN = re.compile(
    r"(?<!\d)(20\d{2})\s*(?:년|[-./])\s*(\d{1,2})\s*"
    r"(?:월|[-./])\s*(\d{1,2})\s*(?:일)?(?!\d)"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inquiry-id", type=int, required=True)
    arguments = parser.parse_args()
    os.environ["PHASE85_INQUIRY_ID"] = str(arguments.inquiry_id)

    database = Database()
    database.initialize()
    inquiry = InquiryRepository(database).get(arguments.inquiry_id)
    if inquiry is None:
        raise SystemExit("INQUIRY_NOT_FOUND")
    order_id = str(inquiry.get("order_id") or "")
    dps = DpsRepository(database).get_preferred_for_inquiry_and_order(
        arguments.inquiry_id, order_id
    )
    draft = AnswerRepository(database).active_for_inquiry(
        arguments.inquiry_id
    )
    app = AppTest.from_file(
        str(PROJECT_ROOT / "uat" / "phase85_streamlit_probe.py")
    )
    app.run(timeout=30)
    if app.exception:
        raise RuntimeError(str(app.exception[0].message))
    if app.segmented_control:
        app.segmented_control[0].set_value("Program Answer")
        app.run(timeout=30)
    program_areas = [
        area
        for area in app.text_area
        if area.label == "Program Answer"
    ]
    if not program_areas:
        raise SystemExit("PROGRAM_ANSWER_WIDGET_NOT_RENDERED")
    rendered_answer = str(program_areas[0].value or "")
    rendered_ui_text = "\n".join(
        str(element.value or "")
        for element in [
            *app.markdown,
            *app.caption,
            *app.info,
            *app.warning,
        ]
    )
    rendered_dates = [
        f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-"
        f"{int(match.group(3)):02d}"
        for match in DATE_PATTERN.finditer(rendered_answer)
    ]
    expected = dps.get("installation_date") if dps else None
    report = {
        "inquiry_id": arguments.inquiry_id,
        "dps_lookup_id": dps.get("id") if dps else None,
        "draft_id": draft.get("id") if draft else None,
        "widget_key_expected": (
            f"program_answer_{arguments.inquiry_id}_"
            f"{draft.get('id') if draft else 'empty'}"
        ),
        "dashboard_installation_date": expected,
        "dashboard_date_rendered": bool(
            expected and expected in rendered_ui_text
        ),
        "program_answer_dates": rendered_dates,
        "program_answer_rendered": bool(rendered_answer),
        "active_draft_rendered": bool(
            draft
            and rendered_answer
            == str(draft.get("original_answer") or "")
        ),
        "date_matches": bool(expected and expected in rendered_dates),
        "streamlit_exception_count": len(app.exception),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
