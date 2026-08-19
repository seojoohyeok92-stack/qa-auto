from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from dps.dps_ui_automation import DpsUiAutomation
from dps.sales_detail import (
    map_detail_items,
    mask_address,
    mask_name,
    mask_phone,
    merge_list_and_detail,
    normalize_date,
    parse_flat_detail,
    resolve_delivery_date,
    sanitize_detail_for_cache,
)
from ui.components import _dps_summary_values, _format_dps_money


class Rect:
    def __init__(self, left: int, top: int, right: int, bottom: int):
        self.left, self.top, self.right, self.bottom = (
            left,
            top,
            right,
            bottom,
        )


class Element:
    def __init__(
        self,
        name: str,
        control_type: str = "Text",
        *,
        left: int = 0,
        top: int = 0,
        right: int = 100,
        bottom: int = 20,
        parent: "Element | None" = None,
        class_name: str = "",
        automation_id: str = "",
    ):
        self.element_info = SimpleNamespace(
            name=name,
            control_type=control_type,
            class_name=class_name,
            automation_id=automation_id,
            runtime_id=(id(self),),
            handle=0,
        )
        self._rect = Rect(left, top, right, bottom)
        self._parent = parent
        self._children: list[Element] = []
        if parent:
            parent._children.append(self)
        self.invoke = Mock()
        self.click_input = Mock()

    def rectangle(self):
        return self._rect

    def window_text(self):
        return self.element_info.name

    def parent(self):
        if self._parent is None:
            raise AttributeError
        return self._parent

    def children(self):
        return list(self._children)

    def is_visible(self):
        return True

    def is_enabled(self):
        return True


class Window:
    def __init__(self, elements: list[Element], handle: int = 10):
        self.elements = elements
        self.handle = handle
        self.element_info = SimpleNamespace(handle=handle)
        self.close = Mock()

    def descendants(self, control_type=None):
        if control_type is None:
            return list(self.elements)
        return [
            value
            for value in self.elements
            if value.element_info.control_type == control_type
        ]

    def window_text(self):
        return "판매조회"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-07-14", "2026-07-14"),
        ("2026.07.14", "2026-07-14"),
        ("2026/07/14", "2026-07-14"),
        ("20260714", "2026-07-14"),
        (" 2026년 07월 14일 ", "2026-07-14"),
        ("2026-02-30", None),
        ("", None),
    ],
)
def test_detail_date_normalization(raw, expected):
    assert normalize_date(raw) == expected


def test_customer_date_is_delivery_date():
    result = resolve_delivery_date(
        customer_requested_date="2026-07-14",
        item_requested_dates=["2026-07-14"],
    )
    assert result["delivery_scheduled_date"] == "2026-07-14"
    assert result["installation_date"] == "2026-07-14"
    assert result["installation_date_source"] == (
        "DPS_ITEM_DETAIL_REQUIRED_DELIVERY_DATE"
    )


def test_item_date_fallback():
    result = resolve_delivery_date(
        item_requested_dates=["2026.07.30", "2026-07-30"]
    )
    assert result["delivery_scheduled_date"] == "2026-07-30"
    assert result["delivery_date_source"] == (
        "DPS_ITEM_DETAIL_REQUIRED_DELIVERY_DATE"
    )


def test_list_date_is_not_an_installation_date_fallback():
    result = resolve_delivery_date(list_requested_date="2026/07/31")
    assert result["delivery_scheduled_date"] is None
    assert result["installation_date"] is None
    assert result["delivery_date_status"] == "NOT_AVAILABLE"


def test_customer_summary_date_is_not_an_installation_date_source():
    result = resolve_delivery_date(
        customer_requested_date="2026-07-14",
        item_requested_dates=["2026-07-30"],
    )
    assert result["delivery_scheduled_date"] == "2026-07-30"
    assert result["installation_date"] == "2026-07-30"
    assert result["delivery_date_status"] == "CONFIRMED"
    assert result["customer_requested_date"] is None
    assert result["item_requested_dates"] == ["2026-07-30"]


def test_multiple_item_dates_are_not_guessed():
    result = resolve_delivery_date(
        item_requested_dates=["2026-07-30", "2026-07-31"]
    )
    assert result["delivery_scheduled_date"] is None
    assert result["delivery_date_status"] == "MULTIPLE_DATES"


def test_missing_dates_remain_null():
    result = resolve_delivery_date()
    assert result["delivery_scheduled_date"] is None
    assert result["delivery_date_status"] == "NOT_AVAILABLE"


def test_item_header_mapping_preserves_empty_cell_and_null_svc():
    items = map_detail_items(
        ["행번", "모델", "수량", "창고", "판매금액", "요구납기일", "SVC주문번호"],
        [["10", "LH50BEFHLGFXKR", "1", "", "680,000", "2026-07-30", ""]],
    )
    assert items == [
        {
            "line_number": "10",
            "model_name": "LH50BEFHLGFXKR",
            "quantity": 1,
            "warehouse": None,
            "sale_amount": "680,000",
            "raw_required_delivery_date": "2026-07-30",
            "required_delivery_date": "2026-07-30",
            "date_parse_status": "PARSED",
            "requested_delivery_date": "2026-07-30",
            "service_order_number": None,
        }
    ]


def test_multiple_detail_items_are_kept():
    items = map_detail_items(
        ["모델", "수량"],
        [["MODEL-A", "1"], ["MODEL-B", "2"]],
    )
    assert [item["model_name"] for item in items] == ["MODEL-A", "MODEL-B"]
    assert [item["quantity"] for item in items] == [1, 2]


def _records():
    values = [
        ("고객정보", 0, 0),
        ("고객번호", 0, 20),
        ("C-1", 120, 20),
        ("구매자", 0, 40),
        ("홍길동", 120, 40),
        ("전화번호", 0, 60),
        ("010-1234-5678", 120, 60),
        ("인수자", 0, 80),
        ("김고객", 120, 80),
        ("전화번호", 0, 100),
        ("010-9876-5432", 120, 100),
        ("주소", 0, 120),
        ("서울시 강남구 테스트로 1", 120, 120),
        ("요구납기일", 0, 140),
        ("2026-07-14", 120, 140),
        ("배달시간", 0, 160),
        ("하루", 120, 160),
        ("배송정보", 0, 180),
        ("설치 전에 제품 박스 개봉", 120, 180),
    ]
    return [
        {"name": name, "left": left, "top": top}
        for name, left, top in values
    ]


def test_customer_information_parsing():
    parsed = parse_flat_detail(_records())
    customer = parsed["customer_info"]
    assert customer["customer_number"] == "C-1"
    assert customer["buyer_name"] == "홍길동"
    assert customer["buyer_phone"] == "010-1234-5678"
    assert customer["recipient_name"] == "김고객"
    assert customer["recipient_phone"] == "010-9876-5432"
    assert customer["delivery_address"].startswith("서울시")
    assert customer["requested_delivery_date"] == "2026-07-14"
    assert customer["delivery_time"] == "하루"
    assert customer["delivery_note"] == "설치 전에 제품 박스 개봉"


def test_side_by_side_sections_use_x_boundaries_not_last_marker():
    parsed = parse_flat_detail(
        [
            {"name": "판매처정보", "left": 0, "top": 0},
            {"name": "고객정보", "left": 300, "top": 0},
            {"name": "입금정보", "left": 600, "top": 0},
            {"name": "주문사유", "left": 0, "top": 20},
            {"name": "온라인주문", "left": 100, "top": 20},
            {"name": "요구납기일", "left": 300, "top": 20},
            {"name": "2026-07-14", "left": 400, "top": 20},
            {"name": "주문금액", "left": 600, "top": 20},
            {"name": "680,000", "left": 700, "top": 20},
        ]
    )
    assert parsed["sales_office_info"]["order_reason"] == "온라인주문"
    assert (
        parsed["customer_info"]["requested_delivery_date"]
        == "2026-07-14"
    )
    assert parsed["payment_info"]["order_amount"] == "680,000"


def test_item_table_label_does_not_overwrite_customer_date():
    parsed = parse_flat_detail(
        [
            {"name": "고객정보", "left": 300, "top": 0},
            {"name": "요구납기일", "left": 300, "top": 20},
            {
                "name": "2026-07-14",
                "control_type": "Edit",
                "left": 400,
                "top": 25,
            },
            {"name": "품목상세내역", "left": 0, "top": 100},
            {"name": "요구납기일", "left": 500, "top": 120},
            {"name": "2026-07-30", "left": 500, "top": 140},
        ]
    )
    assert (
        parsed["customer_info"]["requested_delivery_date"]
        == "2026-07-14"
    )


def test_merge_preserves_list_numbers_and_adds_detail():
    merged = merge_list_and_detail(
        {
            "dps_sales_number": "SALE-1",
            "dps_order_number": "ORDER-1",
            "model_name": "MODEL-A",
            "requested_date": "2026-07-31",
        },
        {
            "customer_info": {
                "requested_delivery_date": "2026-07-14",
                "recipient_name": "김고객",
            },
            "detail_items": [],
        },
        detail_lookup={"parsed": True},
    )
    assert merged["dps_sales_number"] == "SALE-1"
    assert merged["dps_order_number"] == "ORDER-1"
    assert merged["model_name"] == "MODEL-A"
    assert merged["recipient_name"] == "김고객"
    assert merged["delivery_scheduled_date"] is None
    assert merged["installation_date"] is None


def test_cache_removes_phone_and_full_address_and_masks_names():
    cached = sanitize_detail_for_cache(
        {
            "recipient_phone": "010-1234-5678",
            "delivery_address": "서울시 강남구 테스트로 1",
            "recipient_name": "김고객",
            "data": {
                "buyer_phone": "010-1111-2222",
                "delivery_address": "서울시 강남구 테스트로 1",
                "buyer_name": "홍길동",
            },
            "diagnostics": {
                "detail_raw_labels": {"주소": "원문"},
                "detail_raw_rows": [["원문"]],
            },
        }
    )
    assert "recipient_phone" not in cached
    assert "delivery_address" not in cached
    assert "buyer_phone" not in cached["data"]
    assert "delivery_address" not in cached["data"]
    assert cached["recipient_name"] == "김**"
    assert cached["data"]["buyer_name"] == "홍**"
    assert "detail_raw_rows" not in cached["diagnostics"]


def test_security_mask_helpers():
    assert mask_phone("010-1234-5678") == "010-****-5678"
    assert mask_name("홍길동") == "홍**"
    assert mask_address("서울시 강남구 테스트로 1") == "서울시 강남구 ***"


def _list_window(*, duplicate=False, text_parent=False):
    online_header = Element(
        "온라인판매 주문번호",
        "DataItem",
        left=100,
        top=100,
        right=250,
        bottom=120,
    )
    header = Element(
        "DPS판매번호", "DataItem", left=300, top=100, right=420, bottom=120
    )
    order = Element(
        "NAVER-1", "Text", left=100, top=150, right=250, bottom=170
    )
    if text_parent:
        link = Element(
            "SALE-1", "Hyperlink", left=300, top=150, right=420, bottom=170
        )
        Element(
            "SALE-1",
            "Text",
            left=300,
            top=150,
            right=420,
            bottom=170,
            parent=link,
        )
        values = [online_header, header, order, link, *link.children()]
    else:
        link = Element(
            "SALE-1", "Hyperlink", left=300, top=150, right=420, bottom=170
        )
        values = [online_header, header, order, link]
    if duplicate:
        values.append(
            Element(
                "SALE-1",
                "Button",
                left=300,
                top=150,
                right=420,
                bottom=170,
            )
        )
    return Window(values), link


def test_link_is_selected_from_exact_header_column_and_order_row():
    window, link = _list_window()
    selected, diagnostic = DpsUiAutomation().find_dps_sales_link(
        window,
        expected_order_id="NAVER-1",
        dps_sales_number="SALE-1",
        matched_row_index=0,
    )
    assert selected is link
    assert diagnostic["selected_reason"].startswith("single exact Hyperlink")


def test_clickable_parent_of_sales_number_text_is_used():
    window, link = _list_window(text_parent=True)
    selected, _ = DpsUiAutomation().find_dps_sales_link(
        window,
        expected_order_id="NAVER-1",
        dps_sales_number="SALE-1",
        matched_row_index=0,
    )
    assert selected is link


def test_parsed_row_index_fallback_when_order_cell_is_not_exposed():
    window, link = _list_window()
    window.elements = [
        element
        for element in window.elements
        if element.element_info.name != "NAVER-1"
    ]
    selected, diagnostic = DpsUiAutomation().find_dps_sales_link(
        window,
        expected_order_id="NAVER-1",
        dps_sales_number="SALE-1",
        matched_row_index=0,
    )
    assert selected is link
    assert diagnostic["row_resolution"] == (
        "parsed_row_index_and_sales_column"
    )


def test_unnamed_sales_control_uses_column_and_parsed_row_only():
    window, link = _list_window()
    window.elements = [
        element
        for element in window.elements
        if element.element_info.name != "NAVER-1"
    ]
    link.element_info.name = ""
    selected, diagnostic = DpsUiAutomation().find_dps_sales_link(
        window,
        expected_order_id="NAVER-1",
        dps_sales_number="SALE-1",
        matched_row_index=0,
    )
    assert selected is link
    assert diagnostic["row_resolution"] == (
        "parsed_row_index_and_unnamed_sales_control"
    )


def test_unnamed_sales_text_control_can_be_clicked_spatially():
    window, link = _list_window()
    window.elements = [
        element
        for element in window.elements
        if element.element_info.name != "NAVER-1"
    ]
    link.element_info.name = ""
    link.element_info.control_type = "Text"
    selected, diagnostic = DpsUiAutomation().find_dps_sales_link(
        window,
        expected_order_id="NAVER-1",
        dps_sales_number="SALE-1",
        matched_row_index=0,
    )
    assert selected is link
    assert diagnostic["spatial_row_fallback"] is True


def test_duplicate_order_cell_centers_fall_back_to_parsed_row():
    window, link = _list_window()
    window.elements.append(
        Element(
            "NAVER-1",
            "Text",
            left=100,
            top=200,
            right=250,
            bottom=220,
        )
    )
    selected, diagnostic = DpsUiAutomation().find_dps_sales_link(
        window,
        expected_order_id="NAVER-1",
        dps_sales_number="SALE-1",
        matched_row_index=0,
    )
    assert selected is link
    assert diagnostic["duplicate_order_cell_fallback"] is True


def test_sales_hyperlink_opens_detail_and_parses_multiple_item_dates():
    automation = DpsUiAutomation()
    purchase_window, link = _list_window()
    detail_window = Window(
        [
            Element("판매조회", "Text"),
            Element("품목상세내역", "Text"),
            Element("요구납기일", "Text"),
        ],
        handle=20,
    )
    calls = {"count": 0}

    def windows():
        calls["count"] += 1
        return (
            [purchase_window]
            if calls["count"] == 1
            else [purchase_window, detail_window]
        )

    parsed_items = [
        {"required_delivery_date": "2026-08-25"},
        {"required_delivery_date": "2026-08-26"},
    ]
    automation.collect_sales_detail_snapshot = Mock(
        return_value={
            "parsed": {
                "customer_info": {},
                "detail_items": parsed_items,
            },
            "headers": ["요구납기일"],
            "rows": [["2026-08-25"], ["2026-08-26"]],
        }
    )
    progress: list[str] = []

    result = automation.lookup_sales_detail(
        purchase_window=purchase_window,
        list_snapshot={},
        list_data={
            "dps_sales_number": "SALE-1",
            "dps_query_value": "NAVER-1",
        },
        list_diagnostics={
            "matched_row_index": 0,
            "raw_rows": [["NAVER-1", "SALE-1"]],
        },
        expected_order_id="NAVER-1",
        window_provider=windows,
        url_reader=lambda window: (
            "https://dps2u.co.kr/dpsweb/sd010_0050_DP_SSearchSalesMain.do"
            if window is detail_window
            else "https://dps2u.co.kr/purchase"
        ),
        timeout=1,
        progress_callback=progress.append,
    )

    assert result["detail_lookup"]["parsed"] is True
    assert result["detail_lookup"]["status"] == "DETAIL_CLOSED"
    assert result["detail_lookup"]["invocation_count"] == 1
    assert result["detail"]["detail_items"] == parsed_items
    link.click_input.assert_called_once()
    link.invoke.assert_not_called()
    assert "DPS_SALES_NUMBER_CLICK_STARTED" in progress
    assert "DPS_SALES_DETAIL_OPENED" in progress
    assert "ITEM_ROWS_FOUND" in progress
    assert "REQUIRED_DELIVERY_DATES_PARSED" in progress
    assert "INSTALLATION_DATE_SELECTED" not in progress


def test_other_row_sales_number_is_excluded():
    window, link = _list_window()
    other = Element(
        "SALE-1", "Hyperlink", left=300, top=200, right=420, bottom=220
    )
    window.elements.append(other)
    selected, _ = DpsUiAutomation().find_dps_sales_link(
        window,
        expected_order_id="NAVER-1",
        dps_sales_number="SALE-1",
        matched_row_index=0,
    )
    assert selected is link


def test_unmatched_row_prevents_link_selection():
    window, _ = _list_window()
    selected, diagnostic = DpsUiAutomation().find_dps_sales_link(
        window,
        expected_order_id="NAVER-1",
        dps_sales_number="SALE-1",
        matched_row_index=None,
    )
    assert selected is None
    assert diagnostic["reason"] == "RESULT_ROW_NOT_MATCHED"


def test_exact_close_uses_invoke_not_chrome_caption():
    caption = Element(
        "닫기",
        "Button",
        class_name="WindowsCaptionButton",
        automation_id="view_4",
    )
    close = Element("닫기", "Button")
    detail = Window([caption, close], handle=20)
    closed, method = DpsUiAutomation().close_sales_detail(
        detail, purchase_window=Window([]), was_new_window=True
    )
    assert closed is True
    assert method == "invoke"
    close.invoke.assert_called_once()
    caption.invoke.assert_not_called()


def test_close_click_input_fallback():
    close = Element("닫기", "Button")
    close.invoke.side_effect = RuntimeError
    detail = Window([close], handle=20)
    closed, method = DpsUiAutomation().close_sales_detail(
        detail, purchase_window=Window([]), was_new_window=False
    )
    assert closed is True
    assert method == "click_input"
    close.click_input.assert_called_once()


def test_new_detail_window_only_is_closed_as_last_fallback():
    detail = Window([], handle=20)
    closed, method = DpsUiAutomation().close_sales_detail(
        detail, purchase_window=Window([], handle=10), was_new_window=True
    )
    assert closed is True
    assert method == "window.close"
    detail.close.assert_called_once()


def test_summary_has_six_cards_and_delivery_first():
    values = _dps_summary_values(
        {
            "delivery_scheduled_date": "2026-07-14",
            "delivery_date_status": "CONFIRMED",
            "progress_status": "구매요청",
            "model_name": "MODEL-A",
            "quantity": 1,
            "dps_sales_number": "SALE-1",
            "dps_order_number": "ORDER-1",
        },
        {},
    )
    assert len(values) == 6
    assert values[0] == ("배송 예정일", "2026-07-14")
    assert values[3] == ("수량", "1대")


def test_summary_shows_conflict_and_null_without_blank():
    values = _dps_summary_values(
        {"delivery_date_status": "DATE_CONFLICT"}, {}
    )
    assert values[0][1] == "날짜 확인 필요"
    assert all(value for _, value in values)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(680000, "680,000원"), ("680,000", "680,000원"), (None, "확인되지 않음")],
)
def test_money_is_displayed_in_won(raw, expected):
    assert _format_dps_money(raw) == expected
