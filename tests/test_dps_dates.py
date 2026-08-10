from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from api.order import get_order_summary
from dps import agent_server
from dps.dates import (
    calculate_dps_lookup_period,
    parse_date_value,
    select_dps_date_source,
    validate_dps_lookup_period,
)
from dps.dps_ui_automation import DpsUiAutomation
from services import dps_agent_client
from ui.components import _dps_query_context


@pytest.mark.parametrize(
    ("reference", "today", "expected_start", "expected_end"),
    [
        ("2026-01-18", date(2026, 7, 28), "2026-01-01", "2026-01-31"),
        ("2026-04-18", date(2026, 7, 28), "2026-04-01", "2026-04-30"),
        ("2025-02-18", date(2026, 7, 28), "2025-02-01", "2025-02-28"),
        ("2024-02-18", date(2026, 7, 28), "2024-02-01", "2024-02-29"),
        ("2026-06-29", date(2026, 7, 28), "2026-06-01", "2026-06-30"),
        ("2026-07-08", date(2026, 7, 28), "2026-07-01", "2026-07-28"),
    ],
)
def test_calculate_month_periods(
    reference: str,
    today: date,
    expected_start: str,
    expected_end: str,
) -> None:
    result = calculate_dps_lookup_period(reference, today=today)
    assert result.start.isoformat() == expected_start
    assert result.end.isoformat() == expected_end
    assert (result.start.year, result.start.month) == (
        result.end.year,
        result.end.month,
    )


def test_future_reference_is_clamped_safely() -> None:
    result = calculate_dps_lookup_period(
        "2026-08-03",
        today=date(2026, 7, 28),
    )
    assert result.start == date(2026, 7, 1)
    assert result.end == date(2026, 7, 28)
    assert "REFERENCE_DATE_IN_FUTURE" in result.warnings[0]


@pytest.mark.parametrize(
    ("payload", "source"),
    [
        (
            {
                "order_date": "2026-07-01",
                "payment_date": "2026-07-02",
                "place_order_date": "2026-07-03",
                "shipping_due_date": "2026-07-04",
            },
            "order_date",
        ),
        (
            {
                "order_created_at": "2026-07-01",
                "payment_date": "2026-07-02",
            },
            "order_created_at",
        ),
        (
            {
                "payment_date": "2026-07-02",
                "place_order_date": "2026-07-03",
            },
            "payment_date",
        ),
        (
            {
                "payment_completed_at": "2026-07-02",
                "place_order_date": "2026-07-03",
            },
            "payment_completed_at",
        ),
        ({"place_order_date": "2026-07-03"}, "place_order_date"),
        ({"shipping_due_date": "2026-07-04"}, "shipping_due_date"),
    ],
)
def test_date_source_priority(payload: dict[str, str], source: str) -> None:
    selected = select_dps_date_source(payload)
    assert selected.source == source
    assert selected.reference_date is not None


def test_missing_date_source_is_explicit() -> None:
    selected = select_dps_date_source({})
    assert selected.source is None
    assert selected.reference_date is None
    assert "DATE_SOURCE_MISSING" in selected.warnings


@pytest.mark.parametrize(
    "raw",
    [
        "2026-07-01",
        "2026.07.01",
        "2026/07/01",
        "2026-7-1",
        "20260701",
        "2026-07-01T13:14:15.123+09:00",
    ],
)
def test_parse_supported_date_formats(raw: str) -> None:
    assert parse_date_value(raw) == date(2026, 7, 1)


@pytest.mark.parametrize("raw", [None, "", "not-a-date"])
def test_parse_invalid_date_values(raw: object) -> None:
    assert parse_date_value(raw) is None


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2026-07-01", "2026-07-28"),
        ("2026.07.01", "2026.07.28"),
        ("2026/07/01", "2026/07/28"),
        ("20260701", "20260728"),
    ],
)
def test_validate_period_formats(start: str, end: str) -> None:
    valid, code, _, _ = validate_dps_lookup_period(
        start,
        end,
        today=date(2026, 7, 28),
    )
    assert valid
    assert code == "DATE_RANGE_READY"


@pytest.mark.parametrize(
    ("start", "end", "code"),
    [
        ("", "2026-07-28", "DATE_START_VERIFY_FAILED"),
        ("2026-07-01", "", "DATE_END_VERIFY_FAILED"),
        ("2026-07-28", "2026-07-01", "DATE_RANGE_INVALID"),
        ("2026-06-30", "2026-07-01", "DATE_RANGE_INVALID"),
        ("2026-07-01", "2026-07-29", "DATE_RANGE_INVALID"),
    ],
)
def test_validate_period_rejects_unsafe_ranges(
    start: str,
    end: str,
    code: str,
) -> None:
    valid, actual_code, _, _ = validate_dps_lookup_period(
        start,
        end,
        today=date(2026, 7, 28),
    )
    assert not valid
    assert actual_code == code


@pytest.mark.parametrize(
    ("raw", "month"),
    [
        ("7월", 7),
        ("07월", 7),
        ("7", 7),
        ("07", 7),
        ("July", 7),
    ],
)
def test_calendar_month_text_normalization(raw: str, month: int) -> None:
    assert DpsUiAutomation._month_from_text(raw) == month


@pytest.mark.parametrize(
    ("raw", "year"),
    [("2026", 2026), ("2026년", 2026), ("선택 2026 년", 2026)],
)
def test_calendar_year_text_normalization(raw: str, year: int) -> None:
    assert DpsUiAutomation._year_from_text(raw) == year


class Rect:
    def __init__(self, left: int, top: int, right: int, bottom: int):
        self.left, self.top, self.right, self.bottom = (
            left,
            top,
            right,
            bottom,
        )


class Element:
    _next_id = 1

    def __init__(
        self,
        name: str,
        control_type: str,
        rect: Rect,
        *,
        automation_id: str = "",
        class_name: str = "",
        parent: "Element | None" = None,
    ):
        runtime_id = [Element._next_id]
        Element._next_id += 1
        self.element_info = SimpleNamespace(
            name=name,
            control_type=control_type,
            automation_id=automation_id,
            class_name=class_name,
            runtime_id=runtime_id,
            framework_id="Chrome",
            handle=0,
        )
        self._rect = rect
        self._parent = parent
        self._children: list[Element] = []
        if parent is not None:
            parent._children.append(self)

    def rectangle(self):
        return self._rect

    def parent(self):
        if self._parent is None:
            raise AttributeError
        return self._parent

    def children(self):
        return list(self._children)

    def window_text(self):
        return self.element_info.name

    def is_visible(self):
        return True

    def is_enabled(self):
        return True


class Window:
    def __init__(self, elements: list[Element]):
        self.elements = elements

    def descendants(self, control_type: str | None = None):
        if control_type is None:
            return list(self.elements)
        return [
            item
            for item in self.elements
            if item.element_info.control_type == control_type
        ]


def period_window(*, ambiguous: bool = False) -> Window:
    row = Element("기간 행", "DataItem", Rect(100, 100, 600, 140))
    label = Element("기간", "DataItem", Rect(100, 100, 200, 140), parent=row)
    start = Element(
        "",
        "Edit",
        Rect(220, 105, 300, 135),
        automation_id="I_SDATE",
        class_name="calendar hasDatepicker",
        parent=row,
    )
    start_trigger = Element(
        "달력",
        "Image",
        Rect(285, 110, 298, 130),
        class_name="ui-datepicker-trigger",
        parent=row,
    )
    end = Element(
        "",
        "Edit",
        Rect(330, 105, 410, 135),
        automation_id="I_EDATE",
        class_name="calendar hasDatepicker",
        parent=row,
    )
    end_trigger = Element(
        "달력",
        "Image",
        Rect(395, 110, 408, 130),
        class_name="ui-datepicker-trigger",
        parent=row,
    )
    elements = [
        row,
        label,
        start,
        start_trigger,
        end,
        end_trigger,
    ]
    if ambiguous:
        elements.append(
            Element(
                "",
                "Edit",
                Rect(430, 105, 500, 135),
                class_name="calendar hasDatepicker",
                parent=row,
            )
        )
    return Window(elements)


def test_period_label_resolves_start_and_end_controls() -> None:
    controls = DpsUiAutomation().find_period_controls(period_window())
    assert controls is not None
    assert controls.start_edit.element_info.automation_id == "I_SDATE"
    assert controls.end_edit.element_info.automation_id == "I_EDATE"


def test_period_control_roles_are_based_on_structure() -> None:
    controls = DpsUiAutomation().find_period_controls(period_window())
    assert controls is not None
    assert controls.start_edit.rectangle().left < controls.end_edit.rectangle().left
    assert controls.start_trigger.rectangle().left < controls.end_trigger.rectangle().left


def test_period_label_ambiguity_blocks_selection() -> None:
    window = period_window()
    window.elements.append(
        Element("기간", "Text", Rect(100, 150, 200, 180))
    )
    automation = DpsUiAutomation()
    assert automation.find_period_controls(window) is None
    assert (
        automation._last_period_resolution["status"]
        == "CALENDAR_CONTROL_AMBIGUOUS"
    )


def test_year_combobox_is_selected_by_exact_text() -> None:
    combo = Mock()
    method = DpsUiAutomation()._select_combo_value(
        combo, ("2026", "2026년")
    )
    assert method == "select:2026"
    combo.select.assert_called_once_with("2026")


def test_month_combobox_uses_index_only_after_text_fails() -> None:
    combo = Mock()
    combo.select.side_effect = [
        RuntimeError,
        RuntimeError,
        RuntimeError,
        RuntimeError,
        RuntimeError,
        None,
    ]
    method = DpsUiAutomation()._select_combo_value(
        combo,
        ("7월", "07월", "7", "07", "July"),
        index=6,
    )
    assert method == "select_index:6"


def test_uia_control_click_is_fallback_after_patterns_fail() -> None:
    control = Mock()
    control.invoke.side_effect = RuntimeError
    control.select.side_effect = RuntimeError
    control.expand.side_effect = RuntimeError
    assert (
        DpsUiAutomation._execute_uia_control(control)
        == "click_input"
    )
    control.click_input.assert_called_once_with()


def test_ambiguous_calendar_days_are_not_clicked() -> None:
    automation = DpsUiAutomation()
    first, second = Mock(), Mock()
    with (
        patch.object(
            automation,
            "_open_calendar",
            return_value=({"table": object()}, {}),
        ),
        patch.object(
            automation,
            "_move_calendar_to_month",
            return_value=(True, {}),
        ),
        patch.object(
            automation,
            "_calendar_displayed_year_month",
            return_value=(2026, 7),
        ),
        patch.object(
            automation,
            "_calendar_day_candidates",
            return_value=([first, second], {"candidates": [1, 2]}),
        ),
    ):
        result = automation._select_calendar_date(
            object(), date(2026, 7, 1), role="start"
        )
    assert result["code"] == "CALENDAR_DAY_AMBIGUOUS"
    first.invoke.assert_not_called()
    first.click_input.assert_not_called()
    second.invoke.assert_not_called()
    second.click_input.assert_not_called()


@pytest.mark.parametrize(
    ("order", "source", "start", "end"),
    [
        (
            {
                "order_id": "O",
                "product_order_id": "P",
                "order_date": "2026-07-08",
            },
            "order_date",
            "2026-07-01",
            "2026-07-31",
        ),
        (
            {
                "order_id": "O",
                "product_order_id": "P",
                "payment_date": "2026-06-08",
            },
            "payment_date",
            "2026-06-01",
            "2026-06-30",
        ),
        (
            {
                "order_id": "O",
                "product_order_id": "P",
                "shipping_due_date": "2026-05-08",
            },
            "shipping_due_date",
            "2026-05-01",
            "2026-05-31",
        ),
    ],
)
def test_streamlit_query_context_keeps_date_metadata(
    order: dict[str, str],
    source: str,
    start: str,
    end: str,
) -> None:
    context = _dps_query_context({"orders": [order]})
    assert context["dps_query_value"] == "O"
    assert context["dps_query_value_type"] == "order_id"
    assert context["dps_date_source"] == source
    assert context["dps_period_start"] == start
    expected_end = (
        date.today().isoformat()
        if start[:7] == date.today().isoformat()[:7]
        else end
    )
    assert context["dps_period_end"] == expected_end


def test_order_summary_preserves_naver_date_fields() -> None:
    summary = get_order_summary(
        {
            "order": {
                "orderId": "O",
                "orderDate": "2026-07-27T01:00:00+09:00",
                "paymentDate": "2026-07-27T01:01:00+09:00",
            },
            "productOrder": {
                "productOrderId": "P",
                "placeOrderDate": "2026-07-27T01:02:00+09:00",
            },
        }
    )
    assert summary["order_date"].startswith("2026-07-27")
    assert summary["payment_date"].startswith("2026-07-27")
    assert summary["place_order_date"].startswith("2026-07-27")


def test_result_parser_keeps_period_and_requested_date() -> None:
    result = DpsUiAutomation().parse_lookup_result(
        {
            "raw_result_texts": [],
            "table_headers": [
                "온라인판매 주문번호",
                "희망일",
                "DPS판매번호",
            ],
            "table_rows": [["O", "2026-08-01", "S"]],
        },
        order_id="O",
        product_order_id="P",
        dps_query_value="O",
        dps_query_value_type="order_id",
        dps_date_source="order_date",
        dps_reference_date="2026-07-27",
        dps_period_start="2026-07-01",
        dps_period_end="2026-07-28",
    )
    assert result["data"]["requested_date"] == "2026-08-01"
    assert result["data"]["dps_period_start"] == "2026-07-01"


def test_agent_cache_schema_is_five() -> None:
    assert agent_server.CACHE_SCHEMA_VERSION == 5


def test_last_result_log_masks_order_and_secrets() -> None:
    safe = agent_server._safe_lookup_log_payload(
        {
            "product_order_id": "2026072773703741",
            "cookie": "secret-cookie",
            "nested": ["order 2026072717193141"],
        }
    )
    assert safe["product_order_id"] == "20260727****3741"
    assert safe["cookie"] == "[REDACTED]"
    assert safe["nested"] == ["order 20260727****3141"]


def test_period_cache_key_is_stable_for_same_period() -> None:
    first = agent_server.dps_cache_key(
        "O", "order_id", "2026-07-01", "2026-07-28"
    )
    second = agent_server.dps_cache_key(
        "O", "order_id", "2026-07-01", "2026-07-28"
    )
    assert first == second
    assert first == "order:O:2026-07-01:2026-07-28"


def test_period_cache_key_changes_for_different_period() -> None:
    july = agent_server.dps_cache_key(
        "O", "order_id", "2026-07-01", "2026-07-28"
    )
    june = agent_server.dps_cache_key(
        "O", "order_id", "2026-06-01", "2026-06-30"
    )
    assert july != june


def test_product_cache_key_is_rejected() -> None:
    with pytest.raises(ValueError):
        agent_server.dps_cache_key(
            "P", "product_order_id", "2026-07-01", "2026-07-28"
        )


def test_client_sends_all_date_fields() -> None:
    with (
        patch.object(
            dps_agent_client,
            "start_dps_agent",
            return_value={"agent_running": True},
        ),
        patch.object(
            dps_agent_client,
            "_request",
            return_value={"success": True},
        ) as request,
    ):
        dps_agent_client.lookup_dps_order(
            order_id="O",
            product_order_id="P",
            order_date="2026-07-27",
            dps_date_source="order_date",
            dps_reference_date="2026-07-27",
            dps_period_start="2026-07-01",
            dps_period_end="2026-07-28",
        )
    payload = request.call_args.args[1]
    assert payload["order_date"] == "2026-07-27"
    assert payload["dps_period_start"] == "2026-07-01"
    assert payload["dps_period_end"] == "2026-07-28"


def test_automation_source_has_no_forbidden_input_paths() -> None:
    source = (
        Path(__file__).parents[1]
        / "dps"
        / "dps_ui_automation.py"
    ).read_text(encoding="utf-8")
    forbidden = ("send_keys(", "type_keys(", "pyautogui", "clipboard")
    assert not any(token in source for token in forbidden)
