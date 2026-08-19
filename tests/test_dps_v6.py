from __future__ import annotations

import io
import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from dps.agent_server import DpsWindowsAgent
from dps.chrome_tab_manager import (
    AddressReadResult,
    ChromeTabManager,
    RuntimeConnection,
    TabCandidate,
)
from dps.connection_store import ConnectionStore
from dps.dps_ui_automation import DpsUiAutomation
from dps.identifiers import select_dps_query_identifier
from ui.components import _dps_lookup_is_disabled


class FakeRect:
    def __init__(
        self,
        *,
        left: int = 420,
        top: int = 220,
        right: int = 700,
        bottom: int = 260,
    ) -> None:
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom

    def width(self) -> int:
        return self.right - self.left

    def mid_point(self) -> SimpleNamespace:
        return SimpleNamespace(
            x=(self.left + self.right) / 2,
            y=(self.top + self.bottom) / 2,
        )


class FakeElement:
    def __init__(
        self,
        name: str,
        control_type: str,
        *,
        selected: bool = False,
        expanded: bool = False,
        class_name: str = "",
        automation_id: str = "",
        left: int = 420,
        top: int = 220,
        parent: "FakeElement | None" = None,
    ) -> None:
        self.element_info = SimpleNamespace(
            name=name,
            control_type=control_type,
            automation_id=automation_id,
            class_name=class_name,
        )
        self.iface_selection_item = SimpleNamespace(
            current_is_selected=selected,
        )
        self.iface_expand_collapse = SimpleNamespace(
            current_expand_collapse_state=1 if expanded else 0,
        )
        self.click_input = Mock()
        self.invoke = Mock()
        self._value = ""
        self.set_edit_text = Mock(side_effect=self._set_edit_text)
        self.type_keys = Mock()
        self._parent = parent
        self._children: list[FakeElement] = []
        self._rect = FakeRect(
            left=left,
            top=top,
            right=left + 280,
            bottom=top + 40,
        )
        if parent is not None:
            parent._children.append(self)

    def window_text(self) -> str:
        return self.element_info.name

    def rectangle(self) -> FakeRect:
        return self._rect

    def parent(self) -> "FakeElement":
        if self._parent is None:
            raise AttributeError("no parent")
        return self._parent

    def children(self) -> list["FakeElement"]:
        return list(self._children)

    def _set_edit_text(self, value: str) -> None:
        self._value = str(value)

    def get_value(self) -> str:
        return self._value


class FakeWindow:
    handle = 101

    def __init__(self) -> None:
        self.sales_menu = FakeElement(
            "판매",
            "Hyperlink",
            selected=True,
        )
        self.online_sales_menu = FakeElement(
            "온라인판매",
            "Hyperlink",
            expanded=True,
        )
        self.purchase_request_target = FakeElement(
            "구매요청리스트",
            "MenuItem",
        )
        self.label = FakeElement("온라인판매 주문번호", "Text")
        self.edit = FakeElement("온라인판매 주문번호", "Edit")
        self.button = FakeElement("조회", "Button")
        self.marker = FakeElement("구매요청리스트", "Text")
        self.elements = [
            self.sales_menu,
            self.online_sales_menu,
            self.purchase_request_target,
            self.label,
            self.edit,
            self.button,
            self.marker,
        ]

    def descendants(self, control_type: str | None = None) -> list[FakeElement]:
        if control_type is None:
            return list(self.elements)
        return [
            element
            for element in self.elements
            if element.element_info.control_type == control_type
        ]

    def window_text(self) -> str:
        return "Samsung DPS 2.0 - Google Chrome"


class LoginDetectionTests(unittest.TestCase):
    def test_home_url_and_logout_are_logged_in(self) -> None:
        window = FakeWindow()
        window.elements = [FakeElement("로그아웃", "Button")]

        result = DpsUiAutomation().detect_login_state(
            window,
            url="https://dps2u.co.kr/dpsweb/main.do",
        )

        self.assertEqual(result["state"], "LOGGED_IN")
        self.assertEqual(result["reason"], "logout_found")

    def test_home_menus_and_widget_are_logged_in_without_logout(self) -> None:
        window = FakeWindow()
        window.elements = [
            FakeElement("경영정보", "Text"),
            FakeElement("구매", "Button"),
            FakeElement("판매", "Text"),
            FakeElement("주문/배송", "Hyperlink"),
        ]

        result = DpsUiAutomation().detect_login_state(
            window,
            url="https://dps2u.co.kr/dpsweb/main.do",
        )

        self.assertEqual(result["state"], "LOGGED_IN")
        self.assertEqual(result["menu_hits"], ["경영정보", "구매", "판매"])
        self.assertEqual(result["widget_hits"], ["주문/배송", "구매"])

    def test_stale_login_url_with_logout_control_is_logged_in(self) -> None:
        window = FakeWindow()
        window.elements = [FakeElement("로그아웃", "Button")]

        result = DpsUiAutomation().detect_login_state(
            window,
            url="https://dps2u.co.kr/dpsweb/login.do",
        )

        self.assertEqual(result["state"], "LOGGED_IN")
        self.assertEqual(result["reason"], "logout_found")

    def test_login_url_with_explicit_credentials_is_login_required(self) -> None:
        window = FakeWindow()
        window.elements = [
            FakeElement("아이디", "Edit"),
            FakeElement("비밀번호", "Edit"),
        ]

        result = DpsUiAutomation().detect_login_state(
            window,
            url="https://dps2u.co.kr/dpsweb/login.do",
        )

        self.assertEqual(result["state"], "LOGIN_REQUIRED")

    def test_otp_ui_overrides_logout_and_home_signals(self) -> None:
        window = FakeWindow()
        window.elements = [
            FakeElement("로그아웃", "Button"),
            FakeElement("경영정보", "Text"),
            FakeElement("구매", "Button"),
            FakeElement("주문/배송", "Hyperlink"),
            FakeElement("SMS 인증번호", "Text"),
        ]

        result = DpsUiAutomation().detect_login_state(
            window,
            url="https://dps2u.co.kr/dpsweb/main.do",
        )

        self.assertEqual(result["state"], "LOGIN_REQUIRED")
        self.assertIn("인증번호", result["otp_ui_hits"])

    def test_valid_dps_url_without_ui_signals_is_uncertain(self) -> None:
        window = FakeWindow()
        window.elements = []

        result = DpsUiAutomation().detect_login_state(
            window,
            url="https://dps2u.co.kr/dpsweb/main.do",
        )

        self.assertEqual(result["state"], "LOGIN_UNCERTAIN")

    def test_home_ui_is_logged_in_when_url_is_unavailable(self) -> None:
        window = FakeWindow()
        window.elements = [
            FakeElement("로그아웃", "Button"),
            FakeElement("경영정보", "Text"),
            FakeElement("주문/배송", "Hyperlink"),
        ]

        result = DpsUiAutomation().detect_login_state(window, url="")

        self.assertEqual(result["state"], "LOGGED_IN")
        self.assertIn("로그아웃", result["dps_ui_hits"])

    def test_logged_in_internal_sales_detail_page_is_not_login_required(self) -> None:
        window = FakeWindow()
        window.elements = [
            FakeElement("로그아웃", "Button"),
            FakeElement("판매상세", "Text"),
            FakeElement("주문/배송", "Hyperlink"),
        ]

        result = DpsUiAutomation().detect_login_state(
            window,
            url="https://dps2u.co.kr/dpsweb/sales/detail.do?id=masked",
        )

        self.assertEqual(result["state"], "LOGGED_IN")

    def test_page_invalid_requires_no_valid_dps_url_and_no_dps_ui(self) -> None:
        window = FakeWindow()
        window.elements = []

        result = DpsUiAutomation().detect_login_state(
            window,
            url="https://example.com/",
        )

        self.assertEqual(result["state"], "DPS_PAGE_INVALID")


class NavigationWindow(FakeWindow):
    def __init__(
        self,
        *,
        expose_target: bool = True,
        online_sales_expanded: bool = False,
        valid_final_page: bool = True,
    ) -> None:
        self.global_menu = FakeElement("", "Group", class_name="gnb")
        self.purchase_menu = FakeElement(
            "구매", "Hyperlink", parent=self.global_menu, top=120
        )
        self.sales_menu = FakeElement(
            "판매", "Hyperlink", parent=self.global_menu, top=120
        )
        self.logout = FakeElement("로그아웃", "Button")
        self.customer = FakeElement(
            "고객", "Text", parent=self.global_menu, top=120
        )
        self.widget = FakeElement("주문/배송", "Hyperlink")
        self.left_menu = FakeElement("", "Group", class_name="lnb")
        self.online_sales_menu = FakeElement(
            "온라인판매",
            "Hyperlink",
            expanded=online_sales_expanded,
            parent=self.left_menu,
            left=190,
            top=260,
        )
        self.target = FakeElement(
            "구매요청리스트",
            "MenuItem",
            parent=self.online_sales_menu,
            left=210,
            top=300,
        )
        self.misleading_purchase_request = FakeElement(
            "다른 메뉴 구매요청",
            "Hyperlink",
        )
        self.misleading_order_lookup = FakeElement(
            "일반주문조회",
            "Hyperlink",
        )
        self.expose_target = expose_target
        self.valid_final_page = valid_final_page
        self.elements = [
            self.logout,
            self.customer,
            self.purchase_menu,
            self.sales_menu,
            self.widget,
            self.misleading_purchase_request,
            self.misleading_order_lookup,
        ]
        self.sales_menu.invoke.side_effect = self._open_sales_menu
        self.online_sales_menu.invoke.side_effect = self._open_online_sales_menu
        self.target.invoke.side_effect = self._open_purchase_page
        if online_sales_expanded:
            self._open_sales_menu()
            if expose_target:
                self.elements.append(self.target)

    def _open_sales_menu(self) -> None:
        self.sales_menu.iface_selection_item.current_is_selected = True
        self.sales_menu.element_info.class_name = "selected"
        if self.online_sales_menu not in self.elements:
            self.elements.append(self.online_sales_menu)

    def _open_online_sales_menu(self) -> None:
        self.online_sales_menu.iface_expand_collapse.current_expand_collapse_state = 1
        if self.expose_target and self.target not in self.elements:
            self.elements.append(self.target)

    def _open_purchase_page(self) -> None:
        self.label = FakeElement("온라인판매 주문번호", "Text")
        self.edit = FakeElement("온라인판매 주문번호", "Edit")
        self.button = FakeElement("조회", "Button")
        self.marker = FakeElement("구매요청리스트", "Text")
        self.elements = [
            self.logout,
            self.purchase_menu,
            self.sales_menu,
            self.online_sales_menu,
            self.target,
            self.label,
            self.edit,
            self.button,
        ]
        if self.valid_final_page:
            self.elements.append(self.marker)

    def descendants(
        self,
        control_type: str | None = None,
        depth: int | None = None,
    ) -> list[FakeElement]:
        del depth
        if control_type is None:
            return list(self.elements)
        return [
            element
            for element in self.elements
            if element.element_info.control_type == control_type
        ]


class NavigationTests(unittest.TestCase):
    def test_exact_sales_online_sales_purchase_request_path_succeeds(self) -> None:
        window = NavigationWindow()
        automation = DpsUiAutomation()

        with patch("dps.dps_ui_automation.time.sleep") as sleep:
            result = automation.navigate_to_online_sales_purchase_request_list(
                window=window,
                validate_target=lambda: (True, {"safe": True}),
                timeout=1.0,
            )

        self.assertTrue(result["success"])
        self.assertTrue(result["navigation_attempted"])
        window.sales_menu.invoke.assert_called_once()
        window.online_sales_menu.invoke.assert_called_once()
        window.target.invoke.assert_called_once()
        window.purchase_menu.invoke.assert_not_called()
        self.assertTrue(sleep.called)

    def test_two_exact_sales_links_with_one_selected_is_success(self) -> None:
        window = FakeWindow()
        window.sales_menu.iface_selection_item.current_is_selected = False
        window.sales_menu.element_info.class_name = "selected"
        breadcrumb = FakeElement("", "Group", class_name="breadcrumb")
        window.elements.append(
            FakeElement("판매", "Hyperlink", parent=breadcrumb, top=330)
        )

        verification = DpsUiAutomation().verify_purchase_request_page(window)

        self.assertTrue(verification.sales_menu_selected)
        self.assertTrue(verification.ok)

    def test_breadcrumb_sales_link_is_excluded_and_top_sales_is_clicked(self) -> None:
        window = NavigationWindow()
        breadcrumb = FakeElement("", "Group", class_name="breadcrumb")
        breadcrumb_sales = FakeElement(
            "판매",
            "Hyperlink",
            parent=breadcrumb,
            left=700,
            top=330,
        )
        window.elements.append(breadcrumb_sales)

        with patch("dps.dps_ui_automation.time.sleep"):
            result = DpsUiAutomation().navigate_to_online_sales_purchase_request_list(
                window=window,
                validate_target=lambda: (True, {"safe": True}),
                timeout=0.1,
            )

        self.assertTrue(result["success"])
        window.sales_menu.invoke.assert_called_once()
        breadcrumb_sales.invoke.assert_not_called()
        window.online_sales_menu.invoke.assert_called_once()
        window.target.invoke.assert_called_once()

    def test_distant_document_breadcrumb_text_does_not_hide_navigation(self) -> None:
        window = NavigationWindow()
        document = FakeElement(
            "홈 > 판매 현재위치",
            "Document",
            class_name="page-location",
        )
        outer = FakeElement("", "Group", parent=document)
        shell = FakeElement("", "Group", parent=outer)
        global_menu = FakeElement("", "Group", class_name="gnb", parent=shell)
        left_menu = FakeElement("", "Group", class_name="lnb", parent=shell)
        window.purchase_menu._parent = global_menu
        window.sales_menu._parent = global_menu
        window.online_sales_menu._parent = left_menu
        global_menu._children.extend([window.purchase_menu, window.sales_menu])
        left_menu._children.append(window.online_sales_menu)

        with patch("dps.dps_ui_automation.time.sleep"):
            result = DpsUiAutomation().navigate_to_online_sales_purchase_request_list(
                window=window,
                validate_target=lambda: (True, {"safe": True}),
                timeout=0.1,
            )

        self.assertTrue(result["success"])
        window.sales_menu.invoke.assert_called_once()
        window.online_sales_menu.invoke.assert_called_once()
        window.target.invoke.assert_called_once()

    def test_two_selected_sales_links_fail_safely(self) -> None:
        window = NavigationWindow()
        window.sales_menu.iface_selection_item.current_is_selected = True
        window.sales_menu.element_info.class_name = "selected"
        second_selected = FakeElement(
            "판매",
            "Hyperlink",
            selected=True,
            class_name="selected",
            top=125,
        )
        window.elements.append(second_selected)

        result = DpsUiAutomation().navigate_to_online_sales_purchase_request_list(
            window=window,
            validate_target=lambda: (True, {"safe": True}),
            timeout=0.1,
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "SALES_MENU_SELECTION_FAILED")
        window.sales_menu.invoke.assert_not_called()
        second_selected.invoke.assert_not_called()
        window.online_sales_menu.invoke.assert_not_called()

    def test_sales_selection_failure_stops_before_any_order_input(self) -> None:
        window = NavigationWindow()
        order_edit = FakeElement("온라인판매 주문번호", "Edit")
        window.elements.append(order_edit)
        window.sales_menu.invoke.side_effect = None

        with patch("dps.dps_ui_automation.time.sleep"):
            result = DpsUiAutomation().navigate_to_online_sales_purchase_request_list(
                window=window,
                validate_target=lambda: (True, {"safe": True}),
                timeout=0.1,
            )

        self.assertEqual(result["code"], "SALES_MENU_SELECTION_FAILED")
        self.assertEqual(
            result["message"],
            "판매 메뉴를 선택했지만 선택 상태를 확인하지 못했습니다.",
        )
        order_edit.set_edit_text.assert_not_called()
        window.online_sales_menu.invoke.assert_not_called()
        window.target.invoke.assert_not_called()

    def test_missing_online_sales_has_stage_specific_error(self) -> None:
        window = NavigationWindow()

        def select_sales_without_submenu() -> None:
            window.sales_menu.element_info.class_name = "selected"

        window.sales_menu.invoke.side_effect = select_sales_without_submenu

        with patch("dps.dps_ui_automation.time.sleep"):
            result = DpsUiAutomation().navigate_to_online_sales_purchase_request_list(
                window=window,
                validate_target=lambda: (True, {"safe": True}),
                timeout=0.1,
            )

        self.assertEqual(result["code"], "ONLINE_SALES_MENU_NOT_FOUND")
        self.assertEqual(
            result["message"],
            "판매 메뉴에서 온라인판매 항목을 찾지 못했습니다.",
        )
        window.target.invoke.assert_not_called()

    def test_duplicate_online_sales_uses_left_menu_only(self) -> None:
        window = NavigationWindow()
        body = FakeElement("", "Group", class_name="content")
        body_online_sales = FakeElement(
            "온라인판매",
            "Hyperlink",
            parent=body,
            left=900,
            top=350,
        )
        window.elements.append(body_online_sales)

        with patch("dps.dps_ui_automation.time.sleep"):
            result = DpsUiAutomation().navigate_to_online_sales_purchase_request_list(
                window=window,
                validate_target=lambda: (True, {"safe": True}),
                timeout=0.1,
            )

        self.assertTrue(result["success"])
        window.online_sales_menu.invoke.assert_called_once()
        body_online_sales.invoke.assert_not_called()

    def test_duplicate_purchase_request_uses_online_sales_child_only(self) -> None:
        window = NavigationWindow()
        body_target = FakeElement(
            "구매요청리스트",
            "Hyperlink",
            left=950,
            top=420,
        )
        original_open = window._open_online_sales_menu

        def expose_both_targets() -> None:
            original_open()
            if body_target not in window.elements:
                window.elements.append(body_target)

        window.online_sales_menu.invoke.side_effect = expose_both_targets

        with patch("dps.dps_ui_automation.time.sleep"):
            result = DpsUiAutomation().navigate_to_online_sales_purchase_request_list(
                window=window,
                validate_target=lambda: (True, {"safe": True}),
                timeout=0.1,
            )

        self.assertTrue(result["success"])
        window.target.invoke.assert_called_once()
        body_target.invoke.assert_not_called()

    def test_closed_online_sales_is_expanded_before_target_click(self) -> None:
        window = NavigationWindow(online_sales_expanded=False)
        automation = DpsUiAutomation()

        result = automation.navigate_to_online_sales_purchase_request_list(
            window=window,
            validate_target=lambda: (True, {"safe": True}),
            timeout=1.0,
        )

        self.assertTrue(result["success"])
        window.online_sales_menu.invoke.assert_called_once()
        window.target.invoke.assert_called_once()

    def test_expanded_online_sales_is_not_clicked_again(self) -> None:
        window = NavigationWindow(online_sales_expanded=True)
        automation = DpsUiAutomation()

        result = automation.navigate_to_online_sales_purchase_request_list(
            window=window,
            validate_target=lambda: (True, {"safe": True}),
            timeout=1.0,
        )

        self.assertTrue(result["success"])
        window.online_sales_menu.invoke.assert_not_called()
        window.target.invoke.assert_called_once()

    def test_missing_purchase_request_target_stops_without_input(self) -> None:
        window = NavigationWindow(expose_target=False)
        automation = DpsUiAutomation()
        automation.perform_lookup = Mock()  # type: ignore[method-assign]

        with patch("dps.dps_ui_automation.time.sleep"):
            result = automation.navigate_to_online_sales_purchase_request_list(
                window=window,
                validate_target=lambda: (True, {"safe": True}),
                timeout=0.1,
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "PURCHASE_REQUEST_LIST_NOT_FOUND")
        self.assertEqual(
            result["message"],
            "온라인판매 메뉴에서 구매요청리스트를 찾지 못했습니다.",
        )
        self.assertEqual(
            result["details"]["navigation_stage"],
            "purchase_request_list",
        )
        automation.perform_lookup.assert_not_called()

    def test_similar_menu_text_is_never_clicked(self) -> None:
        window = NavigationWindow(expose_target=False)
        automation = DpsUiAutomation()

        with patch("dps.dps_ui_automation.time.sleep"):
            result = automation.navigate_to_online_sales_purchase_request_list(
                window=window,
                validate_target=lambda: (True, {"safe": True}),
                timeout=0.1,
            )

        self.assertEqual(result["code"], "PURCHASE_REQUEST_LIST_NOT_FOUND")
        window.misleading_purchase_request.invoke.assert_not_called()
        window.misleading_order_lookup.invoke.assert_not_called()
        window.purchase_menu.invoke.assert_not_called()

    def test_existing_verified_page_skips_menu_navigation(self) -> None:
        window = FakeWindow()
        automation = DpsUiAutomation()

        result = automation.navigate_to_online_sales_purchase_request_list(
            window=window,
            validate_target=lambda: (True, {"safe": True}),
            timeout=0.1,
        )

        self.assertTrue(result["success"])
        self.assertFalse(result["navigation_attempted"])

    def test_generic_query_text_alone_is_not_purchase_page(self) -> None:
        window = FakeWindow()
        window.elements = [
            FakeElement("검색어", "Edit"),
            FakeElement("조회", "Button"),
        ]

        verification = DpsUiAutomation().verify_purchase_request_page(window)

        self.assertFalse(verification.ok)
        self.assertIsNone(verification.edit)

    def test_md_small_item_auto_order_is_never_purchase_request_page(self) -> None:
        window = FakeWindow()
        window.elements.append(FakeElement("MD/소물자동주문", "Text"))

        verification = DpsUiAutomation().verify_purchase_request_page(window)

        self.assertFalse(verification.ok)
        self.assertIn("명시적으로 제외", verification.reason)

    def test_navigation_diagnostics_include_required_uia_fields(self) -> None:
        values = DpsUiAutomation().collect_dps_navigation_diagnostics(
            NavigationWindow()
        )

        purchase = next(value for value in values if value["name"] == "구매")
        self.assertTrue(
            {
                "name",
                "control_type",
                "automation_id",
                "class_name",
                "parent_name",
                "depth",
                "rectangle",
                "invoke_available",
                "selected",
                "expanded",
            }.issubset(purchase)
        )

    def test_navigation_tree_skips_only_broken_branch_and_logs_full_error(
        self,
    ) -> None:
        class TreeElement(FakeElement):
            def __init__(
                self,
                name: str,
                control_type: str,
                children: list["TreeElement"] | None = None,
                *,
                broken: bool = False,
            ) -> None:
                super().__init__(name, control_type)
                self._children = children or []
                self._broken = broken
                self.element_info.runtime_id = [len(name), len(control_type)]
                self.element_info.handle = 202
                self.element_info.framework_id = "Chrome"

            def children(self) -> list["TreeElement"]:
                if self._broken:
                    raise AttributeError("broken UIA branch")
                return list(self._children)

        target = TreeElement("target", "Hyperlink")
        healthy = TreeElement("healthy", "Pane", [target])
        broken = TreeElement("broken", "Pane", broken=True)
        root = TreeElement("window", "Window", [broken, healthy])
        stream = io.StringIO()
        logger = logging.getLogger("test_navigation_tree_branch")
        logger.handlers = []
        logger.propagate = False
        logger.setLevel(logging.INFO)
        logger.addHandler(logging.StreamHandler(stream))

        values = DpsUiAutomation(
            navigation_logger=logger
        ).collect_dps_navigation_diagnostics(root)

        self.assertIn("target", [value["name"] for value in values])
        target_value = next(value for value in values if value["name"] == "target")
        self.assertEqual(target_value["runtime_id"], [6, 9])
        self.assertEqual(target_value["native_window_handle"], 202)
        self.assertEqual(target_value["framework_id"], "Chrome")
        log = stream.getvalue()
        self.assertIn("exception_type=AttributeError", log)
        self.assertIn("exception_message=broken UIA branch", log)
        self.assertIn('"control_type": "Pane"', log)
        self.assertIn("Traceback (most recent call last)", log)

    def test_invalid_url_after_sales_menu_stops_before_online_sales_click(self) -> None:
        window = NavigationWindow()
        automation = DpsUiAutomation()
        validations = iter(
            [
                (True, {"selected_page_address_matches": True}),
                (True, {"selected_page_address_matches": True}),
                (
                    False,
                    {
                        "selected_page_address_matches": False,
                        "current_url": "https://example.com/",
                    },
                ),
            ]
        )

        result = automation.navigate_to_online_sales_purchase_request_list(
            window=window,
            validate_target=lambda: next(validations),
            timeout=0.1,
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "ONLINE_SALES_MENU_NOT_FOUND")
        window.sales_menu.invoke.assert_called_once()
        window.online_sales_menu.invoke.assert_not_called()
        window.target.invoke.assert_not_called()

    def test_final_page_verification_failure_never_enters_order_number(self) -> None:
        window = NavigationWindow(valid_final_page=False)
        automation = DpsUiAutomation()
        automation.perform_lookup = Mock()  # type: ignore[method-assign]

        with patch(
            "dps.dps_ui_automation.time.monotonic",
            side_effect=[0.0, 0.0, 1.0],
        ):
            result = automation.navigate_to_online_sales_purchase_request_list(
                window=window,
                validate_target=lambda: (True, {"safe": True}),
                timeout=0.1,
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "PURCHASE_REQUEST_PAGE_NOT_VERIFIED")
        automation.perform_lookup.assert_not_called()


class AgentLookupGuardTests(unittest.TestCase):
    def _agent_fixture(
        self,
        directory: str,
    ) -> tuple[DpsWindowsAgent, Mock, Mock, TabCandidate]:
        root = Path(directory)
        store = ConnectionStore(
            root / "connection.json",
            state_path=root / "state.json",
        )
        manager = Mock()
        manager.allowed_hosts = ("dps2u.co.kr",)
        manager.capture_previous_context.return_value = SimpleNamespace(
            foreground_hwnd=202,
            window_title="Naver TV Bot",
            selected_tab_title="Naver TV Bot",
        )
        manager.restore_previous_context.return_value = True
        manager.foreground_hwnd.return_value = 101
        ui = Mock(spec=DpsUiAutomation)
        agent = DpsWindowsAgent(
            store=store,
            tab_manager=manager,
            ui_automation=ui,
        )
        candidate = TabCandidate(
            hwnd=101,
            window=FakeWindow(),
            window_title="Samsung DPS 2.0 - Google Chrome",
            tab=FakeElement("Samsung DPS 2.0", "TabItem"),
            tab_title="Samsung DPS 2.0",
            score=400,
            current_url="https://dps2u.co.kr/dpsweb/main.do",
        )
        agent._select_current_dps = Mock(  # type: ignore[method-assign]
            return_value=(candidate, None)
        )
        return agent, manager, ui, candidate

    def test_login_failure_stops_before_navigation_and_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent, _, ui, _ = self._agent_fixture(directory)
            agent._detect_candidate_state = Mock(  # type: ignore[method-assign]
                return_value={
                    "login_state": "LOGIN_REQUIRED",
                    "login_reason": "로그인 URL 키워드 발견",
                    "login_signals": {"login_page": True},
                    "current_page": "UNKNOWN",
                    "current_page_label": "알 수 없음",
                    "current_url": "https://dps2u.co.kr/dpsweb/login.do",
                    "current_selected_tab_title": "Samsung DPS 2.0",
                }
            )

            with patch("dps.agent_server.LOGGER"):
                result = agent.lookup("2026071112345678")

            self.assertFalse(result["success"])
            self.assertEqual(result["code"], "DPS_LOGIN_REQUIRED")
            ui.navigate_to_online_sales_purchase_request_list.assert_not_called()
            ui.perform_lookup.assert_not_called()

    def test_logged_in_home_calls_navigation_before_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent, _, ui, _ = self._agent_fixture(directory)
            agent._detect_candidate_state = Mock(  # type: ignore[method-assign]
                return_value={
                    "login_state": "LOGGED_IN",
                    "login_reason": "logout_found",
                    "login_signals": {"logout_found": True},
                    "current_page": "HOME",
                    "current_page_label": "DPS 홈",
                    "current_url": "https://dps2u.co.kr/dpsweb/main.do",
                    "current_selected_tab_title": "Samsung DPS 2.0",
                }
            )
            ui.navigate_to_online_sales_purchase_request_list.return_value = {
                "ok": False,
                "success": False,
                "code": "DPS_NAVIGATION_FAILED",
                "message": "DPS 홈 화면에서 구매요청리스트로 이동하지 못했습니다.",
                "details": {
                    "menu_found": True,
                    "target_menu_found": False,
                },
            }

            with patch("dps.agent_server.LOGGER"):
                result = agent.lookup("2026071112345678")

            self.assertEqual(result["code"], "DPS_NAVIGATION_FAILED")
            ui.navigate_to_online_sales_purchase_request_list.assert_called_once()
            ui.perform_lookup.assert_not_called()

    def test_logged_in_state_enables_lookup_button(self) -> None:
        self.assertFalse(
            _dps_lookup_is_disabled({"login_state": "LOGGED_IN"})
        )
        self.assertTrue(
            _dps_lookup_is_disabled({"login_state": "LOGIN_UNCERTAIN"})
        )
        self.assertTrue(
            _dps_lookup_is_disabled({"login_state": "LOGIN_REQUIRED"})
        )
        self.assertFalse(
            _dps_lookup_is_disabled(
                {"connected": True, "login_state": "LOGIN_UNCERTAIN"}
            )
        )

    def test_existing_runtime_connection_is_reused_without_rediscovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent, manager, _, candidate = self._agent_fixture(directory)
            agent.connection = RuntimeConnection(
                hwnd=candidate.hwnd,
                window_title=candidate.window_title,
                tab_title=candidate.tab_title,
                connected_at="2026-07-28T11:00:00+09:00",
            )
            manager.candidate_for_connection.return_value = candidate

            result = agent.ensure_connection(select_tab=False)

            self.assertTrue(result["success"])
            self.assertEqual(result["code"], "CONNECTION_REUSED")
            manager.find_candidates.assert_not_called()

    def test_verified_candidate_is_remembered_without_reselecting_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent, manager, _, candidate = self._agent_fixture(directory)
            agent._select_current_dps = DpsWindowsAgent._select_current_dps.__get__(
                agent
            )
            manager.candidate_for_connection.return_value = None
            manager.chrome_windows.return_value = [candidate.window]
            manager.find_candidates.return_value = [candidate]
            manager.select_candidate.return_value = (True, candidate.tab_title)

            result = agent.ensure_connection(select_tab=False)

            self.assertTrue(result["success"])
            self.assertEqual(result["code"], "AUTO_CONNECTED")
            manager.select_candidate.assert_not_called()
            self.assertIs(agent.connection.tab, candidate.tab)
            self.assertEqual(agent.connection.current_url, candidate.current_url)

    def test_chatgpt_active_tab_does_not_prevent_selecting_existing_dps_tab(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent, manager, _, candidate = self._agent_fixture(directory)
            agent._select_current_dps = DpsWindowsAgent._select_current_dps.__get__(
                agent
            )
            manager.selected_tab_title.return_value = "ChatGPT"
            manager.chrome_windows.return_value = [candidate.window]
            manager.find_candidates.return_value = [candidate]
            manager.select_candidate.return_value = (True, "Samsung DPS 2.0")

            result = agent.ensure_connection()

            self.assertTrue(result["success"])
            manager.select_candidate.assert_not_called()
            self.assertEqual(agent.connection.tab_title, "Samsung DPS 2.0")

    def test_repeated_status_poll_preserves_connection_while_chatgpt_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent, manager, _, candidate = self._agent_fixture(directory)
            agent._remember_connection(candidate)
            manager.candidate_for_connection.return_value = candidate
            manager.is_tab_selected.return_value = False

            first = agent.status()
            second = agent.status()

            self.assertTrue(first["connected"])
            self.assertTrue(second["connected"])
            self.assertEqual(second["connected_tab_title"], candidate.tab_title)
            self.assertEqual(second["login_state"], "LOGIN_UNCERTAIN")
            self.assertIsNotNone(agent.connection)
            manager.select_candidate.assert_not_called()

    def test_passive_status_does_not_scan_or_clear_saved_tab(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent, manager, _, candidate = self._agent_fixture(directory)
            agent._remember_connection(candidate)
            manager.candidate_for_connection.side_effect = [candidate, None]
            manager.last_connection_failure_reason = "TAB_CLOSED"
            manager.is_tab_selected.return_value = False

            self.assertTrue(agent.status()["connected"])
            closed = agent.status()

            # Passive /status validates only HWND metadata and never scans tabs.
            self.assertTrue(closed["connected"])
            self.assertEqual(agent.connection_status, "CONNECTED")
            self.assertIsNotNone(agent.connection)
            manager.candidate_for_connection.assert_not_called()

    def test_transient_uia_failure_preserves_runtime_connection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent, manager, _, candidate = self._agent_fixture(directory)
            agent._remember_connection(candidate)
            manager.candidate_for_connection.return_value = None
            manager.last_connection_failure_reason = "UIA_READ_FAILED"

            status = agent.status()

            self.assertTrue(status["connected"])
            self.assertEqual(status["connected_tab_title"], candidate.tab_title)
            self.assertIsNotNone(agent.connection)

    def test_auto_connection_enables_lookup_without_manual_connection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent, manager, _, candidate = self._agent_fixture(directory)
            manager.chrome_windows.return_value = [candidate.window]
            manager.find_candidates.return_value = [candidate]

            connected = agent.ensure_connection()
            status = {
                "connected": connected["connected"],
                "login_state": "LOGIN_UNCERTAIN",
            }

            self.assertTrue(connected["success"])
            self.assertFalse(_dps_lookup_is_disabled(status))

    def test_candidate_listing_and_auto_connection_use_same_verified_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent, manager, _, candidate = self._agent_fixture(directory)
            manager.chrome_windows.return_value = [candidate.window]
            manager.find_candidates.return_value = [candidate]

            listed = agent.list_chrome_windows()
            connected = agent.ensure_connection()

            self.assertEqual(listed["windows"][0]["hwnd"], connected["connected_hwnd"])
            self.assertEqual(
                listed["windows"][0]["tabs"][0]["title"],
                connected["connected_tab_title"],
            )
            self.assertTrue(listed["windows"][0]["tabs"][0]["dps_url_verified"])

    def test_manual_connection_checks_generic_title_after_false_positive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent, manager, _, false_positive = self._agent_fixture(directory)
            actual_dps = TabCandidate(
                hwnd=false_positive.hwnd,
                window=false_positive.window,
                window_title=false_positive.window_title,
                tab=FakeElement("메인", "TabItem"),
                tab_title="메인",
                score=0,
            )
            manager.window_from_handle.return_value = false_positive.window
            manager.find_candidates.return_value = [false_positive, actual_dps]
            manager.select_candidate.side_effect = [
                (False, "DPS_TAB_VERIFICATION_FAILED"),
                (True, actual_dps.tab_title),
            ]
            agent._detect_candidate_state = Mock(  # type: ignore[method-assign]
                return_value={
                    "login_state": "LOGGED_IN",
                    "current_page": "HOME",
                    "current_page_label": "DPS 홈",
                }
            )

            result = agent.connect_window_by_handle(false_positive.hwnd)

            self.assertTrue(result["success"])
            self.assertEqual(agent.connection.tab_title, "메인")
            manager.find_candidates.assert_called_once_with([false_positive.window])
            self.assertEqual(manager.select_candidate.call_count, 2)

    def test_selected_existing_tab_home_ui_is_logged_in_without_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent, manager, _, candidate = self._agent_fixture(directory)
            candidate.window.elements = [
                FakeElement("로그아웃", "Button"),
                FakeElement("경영정보", "Text"),
                FakeElement("주문/배송", "Hyperlink"),
            ]
            agent.ui = DpsUiAutomation()
            manager.current_address_details.return_value = AddressReadResult(
                "", "", "NONE"
            )
            manager.selected_tab_title.return_value = "Samsung DPS 2.0"
            manager.is_tab_selected.return_value = True
            manager.matches_dps_title.return_value = True

            result = agent._detect_candidate_state(candidate)

            self.assertEqual(result["login_state"], "LOGGED_IN")
            self.assertEqual(result["current_page"], "HOME")

    def test_title_only_without_url_or_dps_ui_is_login_uncertain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent, manager, _, candidate = self._agent_fixture(directory)
            candidate.window.elements = []
            agent.ui = DpsUiAutomation()
            manager.current_address_details.return_value = AddressReadResult(
                "", "", "NONE"
            )
            manager.selected_tab_title.return_value = "Samsung DPS 2.0"
            manager.is_tab_selected.return_value = True
            manager.matches_dps_title.return_value = True

            result = agent._detect_candidate_state(candidate)

            self.assertEqual(result["login_state"], "LOGIN_UNCERTAIN")

    def test_existing_tab_not_found_never_opens_browser_or_tab(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent, manager, _, candidate = self._agent_fixture(directory)
            agent._select_current_dps = DpsWindowsAgent._select_current_dps.__get__(
                agent
            )
            manager.chrome_windows.return_value = [candidate.window]
            manager.find_candidates.return_value = []

            result = agent.open_browser()

            self.assertFalse(result["success"])
            self.assertEqual(result["code"], "DPS_TAB_NOT_FOUND")
            self.assertFalse(result["new_tab_opened"])

    def test_lookup_path_never_creates_chrome_process_or_new_tab(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent, _, _, _ = self._agent_fixture(directory)
            agent._detect_candidate_state = Mock(  # type: ignore[method-assign]
                return_value={
                    "login_state": "LOGIN_REQUIRED",
                    "login_reason": "로그인 필요",
                    "login_signals": {},
                    "current_page": "UNKNOWN",
                    "current_page_label": "알 수 없음",
                    "current_url": "",
                    "current_selected_tab_title": "Samsung DPS 2.0",
                }
            )
            result = agent.lookup("2026071112345678")

            self.assertFalse(result["success"])


class DpsTitleScoringTests(unittest.TestCase):
    def test_select_candidate_success_does_not_fail_while_logging_ui_hits(self) -> None:
        manager = ChromeTabManager(desktop_factory=lambda **_: None)
        tab = FakeElement("메인", "TabItem")
        candidate = TabCandidate(
            hwnd=101,
            window=object(),
            window_title="메인 - Chrome",
            tab=tab,
            tab_title="메인",
            score=1000,
            current_url="https://dps2u.co.kr/dpsweb/main.do",
        )
        manager.selected_tab_title = Mock(return_value="메인")  # type: ignore[method-assign]
        manager.tabs_in_window = Mock(return_value=[tab])  # type: ignore[method-assign]
        manager.activate_window = Mock(return_value=True)  # type: ignore[method-assign]
        manager.foreground_hwnd = Mock(return_value=101)  # type: ignore[method-assign]
        manager.is_tab_selected = Mock(return_value=True)  # type: ignore[method-assign]
        manager.current_address_details = Mock(  # type: ignore[method-assign]
            return_value=AddressReadResult(
                "dps2u.co.kr/dpsweb/main.do",
                "dps2u.co.kr/dpsweb/main.do",
                "AddressBar",
            )
        )
        manager.page_ui_dps_hits = Mock(return_value=["로그아웃"])  # type: ignore[method-assign]

        selected, value = manager.select_candidate(candidate, timeout=0.1)

        self.assertTrue(selected)
        self.assertEqual(value, "메인")

    def test_saved_tab_is_valid_even_when_another_tab_is_selected(self) -> None:
        manager = ChromeTabManager(desktop_factory=lambda **_: None)
        window = SimpleNamespace(handle=101)
        dps_tab = FakeElement("메인", "TabItem")
        chatgpt_tab = FakeElement("ChatGPT", "TabItem")
        connection = RuntimeConnection(
            hwnd=101,
            window_title="ChatGPT - Chrome",
            tab_title="메인",
            connected_at="2026-07-28T12:00:00+09:00",
            tab=dps_tab,
            current_url="https://dps2u.co.kr/dpsweb/main.do",
        )
        manager.is_window = Mock(return_value=True)  # type: ignore[method-assign]
        manager.window_from_handle = Mock(return_value=window)  # type: ignore[method-assign]
        manager.tabs_in_window = Mock(  # type: ignore[method-assign]
            return_value=[dps_tab, chatgpt_tab]
        )
        manager.window_title = Mock(return_value="ChatGPT - Chrome")  # type: ignore[method-assign]
        manager.is_tab_selected = Mock(  # type: ignore[method-assign]
            side_effect=lambda tab: tab is chatgpt_tab
        )

        candidate = manager.candidate_for_connection(connection)

        self.assertIsNotNone(candidate)
        self.assertIs(candidate.tab, dps_tab)
        self.assertEqual(candidate.current_url, connection.current_url)

    def test_saved_tab_is_invalid_after_that_tab_is_closed(self) -> None:
        manager = ChromeTabManager(desktop_factory=lambda **_: None)
        window = SimpleNamespace(handle=101)
        dps_tab = FakeElement("메인", "TabItem")
        connection = RuntimeConnection(
            hwnd=101,
            window_title="Chrome",
            tab_title="메인",
            connected_at="2026-07-28T12:00:00+09:00",
            tab=dps_tab,
        )
        manager.is_window = Mock(return_value=True)  # type: ignore[method-assign]
        manager.window_from_handle = Mock(return_value=window)  # type: ignore[method-assign]
        manager.tabs_in_window = Mock(return_value=[])  # type: ignore[method-assign]

        self.assertIsNone(manager.candidate_for_connection(connection))

    def test_candidate_discovery_uses_selected_tab_url_not_dps_words(self) -> None:
        manager = ChromeTabManager(desktop_factory=lambda **_: None)
        window = SimpleNamespace(handle=101)
        misleading = FakeElement("Samsung DPS 검토 - ChatGPT", "TabItem")
        actual = FakeElement("메인", "TabItem")
        manager.capture_previous_context = Mock(  # type: ignore[method-assign]
            return_value=SimpleNamespace(
                foreground_hwnd=202,
                window_title="Naver TV Bot",
                selected_tab_title="Naver TV Bot",
            )
        )
        manager.tabs_in_window = Mock(  # type: ignore[method-assign]
            return_value=[misleading, actual]
        )
        manager.window_title = Mock(return_value="Samsung DPS 검토 - Chrome")  # type: ignore[method-assign]
        manager.address_for_selected_tab = Mock(  # type: ignore[method-assign]
            side_effect=[
                AddressReadResult(
                    "chatgpt.com/c/example",
                    "chatgpt.com/c/example",
                    "AddressBar",
                ),
                AddressReadResult(
                    "dps2u.co.kr/dpsweb/main.do",
                    "dps2u.co.kr/dpsweb/main.do",
                    "AddressBar",
                ),
            ]
        )
        manager.restore_previous_context = Mock(return_value=True)  # type: ignore[method-assign]

        candidates = manager.find_candidates([window])

        self.assertEqual([candidate.tab_title for candidate in candidates], ["메인"])
        self.assertEqual(
            candidates[0].current_url,
            "dps2u.co.kr/dpsweb/main.do",
        )
        manager.restore_previous_context.assert_called_once()

    def test_requested_priority_is_preserved(self) -> None:
        scores = [
            ChromeTabManager.dps_title_score("Samsung DPS 2.0"),
            ChromeTabManager.dps_title_score("Samsung DPS"),
            ChromeTabManager.dps_title_score("삼성 DPS"),
            ChromeTabManager.dps_title_score("DPS 2.0"),
            ChromeTabManager.dps_title_score("DPS 기타"),
        ]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertGreater(scores[-1], 0)
        self.assertEqual(ChromeTabManager.dps_title_score("ChatGPT"), 0)

    def test_selected_tab_title_is_mandatory_for_safety(self) -> None:
        manager = ChromeTabManager(desktop_factory=lambda **_: None)
        candidate = TabCandidate(
            hwnd=101,
            window=object(),
            window_title="Google Chrome",
            tab=object(),
            tab_title="Samsung DPS 2.0",
            score=400,
        )
        manager.is_window = Mock(return_value=True)  # type: ignore[method-assign]
        manager.foreground_hwnd = Mock(return_value=101)  # type: ignore[method-assign]
        manager.is_tab_selected = Mock(return_value=False)  # type: ignore[method-assign]
        manager.selected_tab_title = Mock(return_value="ChatGPT")  # type: ignore[method-assign]

        ok, checks = manager.validate_selected_candidate(candidate)

        self.assertFalse(ok)
        self.assertFalse(checks["tab_element_selected"])
        self.assertFalse(checks["selected_tab_title_matches"])

    def test_dps_words_in_chatgpt_title_do_not_pass_address_check(self) -> None:
        manager = ChromeTabManager(desktop_factory=lambda **_: None)
        candidate = TabCandidate(
            hwnd=101,
            window=object(),
            window_title="Samsung DPS 자동화 검토 - ChatGPT - Google Chrome",
            tab=object(),
            tab_title="Samsung DPS 자동화 검토 - ChatGPT",
            score=400,
        )
        manager.is_window = Mock(return_value=True)  # type: ignore[method-assign]
        manager.foreground_hwnd = Mock(return_value=101)  # type: ignore[method-assign]
        manager.is_tab_selected = Mock(return_value=True)  # type: ignore[method-assign]
        manager.selected_tab_title = Mock(  # type: ignore[method-assign]
            return_value="Samsung DPS 자동화 검토 - ChatGPT"
        )
        manager.current_address = Mock(  # type: ignore[method-assign]
            return_value="https://chatgpt.com/c/example"
        )

        ok, checks = manager.validate_selected_candidate(candidate)

        self.assertFalse(ok)
        self.assertTrue(checks["selected_tab_title_matches"])
        self.assertFalse(checks["selected_page_address_matches"])
        self.assertEqual(checks["selected_page_host"], "chatgpt.com")

    def test_generic_main_title_passes_only_with_exact_dps_host_and_identity(self) -> None:
        manager = ChromeTabManager(desktop_factory=lambda **_: None)
        candidate = TabCandidate(
            hwnd=101,
            window=object(),
            window_title="메인 - Google Chrome",
            tab=object(),
            tab_title="메인",
            score=0,
        )
        manager.is_window = Mock(return_value=True)  # type: ignore[method-assign]
        manager.foreground_hwnd = Mock(return_value=101)  # type: ignore[method-assign]
        manager.is_tab_selected = Mock(return_value=True)  # type: ignore[method-assign]
        manager.selected_tab_title = Mock(return_value="메인")  # type: ignore[method-assign]
        manager.current_address = Mock(  # type: ignore[method-assign]
            return_value="https://dps2u.co.kr/dpsweb/main.do"
        )
        manager.page_ui_matches_dps = Mock(return_value=False)  # type: ignore[method-assign]

        ok, checks = manager.validate_selected_candidate(candidate)

        self.assertTrue(ok)
        self.assertTrue(checks["selected_tab_identity_matches"])
        self.assertFalse(checks["selected_tab_title_matches"])
        self.assertTrue(checks["selected_page_address_matches"])

    def test_same_selected_tab_allows_dps_dynamic_page_title(self) -> None:
        manager = ChromeTabManager(desktop_factory=lambda **_: None)
        candidate = TabCandidate(
            hwnd=101,
            window=object(),
            window_title="메인 - Google Chrome",
            tab=object(),
            tab_title="메인",
            score=0,
        )
        manager.is_window = Mock(return_value=True)  # type: ignore[method-assign]
        manager.foreground_hwnd = Mock(return_value=101)  # type: ignore[method-assign]
        manager.is_tab_selected = Mock(return_value=True)  # type: ignore[method-assign]
        manager.selected_tab_title = Mock(
            return_value="메인:판매조회 > 판매생성/변경/조회 > 판매"
        )
        manager.current_address = Mock(  # type: ignore[method-assign]
            return_value="https://dps2u.co.kr/dpsweb/main.do"
        )
        manager.page_ui_matches_dps = Mock(return_value=True)  # type: ignore[method-assign]

        ok, checks = manager.validate_selected_candidate(candidate)

        self.assertTrue(ok)
        self.assertTrue(checks["tab_element_selected"])
        self.assertFalse(checks["selected_tab_identity_matches"])
        self.assertTrue(checks["selected_page_address_matches"])


class ConnectionStoreTests(unittest.TestCase):
    def test_hwnd_is_never_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ConnectionStore(
                root / "dps_connection.json",
                state_path=root / "dps_agent_state.json",
            )
            store.save(
                {
                    "browser": "chrome",
                    "tab_title_keywords": ["Samsung DPS"],
                    "auto_connect": True,
                    "connected_hwnd": 123456,
                }
            )

            saved = json.loads(store.path.read_text(encoding="utf-8"))
            self.assertNotIn("connected_hwnd", saved)
            self.assertNotIn("hwnd", saved)

    def test_corrupt_json_recovers_to_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "dps_connection.json"
            path.write_text("{broken", encoding="utf-8")
            store = ConnectionStore(path, state_path=root / "state.json")

            loaded = store.load()

            self.assertTrue(loaded["auto_connect"])
            self.assertIn("Samsung DPS 2.0", loaded["tab_title_keywords"])
            self.assertTrue(path.with_suffix(".json.corrupt").exists())


class InputSafetyTests(unittest.TestCase):
    def test_login_diagnostic_elements_include_control_type_and_name(self) -> None:
        window = FakeWindow()
        automation = DpsUiAutomation()

        elements = automation.diagnostic_elements(window)

        self.assertIn(
            {"control_type": "Text", "name": "구매요청리스트"},
            elements,
        )
        self.assertIn(
            {"control_type": "Edit", "name": "온라인판매 주문번호"},
            elements,
        )

    def test_no_input_or_click_when_tab_validation_fails(self) -> None:
        window = FakeWindow()
        automation = DpsUiAutomation()

        result = automation.perform_lookup(
            window=window,
            order_number="2026071112345678",
            validate_target=lambda: (
                False,
                {
                    "target_window_foreground": True,
                    "tab_element_selected": False,
                    "selected_tab_title_matches": False,
                },
            ),
            result_timeout=0.1,
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "INPUT_SAFETY_CHECK_FAILED")
        window.edit.click_input.assert_not_called()
        window.edit.set_edit_text.assert_not_called()
        window.edit.type_keys.assert_not_called()
        window.button.invoke.assert_not_called()
        window.button.click_input.assert_not_called()

    def test_final_page_verification_failure_causes_zero_order_input(self) -> None:
        window = FakeWindow()
        window.elements.remove(window.marker)
        automation = DpsUiAutomation()

        result = automation.perform_lookup(
            window=window,
            order_number="2026071112345678",
            validate_target=lambda: (True, {"safe": True}),
            result_timeout=0.1,
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "PURCHASE_REQUEST_PAGE_NOT_VERIFIED")
        window.edit.click_input.assert_not_called()
        window.edit.set_edit_text.assert_not_called()
        window.button.invoke.assert_not_called()

    def test_chatgpt_tab_never_receives_order_number(self) -> None:
        window = FakeWindow()
        automation = DpsUiAutomation()

        result = automation.perform_lookup(
            window=window,
            order_number="2026071112345678",
            validate_target=lambda: (
                False,
                {
                    "selected_tab_title": "Samsung DPS 검토 - ChatGPT",
                    "selected_tab_title_matches": True,
                    "current_url": "https://chatgpt.com/c/example",
                    "selected_page_address_matches": False,
                },
            ),
            result_timeout=0.1,
        )

        self.assertEqual(result["code"], "INPUT_SAFETY_CHECK_FAILED")
        window.edit.set_edit_text.assert_not_called()
        window.button.invoke.assert_not_called()
        window.button.click_input.assert_not_called()

    def test_verified_target_uses_uia_edit_and_button(self) -> None:
        window = FakeWindow()
        automation = DpsUiAutomation()
        snapshot = {
            "raw_result_texts": [
                "온라인판매 주문번호",
                "2026071112345678",
                "판매번호",
                "3141194318",
            ],
            "table_headers": ["온라인판매 주문번호", "판매번호"],
            "table_rows": [["2026071112345678", "3141194318"]],
            "elements": [],
        }
        automation.collect_result_snapshot = Mock(  # type: ignore[method-assign]
            return_value=snapshot
        )
        automation.wait_for_lookup_result = Mock(  # type: ignore[method-assign]
            return_value={"status": "complete", "snapshot": snapshot}
        )

        result = automation.perform_lookup(
            window=window,
            order_number="2026071112345678",
            validate_target=lambda: (
                True,
                {
                    "target_hwnd_valid": True,
                    "target_window_foreground": True,
                    "tab_element_selected": True,
                    "selected_tab_title_matches": True,
                },
            ),
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["automation_method"], "CHROME_TAB_UIA_V6")
        self.assertEqual(
            window.edit.set_edit_text.call_args_list[-1].args[0],
            "2026071112345678",
        )
        window.button.invoke.assert_called_once()

    def test_validation_loss_during_edit_stops_before_order_value(self) -> None:
        window = FakeWindow()
        automation = DpsUiAutomation()
        validations = iter(
            [
                (True, {"selected_tab_title_matches": True}),
                (False, {"selected_tab_title_matches": False}),
            ]
        )

        result = automation.perform_lookup(
            window=window,
            order_number="2026071112345678",
            validate_target=lambda: next(validations),
            result_timeout=0.1,
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "INPUT_SAFETY_CHECK_FAILED")
        self.assertEqual(window.edit.set_edit_text.call_args_list[0].args[0], "")
        self.assertNotIn(
            "2026071112345678",
            [call.args[0] for call in window.edit.set_edit_text.call_args_list],
        )
        window.edit.type_keys.assert_not_called()
        window.button.invoke.assert_not_called()

    def test_no_keyboard_fallback_when_value_pattern_fails(self) -> None:
        window = FakeWindow()
        window.edit.set_edit_text.side_effect = RuntimeError("ValuePattern unavailable")
        automation = DpsUiAutomation()

        result = automation.perform_lookup(
            window=window,
            order_number="2026071112345678",
            validate_target=lambda: (
                True,
                {
                    "target_hwnd_valid": True,
                    "target_window_foreground": True,
                    "tab_element_selected": True,
                    "selected_tab_title_matches": True,
                    "selected_page_address_matches": True,
                },
            ),
            result_timeout=0.1,
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "ORDER_INPUT_FAILED")
        window.edit.type_keys.assert_not_called()
        window.button.invoke.assert_not_called()

    def test_query_button_is_not_clicked_if_validation_later_fails(self) -> None:
        window = FakeWindow()
        automation = DpsUiAutomation()
        validations = iter(
            [
                (True, {"selected_page_address_matches": True}),
                (True, {"selected_page_address_matches": True}),
                (True, {"selected_page_address_matches": True}),
                (False, {"selected_page_address_matches": False}),
            ]
        )

        result = automation.perform_lookup(
            window=window,
            order_number="2026071112345678",
            validate_target=lambda: next(validations),
            result_timeout=0.1,
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "QUERY_SAFETY_CHECK_FAILED")
        window.button.invoke.assert_not_called()
        window.button.click_input.assert_not_called()


class DpsCompletionTests(unittest.TestCase):
    def test_navigation_success_is_separate_from_input_ready(self) -> None:
        window = FakeWindow()
        window.elements.remove(window.edit)
        window.elements.remove(window.button)

        verification = DpsUiAutomation().verify_purchase_request_page(window)

        self.assertTrue(verification.navigation_ok)
        self.assertFalse(verification.input_ready)
        self.assertFalse(verification.ok)

    def test_navigation_succeeds_even_when_edit_is_missing(self) -> None:
        window = FakeWindow()
        window.elements.remove(window.edit)
        window.elements.remove(window.button)

        result = DpsUiAutomation().navigate_to_online_sales_purchase_request_list(
            window=window,
            validate_target=lambda: (True, {"safe": True}),
            timeout=0.1,
        )

        self.assertTrue(result["success"])
        self.assertTrue(result["navigation_completed"])
        self.assertFalse(result["input_ready"])

    def test_missing_edit_returns_order_input_not_found(self) -> None:
        window = FakeWindow()
        window.elements.remove(window.edit)

        result = DpsUiAutomation().perform_lookup(
            window=window,
            order_number="2026071112345678",
            validate_target=lambda: (True, {"safe": True}),
            result_timeout=0.1,
        )

        self.assertEqual(result["code"], "ORDER_INPUT_NOT_FOUND")
        window.button.invoke.assert_not_called()

    def test_query_hyperlink_is_supported(self) -> None:
        window = FakeWindow()
        window.elements.remove(window.button)
        query = FakeElement("조회", "Hyperlink", class_name="btn_searchWhite bold")
        window.elements.append(query)

        result = DpsUiAutomation().find_query_action(
            window,
            order_edit=window.edit,
        )

        self.assertIs(result, query)

    def test_query_button_is_supported(self) -> None:
        window = FakeWindow()

        result = DpsUiAutomation().find_query_action(
            window,
            order_edit=window.edit,
        )

        self.assertIs(result, window.button)

    def test_unnamed_order_edit_without_exact_label_is_not_selected(self) -> None:
        window = FakeWindow()
        window.elements.remove(window.label)
        window.elements.remove(window.edit)
        window.elements.remove(window.button)
        merchant = FakeElement(
            "BGH1/(주)오제앤에스",
            "Edit",
            left=420,
            top=240,
        )
        order_edit = FakeElement(
            "￼",
            "Edit",
            left=420,
            top=340,
        )
        date_edit = FakeElement(
            "2026-07-28",
            "Edit",
            left=420,
            top=440,
        )
        query = FakeElement(
            "조회",
            "Hyperlink",
            class_name="btn_searchWhite bold",
            left=760,
            top=340,
        )
        window.elements.extend((merchant, order_edit, date_edit, query))

        result = DpsUiAutomation().find_order_edit(window)

        self.assertIsNone(result)

    def test_missing_query_returns_query_action_not_found(self) -> None:
        window = FakeWindow()
        window.elements.remove(window.button)

        result = DpsUiAutomation().perform_lookup(
            window=window,
            order_number="2026071112345678",
            validate_target=lambda: (True, {"safe": True}),
            result_timeout=0.1,
        )

        self.assertEqual(result["code"], "QUERY_ACTION_NOT_FOUND")
        window.edit.set_edit_text.assert_not_called()

    def test_query_in_other_screen_is_not_selected(self) -> None:
        window = FakeWindow()
        window.elements.remove(window.button)
        unrelated = FakeElement(
            "조회",
            "Hyperlink",
            class_name="btn_searchWhite",
            left=1400,
            top=800,
        )
        window.elements.append(unrelated)

        result = DpsUiAutomation().find_query_action(
            window,
            order_edit=window.edit,
        )

        self.assertIsNone(result)

    def test_input_value_mismatch_invokes_query_zero_times(self) -> None:
        window = FakeWindow()
        window.edit.set_edit_text.side_effect = lambda value: None

        result = DpsUiAutomation().perform_lookup(
            window=window,
            order_number="2026071112345678",
            validate_target=lambda: (True, {"safe": True}),
            result_timeout=0.1,
        )

        self.assertEqual(result["code"], "INPUT_VERIFY_FAILED")
        window.button.invoke.assert_not_called()
        window.button.click_input.assert_not_called()

    def test_no_result_is_a_successful_empty_response(self) -> None:
        window = FakeWindow()
        automation = DpsUiAutomation()
        before = {
            "raw_result_texts": [],
            "table_headers": [],
            "table_rows": [],
            "elements": [],
        }
        after = {
            "raw_result_texts": ["조회 결과가 없습니다"],
            "table_headers": [],
            "table_rows": [],
            "elements": [],
        }
        automation.collect_result_snapshot = Mock(return_value=before)  # type: ignore[method-assign]
        automation.wait_for_lookup_result = Mock(  # type: ignore[method-assign]
            return_value={"status": "no_result", "snapshot": after}
        )

        result = automation.perform_lookup(
            window=window,
            order_number="2026071112345678",
            validate_target=lambda: (True, {"safe": True}),
            result_timeout=0.1,
        )

        self.assertTrue(result["success"])
        self.assertFalse(result["found"])
        self.assertEqual(result["code"], "NO_DPS_RESULT")

    def test_single_result_row_is_parsed(self) -> None:
        snapshot = {
            "raw_result_texts": [],
            "table_headers": ["온라인판매 주문번호", "판매번호", "모델명", "수량"],
            "table_rows": [
                ["2026071112345678", "3141194318", "AF17B7538", "1"]
            ],
        }

        result = DpsUiAutomation().parse_lookup_result(
            snapshot,
            naver_order_id="2026071112345678",
        )

        self.assertTrue(result["found"])
        self.assertEqual(result["data"]["dps_sales_number"], "3141194318")
        self.assertEqual(result["data"]["model_name"], "AF17B7538")
        self.assertEqual(result["data"]["quantity"], 1)

    def test_multiple_rows_select_exact_naver_order(self) -> None:
        snapshot = {
            "raw_result_texts": [],
            "table_headers": ["온라인판매 주문번호", "판매번호"],
            "table_rows": [
                ["2026071199999999", "3000000001"],
                ["2026071112345678", "3141194318"],
            ],
        }

        result = DpsUiAutomation().parse_lookup_result(
            snapshot,
            naver_order_id="2026071112345678",
        )

        self.assertEqual(result["data"]["dps_sales_number"], "3141194318")

    def test_uncertain_parser_fields_remain_null(self) -> None:
        snapshot = {
            "raw_result_texts": [],
            "table_headers": ["온라인판매 주문번호", "판매번호"],
            "table_rows": [["2026071112345678", "3141194318"]],
        }

        result = DpsUiAutomation().parse_lookup_result(
            snapshot,
            naver_order_id="2026071112345678",
        )

        self.assertIsNone(result["data"]["installation_date"])
        self.assertIsNone(result["data"]["assignment_status"])
        self.assertIsNone(result["data"]["delivery_status"])

    def test_full_order_number_is_masked_for_logs(self) -> None:
        masked = DpsUiAutomation._safe_log_text("2026071112345678")

        self.assertNotIn("2026071112345678", masked)
        self.assertTrue(masked.startswith("2026"))
        self.assertTrue(masked.endswith("5678"))

    def _cache_agent(self, directory: str) -> tuple[DpsWindowsAgent, Mock, Mock]:
        root = Path(directory)
        store = ConnectionStore(
            root / "connection.json",
            state_path=root / "state.json",
        )
        manager = Mock()
        manager.allowed_hosts = ("dps2u.co.kr",)
        manager.capture_previous_context.return_value = SimpleNamespace(
            foreground_hwnd=202,
            window_title="Naver TV Bot",
            selected_tab_title="Naver TV Bot",
        )
        manager.restore_previous_context.return_value = True
        ui = Mock(spec=DpsUiAutomation)
        agent = DpsWindowsAgent(
            store=store,
            tab_manager=manager,
            ui_automation=ui,
        )
        candidate = TabCandidate(
            hwnd=101,
            window=FakeWindow(),
            window_title="Samsung DPS 2.0 - Google Chrome",
            tab=FakeElement("Samsung DPS 2.0", "TabItem"),
            tab_title="Samsung DPS 2.0",
            score=400,
            current_url="https://dps2u.co.kr/dpsweb/main.do",
        )
        agent._select_current_dps = Mock(return_value=(candidate, None))  # type: ignore[method-assign]
        agent._detect_candidate_state = Mock(  # type: ignore[method-assign]
            return_value={
                "login_state": "LOGGED_IN",
                "login_reason": "logout_found",
                "login_signals": {"logout_found": True},
                "current_page": "HOME",
                "current_page_label": "DPS 홈",
                "current_url": "https://dps2u.co.kr/dpsweb/main.do",
                "current_selected_tab_title": "Samsung DPS 2.0",
            }
        )
        ui.navigate_to_online_sales_purchase_request_list.return_value = {
            "ok": True,
            "success": True,
            "code": "DPS_NAVIGATION_COMPLETE",
            "navigation_completed": True,
        }
        ui.perform_lookup.return_value = {
            "ok": True,
            "success": True,
            "found": True,
            "code": "LOOKUP_COMPLETE",
            "data": {
                "naver_order_id": "2026071112345678",
                "dps_sales_number": "3141194318",
            },
        }
        ui.mask_order_number.return_value = "20260711****5678"
        return agent, manager, ui

    def test_cache_is_saved_and_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent, _, ui = self._cache_agent(directory)
            cache_path = Path(directory) / "cache.json"
            with patch("dps.agent_server.CACHE_FILE", cache_path):
                first = agent.lookup("2026071112345678")
                second = agent.lookup("2026071112345678")

            self.assertTrue(first["success"])
            self.assertTrue(second["cached"])
            self.assertEqual(ui.perform_lookup.call_count, 1)
            self.assertIsNotNone(agent.last_dps_activity_at)
            self.assertIsNotNone(agent.next_keepalive_due_at)
            self.assertFalse(agent.keepalive_due)
            saved = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertIn("expires_at", saved["order:2026071112345678"])

    def test_force_refresh_bypasses_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent, _, ui = self._cache_agent(directory)
            cache_path = Path(directory) / "cache.json"
            with patch("dps.agent_server.CACHE_FILE", cache_path):
                agent.lookup("2026071112345678")
                agent.lookup("2026071112345678", force_refresh=True)

            self.assertEqual(ui.perform_lookup.call_count, 2)

    def test_restore_failure_does_not_override_lookup_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent, manager, _ = self._cache_agent(directory)
            manager.restore_previous_context.return_value = False
            cache_path = Path(directory) / "cache.json"
            with patch("dps.agent_server.CACHE_FILE", cache_path):
                result = agent.lookup("2026071112345678")

            self.assertTrue(result["success"])
            manager.restore_previous_context.assert_called_once()


class DpsIdentifierSelectionTests(unittest.TestCase):
    def test_order_id_has_priority_even_with_product_order_id(self) -> None:
        selected = select_dps_query_identifier(
            "2026062987651931", "2026062961090761"
        )
        self.assertEqual(selected.value, "2026062987651931")
        self.assertEqual(selected.type, "order_id")
        self.assertFalse(selected.fallback_used)

    def test_order_id_is_not_a_fallback(self) -> None:
        selected = select_dps_query_identifier("2026062987651931", "  ")
        self.assertEqual(selected.value, "2026062987651931")
        self.assertEqual(selected.type, "order_id")
        self.assertFalse(selected.fallback_used)

    def test_both_identifiers_missing(self) -> None:
        selected = select_dps_query_identifier(None, None)
        self.assertIsNone(selected.value)
        self.assertIsNone(selected.type)
        self.assertFalse(selected.fallback_used)
        self.assertEqual(selected.error, "DPS_ORDER_ID_MISSING")

    def test_long_identifier_and_leading_zero_are_preserved_as_text(self) -> None:
        value = "000012345678901234567890"
        selected = select_dps_query_identifier(value, "9")
        self.assertEqual(selected.value, value)
        self.assertIsInstance(selected.value, str)

    def test_order_value_is_trimmed_without_numeric_conversion(self) -> None:
        selected = select_dps_query_identifier(" 000123 ", "1")
        self.assertEqual(selected.value, "000123")

    def test_product_order_id_is_never_a_fallback(self) -> None:
        selected = select_dps_query_identifier(None, "2026062961090761")
        self.assertIsNone(selected.value)
        self.assertEqual(selected.error, "DPS_ORDER_ID_MISSING")


class StructuralOrderFieldTests(unittest.TestCase):
    @staticmethod
    def _window_with_fields(*fields: FakeElement) -> FakeWindow:
        window = FakeWindow()
        window.elements.remove(window.label)
        window.elements.remove(window.edit)
        label = FakeElement(
            "온라인판매 주문번호", "Text", left=260, top=340
        )
        label._rect = FakeRect(left=260, top=340, right=500, bottom=380)
        window.label = label
        window.elements.append(label)
        window.elements.extend(fields)
        return window

    def test_exact_label_right_edit_is_selected(self) -> None:
        target = FakeElement("￼", "Edit", left=520, top=340)
        window = self._window_with_fields(target)
        self.assertIs(DpsUiAutomation().find_order_edit(window), target)

    def test_seller_edit_is_excluded(self) -> None:
        seller = FakeElement(
            "￼", "Edit", automation_id="I_SELLERID", left=520, top=340
        )
        window = self._window_with_fields(seller)
        self.assertIsNone(DpsUiAutomation().find_order_edit(window))

    def test_date_edit_is_excluded(self) -> None:
        date = FakeElement(
            "2026-07-28",
            "Edit",
            automation_id="I_SDATE",
            class_name="calendar hasDatepicker",
            left=520,
            top=340,
        )
        window = self._window_with_fields(date)
        self.assertIsNone(DpsUiAutomation().find_order_edit(window))

    def test_same_parent_has_priority(self) -> None:
        row = FakeElement("search row", "Group")
        label = FakeElement(
            "온라인판매 주문번호", "Text", left=260, top=340, parent=row
        )
        label._rect = FakeRect(left=260, top=340, right=500, bottom=380)
        preferred = FakeElement("￼", "Edit", left=520, top=340, parent=row)
        other = FakeElement("￼", "Edit", left=540, top=340)
        window = FakeWindow()
        window.elements = [*window.elements[:3], label, preferred, other, window.button, window.marker]
        self.assertIs(DpsUiAutomation().find_order_edit(window), preferred)

    def test_tied_structural_candidates_are_rejected(self) -> None:
        first = FakeElement("￼", "Edit", left=520, top=340)
        second = FakeElement("￼", "Edit", left=520, top=340)
        window = self._window_with_fields(first, second)
        automation = DpsUiAutomation()
        self.assertIsNone(automation.find_order_edit(window))
        self.assertEqual(
            automation._last_field_resolution["status"], "FIELD_AMBIGUOUS"
        )

    def test_wrong_field_duplicate_blocks_query(self) -> None:
        window = FakeWindow()
        duplicate = FakeElement(
            "￼", "Edit", automation_id="I_SELLERID", left=900, top=220
        )
        duplicate._value = "2026071112345678"
        window.elements.append(duplicate)
        result = DpsUiAutomation().perform_lookup(
            window=window,
            order_number="2026071112345678",
            validate_target=lambda: (True, {"safe": True}),
            result_timeout=0.1,
        )
        self.assertEqual(result["code"], "WRONG_FIELD_INPUT")
        window.button.invoke.assert_not_called()
        window.button.click_input.assert_not_called()


class ExtendedResultParsingTests(unittest.TestCase):
    def test_actual_no_result_phrase_is_detected(self) -> None:
        automation = DpsUiAutomation()
        before = {
            "raw_result_texts": [],
            "table_headers": [],
            "table_rows": [],
        }
        after = {
            "raw_result_texts": ["조회된 결과가 없습니다."],
            "table_headers": [],
            "table_rows": [],
        }
        automation.collect_result_snapshot = Mock(return_value=after)  # type: ignore[method-assign]
        with patch("dps.dps_ui_automation.time.sleep"):
            result = automation.wait_for_lookup_result(
                FakeWindow(), before, timeout=0.1
            )
        self.assertEqual(result["status"], "no_result")
        parsed = automation.parse_lookup_result(
            {
                **after,
                "table_headers": ["온라인판매 주문번호", "DPS판매번호"],
                "table_rows": [["조회된 결과가 없습니다."]],
            },
            order_id="O-1",
            product_order_id="P-1",
            dps_query_value="P-1",
            dps_query_value_type="product_order_id",
        )
        self.assertIsNone(parsed["data"]["dps_sales_number"])
        self.assertIsNone(parsed["data"]["product_name"])
        self.assertEqual(parsed["data"]["product_order_id"], "P-1")

    def test_header_order_changes_are_supported(self) -> None:
        snapshot = {
            "raw_result_texts": [],
            "table_headers": ["모델명", "DPS 주문번호", "온라인판매 주문번호", "DPS 판매번호"],
            "table_rows": [["MODEL-A", "DPS-O-1", "P-123", "DPS-S-1"]],
        }
        result = DpsUiAutomation().parse_lookup_result(
            snapshot,
            product_order_id="P-123",
            dps_query_value="P-123",
            dps_query_value_type="product_order_id",
        )
        self.assertEqual(result["data"]["dps_sales_number"], "DPS-S-1")
        self.assertEqual(result["data"]["dps_order_number"], "DPS-O-1")

    def test_order_and_product_order_are_not_swapped(self) -> None:
        snapshot = {
            "raw_result_texts": [],
            "table_headers": ["네이버 주문번호", "상품주문번호", "판매번호"],
            "table_rows": [["O-1", "P-1", "S-1"]],
        }
        result = DpsUiAutomation().parse_lookup_result(
            snapshot,
            order_id="O-1",
            product_order_id="P-1",
            dps_query_value="P-1",
            dps_query_value_type="product_order_id",
        )
        self.assertEqual(result["data"]["order_id"], "O-1")
        self.assertEqual(result["data"]["product_order_id"], "P-1")

    def test_sales_and_electronic_order_numbers_are_not_swapped(self) -> None:
        snapshot = {
            "raw_result_texts": [],
            "table_headers": ["DPS 판매번호", "전자주문번호", "상품주문번호"],
            "table_rows": [["SALE-1", "ORDER-1", "P-1"]],
        }
        result = DpsUiAutomation().parse_lookup_result(
            snapshot,
            dps_query_value="P-1",
            dps_query_value_type="product_order_id",
        )
        self.assertEqual(result["data"]["dps_sales_number"], "SALE-1")
        self.assertEqual(result["data"]["dps_order_number"], "ORDER-1")

    def test_multiple_exact_rows_are_not_selected_arbitrarily(self) -> None:
        snapshot = {
            "raw_result_texts": [],
            "table_headers": ["상품주문번호", "판매번호"],
            "table_rows": [["P-1", "S-1"], ["P-1", "S-2"]],
        }
        result = DpsUiAutomation().parse_lookup_result(
            snapshot,
            dps_query_value="P-1",
            dps_query_value_type="product_order_id",
        )
        self.assertFalse(result["found"])
        self.assertIsNone(result["diagnostics"]["matched_row_index"])


if __name__ == "__main__":
    unittest.main()
