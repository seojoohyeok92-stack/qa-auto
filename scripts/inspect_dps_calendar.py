"""Read-only-ish UIA diagnostic for the DPS date picker.

The only mutation is opening the start-date calendar through its UIA element.
No keyboard input, clipboard, JavaScript, CDP, or absolute coordinates are used.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import time

from pywinauto import Application

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.dps_agent_client import get_dps_agent_status, run_dps_diagnostics


LOG_FILE = ROOT / "logs" / "dps_calendar_tree.log"


def info(element):
    try:
        rect = element.rectangle()
        rectangle = {
            "left": int(rect.left),
            "top": int(rect.top),
            "right": int(rect.right),
            "bottom": int(rect.bottom),
        }
    except Exception:
        rectangle = None
    element_info = element.element_info
    try:
        visible = bool(element.is_visible())
    except Exception:
        visible = None
    try:
        enabled = bool(element.is_enabled())
    except Exception:
        enabled = None
    name = str(element_info.name or "")
    name = re.sub(
        r"(?<!\d)(\d{8})\d{4}(\d{4})(?!\d)",
        r"\1****\2",
        name,
    )
    name = re.sub(
        r"\b01\d[- ]?\d{3,4}[- ]?\d{4}\b",
        "[phone]",
        name,
    )
    return {
        "name": name,
        "control_type": str(element_info.control_type or ""),
        "automation_id": str(element_info.automation_id or ""),
        "class_name": str(element_info.class_name or ""),
        "rectangle": rectangle,
        "visible": visible,
        "enabled": enabled,
    }


def main() -> None:
    # Diagnostics selects the already-connected DPS tab without entering data.
    run_dps_diagnostics()
    status = get_dps_agent_status()
    hwnd = int(status["connected_hwnd"])
    window = Application(backend="uia").connect(handle=hwnd).window(handle=hwnd)
    descendants = window.descendants()
    start_edits = [
        element
        for element in descendants
        if str(element.element_info.control_type or "") == "Edit"
        and str(element.element_info.automation_id or "") == "I_SDATE"
    ]
    if len(start_edits) != 1:
        raise RuntimeError(f"I_SDATE count={len(start_edits)}")
    start_edit = start_edits[0]
    parent = start_edit.parent()
    icons = [
        element
        for element in parent.children()
        if str(element.element_info.class_name or "") == "ui-datepicker-trigger"
    ]
    start_rect = start_edit.rectangle()
    icons = [
        element
        for element in icons
        if element.rectangle().left >= start_rect.left
        and element.rectangle().left <= start_rect.right + start_rect.width()
    ]
    if not icons:
        raise RuntimeError("start calendar icon not found")
    icon = min(
        icons,
        key=lambda element: abs(element.rectangle().left - start_rect.right),
    )
    existing_popup = [
        element
        for element in descendants
        if "datepicker" in str(element.element_info.class_name or "").casefold()
        and "trigger" not in str(element.element_info.class_name or "").casefold()
        and "hasdatepicker" not in str(element.element_info.class_name or "").casefold()
        and element.is_visible()
    ]
    if existing_popup:
        method = "already_open"
    else:
        try:
            icon.invoke()
            method = "invoke"
        except Exception:
            icon.click_input()
            method = "click_input"
        time.sleep(0.4)
        opened = [
            element
            for element in window.descendants()
            if "ui-datepicker" in str(element.element_info.class_name or "").casefold()
            and "trigger" not in str(element.element_info.class_name or "").casefold()
            and "hasdatepicker" not in str(element.element_info.class_name or "").casefold()
        ]
        if not opened and method == "invoke":
            # Some HTML Image wrappers expose InvokePattern but the invocation is
            # a no-op. Fall back to clicking that UIA element itself.
            icon = min(
                [
                    element
                    for element in window.descendants()
                    if str(element.element_info.class_name or "")
                    == "ui-datepicker-trigger"
                    and element.rectangle().left >= start_rect.left
                ],
                key=lambda element: abs(
                    element.rectangle().left - start_rect.right
                ),
            )
            icon.click_input()
            method = "invoke_then_click_input"
            time.sleep(0.4)

    after = window.descendants()
    records = []
    for element in after:
        item = info(element)
        name = item["name"].strip()
        class_name = item["class_name"].casefold()
        automation_id = item["automation_id"].casefold()
        rect = item["rectangle"] or {}
        near_calendar = (
            rect.get("top", 0) >= start_rect.bottom
            and rect.get("top", 0) <= start_rect.bottom + 450
            and rect.get("left", 0) >= start_rect.left - 180
            and rect.get("right", 0) <= start_rect.right + 500
        )
        if (
            "datepicker" in class_name
            or "datepicker" in automation_id
            or near_calendar
        ):
            records.append(item)
    payload = {
        "status": {
            "login_status": status.get("login_status"),
            "current_page": status.get("current_page"),
            "connected_hwnd": hwnd,
        },
        "start_edit": info(start_edit),
        "start_calendar_control": info(icon),
        "open_method": method,
        "elements": records[:500],
    }
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOG_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
