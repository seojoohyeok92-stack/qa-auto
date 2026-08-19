from __future__ import annotations

import json
import logging
import calendar
import re
import time
import traceback
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable
from urllib.parse import urlparse

from dps.dates import parse_date_value, validate_dps_lookup_period
from dps.sales_detail import (
    DETAIL_MARKERS,
    ITEM_FIELDS,
    canonical_detail_label,
    merge_list_and_detail,
    normalize_label,
    parse_flat_detail,
)

PAGE_MARKERS = (
    "온라인판매 주문번호",
    "구매요청리스트",
    "설치정보",
    "Samsung DPS",
)
LOGIN_STATES = (
    "LOGGED_IN",
    "LOGIN_REQUIRED",
    "LOGIN_UNCERTAIN",
    "DPS_TAB_NOT_FOUND",
    "DPS_PAGE_INVALID",
)
TOP_MENU_MARKERS = (
    "경영정보",
    "고객",
    "구매",
    "판매",
    "재고/물류",
    "기준정보",
    "인사",
)
TOP_MENU_SIBLING_NAMES = frozenset(
    {"경영정보", "고객", "구매", "판매", "재고/물류", "기준정보", "인사"}
)
GLOBAL_MENU_HINTS = ("gnb", "global", "topmenu", "top_menu", "mainmenu")
LEFT_MENU_HINTS = (
    "lnb",
    "left",
    "snb",
    "sidebar",
    "submenu",
    "sub_menu",
    "leftmenu",
)
BREADCRUMB_HINTS = ("breadcrumb", "location", "crumb", "현재위치")
HOME_WIDGET_MARKERS = (
    "주문/배송",
    "고객관리",
    "판매조회",
    "구매",
    "배송조회",
    "주문진행상태",
)
LOGIN_URL_MARKERS = ("login.do", "login", "auth", "otp")
LOGIN_ID_MARKERS = ("아이디", "사용자 ID", "사용자ID", "user id", "username")
LOGIN_PASSWORD_MARKERS = ("비밀번호", "password")
OTP_UI_MARKERS = (
    "OTP",
    "인증번호",
    "인증코드",
    "SMS 인증",
    "휴대폰 인증",
    "일회용 비밀번호",
)
PURCHASE_PAGE_MARKERS = (
    "온라인판매 주문번호",
    "구매요청리스트",
    "설치정보",
    "판매번호",
    "주문번호",
)
SALES_TOP_MENU_NAME = "판매"
ONLINE_SALES_MENU_NAME = "온라인판매"
PURCHASE_REQUEST_LIST_NAME = "구매요청리스트"
MENU_CLICK_CONTROL_TYPES = ("Hyperlink", "MenuItem", "Button")
NAVIGATION_CONTROL_TYPES = (
    "Document",
    "Pane",
    "Group",
    "Menu",
    "MenuItem",
    "TabItem",
    "Button",
    "Hyperlink",
    "Text",
)
NAVIGATION_DIAGNOSTIC_KEYWORDS = (
    "설치",
    "배송",
    "배정",
    "온라인판매",
    "판매번호",
    "주문번호",
    "구매요청",
    "고객",
    "납기",
)
REJECTED_NAVIGATION_NAMES = ("MD/소물자동주문",)
RESULT_HINTS = (
    "설치",
    "기사",
    "배정",
    "납기",
    "방문",
    "진행",
    "상태",
)
ORDER_LABEL_NAME = "온라인판매 주문번호"
EXCLUDED_FIELD_LABELS = (
    "사업장",
    "셀러",
    "DPS 판매번호",
    "전자주문번호",
    "모델",
    "기간",
    "인수자",
    "날짜",
)
QUERY_CONTROL_TYPES = ("Button", "Hyperlink", "MenuItem", "Custom")
RESULT_CONTROL_TYPES = (
    "Text",
    "DataGrid",
    "Table",
    "DataItem",
    "List",
    "ListItem",
    "Row",
    "Cell",
    "Header",
    "HeaderItem",
    "Hyperlink",
    "Edit",
)
NO_RESULT_MARKERS = (
    "조회 결과가 없습니다",
    "검색 결과가 없습니다",
    "조회된 데이터가 없습니다",
    "조회된 결과가 없습니다",
    "데이터가 없습니다",
    "0건",
    "총 0 건",
    "해당 주문이 없습니다",
)


@dataclass(slots=True)
class PageVerification:
    ok: bool
    matched_markers: list[str]
    visible_text_count: int


@dataclass(slots=True)
class PurchasePageVerification:
    navigation_ok: bool
    input_ready: bool
    edit: Any | None
    query_action: Any | None
    marker_hits: list[str]
    reason: str
    sales_menu_selected: bool
    online_sales_expanded: bool
    page_marker_found: bool

    @property
    def ok(self) -> bool:
        """기존 호출부 호환용: 입력과 조회까지 준비된 상태입니다."""

        return self.input_ready

    @property
    def button(self) -> Any | None:
        """기존 호출부 호환용 조회 액션 별칭입니다."""

        return self.query_action


@dataclass(slots=True)
class PeriodControls:
    label: Any
    start_edit: Any
    end_edit: Any
    start_trigger: Any
    end_trigger: Any
    diagnostics: dict[str, Any]


class InputSafetyError(RuntimeError):
    def __init__(self, checks: dict[str, Any]) -> None:
        super().__init__("입력 도중 대상 검증이 실패했습니다.")
        self.checks = checks


class DpsUiAutomation:
    """선택된 DPS 탭의 페이지 요소만 조작합니다. 전역 키 입력은 사용하지 않습니다."""

    def __init__(
        self,
        *,
        logger: logging.Logger | None = None,
        navigation_logger: logging.Logger | None = None,
        result_logger: logging.Logger | None = None,
        calendar_logger: logging.Logger | None = None,
        detail_logger: logging.Logger | None = None,
    ) -> None:
        self.logger = logger or logging.getLogger(__name__)
        self.navigation_logger = navigation_logger or self.logger
        self.result_logger = result_logger or self.logger
        self.calendar_logger = calendar_logger or self.result_logger
        self.detail_logger = detail_logger or self.result_logger
        self._last_navigation_collected_elements: list[Any] = []
        self._last_field_resolution: dict[str, Any] = {}
        self._last_query_resolution: dict[str, Any] = {}
        self._last_period_resolution: dict[str, Any] = {}
        self._detail_invocation_count = 0

    def visible_texts(self, window: Any, *, limit: int = 160) -> list[str]:
        values: list[str] = []
        seen: set[str] = set()
        try:
            elements = window.descendants()
        except Exception:
            return values
        for element in elements:
            if len(values) >= limit:
                break
            try:
                is_visible = getattr(element, "is_visible", None)
                if callable(is_visible) and not is_visible():
                    continue
                control_type = str(element.element_info.control_type or "")
                if control_type not in {
                    "Text",
                    "Document",
                    "Group",
                    "Pane",
                    "Button",
                    "Edit",
                    "DataItem",
                    "ListItem",
                    "MenuItem",
                    "TabItem",
                    "Hyperlink",
                }:
                    continue
                text = str(element.element_info.name or element.window_text() or "").strip()
                text = " ".join(text.split())
                if not text or len(text) > 300 or text in seen:
                    continue
                seen.add(text)
                values.append(text)
            except Exception:
                continue
        return values

    def diagnostic_elements(
        self,
        window: Any,
        *,
        limit: int = 100,
    ) -> list[dict[str, str]]:
        """로그인 판별에 노출된 UIA 요소의 Control Type과 Name을 수집합니다."""

        values: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        try:
            elements = window.descendants()
        except Exception as error:
            self.logger.warning(
                "DPS 로그인 진단 UI 요소 수집 실패: error=%s",
                error.__class__.__name__,
            )
            return values
        for element in elements:
            if len(values) >= limit:
                break
            try:
                is_visible = getattr(element, "is_visible", None)
                if callable(is_visible) and not is_visible():
                    continue
                control_type = str(element.element_info.control_type or "")
                if control_type not in {
                    "Document",
                    "Pane",
                    "Text",
                    "Menu",
                    "MenuItem",
                    "Hyperlink",
                    "Edit",
                    "Button",
                }:
                    continue
                name = str(
                    element.element_info.name or element.window_text() or ""
                ).strip()
                name = " ".join(name.split())
                if not name or len(name) > 300:
                    continue
                identity = (control_type, name)
                if identity in seen:
                    continue
                seen.add(identity)
                values.append(
                    {
                        "control_type": control_type,
                        "name": self._safe_diagnostic_name(name),
                    }
                )
            except Exception:
                continue
        return values

    def verify_page(self, window: Any) -> PageVerification:
        texts = self.visible_texts(window)
        folded = "\n".join(texts).casefold()
        matched = [marker for marker in PAGE_MARKERS if marker.casefold() in folded]
        verification = PageVerification(
            ok=bool(matched),
            matched_markers=matched,
            visible_text_count=len(texts),
        )
        self.logger.info(
            "DPS 페이지 고유 요소 검증: ok=%s markers=%s text_count=%d",
            verification.ok,
            verification.matched_markers,
            verification.visible_text_count,
        )
        return verification

    def collect_login_signals(
        self,
        window: Any,
        *,
        url: str,
        allowed_hosts: tuple[str, ...] = ("dps2u.co.kr",),
    ) -> dict[str, Any]:
        texts = self.visible_texts(window)
        folded_texts = [text.casefold() for text in texts]
        parsed = urlparse(url if "://" in str(url) else f"https://{url}")
        host = str(parsed.hostname or "").casefold().rstrip(".")
        path = str(parsed.path or "")
        normalized_url = str(url or "").casefold()
        normalized_allowed = tuple(
            str(value).casefold().lstrip(".") for value in allowed_hosts
        )
        domain_ok = any(
            host == allowed or host.endswith(f".{allowed}")
            for allowed in normalized_allowed
        )
        path_ok = path.casefold().startswith("/dpsweb/")
        login_url_hits = [
            marker for marker in LOGIN_URL_MARKERS if marker in normalized_url
        ]
        logout_found = any("로그아웃" in text for text in texts)
        menu_hits = [
            marker
            for marker in TOP_MENU_MARKERS
            if any(text == marker.casefold() for text in folded_texts)
        ]
        widget_hits = [
            marker
            for marker in HOME_WIDGET_MARKERS
            if any(text == marker.casefold() for text in folded_texts)
        ]
        login_id_hits = [
            marker
            for marker in LOGIN_ID_MARKERS
            if any(marker.casefold() in text for text in folded_texts)
        ]
        login_password_hits = [
            marker
            for marker in LOGIN_PASSWORD_MARKERS
            if any(marker.casefold() in text for text in folded_texts)
        ]
        otp_ui_hits = [
            marker
            for marker in OTP_UI_MARKERS
            if any(marker.casefold() in text for text in folded_texts)
        ]
        login_ui_hits = login_id_hits + login_password_hits + otp_ui_hits
        dps_ui_hits = list(
            dict.fromkeys(
                (["로그아웃"] if logout_found else [])
                + menu_hits
                + widget_hits
                + [
                    marker
                    for marker in PAGE_MARKERS
                    if any(marker.casefold() in text for text in folded_texts)
                ]
            )
        )
        home_ui_hits = list(
            dict.fromkeys(
                (["로그아웃"] if logout_found else []) + menu_hits + widget_hits
            )
        )
        return {
            "url": str(url or ""),
            "host": host,
            "path": path,
            "domain_ok": domain_ok,
            "path_ok": path_ok,
            "login_page": bool(login_url_hits),
            "login_url_hits": login_url_hits,
            "login_ui_hits": login_ui_hits,
            "login_id_hits": login_id_hits,
            "login_password_hits": login_password_hits,
            "otp_ui_hits": otp_ui_hits,
            "logout_found": logout_found,
            "menu_hits": menu_hits,
            "widget_hits": widget_hits,
            "dps_ui_hits": dps_ui_hits,
            "home_ui_hits": home_ui_hits,
            "visible_text_count": len(texts),
        }

    def detect_login_state(
        self,
        window: Any,
        *,
        url: str,
        allowed_hosts: tuple[str, ...] = ("dps2u.co.kr",),
    ) -> dict[str, Any]:
        signals = self.collect_login_signals(
            window,
            url=url,
            allowed_hosts=allowed_hosts,
        )
        login_ui_failure = bool(
            signals["otp_ui_hits"]
            or (
                signals["login_id_hits"]
                and signals["login_password_hits"]
            )
        )
        dps_url_found = bool(signals["domain_ok"] and signals["path_ok"])
        dps_ui_found = bool(signals["dps_ui_hits"])
        home_ui_found = bool(
            signals["logout_found"] or len(signals["home_ui_hits"]) >= 2
        )
        url_available = bool(str(signals["url"] or "").strip())
        # 읽힌 타 사이트 URL은 실패시킵니다. URL이 없을 때는 실제 UI를 우선합니다.
        if url_available and not dps_url_found:
            state = "DPS_PAGE_INVALID"
            reason = "읽은 URL이 DPS 도메인 또는 /dpsweb/ 경로와 불일치"
        elif login_ui_failure:
            state = "LOGIN_REQUIRED"
            reason = "아이디/비밀번호 또는 OTP 로그인 UI 발견"
        elif home_ui_found:
            state = "LOGGED_IN"
            reason = (
                "logout_found"
                if signals["logout_found"]
                else "DPS 홈 UI 요소 발견"
            )
        elif signals["login_page"] and dps_url_found:
            # DPS may keep /login.do in the address bar after a successful
            # manual login.  Treat that stale URL as logged out only when no
            # strong authenticated UI (logout/menu/widgets) is present.
            state = "LOGIN_REQUIRED"
            reason = "로그인 URL 키워드 발견 (로그인 성공 UI 없음)"
        elif not dps_url_found and not dps_ui_found:
            state = "DPS_PAGE_INVALID"
            reason = "유효한 DPS URL과 DPS 관련 UI 요소를 모두 찾지 못함"
        else:
            state = "LOGIN_UNCERTAIN"
            reason = "DPS 신호는 있으나 로그인 성공 UI 신호가 부족함"
        result = {**signals, "state": state, "result": state, "reason": reason}
        self.logger.info(
            "login_check: %s",
            json.dumps(result, ensure_ascii=False, default=str),
        )
        return result

    def verify_purchase_request_page(
        self,
        window: Any,
    ) -> PurchasePageVerification:
        texts = self.visible_texts(window)
        folded = "\n".join(texts).casefold()
        marker_hits = [
            marker
            for marker in PURCHASE_PAGE_MARKERS
            if marker.casefold() in folded
        ]
        wrong_page_hits = [
            marker
            for marker in REJECTED_NAVIGATION_NAMES
            if marker.casefold() in folded
        ]
        sales_menus = self._exact_named_elements(
            window,
            name=SALES_TOP_MENU_NAME,
            control_types=("Hyperlink",),
        )
        sales_menu_selected = (
            self._resolve_sales_top_menu(
                sales_menus,
                require_selected=True,
            )
            is not None
        )
        online_sales_menus = self._exact_named_elements(
            window,
            name=ONLINE_SALES_MENU_NAME,
            control_types=MENU_CLICK_CONTROL_TYPES,
        )
        purchase_request_targets = self._exact_named_elements(
            window,
            name=PURCHASE_REQUEST_LIST_NAME,
            control_types=MENU_CLICK_CONTROL_TYPES,
        )
        online_sales_menu = self._resolve_left_menu_candidate(
            online_sales_menus,
            role="online_sales",
        )
        scoped_purchase_targets = self._left_purchase_request_targets(
            purchase_request_targets,
            online_sales_menu=online_sales_menu,
        )
        online_sales_expanded = (
            online_sales_menu is not None
            and self._is_online_sales_expanded(
                online_sales_menu,
                scoped_purchase_targets,
            )
        )
        page_marker_found = self._purchase_request_page_marker_found(window)
        navigation_ok = (
            not wrong_page_hits
            and sales_menu_selected
            and online_sales_expanded
            and page_marker_found
        )
        edit = self.find_order_edit(window) if navigation_ok else None
        query_action = (
            self.find_query_action(window, order_edit=edit)
            if navigation_ok and edit is not None
            else None
        )
        input_ready = (
            navigation_ok
            and edit is not None
            and query_action is not None
        )
        if wrong_page_hits:
            reason = (
                "명시적으로 제외된 잘못된 화면: "
                + ", ".join(wrong_page_hits)
            )
        elif not sales_menu_selected:
            reason = "상단 판매 메뉴 선택 상태를 확인하지 못함"
        elif not online_sales_expanded:
            reason = "왼쪽 온라인판매 메뉴 펼침 상태를 확인하지 못함"
        elif not page_marker_found:
            reason = "구매요청리스트 화면 제목 또는 breadcrumb를 확인하지 못함"
        elif edit is None:
            reason = "구매요청리스트 화면은 열렸지만 주문번호 입력칸을 찾지 못함"
        elif query_action is None:
            reason = "주문번호 입력칸은 확인했지만 조회 실행 요소를 찾지 못함"
        else:
            reason = "구매요청리스트 이동 및 입력 준비 확인"
        self.logger.info(
            "DPS 구매요청 화면 검증: navigation_ok=%s input_ready=%s "
            "sales_selected=%s online_sales_expanded=%s page_marker=%s "
            "order_input_found=%s query_action_found=%s "
            "markers=%s wrong_page_hits=%s reason=%s",
            navigation_ok,
            input_ready,
            sales_menu_selected,
            online_sales_expanded,
            page_marker_found,
            edit is not None,
            query_action is not None,
            marker_hits,
            wrong_page_hits,
            reason,
        )
        return PurchasePageVerification(
            navigation_ok,
            input_ready,
            edit,
            query_action,
            marker_hits,
            reason,
            sales_menu_selected,
            online_sales_expanded,
            page_marker_found,
        )

    def detect_current_dps_page(
        self,
        window: Any,
        *,
        url: str,
        login_check: dict[str, Any],
    ) -> dict[str, Any]:
        purchase = self.verify_purchase_request_page(window)
        if purchase.navigation_ok:
            page = "PURCHASE_REQUEST_LIST"
            label = "구매요청리스트"
            reason = purchase.reason
        elif login_check.get("state") == "LOGGED_IN" and bool(
            login_check.get("home_ui_hits")
        ):
            page = "HOME"
            label = "DPS 홈"
            reason = "DPS 홈 UI 요소 발견"
        elif login_check.get("state") == "LOGGED_IN" and (
            str(url).casefold().split("?", 1)[0].endswith("/main.do")
        ):
            page = "HOME"
            label = "DPS 홈"
            reason = "홈 URL 보조 신호 발견"
        else:
            page = "UNKNOWN"
            label = "알 수 없음"
            reason = purchase.reason
        return {
            "page": page,
            "page_label": label,
            "reason": reason,
            "purchase_page_verified": purchase.navigation_ok,
            "purchase_marker_hits": purchase.marker_hits,
        }

    def collect_dps_navigation_diagnostics(
        self,
        window: Any,
        *,
        limit: int = 500,
        max_depth: int = 20,
        anchor: Any | None = None,
        attempt: str = "single",
        roots: list[tuple[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        collection_roots = (
            list(roots)
            if roots is not None
            else self._navigation_collection_roots(window, anchor)
        )
        values: list[dict[str, Any]] = []
        collected_elements: list[Any] = []
        seen_values: set[Any] = set()
        seen_elements: set[Any] = set()
        window_identity = self._element_identity_key(window)
        total_counts: Counter[str] = Counter()
        total_direct_children = 0
        for root_label, root in collection_roots:
            elements, counts, direct_children = self._walk_navigation_root(
                root,
                root_label=root_label,
                max_depth=max_depth,
            )
            total_counts.update(counts)
            total_direct_children += direct_children
            self.navigation_logger.info(
                "navigation_tree_retry_root attempt=%s root=%s children=%d "
                "Pane=%d Menu=%d Hyperlink=%d visited=%d",
                attempt,
                root_label,
                direct_children,
                counts["Pane"],
                counts["Menu"],
                counts["Hyperlink"],
                len(elements),
            )
            for element in elements:
                identity = self._element_identity_key(element)
                if identity not in seen_elements:
                    seen_elements.add(identity)
                    collected_elements.append(element)
                if identity in seen_values or len(values) >= limit:
                    continue
                try:
                    is_visible = getattr(element, "is_visible", None)
                    if callable(is_visible) and not is_visible():
                        continue
                    control_type = self._safe_element_info_value(
                        element, "control_type"
                    )
                    if control_type not in NAVIGATION_CONTROL_TYPES:
                        continue
                    name = self._safe_diagnostic_name(self._name(element))
                    if not name:
                        continue
                    values.append(
                        self._navigation_element_record(
                            element,
                            name=name,
                            control_type=control_type,
                            window_identity=window_identity,
                            max_depth=max_depth,
                        )
                    )
                    seen_values.add(identity)
                except Exception as error:
                    self._log_navigation_tree_exception(
                        "element_record", error, element
                    )
                    # A broken element must not discard its siblings or prior results.
                    continue
        self._last_navigation_collected_elements = collected_elements
        self.navigation_logger.info(
            "navigation_tree_retry attempt=%s roots=%d children=%d Pane=%d "
            "Menu=%d Hyperlink=%d collected=%d diagnostic_elements=%d",
            attempt,
            len(collection_roots),
            total_direct_children,
            total_counts["Pane"],
            total_counts["Menu"],
            total_counts["Hyperlink"],
            len(collected_elements),
            len(values),
        )
        return values

    def _navigation_collection_roots(
        self,
        window: Any,
        anchor: Any | None,
    ) -> list[tuple[str, Any]]:
        roots: list[tuple[str, Any]] = []
        if anchor is not None:
            current = anchor
            seen: set[Any] = set()
            for distance in range(1, 41):
                try:
                    current = current.parent()
                except Exception as error:
                    self._log_navigation_tree_exception(
                        f"ancestor_parent distance={distance}", error, current
                    )
                    break
                identity = self._element_identity_key(current)
                if identity in seen:
                    break
                seen.add(identity)
                if distance == 1:
                    roots.append(("anchor_parent", current))
                elif distance == 2:
                    roots.append(("anchor_grandparent", current))
                control_type = self._safe_element_info_value(
                    current, "control_type"
                )
                class_name = self._safe_element_info_value(current, "class_name")
                automation_id = self._safe_element_info_value(
                    current, "automation_id"
                )
                if control_type == "Window":
                    roots.append(("Window", current))
                if class_name == "BrowserRootView":
                    roots.append(("BrowserRootView", current))
                if automation_id == "RootWebArea":
                    roots.append(("RootWebArea", current))
                if control_type == "Document":
                    roots.append(("Document", current))
        roots.append(("Window", window))

        unique: list[tuple[str, Any]] = []
        seen_root_labels: set[tuple[str, Any]] = set()
        for label, root in roots:
            key = (label, self._element_identity_key(root))
            if key in seen_root_labels:
                continue
            seen_root_labels.add(key)
            unique.append((label, root))
        self.navigation_logger.info(
            "navigation_tree_roots roots=%s",
            json.dumps(
                [
                    {
                        "root": label,
                        **self._uia_object_diagnostics(root),
                    }
                    for label, root in unique
                ],
                ensure_ascii=False,
                default=str,
            ),
        )
        return unique

    def _walk_navigation_root(
        self,
        root: Any,
        *,
        root_label: str,
        max_depth: int,
    ) -> tuple[list[Any], Counter[str], int]:
        collected: list[Any] = []
        counts: Counter[str] = Counter()
        direct_children = 0
        stack: list[tuple[Any, int]] = []
        try:
            if (
                getattr(getattr(root, "element_info", None), "_element", None)
                is None
                and not callable(getattr(root, "children", None))
            ):
                raise AttributeError(
                    f"{type(root).__name__!r} object has no UIA child walker"
                )
            children = self._immediate_navigation_children(
                root, context=f"root={root_label}"
            )
            direct_children = len(children)
            stack.extend((child, 1) for child in reversed(children))
        except AttributeError as error:
            # Lightweight test doubles and some wrappers do not expose children().
            # descendants() remains a root-local fallback; failures are still isolated
            # from every other ancestor root.
            self._log_navigation_tree_exception(
                f"root_children root={root_label}", error, root
            )
            try:
                try:
                    descendants = list(root.descendants(depth=max_depth))
                except TypeError:
                    descendants = list(root.descendants())
                direct_children = len(descendants)
                stack.extend((child, 1) for child in reversed(descendants))
            except Exception as fallback_error:
                self._log_navigation_tree_exception(
                    f"root_descendants_fallback root={root_label}",
                    fallback_error,
                    root,
                )
        except Exception as error:
            self._log_navigation_tree_exception(
                f"root_children root={root_label}", error, root
            )

        seen: set[Any] = set()
        while stack:
            element, depth = stack.pop()
            identity = self._element_identity_key(element)
            if identity in seen:
                continue
            seen.add(identity)
            collected.append(element)
            control_type = self._safe_element_info_value(element, "control_type")
            if control_type:
                counts[control_type] += 1
            if depth >= max_depth:
                continue
            try:
                children = self._immediate_navigation_children(
                    element,
                    context=f"root={root_label} depth={depth}",
                )
            except Exception as error:
                self._log_navigation_tree_exception(
                    f"branch_children root={root_label} depth={depth}",
                    error,
                    element,
                )
                # Only this branch is skipped. Already queued siblings remain intact.
                continue
            stack.extend((child, depth + 1) for child in reversed(children))
        return collected, counts, direct_children

    def _immediate_navigation_children(
        self,
        element: Any,
        *,
        context: str,
    ) -> list[Any]:
        """Return UIA children while isolating wrapper failures per sibling."""
        element_info = getattr(element, "element_info", None)
        raw_element = getattr(element_info, "_element", None)
        if raw_element is None:
            children_method = getattr(element, "children", None)
            if not callable(children_method):
                return []
            return list(children_method())

        # pywinauto's wrapper.children() converts every result in one list
        # comprehension. One broken conversion therefore loses all siblings.
        # Walking COM pointers one at a time keeps the remaining siblings usable.
        from pywinauto.controls.uiawrapper import UIAWrapper
        from pywinauto.uia_defines import IUIA
        from pywinauto.uia_element_info import UIAElementInfo

        walker = IUIA().iuia.ControlViewWalker
        children: list[Any] = []
        try:
            child_pointer = walker.GetFirstChildElement(raw_element)
        except Exception as error:
            self._log_navigation_tree_exception(
                f"treewalker_first_child {context}", error, element
            )
            return children
        sibling_index = 0
        while child_pointer:
            sibling_index += 1
            diagnostic_object: Any = child_pointer
            try:
                child_info = UIAElementInfo(child_pointer)
                diagnostic_object = child_info
                backend = getattr(element, "backend", None)
                wrapper_factory = getattr(backend, "generic_wrapper_class", None)
                child = (
                    wrapper_factory(child_info)
                    if callable(wrapper_factory)
                    else UIAWrapper(child_info)
                )
                children.append(child)
            except Exception as error:
                self._log_navigation_tree_exception(
                    f"treewalker_wrap_child {context} sibling={sibling_index}",
                    error,
                    diagnostic_object,
                )
            try:
                child_pointer = walker.GetNextSiblingElement(child_pointer)
            except Exception as error:
                self._log_navigation_tree_exception(
                    f"treewalker_next_sibling {context} sibling={sibling_index}",
                    error,
                    diagnostic_object,
                )
                break
        return children

    def _navigation_element_record(
        self,
        element: Any,
        *,
        name: str,
        control_type: str,
        window_identity: Any,
        max_depth: int,
    ) -> dict[str, Any]:
        rect_value = None
        try:
            rect = element.rectangle()
            rect_value = {
                "left": int(rect.left),
                "top": int(rect.top),
                "right": int(rect.right),
                "bottom": int(rect.bottom),
            }
        except Exception:
            pass
        return {
            "name": name,
            "control_type": control_type,
            "automation_id": self._safe_diagnostic_name(
                self._safe_element_info_value(element, "automation_id")
            ),
            "class_name": self._safe_diagnostic_name(
                self._safe_element_info_value(element, "class_name")
            ),
            "parent_name": self._parent_name(element),
            "depth": self._element_depth(
                element,
                window_identity=window_identity,
                max_depth=max_depth,
            ),
            "rectangle": rect_value,
            "invoke_available": self._invoke_available(element),
            "selected": self._selected_state(element),
            "expanded": self._expanded_state(element),
            "window_handle": self._safe_uia_integer(element, "handle"),
            "runtime_id": self._safe_uia_sequence(element, "runtime_id"),
            "native_window_handle": self._safe_uia_integer(element, "handle"),
            "framework_id": self._safe_element_info_value(
                element, "framework_id"
            ),
        }

    def _log_navigation_tree_exception(
        self,
        context: str,
        error: BaseException,
        element: Any,
    ) -> None:
        details = self._uia_object_diagnostics(element)
        self.navigation_logger.warning(
            "navigation_tree_collect_failed context=%s error=%s exception_type=%s "
            "exception_message=%s object=%s traceback=\n%s",
            context,
            type(error).__name__,
            type(error).__name__,
            str(error),
            json.dumps(details, ensure_ascii=False, default=str),
            traceback.format_exc(),
        )

    def _uia_object_diagnostics(self, element: Any) -> dict[str, Any]:
        try:
            representation = repr(element)
        except Exception as error:
            representation = f"<repr failed: {type(error).__name__}: {error}>"
        return {
            "type": str(type(element)),
            "repr": representation,
            "automation_id": self._safe_element_info_value(
                element, "automation_id"
            ),
            "control_type": self._safe_element_info_value(
                element, "control_type"
            ),
            "name": self._safe_diagnostic_name(self._name(element)),
            "window_handle": self._safe_uia_integer(element, "handle"),
            "runtime_id": self._safe_uia_sequence(element, "runtime_id"),
            "native_window_handle": self._safe_uia_integer(element, "handle"),
            "framework_id": self._safe_element_info_value(
                element, "framework_id"
            ),
        }

    @staticmethod
    def _safe_element_info_value(element: Any, attribute: str) -> str:
        try:
            element_info = getattr(element, "element_info", element)
            return str(getattr(element_info, attribute, "") or "")
        except Exception:
            return ""

    @staticmethod
    def _safe_uia_integer(element: Any, attribute: str) -> int | None:
        try:
            element_info = getattr(element, "element_info", element)
            value = getattr(element_info, attribute, None)
            if value is None and attribute == "handle":
                value = getattr(element, attribute, None)
            return int(value) if value is not None else None
        except Exception:
            return None

    @staticmethod
    def _safe_uia_sequence(element: Any, attribute: str) -> list[Any] | None:
        try:
            element_info = getattr(element, "element_info", element)
            value = getattr(element_info, attribute, None)
            return list(value) if value is not None else None
        except Exception:
            return None

    def navigate_to_online_sales_purchase_request_list(
        self,
        *,
        window: Any,
        validate_target: Callable[[], tuple[bool, dict[str, Any]]],
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        safe, checks = validate_target()
        if not safe:
            return self._navigation_failure(
                "Samsung DPS 탭 선택 및 검증 실패",
                menu_found=False,
                target_found=False,
                checks=checks,
            )
        existing = self.verify_purchase_request_page(window)
        if existing.navigation_ok:
            return {
                "ok": True,
                "success": True,
                "code": "DPS_NAVIGATION_SKIPPED",
                "message": "이미 판매 > 온라인판매 > 구매요청리스트 화면입니다.",
                "navigation_attempted": False,
                "navigation_completed": True,
                "purchase_page_verified": True,
                "input_ready": existing.input_ready,
            }

        before = self.collect_dps_navigation_diagnostics(window)
        self._log_navigation_snapshot("before_sales_menu", before)

        sales_result = self.click_sales_menu(
            window=window,
            validate_target=validate_target,
        )
        if not sales_result["success"]:
            return sales_result

        online_sales_result = self.expand_online_sales_menu(
            window=window,
            validate_target=validate_target,
        )
        if not online_sales_result["success"]:
            return online_sales_result

        purchase_request_result = self.click_purchase_request_list(
            window=window,
            validate_target=validate_target,
        )
        if not purchase_request_result["success"]:
            return purchase_request_result

        deadline = time.monotonic() + max(1.0, timeout)
        while time.monotonic() < deadline:
            time.sleep(0.35)
            safe, checks = validate_target()
            if not safe:
                return self._navigation_failure(
                    "메뉴 이동 후 DPS 탭 안전 검증 실패",
                    menu_found=True,
                    target_found=True,
                    checks=checks,
                )
            verification = self.verify_purchase_request_page(window)
            self._log_resulting_page_markers(window)
            if verification.navigation_ok:
                after = self.collect_dps_navigation_diagnostics(window)
                self._log_navigation_snapshot("purchase_page_ready", after)
                return {
                    "ok": True,
                    "success": True,
                    "code": "DPS_NAVIGATION_COMPLETE",
                    "message": "판매 > 온라인판매 > 구매요청리스트 화면으로 이동했습니다.",
                    "navigation_attempted": True,
                    "navigation_completed": True,
                    "menu_found": True,
                    "online_sales_menu_found": True,
                    "target_menu_found": True,
                    "purchase_page_verified": True,
                    "input_ready": verification.input_ready,
                }
        return self._navigation_stage_failure(
            "PURCHASE_REQUEST_PAGE_NOT_VERIFIED",
            "메뉴는 실행했지만 구매요청리스트 화면 진입을 확인하지 못했습니다.",
            stage="purchase_request_page",
            menu_found=True,
            target_found=True,
            reason=(
                "판매 선택·온라인판매 펼침·구매요청리스트 제목 또는 "
                "breadcrumb 중 하나를 확인하지 못했습니다."
            ),
        )

    def click_sales_menu(
        self,
        *,
        window: Any,
        validate_target: Callable[[], tuple[bool, dict[str, Any]]],
    ) -> dict[str, Any]:
        sales_menus = self._exact_named_elements(
            window,
            name=SALES_TOP_MENU_NAME,
            control_types=("Hyperlink",),
        )
        sales_menu = self._resolve_sales_top_menu(sales_menus)
        if sales_menu is None:
            if len(self._selected_sales_candidates(sales_menus)) > 1:
                return self._navigation_stage_failure(
                    "SALES_MENU_SELECTION_FAILED",
                    "판매 메뉴를 선택했지만 선택 상태를 확인하지 못했습니다.",
                    stage="sales_menu",
                    menu_found=True,
                    target_found=False,
                    reason="선택 상태인 상단 판매 메뉴 후보가 둘 이상입니다.",
                )
            return self._navigation_target_unknown(
                stage="sales_menu",
                diagnostics=self.collect_dps_navigation_diagnostics(window),
                rejected=self._rejected_exact_navigation_candidates(window),
            )
        if self._is_navigation_element_selected(sales_menu):
            return {"success": True, "clicked": False}
        clicked_identity = self._structural_identity(sales_menu)
        safe, checks = validate_target()
        if not safe:
            return self._navigation_failure(
                "판매 메뉴 클릭 전 DPS 탭 안전 검증 실패",
                menu_found=True,
                target_found=False,
                checks=checks,
            )
        try:
            self._log_clicked_identity("sales_menu", sales_menu)
            self._click_uia_element(sales_menu)
        except Exception as error:
            return self._navigation_failure(
                f"판매 메뉴 실행 실패: {error.__class__.__name__}",
                menu_found=True,
                target_found=False,
            )
        for delay in (0.0, 0.1, 0.3, 0.7, 1.5):
            if delay:
                time.sleep(delay)
            current_sales = self._exact_named_elements(
                window,
                name=SALES_TOP_MENU_NAME,
                control_types=("Hyperlink",),
            )
            selected_sales = self._resolve_sales_top_menu(
                current_sales,
                require_selected=True,
                clicked_identity=clicked_identity,
            )
            if selected_sales is not None:
                return {"success": True, "clicked": True}
        return self._navigation_stage_failure(
            "SALES_MENU_SELECTION_FAILED",
            "판매 메뉴를 선택했지만 선택 상태를 확인하지 못했습니다.",
            stage="sales_menu",
            menu_found=True,
            target_found=False,
            reason="상단 글로벌 메뉴 영역에서 선택된 판매 Hyperlink를 하나로 확인하지 못했습니다.",
        )

    def expand_online_sales_menu(
        self,
        *,
        window: Any,
        validate_target: Callable[[], tuple[bool, dict[str, Any]]],
    ) -> dict[str, Any]:
        online_sales_menu = self._wait_for_left_menu_candidate(
            window,
            name=ONLINE_SALES_MENU_NAME,
            control_types=MENU_CLICK_CONTROL_TYPES,
            role="online_sales",
        )
        if online_sales_menu is None:
            return self._navigation_stage_failure(
                "ONLINE_SALES_MENU_NOT_FOUND",
                "판매 메뉴에서 온라인판매 항목을 찾지 못했습니다.",
                stage="online_sales_menu",
                menu_found=False,
                target_found=False,
                reason="왼쪽 메뉴 영역의 정확한 온라인판매 항목을 하나로 식별하지 못했습니다.",
            )
        targets = self._exact_named_elements(
            window,
            name=PURCHASE_REQUEST_LIST_NAME,
            control_types=MENU_CLICK_CONTROL_TYPES,
        )
        scoped_targets = self._left_purchase_request_targets(
            targets,
            online_sales_menu=online_sales_menu,
        )
        if self._is_online_sales_expanded(online_sales_menu, scoped_targets):
            return {"success": True, "clicked": False}
        safe, checks = validate_target()
        if not safe:
            return self._navigation_failure(
                "온라인판매 메뉴 클릭 전 DPS 탭 안전 검증 실패",
                menu_found=True,
                target_found=False,
                checks=checks,
            )
        try:
            self._log_clicked_identity("online_sales_menu", online_sales_menu)
            self._click_uia_element(online_sales_menu)
        except Exception as error:
            return self._navigation_failure(
                f"온라인판매 메뉴 펼치기 실패: {error.__class__.__name__}",
                menu_found=True,
                target_found=False,
            )
        for delay in (0.0, 0.1, 0.3, 0.7, 1.5):
            if delay:
                time.sleep(delay)
            current_online_sales_candidates = self._exact_named_elements(
                window,
                name=ONLINE_SALES_MENU_NAME,
                control_types=MENU_CLICK_CONTROL_TYPES,
            )
            current_online_sales = self._resolve_left_menu_candidate(
                current_online_sales_candidates,
                role="online_sales",
            )
            current_targets = self._exact_named_elements(
                window,
                name=PURCHASE_REQUEST_LIST_NAME,
                control_types=MENU_CLICK_CONTROL_TYPES,
            )
            if (
                current_online_sales is not None
                and self._is_online_sales_expanded(
                    current_online_sales,
                    self._left_purchase_request_targets(
                        current_targets,
                        online_sales_menu=current_online_sales,
                    ),
                )
            ):
                return {"success": True, "clicked": True}
        return self._navigation_failure(
            "온라인판매 메뉴 클릭 후 펼침 상태를 확인하지 못했습니다.",
            menu_found=True,
            target_found=False,
        )

    def click_purchase_request_list(
        self,
        *,
        window: Any,
        validate_target: Callable[[], tuple[bool, dict[str, Any]]],
    ) -> dict[str, Any]:
        target = self._wait_for_left_menu_candidate(
            window,
            name=PURCHASE_REQUEST_LIST_NAME,
            control_types=MENU_CLICK_CONTROL_TYPES,
            role="purchase_request",
        )
        if target is None:
            return self._navigation_stage_failure(
                "PURCHASE_REQUEST_LIST_NOT_FOUND",
                "온라인판매 메뉴에서 구매요청리스트를 찾지 못했습니다.",
                stage="purchase_request_list",
                menu_found=True,
                target_found=False,
                reason="온라인판매 하위 왼쪽 메뉴의 정확한 구매요청리스트를 하나로 식별하지 못했습니다.",
            )
        safe, checks = validate_target()
        if not safe:
            return self._navigation_failure(
                "구매요청리스트 클릭 전 DPS 탭 안전 검증 실패",
                menu_found=True,
                target_found=True,
                checks=checks,
            )
        try:
            self._log_clicked_identity("purchase_request_list", target)
            self._click_uia_element(target)
        except Exception as error:
            return self._navigation_failure(
                f"구매요청리스트 실행 실패: {error.__class__.__name__}",
                menu_found=True,
                target_found=True,
            )
        return {"success": True, "clicked": True}

    def _all_descendants(self, window: Any) -> list[Any]:
        """Chrome UIA filters can omit table text; filter one raw walk ourselves."""

        try:
            return list(window.descendants())
        except Exception:
            return []

    def _field_exclusion_reason(self, edit: Any) -> str | None:
        identity = self._diagnostic_identity(edit)
        name = " ".join(self._name(edit).split())
        if name == ORDER_LABEL_NAME:
            return None
        searchable = " ".join(
            (
                name,
                str(identity.get("automation_id") or ""),
                str(identity.get("class_name") or ""),
                self._parent_name(edit),
            )
        ).casefold()
        if any(value in searchable for value in ("i_sellerid", "seller", "vkbur")):
            return "seller_field"
        if any(value in searchable for value in ("i_sdate", "i_edate", "calendar", "datepicker")):
            return "date_field"
        if any(value.casefold() in searchable for value in EXCLUDED_FIELD_LABELS):
            return "excluded_label_or_identity"
        if re.fullmatch(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}", name):
            return "date_value"
        if "(주)" in name or re.search(r"[가-힣]{2,}(?:회사|상사|전자|서비스)", name):
            return "company_value"
        if name and name not in {"￼", "\ufffc"} and len(name) <= 10:
            return "short_code_or_model"
        if self._has_ancestor_control_type(edit, "ComboBox"):
            return "combobox_edit"
        try:
            visible = getattr(edit, "is_visible", None)
            if callable(visible) and not visible():
                return "hidden"
            enabled = getattr(edit, "is_enabled", None)
            if callable(enabled) and not enabled():
                return "disabled"
        except Exception:
            return "state_unreadable"
        rect = self._rectangle_record(edit)
        if rect is None or rect["right"] <= rect["left"] or rect["bottom"] <= rect["top"]:
            return "empty_rectangle"
        try:
            if bool(edit.iface_value.current_is_read_only):
                return "readonly"
        except Exception:
            pass
        return None

    def _has_ancestor_control_type(self, element: Any, control_type: str) -> bool:
        current = self._safe_parent(element)
        for _ in range(8):
            if current is None:
                return False
            if self._safe_element_info_value(current, "control_type") == control_type:
                return True
            current = self._safe_parent(current)
        return False

    def find_order_edit(self, window: Any) -> Any | None:
        all_elements = self._all_descendants(window)
        labels = [
            item
            for item in all_elements
            if self._safe_element_info_value(item, "control_type")
            in {
                "Text", "Static", "Label", "HeaderItem", "Custom",
                "Document", "DataItem", "Cell",
            }
            and " ".join(self._name(item).split()) == ORDER_LABEL_NAME
        ]
        raw_edits = [
            item
            for item in all_elements
            if self._safe_element_info_value(item, "control_type") == "Edit"
        ]
        raw_edits.extend(self._elements(window, "Edit"))
        edits: list[Any] = []
        seen_edits: set[Any] = set()
        for edit in raw_edits:
            key = self._element_identity_key(edit)
            if key not in seen_edits:
                seen_edits.add(key)
                edits.append(edit)
        if not labels:
            labels = [
                item
                for control_type in (
                    "Text", "Static", "Label", "HeaderItem",
                    "Custom", "DataItem", "Cell",
                )
                for item in self._elements(window, control_type)
                if " ".join(self._name(item).split()) == ORDER_LABEL_NAME
            ]

        self._log_order_edit_diagnostics(edits, labels=labels, matching_labels=labels)
        records: list[dict[str, Any]] = []
        ranked: list[tuple[int, Any, Any, dict[str, Any]]] = []
        for edit in edits:
            exclusion = self._field_exclusion_reason(edit)
            if exclusion:
                records.append(
                    {
                        **self._diagnostic_identity(edit),
                        "rectangle": self._rectangle_record(edit),
                        "selected": False,
                        "reason": exclusion,
                    }
                )
                continue
            edit_rect = self._rectangle_record(edit)
            if edit_rect is None:
                continue
            for label in labels:
                label_rect = self._rectangle_record(label)
                if label_rect is None:
                    continue
                edit_y = (edit_rect["top"] + edit_rect["bottom"]) / 2
                label_y = (label_rect["top"] + label_rect["bottom"]) / 2
                y_distance = abs(edit_y - label_y)
                tolerance = max(
                    12.0,
                    (edit_rect["bottom"] - edit_rect["top"]) * 0.75,
                    (label_rect["bottom"] - label_rect["top"]) * 0.75,
                )
                semantic_edit = " ".join(self._name(edit).split()) == ORDER_LABEL_NAME
                if not semantic_edit and (
                    y_distance > tolerance
                    or edit_rect["left"] < label_rect["right"] - 2
                ):
                    continue
                x_distance = max(0, edit_rect["left"] - label_rect["right"])
                shared_distance = self._nearest_shared_ancestor_distance(edit, label)
                same_parent = self._same_element(
                    self._safe_parent(edit), self._safe_parent(label)
                )
                score = 300 if semantic_edit else 180
                score += 100 if same_parent else 0
                score += max(0, 80 - shared_distance * 12)
                score += max(0, 100 - int(x_distance / 4))
                score += max(0, 80 - int(y_distance * 3))
                detail = {
                    "label": self._diagnostic_identity(label),
                    "edit": self._diagnostic_identity(edit),
                    "label_rectangle": label_rect,
                    "edit_rectangle": edit_rect,
                    "parent": self._parent_name(edit),
                    "common_ancestor_distance": shared_distance,
                    "x_distance": x_distance,
                    "y_distance": y_distance,
                    "score": score,
                    "selected": False,
                    "reason": "exact_label_same_row_candidate",
                }
                ranked.append((score, edit, label, detail))
                records.append(detail)

        ranked.sort(key=lambda item: item[0], reverse=True)
        ambiguous = (
            len(ranked) > 1
            and ranked[0][1] is not ranked[1][1]
            and ranked[0][0] - ranked[1][0] < 35
        )
        if not ranked:
            self._last_field_resolution = {
                "status": "NOT_FOUND",
                "reason": "label_not_found" if not labels else "no_safe_candidate",
                "labels": [self._diagnostic_identity(item) for item in labels],
                "candidates": records,
            }
            return None
        if ambiguous:
            self._last_field_resolution = {
                "status": "FIELD_AMBIGUOUS",
                "reason": "candidate_ambiguous",
                "labels": [self._diagnostic_identity(item) for item in labels],
                "candidates": records,
            }
            self.result_logger.warning(
                "order_edit_resolution=%s",
                json.dumps(
                    self._mask_diagnostic_record(self._last_field_resolution),
                    ensure_ascii=False,
                    default=str,
                ),
            )
            return None

        ranked[0][3]["selected"] = True
        ranked[0][3]["reason"] = "selected_exact_label_structural_match"
        self._last_field_resolution = {
            "status": "SELECTED",
            "selected_edit": ranked[0][3],
            "candidates": records,
        }
        self.result_logger.info(
            "order_edit_resolution=%s",
            json.dumps(
                self._mask_diagnostic_record(self._last_field_resolution),
                ensure_ascii=False,
                default=str,
            ),
        )
        return ranked[0][1]

    def _query_action_candidates(self, window: Any) -> list[Any]:
        candidates: list[Any] = []
        for control_type in QUERY_CONTROL_TYPES:
            for candidate in self._elements(window, control_type):
                if " ".join(self._name(candidate).split()) != "조회":
                    continue
                try:
                    is_visible = getattr(candidate, "is_visible", None)
                    if callable(is_visible) and not is_visible():
                        continue
                except Exception:
                    continue
                rect = self._rectangle_record(candidate)
                if rect is not None and rect["top"] >= 120:
                    candidates.append(candidate)
        return candidates

    def find_query_action(
        self,
        window: Any,
        *,
        order_edit: Any | None,
    ) -> Any | None:
        if order_edit is None:
            return None
        ranked: list[tuple[int, Any]] = []
        edit_rect = self._rectangle_record(order_edit)
        for candidate in self._query_action_candidates(window):
            control_type = self._safe_element_info_value(
                candidate,
                "control_type",
            )
            rect = self._rectangle_record(candidate)
            if rect is None:
                continue
            score = 0
            context_match = False
            class_name = self._safe_element_info_value(candidate, "class_name").casefold()
            automation_id = self._safe_element_info_value(
                candidate, "automation_id"
            ).casefold()
            search_identity = f"{automation_id} {class_name}"
            if "btn_searchwhite" in search_identity:
                score += 80
            elif "btn_searchblue" in search_identity:
                score += 45
            if control_type == "Hyperlink":
                score += 20
            elif control_type == "Button":
                score += 15
            if self._same_element(
                self._safe_parent(candidate),
                self._safe_parent(order_edit),
            ):
                score += 70
                context_match = True
            shared_distance = self._nearest_shared_ancestor_distance(
                candidate,
                order_edit,
            )
            if shared_distance <= 4:
                score += 45
                context_match = True
            elif (
                shared_distance <= 8
                and any(
                    hint in search_identity
                    for hint in ("btn_searchwhite", "btn_searchblue")
                )
            ):
                score += 25
                context_match = True
            if edit_rect is not None:
                vertical = abs(
                    (rect["top"] + rect["bottom"]) / 2
                    - (edit_rect["top"] + edit_rect["bottom"]) / 2
                )
                horizontal = abs(
                    (rect["left"] + rect["right"]) / 2
                    - (edit_rect["left"] + edit_rect["right"]) / 2
                )
                if vertical <= 100 and horizontal <= 1400:
                    score += max(
                        10,
                        70 - int(vertical / 2) - int(horizontal / 30),
                    )
                    context_match = True
                if rect["left"] >= edit_rect["left"] and vertical <= 60:
                    score += 35
            if context_match:
                ranked.append((score, candidate))
        diagnostics = [
            {"score": score, **self._diagnostic_identity(candidate)}
            for score, candidate in sorted(
                ranked, key=lambda item: item[0], reverse=True
            )
        ]
        self.logger.info(
            "DPS 조회 액션 후보: %s",
            json.dumps(diagnostics, ensure_ascii=False),
        )
        if not ranked:
            self._last_query_resolution = {
                "status": "NOT_FOUND",
                "candidates": diagnostics,
            }
            return None
        ranked.sort(key=lambda item: item[0], reverse=True)
        if ranked[0][0] <= 0:
            return None
        if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
            self._last_query_resolution = {
                "status": "AMBIGUOUS",
                "candidates": diagnostics,
            }
            return None
        self._last_query_resolution = {
            "status": "SELECTED",
            "selected_query_control": {
                "score": ranked[0][0],
                **self._diagnostic_identity(ranked[0][1]),
            },
            "candidates": diagnostics,
        }
        return ranked[0][1]

    def find_query_button(self, window: Any) -> Any | None:
        """이전 호출부 호환용. 입력칸 기준으로 조회 액션을 찾습니다."""

        return self.find_query_action(
            window,
            order_edit=self.find_order_edit(window),
        )

    def _log_order_edit_diagnostics(
        self,
        edits: list[Any],
        *,
        labels: list[Any],
        matching_labels: list[Any],
    ) -> None:
        values: list[dict[str, Any]] = []
        for edit in edits:
            identity = self._diagnostic_identity(edit)
            rect = self._rectangle_record(edit)
            nearby: list[str] = []
            if rect is not None:
                for label in labels:
                    label_rect = self._rectangle_record(label)
                    if label_rect is None:
                        continue
                    vertical = abs(
                        (rect["top"] + rect["bottom"]) / 2
                        - (label_rect["top"] + label_rect["bottom"]) / 2
                    )
                    horizontal = abs(
                        (rect["left"] + rect["right"]) / 2
                        - (label_rect["left"] + label_rect["right"]) / 2
                    )
                    if vertical <= 100 and horizontal <= 1000:
                        text = " ".join(self._name(label).split())
                        if text and text not in nearby:
                            nearby.append(text)
            values.append(
                {
                    **identity,
                    "parent_name": self._parent_name(edit),
                    "rectangle": rect,
                    "value_pattern_available": self._value_pattern_available(edit),
                    "nearby_texts": nearby[:12],
                }
            )
        self.result_logger.info(
            "order_edit_diagnostics matching_labels=%s edits=%s",
            [
                self._safe_log_text(self._name(label))
                for label in matching_labels
            ],
            json.dumps(
                [self._mask_diagnostic_record(value) for value in values],
                ensure_ascii=False,
            ),
        )

    @staticmethod
    def _value_pattern_available(element: Any) -> bool:
        try:
            getattr(element, "iface_value")
            return True
        except Exception:
            return callable(getattr(element, "set_edit_text", None))

    def _read_edit_value(self, edit: Any) -> str | None:
        for getter in (
            lambda: edit.iface_value.current_value,
            lambda: edit.iface_value.CurrentValue,
            lambda: edit.get_value(),
            lambda: edit.window_text(),
        ):
            try:
                value = getter()
                if value is not None:
                    return str(value)
            except Exception:
                continue
        return None

    def _edits_with_value(
        self,
        window: Any,
        value: str,
        *,
        selected_edit: Any,
    ) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        elements = self._all_descendants(window)
        edits = [
            item
            for item in elements
            if self._safe_element_info_value(item, "control_type") == "Edit"
        ] or self._elements(window, "Edit")
        for edit in edits:
            if self._read_edit_value(edit) is None:
                continue
            if str(self._read_edit_value(edit)).strip() != value.strip():
                continue
            if self._same_element(edit, selected_edit):
                continue
            matches.append(
                {
                    **self._diagnostic_identity(edit),
                    "parent_name": self._parent_name(edit),
                    "rectangle": self._rectangle_record(edit),
                    "field_reason": self._field_exclusion_reason(edit),
                }
            )
        return matches

    def _visible_enabled(self, element: Any) -> bool:
        try:
            if not element.is_visible() or not element.is_enabled():
                return False
        except Exception:
            return False
        rect = self._rectangle_record(element)
        return bool(
            rect
            and rect["right"] > rect["left"]
            and rect["bottom"] > rect["top"]
        )

    def _calendar_trigger_for_edit(
        self,
        edit: Any,
        candidates: list[Any],
    ) -> Any | None:
        edit_rect = self._rectangle_record(edit)
        if edit_rect is None:
            return None
        ranked: list[tuple[float, Any]] = []
        edit_y = (edit_rect["top"] + edit_rect["bottom"]) / 2
        for candidate in candidates:
            rect = self._rectangle_record(candidate)
            if rect is None or not self._visible_enabled(candidate):
                continue
            center_y = (rect["top"] + rect["bottom"]) / 2
            if abs(center_y - edit_y) > max(
                18,
                edit_rect["bottom"] - edit_rect["top"],
            ):
                continue
            if rect["left"] < edit_rect["left"]:
                continue
            distance = abs(rect["left"] - edit_rect["right"])
            if distance <= max(60, edit_rect["right"] - edit_rect["left"]):
                ranked.append((distance, candidate))
        if not ranked:
            return None
        ranked.sort(key=lambda item: item[0])
        if (
            len(ranked) > 1
            and abs(ranked[0][0] - ranked[1][0]) < 1
        ):
            return None
        return ranked[0][1]

    def find_period_controls(
        self,
        window: Any,
    ) -> PeriodControls | None:
        """Resolve the period row from its exact label and structural controls."""

        elements = self._all_descendants(window)
        labels = [
            element
            for element in elements
            if " ".join(self._name(element).split()) == "기간"
            and self._safe_element_info_value(
                element, "control_type"
            ) in {"DataItem", "Text", "Static", "Label"}
            and self._visible_enabled(element)
        ]
        result_roots = [
            element
            for element in elements
            if self._safe_element_info_value(element, "automation_id")
            in {"tblSort", "tbList1-1"}
        ]
        if result_roots:
            result_tops = [
                rect["top"]
                for element in result_roots
                if (rect := self._rectangle_record(element))
            ]
            if result_tops:
                labels = [
                    label
                    for label in labels
                    if (
                        (self._rectangle_record(label) or {}).get("top", 10**9)
                        < min(result_tops)
                    )
                ]
        if len(labels) != 1:
            self._last_period_resolution = {
                "status": "CALENDAR_CONTROL_AMBIGUOUS",
                "reason": "period_label_count",
                "labels": [
                    self._diagnostic_identity(label)
                    for label in labels
                ],
            }
            return None
        label = labels[0]
        label_rect = self._rectangle_record(label)
        if label_rect is None:
            return None
        label_y = (label_rect["top"] + label_rect["bottom"]) / 2

        edits = [
            element
            for element in elements
            if self._safe_element_info_value(element, "control_type") == "Edit"
            and self._visible_enabled(element)
        ]
        date_edits = [
            edit
            for edit in edits
            if self._safe_element_info_value(
                edit, "automation_id"
            ) in {"I_SDATE", "I_EDATE"}
        ]
        if len(date_edits) != 2:
            date_edits = []
            for edit in edits:
                rect = self._rectangle_record(edit)
                class_name = self._safe_element_info_value(
                    edit, "class_name"
                ).casefold()
                if rect is None or "calendar" not in class_name:
                    continue
                edit_y = (rect["top"] + rect["bottom"]) / 2
                tolerance = max(
                    20,
                    label_rect["bottom"] - label_rect["top"],
                    rect["bottom"] - rect["top"],
                )
                if (
                    abs(edit_y - label_y) <= tolerance
                    and rect["left"] >= label_rect["right"]
                ):
                    date_edits.append(edit)
        date_edits.sort(
            key=lambda element: (
                self._rectangle_record(element) or {}
            ).get("left", 10**9)
        )
        if len(date_edits) != 2:
            self._last_period_resolution = {
                "status": "CALENDAR_CONTROL_AMBIGUOUS",
                "reason": "date_edit_count",
                "label": self._diagnostic_identity(label),
                "edits": [
                    self._diagnostic_identity(edit)
                    for edit in date_edits
                ],
            }
            return None
        start_edit, end_edit = date_edits
        if (
            self._safe_element_info_value(
                start_edit, "automation_id"
            ) == "I_EDATE"
            or self._safe_element_info_value(
                end_edit, "automation_id"
            ) == "I_SDATE"
        ):
            self._last_period_resolution = {
                "status": "CALENDAR_CONTROL_AMBIGUOUS",
                "reason": "date_role_conflict",
            }
            return None

        triggers = [
            element
            for element in elements
            if self._safe_element_info_value(
                element, "control_type"
            ) in {"Image", "Button", "Hyperlink", "Custom"}
            and (
                self._name(element).strip() == "달력"
                or "datepicker-trigger"
                in self._safe_element_info_value(
                    element, "class_name"
                ).casefold()
            )
        ]
        start_trigger = self._calendar_trigger_for_edit(
            start_edit, triggers
        )
        end_trigger = self._calendar_trigger_for_edit(
            end_edit, triggers
        )
        if (
            start_trigger is None
            or end_trigger is None
            or self._same_element(start_trigger, end_trigger)
        ):
            self._last_period_resolution = {
                "status": "CALENDAR_CONTROL_AMBIGUOUS",
                "reason": "calendar_trigger_resolution",
            }
            return None
        diagnostics = {
            "status": "SELECTED",
            "period_label": {
                **self._diagnostic_identity(label),
                "rectangle": label_rect,
            },
            "selected_start_control": {
                **self._diagnostic_identity(start_edit),
                "rectangle": self._rectangle_record(start_edit),
                "reason": "left_date_control_in_period_row",
            },
            "selected_end_control": {
                **self._diagnostic_identity(end_edit),
                "rectangle": self._rectangle_record(end_edit),
                "reason": "right_date_control_in_period_row",
            },
            "start_trigger": {
                **self._diagnostic_identity(start_trigger),
                "rectangle": self._rectangle_record(start_trigger),
            },
            "end_trigger": {
                **self._diagnostic_identity(end_trigger),
                "rectangle": self._rectangle_record(end_trigger),
            },
            "common_ancestor_distance": (
                self._nearest_shared_ancestor_distance(
                    start_edit, end_edit
                )
            ),
        }
        self._last_period_resolution = diagnostics
        self.calendar_logger.info(
            "period_control_resolution=%s",
            json.dumps(diagnostics, ensure_ascii=False, default=str),
        )
        return PeriodControls(
            label=label,
            start_edit=start_edit,
            end_edit=end_edit,
            start_trigger=start_trigger,
            end_trigger=end_trigger,
            diagnostics=diagnostics,
        )

    def _calendar_table(self, window: Any) -> Any | None:
        tables = [
            element
            for element in self._all_descendants(window)
            if self._safe_element_info_value(element, "control_type")
            in {"Table", "Calendar"}
            and (
                "ui-datepicker-calendar"
                in self._safe_element_info_value(
                    element, "class_name"
                ).casefold()
                or self._safe_element_info_value(
                    element, "control_type"
                ) == "Calendar"
            )
            and self._visible_enabled(element)
        ]
        return tables[0] if len(tables) == 1 else None

    def _calendar_controls(
        self,
        window: Any,
    ) -> dict[str, Any] | None:
        table = self._calendar_table(window)
        if table is None:
            return None
        table_rect = self._rectangle_record(table)
        if table_rect is None:
            return None
        nearby = []
        for element in self._all_descendants(window):
            rect = self._rectangle_record(element)
            if rect is None or not self._visible_enabled(element):
                continue
            center_x = (rect["left"] + rect["right"]) / 2
            if (
                table_rect["left"] - 30 <= center_x
                <= table_rect["right"] + 30
                and table_rect["top"] - 60 <= rect["top"]
                <= table_rect["bottom"]
            ):
                nearby.append(element)
        years = [
            element
            for element in nearby
            if self._safe_element_info_value(
                element, "control_type"
            ) == "ComboBox"
            and "datepicker-year"
            in self._safe_element_info_value(
                element, "class_name"
            ).casefold()
        ]
        months = [
            element
            for element in nearby
            if self._safe_element_info_value(
                element, "control_type"
            ) == "ComboBox"
            and "datepicker-month"
            in self._safe_element_info_value(
                element, "class_name"
            ).casefold()
        ]
        return {
            "table": table,
            "table_rectangle": table_rect,
            "year": years[0] if len(years) == 1 else None,
            "month": months[0] if len(months) == 1 else None,
            "elements": nearby,
        }

    @staticmethod
    def _execute_uia_control(element: Any) -> str:
        try:
            element.invoke()
            return "invoke"
        except Exception:
            try:
                element.select()
                return "select"
            except Exception:
                try:
                    element.expand()
                    return "expand"
                except Exception:
                    element.click_input()
                    return "click_input"

    def _open_calendar(
        self,
        window: Any,
        *,
        role: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        controls = self.find_period_controls(window)
        if controls is None:
            return None, {
                "code": "CALENDAR_CONTROL_AMBIGUOUS",
                "role": role,
            }
        existing_popup = self._calendar_controls(window)
        if existing_popup is not None:
            diagnostic = {
                "role": role,
                "open_method": "already_open",
                "trigger": None,
                "calendar_year_control": (
                    self._diagnostic_identity(
                        existing_popup["year"]
                    )
                    if existing_popup.get("year") is not None
                    else None
                ),
                "calendar_month_control": (
                    self._diagnostic_identity(
                        existing_popup["month"]
                    )
                    if existing_popup.get("month") is not None
                    else None
                ),
                "calendar_table": {
                    **self._diagnostic_identity(
                        existing_popup["table"]
                    ),
                    "rectangle": existing_popup[
                        "table_rectangle"
                    ],
                },
            }
            return existing_popup, diagnostic
        trigger = (
            controls.start_trigger
            if role == "start"
            else controls.end_trigger
        )
        method = self._execute_uia_control(trigger)
        time.sleep(0.15)
        popup = self._calendar_controls(window)
        if popup is None and method == "invoke":
            # Chrome exposes a no-op InvokePattern for the real DPS Image.
            controls = self.find_period_controls(window)
            if controls is None:
                return None, {
                    "code": "CALENDAR_CONTROL_AMBIGUOUS",
                    "role": role,
                }
            trigger = (
                controls.start_trigger
                if role == "start"
                else controls.end_trigger
            )
            trigger.click_input()
            method = "invoke_then_click_input"
            time.sleep(0.2)
            popup = self._calendar_controls(window)
        diagnostic = {
            "role": role,
            "open_method": method,
            "trigger": self._diagnostic_identity(trigger),
            "calendar_year_control": (
                self._diagnostic_identity(popup["year"])
                if popup and popup.get("year") is not None
                else None
            ),
            "calendar_month_control": (
                self._diagnostic_identity(popup["month"])
                if popup and popup.get("month") is not None
                else None
            ),
            "calendar_table": (
                {
                    **self._diagnostic_identity(popup["table"]),
                    "rectangle": popup["table_rectangle"],
                }
                if popup
                else None
            ),
        }
        self.calendar_logger.info(
            "calendar_open=%s",
            json.dumps(diagnostic, ensure_ascii=False, default=str),
        )
        return popup, diagnostic

    def _combobox_selected_text(self, combo: Any) -> str | None:
        for getter in (
            lambda: combo.selected_text(),
            lambda: combo.iface_value.current_value,
            lambda: combo.iface_value.CurrentValue,
            lambda: combo.window_text(),
            lambda: combo.element_info.name,
        ):
            try:
                value = getter()
                if value not in (None, ""):
                    return " ".join(str(value).split())
            except Exception:
                continue
        return None

    @staticmethod
    def _year_from_text(value: Any) -> int | None:
        match = re.search(r"(?<!\d)(20\d{2})(?!\d)", str(value or ""))
        return int(match.group(1)) if match else None

    @staticmethod
    def _month_from_text(value: Any) -> int | None:
        text = str(value or "").strip().casefold()
        match = re.search(r"(?<!\d)(1[0-2]|0?[1-9])\s*월?", text)
        if match:
            return int(match.group(1))
        english = {
            name.casefold(): index
            for index, name in enumerate(calendar.month_name)
            if name
        }
        return english.get(text)

    def _select_combo_value(
        self,
        combo: Any,
        labels: tuple[str, ...],
        *,
        index: int | None = None,
    ) -> str | None:
        for label in labels:
            try:
                combo.select(label)
                return f"select:{label}"
            except Exception:
                continue
        if index is not None:
            try:
                combo.select(index)
                return f"select_index:{index}"
            except Exception:
                pass
        try:
            combo.expand()
        except Exception:
            try:
                combo.click_input()
            except Exception:
                return None
        try:
            items = combo.descendants(control_type="ListItem")
        except Exception:
            items = []
        for item in items:
            if " ".join(self._name(item).split()) not in labels:
                continue
            return self._execute_uia_control(item)
        return None

    def _calendar_displayed_year_month(
        self,
        window: Any,
        *,
        role: str,
    ) -> tuple[int | None, int | None]:
        popup = self._calendar_controls(window)
        if popup is None:
            return None, None
        year = (
            self._year_from_text(
                self._combobox_selected_text(popup["year"])
            )
            if popup.get("year") is not None
            else None
        )
        month = (
            self._month_from_text(
                self._combobox_selected_text(popup["month"])
            )
            if popup.get("month") is not None
            else None
        )
        if year is None or month is None:
            period = self.find_period_controls(window)
            edit = (
                period.start_edit
                if period is not None and role == "start"
                else period.end_edit
                if period is not None
                else None
            )
            current = (
                parse_date_value(self._read_edit_value(edit))
                if edit is not None
                else None
            )
            if current is not None:
                year = year or current.year
                month = month or current.month
        return year, month

    def _move_calendar_to_month(
        self,
        window: Any,
        target: date,
        *,
        role: str,
    ) -> tuple[bool, dict[str, Any]]:
        methods: list[str] = []
        popup = self._calendar_controls(window)
        if popup is None:
            return False, {"code": "CALENDAR_OPEN_FAILED"}
        current_year, current_month = self._calendar_displayed_year_month(
            window, role=role
        )
        if current_year == target.year and current_month == target.month:
            return True, {
                "displayed_year": current_year,
                "displayed_month": current_month,
                "methods": methods,
            }

        if popup.get("year") is not None and current_year != target.year:
            method = self._select_combo_value(
                popup["year"],
                (str(target.year), f"{target.year}년"),
            )
            if method:
                methods.append(f"year:{method}")
                time.sleep(0.15)
        popup = self._calendar_controls(window)
        if popup and popup.get("month") is not None:
            _, current_month = self._calendar_displayed_year_month(
                window, role=role
            )
            if current_month != target.month:
                method = self._select_combo_value(
                    popup["month"],
                    (
                        f"{target.month}월",
                        f"{target.month:02d}월",
                        str(target.month),
                        f"{target.month:02d}",
                        calendar.month_name[target.month],
                    ),
                    index=target.month - 1,
                )
                if method:
                    methods.append(f"month:{method}")
                    time.sleep(0.15)

        current_year, current_month = self._calendar_displayed_year_month(
            window, role=role
        )
        for _ in range(36):
            if current_year == target.year and current_month == target.month:
                return True, {
                    "displayed_year": current_year,
                    "displayed_month": current_month,
                    "methods": methods,
                }
            if current_year is None or current_month is None:
                break
            current_index = current_year * 12 + current_month
            target_index = target.year * 12 + target.month
            direction = "다음달" if current_index < target_index else "이전달"
            popup = self._calendar_controls(window)
            if popup is None:
                break
            candidates = [
                element
                for element in popup["elements"]
                if " ".join(self._name(element).split()) == direction
                and self._safe_element_info_value(
                    element, "control_type"
                ) in {"Hyperlink", "Button"}
            ]
            if len(candidates) != 1:
                break
            methods.append(
                f"{direction}:{self._execute_uia_control(candidates[0])}"
            )
            time.sleep(0.12)
            current_year, current_month = (
                self._calendar_displayed_year_month(
                    window, role=role
                )
            )
        return False, {
            "code": (
                "YEAR_SELECT_FAILED"
                if current_year != target.year
                else "MONTH_SELECT_FAILED"
            ),
            "displayed_year": current_year,
            "displayed_month": current_month,
            "methods": methods,
        }

    def _calendar_day_candidates(
        self,
        window: Any,
        target: date,
    ) -> tuple[list[Any], dict[str, Any]]:
        popup = self._calendar_controls(window)
        if popup is None:
            return [], {"reason": "calendar_missing"}
        table = popup["table"]
        table_rect = popup["table_rectangle"]
        elements = self._all_descendants(window)
        headers = [
            element
            for element in elements
            if self._name(element).strip()
            in {"일", "월", "화", "수", "목", "금", "토"}
            and self._is_descendant_of(element, table)
        ]
        headers.sort(
            key=lambda element: (
                self._rectangle_record(element) or {}
            ).get("left", 10**9)
        )
        header_names = [self._name(element).strip() for element in headers]
        sunday_first = header_names == [
            "일", "월", "화", "수", "목", "금", "토"
        ]
        month_weeks = calendar.Calendar(
            firstweekday=6 if sunday_first else 0
        ).monthdayscalendar(target.year, target.month)
        expected_week = next(
            (
                row_index
                for row_index, week in enumerate(month_weeks)
                if target.day in week
            ),
            None,
        )
        expected_column = next(
            (
                column_index
                for week in month_weeks
                for column_index, day_value in enumerate(week)
                if day_value == target.day
            ),
            None,
        )
        raw_candidates = [
            element
            for element in elements
            if self._safe_element_info_value(
                element, "control_type"
            ) in {"Hyperlink", "Button"}
            and self._name(element).strip() == str(target.day)
            and self._is_descendant_of(element, table)
            and self._visible_enabled(element)
        ]
        candidate_records: list[dict[str, Any]] = []
        accepted: list[Any] = []
        day_rows = sorted(
            {
                (self._rectangle_record(element) or {}).get("top")
                for element in elements
                if self._safe_element_info_value(
                    element, "control_type"
                ) == "Hyperlink"
                and self._name(element).strip().isdigit()
                and self._is_descendant_of(element, table)
                and (self._rectangle_record(element) or {}).get("top")
                is not None
            }
        )
        for candidate in raw_candidates:
            rect = self._rectangle_record(candidate)
            parent = self._safe_parent(candidate)
            parent_class = self._safe_element_info_value(
                parent, "class_name"
            ).casefold()
            reason = None
            if any(
                token in parent_class
                for token in (
                    "other-month",
                    "unselectable",
                    "state-disabled",
                )
            ):
                reason = "outside_current_month"
            column = None
            row = None
            if rect is not None:
                center_x = (rect["left"] + rect["right"]) / 2
                column_width = (
                    table_rect["right"] - table_rect["left"]
                ) / 7
                column = min(
                    6,
                    max(
                        0,
                        int(
                            (center_x - table_rect["left"])
                            / max(1, column_width)
                        ),
                    ),
                )
                if rect["top"] in day_rows:
                    row = day_rows.index(rect["top"])
            if (
                reason is None
                and expected_column is not None
                and column != expected_column
            ):
                reason = "weekday_column_mismatch"
            if (
                reason is None
                and expected_week is not None
                and row != expected_week
            ):
                reason = "calendar_week_mismatch"
            record = {
                **self._diagnostic_identity(candidate),
                "rectangle": rect,
                "parent_class_name": parent_class,
                "row": row,
                "column": column,
                "selected": reason is None,
                "reason": reason or "target_month_grid_match",
            }
            candidate_records.append(record)
            if reason is None:
                accepted.append(candidate)
        diagnostics = {
            "weekday_headers": header_names,
            "sunday_first": sunday_first,
            "expected_week": expected_week,
            "expected_column": expected_column,
            "candidates": candidate_records,
        }
        self.calendar_logger.info(
            "calendar_day_candidates=%s",
            json.dumps(diagnostics, ensure_ascii=False, default=str),
        )
        return accepted, diagnostics

    def _select_calendar_date(
        self,
        window: Any,
        target: date,
        *,
        role: str,
    ) -> dict[str, Any]:
        popup, open_diagnostic = self._open_calendar(
            window, role=role
        )
        if popup is None:
            return {
                "success": False,
                "code": (
                    "CALENDAR_OPEN_FAILED_START"
                    if role == "start"
                    else "CALENDAR_OPEN_FAILED_END"
                ),
                "diagnostics": open_diagnostic,
            }
        moved, move_diagnostic = self._move_calendar_to_month(
            window, target, role=role
        )
        if not moved:
            return {
                "success": False,
                "code": move_diagnostic.get(
                    "code", "MONTH_SELECT_FAILED"
                ),
                "diagnostics": {
                    **open_diagnostic,
                    "month_selection": move_diagnostic,
                },
            }
        displayed_year, displayed_month = (
            self._calendar_displayed_year_month(
                window, role=role
            )
        )
        if (
            displayed_year != target.year
            or displayed_month != target.month
        ):
            return {
                "success": False,
                "code": "MONTH_SELECT_FAILED",
                "diagnostics": {
                    **open_diagnostic,
                    "month_selection": move_diagnostic,
                },
            }
        candidates, day_diagnostic = self._calendar_day_candidates(
            window, target
        )
        if len(candidates) != 1:
            return {
                "success": False,
                "code": (
                    "CALENDAR_DAY_AMBIGUOUS"
                    if len(candidates) > 1
                    else "DAY_SELECT_FAILED"
                ),
                "diagnostics": {
                    **open_diagnostic,
                    "month_selection": move_diagnostic,
                    "day_selection": day_diagnostic,
                },
            }
        day = candidates[0]
        day_identity = self._diagnostic_identity(day)
        day_rectangle = self._rectangle_record(day)
        method = self._execute_uia_control(day)
        time.sleep(0.18)
        period = self.find_period_controls(window)
        edit = (
            period.start_edit
            if period is not None and role == "start"
            else period.end_edit
            if period is not None
            else None
        )
        actual = (
            parse_date_value(self._read_edit_value(edit))
            if edit is not None
            else None
        )
        if actual != target and method == "invoke":
            # Like the trigger, a DOM hyperlink may expose a no-op Invoke.
            candidates, day_diagnostic = self._calendar_day_candidates(
                window, target
            )
            if len(candidates) == 1:
                candidates[0].click_input()
                method = "invoke_then_click_input"
                time.sleep(0.18)
                period = self.find_period_controls(window)
                edit = (
                    period.start_edit
                    if period is not None and role == "start"
                    else period.end_edit
                    if period is not None
                    else None
                )
                actual = (
                    parse_date_value(self._read_edit_value(edit))
                    if edit is not None
                    else None
                )
        return {
            "success": actual == target,
            "code": (
                "DATE_RANGE_READY"
                if actual == target
                else (
                    "DATE_START_VERIFY_FAILED"
                    if role == "start"
                    else "DATE_END_VERIFY_FAILED"
                )
            ),
            "actual": actual.isoformat() if actual else None,
            "diagnostics": {
                **open_diagnostic,
                "month_selection": move_diagnostic,
                "day_selection": day_diagnostic,
                f"selected_{role}_day": {
                    **day_identity,
                    "rectangle": day_rectangle,
                    "target": target.isoformat(),
                    "execution_method": method,
                },
            },
        }

    def verify_period_values(
        self,
        window: Any,
        expected_start: date,
        expected_end: date,
    ) -> dict[str, Any]:
        controls = self.find_period_controls(window)
        if controls is None:
            return {
                "success": False,
                "code": "CALENDAR_CONTROL_AMBIGUOUS",
            }
        raw_start = self._read_edit_value(controls.start_edit)
        raw_end = self._read_edit_value(controls.end_edit)
        valid, code, actual_start, actual_end = (
            validate_dps_lookup_period(raw_start, raw_end)
        )
        exact = (
            valid
            and actual_start == expected_start
            and actual_end == expected_end
        )
        if actual_start != expected_start:
            code = "DATE_START_VERIFY_FAILED"
        elif actual_end != expected_end:
            code = "DATE_END_VERIFY_FAILED"
        return {
            "success": exact,
            "code": "DATE_RANGE_READY" if exact else code,
            "raw_start": raw_start,
            "raw_end": raw_end,
            "actual_start": (
                actual_start.isoformat() if actual_start else None
            ),
            "actual_end": (
                actual_end.isoformat() if actual_end else None
            ),
            "expected_start": expected_start.isoformat(),
            "expected_end": expected_end.isoformat(),
        }

    def configure_lookup_period(
        self,
        window: Any,
        start_value: Any,
        end_value: Any,
    ) -> dict[str, Any]:
        valid, code, start, end = validate_dps_lookup_period(
            start_value, end_value
        )
        if not valid or start is None or end is None:
            return {
                "success": False,
                "code": code,
                "diagnostics": {"query_invocation_count": 0},
            }
        start_result = self._select_calendar_date(
            window, start, role="start"
        )
        if not start_result["success"]:
            return start_result
        # Do not reuse the end control wrapper after the start calendar rerender.
        if self.find_period_controls(window) is None:
            return {
                "success": False,
                "code": "CALENDAR_CONTROL_AMBIGUOUS",
                "diagnostics": {
                    "start": start_result,
                    "query_invocation_count": 0,
                },
            }
        end_result = self._select_calendar_date(
            window, end, role="end"
        )
        if not end_result["success"]:
            return {
                **end_result,
                "diagnostics": {
                    "start": start_result,
                    "end": end_result.get("diagnostics"),
                    "query_invocation_count": 0,
                },
            }
        verification = self.verify_period_values(
            window, start, end
        )
        diagnostics = {
            "selected_start_control": (
                self._last_period_resolution.get(
                    "selected_start_control"
                )
            ),
            "selected_end_control": (
                self._last_period_resolution.get(
                    "selected_end_control"
                )
            ),
            "start": start_result.get("diagnostics"),
            "end": end_result.get("diagnostics"),
            "calendar_year_control": (
                start_result.get("diagnostics", {}).get(
                    "calendar_year_control"
                )
            ),
            "calendar_month_control": (
                start_result.get("diagnostics", {}).get(
                    "calendar_month_control"
                )
            ),
            "selected_start_day": (
                start_result.get("diagnostics", {}).get(
                    "selected_start_day"
                )
            ),
            "selected_end_day": (
                end_result.get("diagnostics", {}).get(
                    "selected_end_day"
                )
            ),
            "date_verification": verification,
            "query_invocation_count": 0,
        }
        self.calendar_logger.info(
            "calendar_period_verification=%s",
            json.dumps(diagnostics, ensure_ascii=False, default=str),
        )
        return {
            "success": verification["success"],
            "code": verification["code"],
            "diagnostics": diagnostics,
        }

    def perform_lookup(
        self,
        *,
        window: Any,
        request_id: str | None = None,
        expected_order_id: str | None = None,
        order_number: str | None = None,
        order_id: str | None = None,
        product_order_id: str | None = None,
        dps_query_value: str | None = None,
        dps_query_value_type: str | None = None,
        query_fallback_used: bool = False,
        dps_date_source: str | None = None,
        dps_reference_date: str | None = None,
        dps_period_start: str | None = None,
        dps_period_end: str | None = None,
        validate_target: Callable[[], tuple[bool, dict[str, Any]]],
        result_timeout: float = 20.0,
        detail_window_provider: Callable[[], list[Any]] | None = None,
        detail_url_reader: Callable[[Any], str] | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        self._detail_invocation_count = 0
        query_value = str(dps_query_value or order_number or "").strip()
        query_value_type = str(
            dps_query_value_type
            or ("order_id" if order_number else "")
        ).strip()
        expected_value = str(
            expected_order_id or order_id or order_number or ""
        ).strip()
        product_value = str(product_order_id or "").strip()
        if not expected_value:
            return self._failure(
                "DPS_ORDER_ID_MISSING",
                "네이버 주문번호가 없어 DPS 조회를 실행할 수 없습니다.",
                {
                    "request_id": request_id,
                    "query_value_type": query_value_type or None,
                    "query_invocation_count": 0,
                },
            )
        if query_value_type != "order_id":
            return self._failure(
                "INVALID_DPS_QUERY_TYPE",
                "상품주문번호가 DPS 조회값으로 전달되어 안전을 위해 중단했습니다.",
                {
                    "request_id": request_id,
                    "query_value_type": query_value_type or None,
                    "query_invocation_count": 0,
                },
            )
        if query_value != expected_value:
            return self._failure(
                "DPS_QUERY_IDENTIFIER_MISMATCH",
                "Automation 네이버 주문번호와 DPS 조회값이 일치하지 않습니다.",
                {
                    "request_id": request_id,
                    "expected_order_id": expected_value,
                    "query_invocation_count": 0,
                },
            )
        if (
            product_value
            and product_value != expected_value
            and query_value == product_value
        ):
            return self._failure(
                "PRODUCT_ORDER_ID_WAS_USED_BY_MISTAKE",
                "상품주문번호를 DPS 입력값으로 사용할 수 없습니다.",
                {
                    "request_id": request_id,
                    "query_invocation_count": 0,
                },
            )
        page = self.verify_purchase_request_page(window)
        if not page.navigation_ok:
            return self._failure(
                "PURCHASE_REQUEST_PAGE_NOT_VERIFIED",
                "구매요청리스트 화면 진입 상태를 확인하지 못했습니다.",
                {
                    "page_markers": page.marker_hits,
                    "sales_menu_selected": page.sales_menu_selected,
                    "online_sales_expanded": page.online_sales_expanded,
                    "purchase_request_page_marker_found": page.page_marker_found,
                    "order_input_found": page.edit is not None,
                    "query_button_found": page.button is not None,
                    "reason": page.reason,
                },
            )
        if page.edit is None:
            field_code = str(
                self._last_field_resolution.get("status") or ""
            )
            return self._failure(
                "FIELD_AMBIGUOUS" if field_code == "FIELD_AMBIGUOUS" else "ORDER_INPUT_NOT_FOUND",
                "구매요청리스트 이동은 성공했지만 온라인판매 주문번호 입력칸을 확정하지 못했습니다.",
                {
                    "navigation_completed": True,
                    "sales_menu_selected": page.sales_menu_selected,
                    "online_sales_expanded": page.online_sales_expanded,
                    "purchase_request_page_marker_found": page.page_marker_found,
                    "selected_edit": self._last_field_resolution,
                    "query_invocation_count": 0,
                },
            )
        if page.query_action is None:
            return self._failure(
                "QUERY_ACTION_NOT_FOUND",
                "주문번호 입력칸은 확인했지만 조회 실행 요소를 찾지 못했습니다.",
                {
                    "navigation_completed": True,
                    "order_input_found": True,
                    "selected_edit": self._last_field_resolution,
                    "selected_query_control": self._last_query_resolution,
                    "query_invocation_count": 0,
                },
            )

        edit = page.edit
        query_action = page.query_action
        if progress_callback:
            progress_callback("ORDER_ID_INPUT")

        # 첫 번째 안전 게이트: 요소를 찾았더라도 탭/foreground가 바뀌었으면 아무 입력도 하지 않습니다.
        target_ok, checks = validate_target()
        if not target_ok:
            return self._failure(
                "INPUT_SAFETY_CHECK_FAILED",
                "DPS 탭 선택 검증에 실패하여 주문번호를 입력하지 않았습니다.",
                checks,
            )

        try:
            self._replace_edit_value(edit, query_value, validate_target)
            self.logger.info(
                "DPS 조회값 입력 완료: request_id=%s type=%s value=%s",
                request_id,
                query_value_type,
                self.mask_order_number(query_value),
            )
        except InputSafetyError as error:
            return self._failure(
                "INPUT_SAFETY_CHECK_FAILED",
                "입력 도중 DPS 탭 검증이 풀려 주문번호 입력을 중단했습니다.",
                error.checks,
            )
        except Exception as error:
            self.logger.exception("주문번호 입력 실패")
            return self._failure(
                "ORDER_INPUT_FAILED",
                "온라인판매 주문번호 입력에 실패했습니다.",
                {"error": error.__class__.__name__},
            )
        if progress_callback:
            progress_callback("ORDER_NUMBER_ENTERED")

        # Rewalk the tree and resolve the field again; stale UIA objects are not trusted.
        verified_edit = self.find_order_edit(window)
        actual_value = (
            self._read_edit_value(verified_edit)
            if verified_edit is not None
            else None
        )
        input_verified = (
            verified_edit is not None
            and str(actual_value or "").strip() == query_value.strip()
        )
        wrong_field_matches = (
            self._edits_with_value(
                window,
                query_value,
                selected_edit=verified_edit,
            )
            if verified_edit is not None
            else []
        )
        self.logger.info(
            "DPS 조회값 재탐색 검증: request_id=%s ok=%s type=%s requested=%s actual=%s wrong=%s",
            request_id,
            input_verified,
            query_value_type,
            self.mask_order_number(query_value),
            self.mask_order_number(actual_value or ""),
            json.dumps(
                self._mask_diagnostic_record(wrong_field_matches),
                ensure_ascii=False,
            ),
        )
        input_diagnostics = {
            "request_id": request_id,
            "automation_expected_order_id": expected_value,
            "query_value": query_value,
            "query_value_type": query_value_type,
            "verified_value": actual_value,
            "verified_field": (
                self._diagnostic_identity(verified_edit)
                if verified_edit is not None
                else None
            ),
            "wrong_field_matches": wrong_field_matches,
            "input_verified": input_verified and not wrong_field_matches,
            "dps_input_verified_value": actual_value,
            "selected_edit": self._last_field_resolution,
            "query_fallback_used": bool(query_fallback_used),
            "query_invocation_count": 0,
        }
        if wrong_field_matches:
            return self._failure(
                "WRONG_FIELD_INPUT",
                "온라인판매 주문번호가 아닌 다른 입력칸에서 조회값이 발견되어 조회하지 않았습니다.",
                input_diagnostics,
            )
        if (
            product_value
            and product_value != expected_value
            and str(actual_value or "").strip() == product_value
        ):
            return self._failure(
                "PRODUCT_ORDER_ID_WAS_USED_BY_MISTAKE",
                "DPS 입력칸에서 상품주문번호가 발견되어 조회하지 않았습니다.",
                input_diagnostics,
            )
        if not input_verified:
            return self._failure(
                "INPUT_VERIFY_FAILED",
                "주문번호 입력값을 다시 확인하지 못해 조회를 실행하지 않았습니다.",
                {
                    **input_diagnostics,
                    "requested_masked": self.mask_order_number(query_value),
                    "actual_masked": self.mask_order_number(actual_value or ""),
                },
            )
        period_diagnostics: dict[str, Any] = {
            "date_range_required": bool(
                dps_reference_date
                or dps_period_start
                or dps_period_end
            ),
            "dps_date_source": dps_date_source,
            "dps_reference_date": dps_reference_date,
            "dps_period_start": dps_period_start,
            "dps_period_end": dps_period_end,
        }
        if period_diagnostics["date_range_required"]:
            if progress_callback:
                progress_callback("DATE_RANGE_SETTING")
            if not (
                dps_date_source
                and dps_reference_date
                and dps_period_start
                and dps_period_end
            ):
                return self._failure(
                    "DATE_SOURCE_MISSING",
                    "주문일을 확인하지 못해 DPS 조회 기간을 계산할 수 없습니다.",
                    {
                        **input_diagnostics,
                        **period_diagnostics,
                        "query_invocation_count": 0,
                    },
                )
            target_ok, checks = validate_target()
            if not target_ok:
                return self._failure(
                    "INPUT_SAFETY_CHECK_FAILED",
                    "기간 설정 전 DPS 탭 검증이 풀려 조회를 중단했습니다.",
                    {
                        **checks,
                        "query_invocation_count": 0,
                    },
                )
            period_result = self.configure_lookup_period(
                window,
                dps_period_start,
                dps_period_end,
            )
            period_diagnostics.update(
                period_result.get("diagnostics") or {}
            )
            if not period_result.get("success"):
                code = str(
                    period_result.get("code")
                    or "DATE_RANGE_INVALID"
                )
                messages = {
                    "CALENDAR_OPEN_FAILED_START": "기간 시작일 달력을 열지 못했습니다.",
                    "CALENDAR_OPEN_FAILED_END": "기간 종료일 달력을 열지 못했습니다.",
                    "YEAR_SELECT_FAILED": "목표 연도를 선택하지 못했습니다.",
                    "MONTH_SELECT_FAILED": "목표 월을 선택하지 못했습니다.",
                    "DAY_SELECT_FAILED": "목표 날짜를 확정하지 못했습니다.",
                    "CALENDAR_DAY_AMBIGUOUS": "달력 날짜 후보가 불명확하여 조회하지 않았습니다.",
                    "DATE_START_VERIFY_FAILED": "선택된 시작일을 다시 확인하지 못해 조회하지 않았습니다.",
                    "DATE_END_VERIFY_FAILED": "선택된 종료일을 다시 확인하지 못해 조회하지 않았습니다.",
                    "DATE_RANGE_INVALID": "DPS의 한 달 조회 범위를 초과해 조회하지 않았습니다.",
                    "CALENDAR_CONTROL_AMBIGUOUS": "기간 시작일과 종료일 컨트롤을 확정하지 못했습니다.",
                }
                return self._failure(
                    code,
                    messages.get(
                        code,
                        "선택된 기간을 다시 확인하지 못해 조회하지 않았습니다.",
                    ),
                    {
                        **input_diagnostics,
                        **period_diagnostics,
                        "query_invocation_count": 0,
                    },
                )
            period_diagnostics.pop("query_invocation_count", None)
            # Calendar operations rerender the search form. Rewalk and verify
            # the order field again before locating the query action.
            verified_edit = self.find_order_edit(window)
            actual_value = (
                self._read_edit_value(verified_edit)
                if verified_edit is not None
                else None
            )
            wrong_field_matches = (
                self._edits_with_value(
                    window,
                    query_value,
                    selected_edit=verified_edit,
                )
                if verified_edit is not None
                else []
            )
            if (
                verified_edit is None
                or str(actual_value or "").strip()
                != query_value.strip()
                or wrong_field_matches
            ):
                return self._failure(
                    (
                        "WRONG_FIELD_INPUT"
                        if wrong_field_matches
                        else "INPUT_VERIFY_FAILED"
                    ),
                    "기간 설정 후 상품주문번호를 다시 확인하지 못해 조회하지 않았습니다.",
                    {
                        **input_diagnostics,
                        **period_diagnostics,
                        "verified_value": actual_value,
                        "wrong_field_matches": wrong_field_matches,
                        "query_invocation_count": 0,
                    },
                )

        edit = verified_edit
        query_action = self.find_query_action(window, order_edit=edit)
        if query_action is None:
            return self._failure(
                "QUERY_ACTION_NOT_FOUND",
                "상품주문번호 입력은 성공했지만 조회 요소를 찾지 못했습니다.",
                {
                    **input_diagnostics,
                    "selected_query_control": self._last_query_resolution,
                },
            )

        # 값 설정 후에도 탭·URL·로그인·페이지·요소 검증이 풀렸다면 조회 금지.
        target_ok, checks = validate_target()
        if not target_ok:
            return self._failure(
                "QUERY_SAFETY_CHECK_FAILED",
                "DPS 탭 검증이 풀려 조회 버튼을 누르지 않았습니다.",
                checks,
            )

        before = self.collect_result_snapshot(window)
        target_ok, checks = validate_target()
        if not target_ok:
            return self._failure(
                "QUERY_SAFETY_CHECK_FAILED",
                "조회 실행 직전 DPS 탭 검증이 풀려 조회 버튼을 누르지 않았습니다.",
                checks,
            )
        final_value = self._read_edit_value(edit)
        final_wrong_matches = self._edits_with_value(
            window,
            query_value,
            selected_edit=edit,
        )
        if (
            str(final_value or "").strip() != query_value.strip()
            or final_wrong_matches
        ):
            return self._failure(
                "INPUT_VERIFY_FAILED" if not final_wrong_matches else "WRONG_FIELD_INPUT",
                "조회 실행 직전 온라인판매 주문번호 입력값 검증에 실패했습니다.",
                {
                    **input_diagnostics,
                    "verified_value": final_value,
                    "wrong_field_matches": final_wrong_matches,
                    "query_invocation_count": 0,
                },
            )
        if period_diagnostics["date_range_required"]:
            expected_start = parse_date_value(dps_period_start)
            expected_end = parse_date_value(dps_period_end)
            if expected_start is None or expected_end is None:
                return self._failure(
                    "DATE_RANGE_INVALID",
                    "조회 실행 직전 기간 값 검증에 실패했습니다.",
                    {
                        **period_diagnostics,
                        "query_invocation_count": 0,
                    },
                )
            final_period = self.verify_period_values(
                window, expected_start, expected_end
            )
            period_diagnostics["final_date_verification"] = (
                final_period
            )
            if not final_period["success"]:
                return self._failure(
                    final_period["code"],
                    "조회 실행 직전 선택된 기간을 다시 확인하지 못했습니다.",
                    {
                        **input_diagnostics,
                        **period_diagnostics,
                        "query_invocation_count": 0,
                    },
                )
        invocation_method = None
        if progress_callback:
            progress_callback("LIST_QUERY_EXECUTED")
        try:
            try:
                query_action.invoke()
                invocation_method = "invoke"
            except Exception:
                target_ok, checks = validate_target()
                if not target_ok:
                    return self._failure(
                        "QUERY_SAFETY_CHECK_FAILED",
                        "조회 버튼 좌표 클릭 직전 DPS 탭 검증이 풀려 실행을 중단했습니다.",
                        checks,
                    )
                query_action.click_input()
                invocation_method = "click_input"
            self.logger.info(
                "조회 액션 실행 성공: method=%s count=1 control=%s",
                invocation_method,
                json.dumps(
                    self._diagnostic_identity(query_action),
                    ensure_ascii=False,
                ),
            )
            if progress_callback:
                progress_callback("SEARCH_CLICKED")
        except Exception as error:
            self.logger.exception("조회 버튼 실행 실패")
            return self._failure(
                "QUERY_BUTTON_CLICK_FAILED",
                "DPS 조회 버튼 실행에 실패했습니다.",
                {"error": error.__class__.__name__},
            )

        polling = self.wait_for_lookup_result(
            window,
            before,
            timeout=result_timeout,
            expected_query_value=query_value,
        )
        after = polling["snapshot"]
        if polling["status"] == "timeout":
            return self._failure(
                "LOOKUP_RESULT_TIMEOUT",
                "DPS 조회 결과 로딩 시간이 초과되었습니다.",
                {
                    "navigation_completed": True,
                    "order_input_verified": True,
                    "query_invoked": True,
                    "query_invocation_count": 1,
                },
            )
        parsed = self.parse_lookup_result(
            after,
            naver_order_id=query_value,
            order_id=order_id,
            product_order_id=product_order_id,
            dps_query_value=query_value,
            dps_query_value_type=query_value_type,
            query_fallback_used=query_fallback_used,
            dps_date_source=dps_date_source,
            dps_reference_date=dps_reference_date,
            dps_period_start=dps_period_start,
            dps_period_end=dps_period_end,
        )
        if polling["status"] == "no_result":
            return {
                "ok": True,
                "success": True,
                "found": False,
                "code": "NO_DPS_RESULT",
                "status": "NO_DPS_RESULT",
                "message": "해당 주문번호의 DPS 조회 결과가 없습니다.",
                "data": {
                    **parsed["data"],
                    "naver_order_id": query_value,
                },
                "raw_result_texts": after["raw_result_texts"],
                "table_headers": after["table_headers"],
                "table_rows": after["table_rows"],
                "diagnostics": {
                    **input_diagnostics,
                    "navigation_completed": True,
                    "order_input_verified": True,
                    "query_invoked": True,
                    "query_invocation_count": 1,
                    "result_parser_version": "v2",
                    "selected_edit": self._last_field_resolution,
                    "selected_query_control": self._last_query_resolution,
                    **period_diagnostics,
                    **parsed.get("diagnostics", {}),
                },
            }
        if not parsed["found"]:
            return self._failure(
                "LOOKUP_RESULT_NOT_FOUND",
                "조회는 완료했지만 결과 항목을 해석하지 못했습니다. 진단 로그를 확인해 주세요.",
                {
                    "navigation_completed": True,
                    "order_input_verified": True,
                    "query_invoked": True,
                    "result_parser_version": "v2",
                    "raw_result_texts": after["raw_result_texts"],
                    "table_headers": after["table_headers"],
                    "table_rows": after["table_rows"],
                },
            )
        if progress_callback:
            progress_callback("LIST_RESULT_FOUND")
            progress_callback("SEARCH_RESULT_FOUND")
            if parsed["data"].get("dps_sales_number"):
                progress_callback("DPS_SALES_NUMBER_FOUND")
        detail_lookup = {
            "attempted": False,
            "opened": False,
            "parsed": False,
            "closed": False,
            "status": "NOT_ATTEMPTED",
            "invocation_count": 0,
        }
        merged_data = parsed["data"]
        detail_diagnostics: dict[str, Any] = {}
        final_status = (
            "RESULT_PARSE_PARTIAL"
            if parsed.get("diagnostics", {}).get("parse_warnings")
            else "RESULT_FOUND"
        )
        final_message = "DPS 조회와 결과 수집을 완료했습니다."
        if detail_window_provider is not None:
            detail_result = self.lookup_sales_detail(
                purchase_window=window,
                list_snapshot=after,
                list_data=parsed["data"],
                list_diagnostics=parsed.get("diagnostics", {}),
                expected_order_id=query_value,
                window_provider=detail_window_provider,
                url_reader=detail_url_reader,
                progress_callback=progress_callback,
            )
            detail_lookup = dict(detail_result.get("detail_lookup") or {})
            detail_diagnostics = dict(
                detail_result.get("diagnostics") or {}
            )
            merged_data = merge_list_and_detail(
                parsed["data"],
                detail_result.get("detail"),
                detail_lookup=detail_lookup,
            )
            if detail_lookup.get("parsed"):
                if merged_data.get("delivery_date_status") in {
                    "DATE_CONFLICT",
                    "MULTIPLE_DATES",
                }:
                    final_status = "DETAIL_DATE_CONFLICT"
                    final_message = (
                        "DPS 주문은 확인했지만 요구납기일 정보가 서로 달라 "
                        "확인이 필요합니다."
                    )
                elif detail_lookup.get("closed"):
                    final_status = "RESULT_FOUND_WITH_DETAIL"
                    final_message = "DPS 판매 상세정보를 확인했습니다."
                else:
                    final_status = "DETAIL_CLOSE_FAILED"
                    final_message = (
                        "DPS 판매 상세정보를 확인했지만 상세 창을 닫지 "
                        "못했습니다."
                    )
            else:
                final_status = "RESULT_FOUND_DETAIL_PARTIAL"
                final_message = (
                    "DPS 주문은 확인했지만 일부 상세정보를 읽지 못했습니다."
                )
        return {
            "ok": True,
            "success": True,
            "found": True,
            "code": "LOOKUP_COMPLETE",
            "status": final_status,
            "message": final_message,
            "data": merged_data,
            "detail_lookup": detail_lookup,
            "requested_delivery_date": merged_data.get(
                "requested_delivery_date"
            ),
            "required_delivery_date": merged_data.get(
                "required_delivery_date"
            ),
            "installation_date": merged_data.get("installation_date"),
            "installation_date_source": merged_data.get(
                "installation_date_source"
            ),
            "raw_required_delivery_date": merged_data.get(
                "raw_required_delivery_date"
            ),
            "date_parse_status": merged_data.get("date_parse_status"),
            "requires_human_review": merged_data.get(
                "requires_human_review", False
            ),
            "required_delivery_date_row_count": merged_data.get(
                "required_delivery_date_row_count", 0
            ),
            "delivery_scheduled_date": merged_data.get(
                "delivery_scheduled_date"
            ),
            "delivery_date_source": merged_data.get(
                "delivery_date_source"
            ),
            "delivery_date_status": merged_data.get(
                "delivery_date_status"
            ),
            "delivery_time": merged_data.get("delivery_time"),
            "buyer_name": merged_data.get("buyer_name"),
            "recipient_name": merged_data.get("recipient_name"),
            "recipient_phone": merged_data.get("recipient_phone"),
            "delivery_address": merged_data.get("delivery_address"),
            "delivery_note": merged_data.get("delivery_note"),
            "customer_number": merged_data.get("customer_number"),
            "order_amount": merged_data.get("order_amount"),
            "detail_items": merged_data.get("detail_items", []),
            "automation_method": "CHROME_TAB_UIA_V6",
            "raw_result_texts": after["raw_result_texts"],
            "table_headers": after["table_headers"],
            "table_rows": after["table_rows"],
            "diagnostics": {
                **input_diagnostics,
                "navigation_completed": True,
                "order_input_verified": True,
                "query_invoked": True,
                "query_invocation_count": 1,
                "query_execution_method": invocation_method,
                "result_parser_version": "v2",
                "selected_edit": self._last_field_resolution,
                "selected_query_control": self._last_query_resolution,
                **period_diagnostics,
                **parsed.get("diagnostics", {}),
                **detail_diagnostics,
            },
            **self._legacy_result_aliases(merged_data),
        }

    def find_dps_sales_link(
        self,
        window: Any,
        *,
        expected_order_id: str,
        dps_sales_number: str,
        matched_row_index: int | None,
    ) -> tuple[Any | None, dict[str, Any]]:
        """Resolve the one executable sales-number element in the matched row."""

        diagnostics: dict[str, Any] = {
            "matched_row_index": matched_row_index,
            "dps_sales_number": self._safe_log_text(dps_sales_number),
            "expected_order_id": self._safe_log_text(expected_order_id),
            "candidates": [],
        }
        if matched_row_index is None:
            diagnostics["reason"] = "RESULT_ROW_NOT_MATCHED"
            return None, diagnostics
        elements = self._all_descendants(window)
        headers = [
            element
            for element in elements
            if normalize_label(self._name(element))
            in {"DPS판매번호", "DPS 판매번호"}
            and self._safe_element_info_value(element, "control_type")
            in {"Header", "HeaderItem", "DataItem", "Text"}
        ]
        header_rects = [
            self._rectangle_record(element)
            for element in headers
            if self._rectangle_record(element)
        ]
        online_header_rects = [
            self._rectangle_record(element)
            for element in elements
            if normalize_label(self._name(element))
            == "온라인판매 주문번호"
            and self._safe_element_info_value(element, "control_type")
            in {"Header", "HeaderItem", "DataItem", "Text"}
            and self._rectangle_record(element)
        ]
        diagnostics["header_rect_count"] = len(header_rects)
        diagnostics["online_header_rect_count"] = len(
            online_header_rects
        )
        diagnostics["dps_column_control_counts"] = {}
        for element in elements:
            rect = self._rectangle_record(element)
            if not rect or not any(
                header["left"]
                <= (rect["left"] + rect["right"]) / 2
                <= header["right"]
                and rect["top"] >= header["bottom"]
                for header in header_rects
            ):
                continue
            control_type = str(
                self._safe_element_info_value(
                    element, "control_type"
                )
                or "UNKNOWN"
            )
            diagnostics["dps_column_control_counts"][control_type] = (
                diagnostics["dps_column_control_counts"].get(
                    control_type, 0
                )
                + 1
            )
        order_cells = [
            element
            for element in elements
            if normalize_label(self._name(element)) == expected_order_id
            and self._rectangle_record(element)
        ]
        if online_header_rects:
            order_cells = [
                element
                for element in order_cells
                if any(
                    header["left"]
                    <= (
                        (
                            self._rectangle_record(element) or {}
                        ).get("left", 0)
                        + (
                            self._rectangle_record(element) or {}
                        ).get("right", 0)
                    )
                    / 2
                    <= header["right"]
                    and (
                        self._rectangle_record(element) or {}
                    ).get("top", 0)
                    >= header["bottom"]
                    for header in online_header_rects
                )
            ]
        if not header_rects or not online_header_rects:
            diagnostics["reason"] = (
                "DPS_SALES_HEADER_NOT_FOUND"
            )
            return None, diagnostics
        order_rect: dict[str, Any] = {}
        if order_cells:
            order_row_centers = {
                int(
                    round(
                        (
                            (self._rectangle_record(value) or {}).get(
                                "top", 0
                            )
                            + (self._rectangle_record(value) or {}).get(
                                "bottom", 0
                            )
                        )
                        / 2
                        / 5.0
                    )
                    * 5
                )
                for value in order_cells
            }
            if len(order_row_centers) != 1:
                diagnostics["order_row_centers"] = sorted(order_row_centers)
                diagnostics["duplicate_order_cell_fallback"] = True
                order_cells = []
        if order_cells:
            order_cells.sort(
                key=lambda value: (
                    self._safe_element_info_value(value, "control_type")
                    == "DataItem",
                    (
                        (self._rectangle_record(value) or {}).get(
                            "right", 0
                        )
                        - (self._rectangle_record(value) or {}).get(
                            "left", 0
                        )
                    ),
                ),
                reverse=True,
            )
            order_rect = self._rectangle_record(order_cells[0]) or {}
            diagnostics["row_resolution"] = "online_order_cell"
        else:
            # Chromium sometimes exposes the parsed grid text but omits the
            # online-order cell from the executable UIA descendants. In that
            # case, use the already verified parsed row index plus the exact
            # DPS sales-number column. Ambiguity is still rejected below.
            sales_row_centers = sorted(
                {
                    int(
                        round(
                            (
                                (rect := self._rectangle_record(element))[
                                    "top"
                                ]
                                + rect["bottom"]
                            )
                            / 2
                            / 5.0
                        )
                        * 5
                    )
                    for element in elements
                    if normalize_label(self._name(element))
                    == dps_sales_number
                    and (rect := self._rectangle_record(element))
                    and any(
                        header["left"]
                        <= (rect["left"] + rect["right"]) / 2
                        <= header["right"]
                        and rect["top"] >= header["bottom"]
                        for header in header_rects
                    )
                }
            )
            spatial_row_fallback = False
            if not sales_row_centers:
                spatial_candidates = [
                    element
                    for element in elements
                    if (
                        rect := self._rectangle_record(element)
                    )
                    and self._safe_element_info_value(
                        element, "control_type"
                    )
                    in {
                        "Hyperlink",
                        "Button",
                        "Custom",
                        "DataItem",
                        "Text",
                    }
                    and (
                        self._invoke_available(element)
                        or callable(getattr(element, "click_input", None))
                    )
                    and any(
                        header["left"]
                        <= (rect["left"] + rect["right"]) / 2
                        <= header["right"]
                        and rect["top"] >= header["bottom"]
                        for header in header_rects
                    )
                ]
                sales_row_centers = sorted(
                    {
                        int(
                            round(
                                (
                                    (
                                        self._rectangle_record(element)
                                        or {}
                                    ).get("top", 0)
                                    + (
                                        self._rectangle_record(element)
                                        or {}
                                    ).get("bottom", 0)
                                )
                                / 2
                                / 5.0
                            )
                            * 5
                        )
                        for element in spatial_candidates
                    }
                )
                spatial_row_fallback = bool(sales_row_centers)
            if not (
                isinstance(matched_row_index, int)
                and 0 <= matched_row_index < len(sales_row_centers)
            ):
                diagnostics["reason"] = "RESULT_ROW_NOT_MATCHED"
                diagnostics["sales_row_count"] = len(sales_row_centers)
                return None, diagnostics
            target_mid = sales_row_centers[matched_row_index]
            order_rect = {
                "top": target_mid - 8,
                "bottom": target_mid + 8,
            }
            diagnostics["row_resolution"] = (
                "parsed_row_index_and_unnamed_sales_control"
                if spatial_row_fallback
                else "parsed_row_index_and_sales_column"
            )
            diagnostics["spatial_row_fallback"] = spatial_row_fallback
        row_mid = (
            int(order_rect.get("top", 0))
            + int(order_rect.get("bottom", 0))
        ) / 2

        raw_candidates: list[Any] = []
        for element in elements:
            exact_sales_name = (
                normalize_label(self._name(element)) == dps_sales_number
            )
            if not exact_sales_name and not diagnostics.get(
                "spatial_row_fallback"
            ):
                continue
            rect = self._rectangle_record(element)
            if not rect:
                continue
            mid_x = (rect["left"] + rect["right"]) / 2
            mid_y = (rect["top"] + rect["bottom"]) / 2
            in_column = any(
                header["left"] <= mid_x <= header["right"]
                for header in header_rects
            )
            same_row = (
                order_rect.get("top", 0) - 8
                <= mid_y
                <= order_rect.get("bottom", 0) + 8
            )
            if not (in_column and same_row):
                continue
            if (
                not exact_sales_name
                and self._safe_element_info_value(
                    element, "control_type"
                )
                not in {
                    "Hyperlink",
                    "Button",
                    "Custom",
                    "DataItem",
                    "Text",
                }
            ):
                continue
            candidate = element
            control_type = self._safe_element_info_value(
                candidate, "control_type"
            )
            if control_type == "Text":
                parent = self._safe_parent(candidate)
                if (
                    parent is not None
                    and normalize_label(self._name(parent))
                    == dps_sales_number
                    and self._safe_element_info_value(
                        parent, "control_type"
                    )
                    in {
                        "Hyperlink",
                        "Button",
                        "Custom",
                        "DataItem",
                    }
                ):
                    candidate = parent
                    control_type = self._safe_element_info_value(
                        candidate, "control_type"
                    )
            visible = getattr(candidate, "is_visible", None)
            enabled = getattr(candidate, "is_enabled", None)
            if callable(visible) and not visible():
                continue
            if callable(enabled) and not enabled():
                continue
            if not (
                self._invoke_available(candidate)
                or callable(getattr(candidate, "click_input", None))
            ):
                continue
            raw_candidates.append(candidate)
            diagnostics["candidates"].append(
                {
                    **self._diagnostic_identity(candidate),
                    "rectangle": self._rectangle_record(candidate),
                    "row_index": matched_row_index,
                    "column": "DPS판매번호",
                    "same_row": abs(mid_y - row_mid) <= 30,
                    "selection_reason": (
                        "exact sales number; header X range; matched order row"
                    ),
                }
            )

        unique: dict[Any, Any] = {
            self._element_identity_key(candidate): candidate
            for candidate in raw_candidates
        }
        candidates = list(unique.values())
        hyperlinks = [
            candidate
            for candidate in candidates
            if self._safe_element_info_value(candidate, "control_type")
            == "Hyperlink"
        ]
        if len(hyperlinks) == 1:
            diagnostics["selected_reason"] = (
                "single exact Hyperlink in matched row and DPS column"
            )
            return hyperlinks[0], diagnostics
        if len(candidates) == 1:
            diagnostics["selected_reason"] = (
                "single executable exact element in matched row and DPS column"
            )
            return candidates[0], diagnostics
        diagnostics["reason"] = "DPS_SALES_LINK_AMBIGUOUS"
        return None, diagnostics

    def _detail_markers(
        self,
        window: Any,
        *,
        url_reader: Callable[[Any], str] | None,
    ) -> tuple[list[str], str]:
        names = {
            normalize_label(self._name(element))
            for element in self._all_descendants(window)
        }
        hits = [
            marker
            for marker in DETAIL_MARKERS
            if any(marker in name for name in names)
        ]
        url = ""
        if url_reader is not None:
            try:
                url = str(url_reader(window) or "")
            except Exception:
                url = ""
        if "SSearchSalesMain" in url:
            hits.append("SSearchSalesMain")
        return list(dict.fromkeys(hits)), url

    @staticmethod
    def _window_handle(window: Any) -> int | None:
        try:
            return int(
                getattr(window, "handle", None)
                or window.element_info.handle
            )
        except Exception:
            return None

    def _detail_records(self, window: Any) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for element in self._all_descendants(window):
            control_type = self._safe_element_info_value(
                element, "control_type"
            )
            if control_type not in {
                "Text",
                "Edit",
                "Cell",
                "DataItem",
                "Header",
                "HeaderItem",
                "Button",
                "Hyperlink",
            }:
                continue
            name = self._response_safe_text(self._name(element))
            rect = self._rectangle_record(element)
            if not name or not rect:
                continue
            records.append(
                {
                    "name": name,
                    "control_type": control_type,
                    "automation_id": self._safe_element_info_value(
                        element, "automation_id"
                    ),
                    "class_name": self._safe_element_info_value(
                        element, "class_name"
                    ),
                    "left": rect["left"],
                    "top": rect["top"],
                    "right": rect["right"],
                    "bottom": rect["bottom"],
                    "parent": self._parent_name(element),
                    "row": self._safe_grid_property(
                        element, "current_row"
                    ),
                    "column": self._safe_grid_property(
                        element, "current_column"
                    ),
                    "invoke_available": self._invoke_available(element),
                }
            )
        return records

    def _detail_table(
        self,
        window: Any,
    ) -> tuple[list[str], list[list[str]]]:
        elements = self._all_descendants(window)
        possible_headers = [
            element
            for element in elements
            if (
                canonical_detail_label(self._name(element))
                or normalize_label(self._name(element))
            )
            in ITEM_FIELDS
            and self._safe_element_info_value(element, "control_type")
            in {"Header", "HeaderItem", "DataItem", "Text", "Cell"}
        ]
        grouped_headers: dict[int, list[Any]] = {}
        for element in possible_headers:
            rect = self._rectangle_record(element)
            if rect:
                key = int(round(rect["top"] / 5.0) * 5)
                grouped_headers.setdefault(key, []).append(element)
        header_elements = max(
            grouped_headers.values(),
            key=lambda values: len(
                {
                    canonical_detail_label(self._name(value))
                    or normalize_label(self._name(value))
                    for value in values
                }
            ),
            default=[],
        )
        if len(
            {
                canonical_detail_label(self._name(value))
                or normalize_label(self._name(value))
                for value in header_elements
            }
        ) < 3:
            return [], []
        header_elements.sort(
            key=lambda element: (
                (self._rectangle_record(element) or {}).get("left", 0)
            )
        )
        headers = [
            canonical_detail_label(self._name(value))
            or normalize_label(self._name(value))
            for value in header_elements
        ]
        header_top = min(
            (self._rectangle_record(value) or {}).get("top", 0)
            for value in header_elements
        )
        rows_by_y: dict[int, list[Any]] = {}
        for element in elements:
            control_type = self._safe_element_info_value(
                element, "control_type"
            )
            if control_type not in {
                "Text",
                "Cell",
                "DataItem",
                "Hyperlink",
            }:
                continue
            rect = self._rectangle_record(element)
            if not rect or rect["top"] <= header_top + 5:
                continue
            y_key = int(round(rect["top"] / 5.0) * 5)
            rows_by_y.setdefault(y_key, []).append(element)
        rows: list[list[str]] = []
        for row_elements in rows_by_y.values():
            row: list[str] = []
            for header in header_elements:
                header_rect = self._rectangle_record(header) or {}
                values = [
                    element
                    for element in row_elements
                    if header_rect.get("left", 0)
                    <= (
                        (
                            self._rectangle_record(element) or {}
                        ).get("left", 0)
                        + (
                            self._rectangle_record(element) or {}
                        ).get("right", 0)
                    )
                    / 2
                    <= header_rect.get("right", 0)
                ]
                row.append(
                    normalize_label(self._name(values[0]))
                    if len(values) == 1
                    else ""
                )
            if any(row) and any(
                re.search(r"\d", value) for value in row
            ):
                rows.append(row)
        return headers, rows[:100]

    def collect_sales_detail_snapshot(
        self,
        window: Any,
        *,
        url: str = "",
    ) -> dict[str, Any]:
        records = self._detail_records(window)
        headers, rows = self._detail_table(window)
        parsed = parse_flat_detail(
            records,
            table_headers=headers,
            table_rows=rows,
        )
        safe_label_names = {
            *DETAIL_MARKERS,
            *ITEM_FIELDS.keys(),
            "주문사유",
            "판매경로",
            "사업장",
            "판매장",
            "판매사원",
            "한도코드",
            "고객번호",
            "판매번호",
            "구매자",
            "전화번호",
            "인수자",
            "주소",
            "요구납기일",
            "배달시간",
            "배송정보",
            "주문금액",
            "입력금액",
            "차이금액",
            "닫기",
        }
        safe_records = []
        for record in records[:400]:
            safe_record = dict(record)
            raw_name = normalize_label(safe_record.get("name"))
            automation_id = str(
                safe_record.get("automation_id") or ""
            ).casefold()
            if "tel" in automation_id:
                safe_record["name"] = "<phone-part>"
                raw_name = "<phone-part>"
            canonical_name = canonical_detail_label(raw_name)
            if canonical_name:
                safe_record["name"] = canonical_name
                raw_name = canonical_name
            structural_label_logged = False
            if (
                safe_record.get("control_type") == "DataItem"
                and "rol_h" in str(safe_record.get("class_name") or "")
                and len(raw_name) <= 30
            ):
                # Structural form labels are safe to retain; value cells use
                # rol_c/tcontent classes and remain masked.
                safe_record["name"] = self._safe_log_text(raw_name)
                raw_name = str(safe_record["name"])
                structural_label_logged = True
            if (
                raw_name not in safe_label_names
                and not structural_label_logged
            ):
                if re.fullmatch(
                    r"\d{4}[-./]\d{1,2}[-./]\d{1,2}", raw_name
                ):
                    safe_record["name"] = raw_name
                elif re.fullmatch(
                    r"[A-Za-z0-9._/-]{1,30}", raw_name
                ):
                    safe_record["name"] = self._safe_log_text(raw_name)
                else:
                    safe_record["name"] = "<masked-value>"
            safe_record["parent"] = "<masked-parent>"
            safe_records.append(
                self._mask_diagnostic_record(safe_record)
            )
        self.detail_logger.info(
            "sales_detail title=%s url=%s elements=%s headers=%s rows=%s",
            self._safe_log_text(
                getattr(window, "window_text", lambda: "")()
            ),
            re.sub(r"([?&][^=]+)=([^&]+)", r"\1=<redacted>", url),
            json.dumps(safe_records, ensure_ascii=False, default=str),
            json.dumps(
                [self._safe_log_text(value) for value in headers],
                ensure_ascii=False,
            ),
            json.dumps(
                [
                    [self._safe_log_text(value) for value in row]
                    for row in rows
                ],
                ensure_ascii=False,
            ),
        )
        return {
            "parsed": parsed,
            "headers": headers,
            "rows": rows,
        }

    def close_sales_detail(
        self,
        detail_window: Any,
        *,
        purchase_window: Any,
        was_new_window: bool,
    ) -> tuple[bool, str]:
        close_candidates = []
        for control_type in ("Button", "Hyperlink"):
            for element in self._elements(detail_window, control_type):
                if normalize_label(self._name(element)) != "닫기":
                    continue
                identity = self._diagnostic_identity(element)
                class_name = str(identity.get("class_name") or "").casefold()
                automation_id = str(
                    identity.get("automation_id") or ""
                ).casefold()
                if (
                    "captionbutton" in class_name
                    or automation_id in {
                        "close",
                        "closebutton",
                        "view_4",
                    }
                ):
                    continue
                close_candidates.append(element)
        if len(close_candidates) == 1:
            try:
                close_candidates[0].invoke()
                return True, "invoke"
            except Exception:
                try:
                    close_candidates[0].click_input()
                    return True, "click_input"
                except Exception:
                    pass
        if was_new_window:
            try:
                detail_window.close()
                return True, "window.close"
            except Exception:
                pass
        return False, "DETAIL_CLOSE_FAILED"

    def lookup_sales_detail(
        self,
        *,
        purchase_window: Any,
        list_snapshot: dict[str, Any],
        list_data: dict[str, Any],
        list_diagnostics: dict[str, Any],
        expected_order_id: str,
        window_provider: Callable[[], list[Any]],
        url_reader: Callable[[Any], str] | None = None,
        timeout: float = 10.0,
        progress_callback: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        lookup = {
            "attempted": False,
            "opened": False,
            "parsed": False,
            "closed": False,
            "status": "DETAIL_PREFLIGHT_FAILED",
            "invocation_count": 0,
        }
        diagnostics: dict[str, Any] = {
            "detail_parse_warnings": [],
        }
        dps_sales_number = str(
            list_data.get("dps_sales_number") or ""
        ).strip()
        matched_row_index = list_diagnostics.get("matched_row_index")
        selected_rows = list_diagnostics.get("raw_rows") or []
        selected_row = (
            selected_rows[matched_row_index]
            if isinstance(matched_row_index, int)
            and 0 <= matched_row_index < len(selected_rows)
            else []
        )
        if (
            not dps_sales_number
            or expected_order_id not in selected_row
            or list_data.get("dps_query_value") != expected_order_id
        ):
            lookup["status"] = (
                "DPS_SALES_NUMBER_MISSING"
                if not dps_sales_number
                else "RESULT_ROW_NOT_MATCHED"
            )
            return {
                "detail": None,
                "detail_lookup": lookup,
                "diagnostics": diagnostics,
            }
        link, link_diagnostics = self.find_dps_sales_link(
            purchase_window,
            expected_order_id=expected_order_id,
            dps_sales_number=dps_sales_number,
            matched_row_index=matched_row_index,
        )
        diagnostics["detail_link"] = link_diagnostics
        if link is None:
            lookup["status"] = "DPS_SALES_LINK_NOT_FOUND"
            return {
                "detail": None,
                "detail_lookup": lookup,
                "diagnostics": diagnostics,
            }
        if self._detail_invocation_count >= 1:
            lookup["status"] = "DETAIL_ALREADY_ATTEMPTED"
            return {
                "detail": None,
                "detail_lookup": lookup,
                "diagnostics": diagnostics,
            }
        try:
            before_windows = list(window_provider())
        except Exception:
            before_windows = [purchase_window]
        before_handles = {
            self._window_handle(value) for value in before_windows
        }
        lookup["attempted"] = True
        self._detail_invocation_count += 1
        if progress_callback:
            progress_callback("DETAIL_LINK_OPENING")
            progress_callback("DPS_SALES_NUMBER_CLICK_STARTED")
        control_type = self._safe_element_info_value(
            link, "control_type"
        )
        try:
            if control_type == "Hyperlink" and callable(
                getattr(link, "click_input", None)
            ):
                link.click_input()
                lookup["open_method"] = "click_input"
            else:
                try:
                    link.invoke()
                    lookup["open_method"] = "invoke"
                except Exception:
                    link.click_input()
                    lookup["open_method"] = "click_input"
            lookup["invocation_count"] = 1
        except Exception as error:
            lookup["status"] = "DETAIL_OPEN_FAILED"
            diagnostics["detail_open_error"] = error.__class__.__name__
            return {
                "detail": None,
                "detail_lookup": lookup,
                "diagnostics": diagnostics,
            }

        deadline = time.monotonic() + max(1.0, timeout)
        detail_window = None
        detail_url = ""
        marker_hits: list[str] = []
        was_new_window = False
        alternate_retry_done = False
        while time.monotonic() < deadline:
            time.sleep(0.25)
            try:
                windows = list(window_provider())
            except Exception:
                windows = [purchase_window]
            new_windows = [
                value
                for value in windows
                if self._window_handle(value) not in before_handles
            ]
            # Only a newly-created popup or the verified DPS purchase window
            # can be the sales detail. Inspecting every pre-existing Chrome
            # window allowed the Streamlit Q&A window to be mistaken for DPS
            # when its diagnostics contained "고객정보/품목상세내역".
            candidates = [*new_windows, purchase_window]
            diagnostics["ignored_preexisting_window_count"] = max(
                0,
                len(windows) - len(new_windows) - 1,
            )
            seen: set[Any] = set()
            for candidate in candidates:
                key = (
                    self._window_handle(candidate),
                    id(candidate),
                )
                if key in seen:
                    continue
                seen.add(key)
                hits, url = self._detail_markers(
                    candidate, url_reader=url_reader
                )
                if len(hits) >= 2:
                    detail_window = candidate
                    detail_url = url
                    marker_hits = hits
                    was_new_window = (
                        self._window_handle(candidate)
                        not in before_handles
                    )
                    break
            if detail_window is not None:
                break
            # Chromium UIA may report invoke/click success even when a
            # JavaScript hyperlink did not receive the browser click.  Retry
            # only this already-verified sales link once; never rerun the
            # order search from the beginning.
            if (
                not alternate_retry_done
                and time.monotonic() >= deadline - max(1.0, timeout / 2)
            ):
                alternate_retry_done = True
                retry_link, retry_diagnostics = self.find_dps_sales_link(
                    purchase_window,
                    expected_order_id=expected_order_id,
                    dps_sales_number=dps_sales_number,
                    matched_row_index=matched_row_index,
                )
                diagnostics["detail_link_retry"] = retry_diagnostics
                if retry_link is not None:
                    try:
                        if lookup.get("open_method") == "click_input":
                            retry_link.invoke()
                            lookup["retry_open_method"] = "invoke"
                        else:
                            retry_link.click_input()
                            lookup["retry_open_method"] = "click_input"
                        lookup["invocation_count"] = 2
                        diagnostics["detail_retry_count"] = 1
                        if progress_callback:
                            progress_callback(
                                "DPS_SALES_NUMBER_CLICK_RETRY"
                            )
                    except Exception as retry_error:
                        diagnostics["detail_retry_error"] = (
                            retry_error.__class__.__name__
                        )
        if detail_window is None:
            lookup["status"] = "DETAIL_OPEN_TIMEOUT"
            diagnostics["detail_page_markers"] = marker_hits
            return {
                "detail": None,
                "detail_lookup": lookup,
                "diagnostics": diagnostics,
            }

        lookup.update(
            {
                "opened": True,
                "status": "DETAIL_OPENED",
                "detail_hwnd": self._window_handle(detail_window),
                "purchase_hwnd": self._window_handle(purchase_window),
                "window_form": (
                    "NEW_WINDOW" if was_new_window else "SAME_WINDOW_OR_MODAL"
                ),
            }
        )
        diagnostics.update(
            {
                "detail_page_markers": marker_hits,
                "detail_url_pattern": (
                    "SSearchSalesMain"
                    if "SSearchSalesMain" in detail_url
                    else None
                ),
            }
        )
        if progress_callback:
            progress_callback("DETAIL_OPENED")
            progress_callback("DPS_SALES_DETAIL_OPENED")
        detail: dict[str, Any] | None = None
        try:
            if progress_callback:
                progress_callback("DETAIL_PARSING")
            snapshot = self.collect_sales_detail_snapshot(
                detail_window, url=detail_url
            )
            detail = dict(snapshot["parsed"])
            detail_items = list(detail.get("detail_items") or [])
            if progress_callback and detail_items:
                progress_callback("ITEM_ROWS_FOUND")
            required_dates = [
                item.get("required_delivery_date")
                for item in detail_items
                if item.get("required_delivery_date")
            ]
            if progress_callback and required_dates:
                progress_callback("REQUIRED_DELIVERY_DATES_PARSED")
                if len(set(required_dates)) == 1:
                    progress_callback("INSTALLATION_DATE_SELECTED")
            lookup["parsed"] = bool(
                detail.get("customer_info")
                or detail.get("detail_items")
            )
            lookup["status"] = (
                "DETAIL_PARSED"
                if lookup["parsed"]
                else "DETAIL_PARSE_FAILED"
            )
            diagnostics.update(
                {
                    "detail_raw_headers": snapshot["headers"],
                    "detail_raw_rows": snapshot["rows"],
                    "detail_parse_warnings": detail.get(
                        "parse_warnings", []
                    ),
                }
            )
        except Exception as error:
            lookup["status"] = "DETAIL_PARSE_FAILED"
            diagnostics["detail_parse_warnings"] = [
                error.__class__.__name__
            ]
        finally:
            if progress_callback:
                progress_callback("DETAIL_CLOSING")
            try:
                closed, close_method = self.close_sales_detail(
                    detail_window,
                    purchase_window=purchase_window,
                    was_new_window=was_new_window,
                )
            except Exception as close_error:
                closed = False
                close_method = "DETAIL_CLOSE_EXCEPTION"
                diagnostics["detail_close_warning"] = (
                    close_error.__class__.__name__
                )
            lookup["closed"] = closed
            lookup["close_method"] = close_method
            if closed and lookup["parsed"]:
                lookup["status"] = "DETAIL_CLOSED"
            elif not closed and lookup["parsed"]:
                lookup["status"] = "DETAIL_CLOSE_FAILED"
        return {
            "detail": detail,
            "detail_lookup": lookup,
            "diagnostics": diagnostics,
        }

    def collect_result_snapshot(self, window: Any) -> dict[str, Any]:
        all_elements = self._all_descendants(window)
        result_roots = [
            element
            for element in all_elements
            if self._safe_element_info_value(element, "control_type")
            == "Document"
            and self._safe_element_info_value(element, "automation_id")
            == "RootWebArea"
            and (
                (self._rectangle_record(element) or {}).get("top", 0)
                >= 380
            )
            and (
                (self._rectangle_record(element) or {}).get("bottom", 0)
                > (self._rectangle_record(element) or {}).get("top", 0)
            )
        ]
        result_root_rect = (
            self._rectangle_record(result_roots[0])
            if result_roots
            else None
        )
        records: list[dict[str, Any]] = []
        raw_texts: list[str] = []
        seen_texts: set[str] = set()
        table_headers: list[str] = []
        table_rows: list[list[str]] = []
        for control_type in RESULT_CONTROL_TYPES:
            for element in self._elements(window, control_type):
                try:
                    is_visible = getattr(element, "is_visible", None)
                    if callable(is_visible) and not is_visible():
                        continue
                except Exception:
                    continue
                element_rect = self._rectangle_record(element)
                if result_root_rect is not None:
                    if (
                        element_rect is None
                        or element_rect["left"] < result_root_rect["left"]
                        or element_rect["top"] < result_root_rect["top"]
                        or element_rect["bottom"] > result_root_rect["bottom"]
                    ):
                        continue
                name = self._response_safe_text(self._name(element))
                if name and name not in seen_texts and len(raw_texts) < 240:
                    seen_texts.add(name)
                    raw_texts.append(name)
                record = {
                    **self._diagnostic_identity(element),
                    "parent_name": self._parent_name(element),
                    "rectangle": element_rect,
                    "row": self._safe_grid_property(element, "current_row"),
                    "column": self._safe_grid_property(element, "current_column"),
                    "row_span": self._safe_grid_property(
                        element,
                        "current_row_span",
                    ),
                    "column_span": self._safe_grid_property(
                        element,
                        "current_column_span",
                    ),
                }
                if len(records) < 600:
                    records.append(record)
                parent = self._safe_parent(element)
                parent_type = (
                    self._safe_element_info_value(parent, "control_type")
                    if parent is not None
                    else ""
                )
                parent_id = (
                    self._safe_element_info_value(parent, "automation_id")
                    if parent is not None
                    else ""
                )
                class_name = self._safe_element_info_value(
                    element, "class_name"
                ).casefold()
                if control_type == "DataItem" and "thead" in class_name:
                    if name and name not in table_headers:
                        table_headers.append(name)
                if (
                    control_type in {"DataItem", "ListItem", "Row"}
                    and parent_type in {"Table", "DataGrid", "List"}
                ):
                    values = self._row_values(element)
                    if parent_id == "tblSort":
                        for value in values:
                            if value and value not in table_headers:
                                table_headers.append(value)
                    elif values and values not in table_rows:
                        table_rows.append(values)
                if control_type in {"Header", "HeaderItem"} or (
                    control_type == "Text"
                    and self._looks_like_table_header(element)
                ):
                    if name and name not in table_headers:
                        table_headers.append(name)
        if table_rows and not table_headers:
            table_headers = self._infer_headers_from_texts(raw_texts)
        snapshot = {
            "raw_result_texts": raw_texts,
            "table_headers": table_headers[:40],
            "table_rows": table_rows[:100],
            "elements": records,
            "result_container_rectangle": result_root_rect,
            "container_counts": dict(
                Counter(
                    str(record.get("control_type") or "")
                    for record in records
                    if record.get("control_type")
                )
            ),
        }
        snapshot["fingerprint"] = repr(self._result_signature(snapshot))
        self.result_logger.info(
            "dps_result_snapshot elements=%s",
            json.dumps(
                [self._mask_diagnostic_record(value) for value in records],
                ensure_ascii=False,
                default=str,
            ),
        )
        self.result_logger.info(
            "dps_result_tables headers=%s rows=%s",
            json.dumps(
                [self._safe_log_text(value) for value in table_headers[:40]],
                ensure_ascii=False,
            ),
            json.dumps(
                [
                    [self._safe_log_text(value) for value in row]
                    for row in table_rows[:100]
                ],
                ensure_ascii=False,
            ),
        )
        return snapshot

    def wait_for_lookup_result(
        self,
        window: Any,
        before: dict[str, Any],
        *,
        timeout: float,
        expected_query_value: str | None = None,
    ) -> dict[str, Any]:
        before_signature = self._result_signature(before)
        deadline = time.monotonic() + max(1.0, timeout)
        latest = before
        stable_matching_polls = 0
        while time.monotonic() < deadline:
            time.sleep(0.35)
            latest = self.collect_result_snapshot(window)
            folded = "\n".join(latest["raw_result_texts"]).casefold()
            if any(marker.casefold() in folded for marker in NO_RESULT_MARKERS):
                return {"status": "no_result", "snapshot": latest}
            changed = self._result_signature(latest) != before_signature
            result_signal = bool(
                latest["table_rows"]
                or any(
                    hint.casefold() in folded
                    for hint in (
                        "조회결과",
                        "판매번호",
                        "설치일",
                        "배송일",
                        "배정",
                        "접수",
                        "상태",
                    )
                )
            )
            loading_text = any(
                marker in folded
                for marker in ("로딩", "loading", "처리중", "조회중")
            )
            if changed and result_signal and not loading_text:
                return {"status": "complete", "snapshot": latest}
            exact_row_present = bool(
                expected_query_value
                and any(
                    expected_query_value
                    in [str(value).strip() for value in row]
                    for row in latest["table_rows"]
                )
            )
            if exact_row_present and result_signal and not loading_text:
                stable_matching_polls += 1
                # A force refresh can legitimately return the same row and
                # fingerprint. Two post-invoke stable polls distinguish that
                # from the pre-invoke snapshot without clicking 조회 again.
                if stable_matching_polls >= 2:
                    return {"status": "complete", "snapshot": latest}
            else:
                stable_matching_polls = 0
        return {"status": "timeout", "snapshot": latest}

    def parse_lookup_result(
        self,
        snapshot: dict[str, Any],
        *,
        naver_order_id: str | None = None,
        order_id: str | None = None,
        product_order_id: str | None = None,
        dps_query_value: str | None = None,
        dps_query_value_type: str | None = None,
        query_fallback_used: bool = False,
        dps_date_source: str | None = None,
        dps_reference_date: str | None = None,
        dps_period_start: str | None = None,
        dps_period_end: str | None = None,
    ) -> dict[str, Any]:
        query_value = str(dps_query_value or naver_order_id or "").strip()
        raw_snapshot_texts = [
            " ".join(str(value).split())
            for value in snapshot.get("raw_result_texts", [])
            if str(value).strip()
        ]
        folded_snapshot = "\n".join(raw_snapshot_texts).casefold()
        if any(marker.casefold() in folded_snapshot for marker in NO_RESULT_MARKERS):
            queried_at = datetime.now().astimezone().isoformat(timespec="seconds")
            data = {
                "naver_order_id": query_value,
                "order_id": order_id,
                "product_order_id": product_order_id,
                "dps_query_value": query_value,
                "dps_query_value_type": dps_query_value_type or "order_id",
                "query_fallback_used": bool(query_fallback_used),
                "dps_date_source": dps_date_source,
                "dps_reference_date": dps_reference_date,
                "dps_period_start": dps_period_start,
                "dps_period_end": dps_period_end,
                "dps_sales_number": None,
                "dps_order_number": None,
                "product_name": None,
                "model_name": None,
                "quantity": None,
                "installation_scheduled_date": None,
                "delivery_scheduled_date": None,
                "installation_completed_date": None,
                "engineer_assignment_status": None,
                "progress_status": None,
                "reception_status": None,
                "requested_date": None,
                "online_order_created_date": None,
                "sales_amount": None,
                "buyer": None,
                "recipient": None,
                "queried_at": queried_at,
                "installation_date": None,
                "delivery_date": None,
                "assignment_status": None,
                "delivery_status": None,
                "installation_status": None,
                "receipt_status": None,
                "lookup_at": queried_at,
            }
            return {
                "found": False,
                "data": data,
                "diagnostics": {
                    "raw_headers": list(snapshot.get("table_headers", [])),
                    "raw_rows": list(snapshot.get("table_rows", [])),
                    "matched_row_index": None,
                    "parse_warnings": [],
                    "query_value_type": dps_query_value_type or "order_id",
                    "query_fallback_used": bool(query_fallback_used),
                    "dps_date_source": dps_date_source,
                    "dps_reference_date": dps_reference_date,
                    "dps_period_start": dps_period_start,
                    "dps_period_end": dps_period_end,
                    "raw_date_values": {
                        "installation_scheduled_date": None,
                        "delivery_scheduled_date": None,
                        "installation_completed_date": None,
                    },
                },
            }

        def normalize_header(value: Any) -> str:
            text = " ".join(str(value or "").replace("\n", " ").split())
            text = text.rstrip(":：").strip()
            return re.sub(r"[\[\]＊*※]", "", text)

        raw_headers = [
            " ".join(str(value).split())
            for value in snapshot.get("table_headers", [])
            if str(value).strip()
        ]
        headers = [normalize_header(value) for value in raw_headers]
        rows = [
            [" ".join(str(value).split()) for value in row]
            for row in snapshot.get("table_rows", [])
            if isinstance(row, list)
        ]
        selected_row: list[str] | None = None
        online_header = normalize_header("온라인판매 주문번호")
        online_header_index = (
            headers.index(online_header)
            if online_header in headers
            else None
        )
        matching_indices: list[int] = []
        row_match_basis = "none"
        for index, row in enumerate(rows):
            if not query_value:
                continue
            if (
                online_header_index is not None
                and len(row) == len(headers)
                and row[online_header_index].strip() == query_value
            ):
                matching_indices.append(index)
                row_match_basis = "online_sales_order_column"
            elif (
                len(row) != len(headers)
                and row.count(query_value) == 1
            ):
                # Chromium omits empty cells. The exact identifier is used only
                # as the anchor for reconstructing the named online-order column.
                matching_indices.append(index)
                row_match_basis = "sparse_online_sales_order_anchor"
            elif (
                dps_query_value_type != "order_id"
                and row.count(query_value) == 1
            ):
                # Parser-only compatibility for historical fixtures. The
                # executable lookup path rejects every non-order_id request.
                matching_indices.append(index)
                row_match_basis = "legacy_parser_only"
        matching_rows = [rows[index] for index in matching_indices]
        matched_row_index: int | None = None
        if len(matching_rows) == 1:
            selected_row = matching_rows[0]
            matched_row_index = matching_indices[0]

        pairs: dict[str, str] = {}
        if selected_row and headers and len(headers) == len(selected_row):
            pairs.update(dict(zip(headers, selected_row)))
        elif selected_row and headers:
            # DPS omits empty td values from UIA while still exposing every
            # header. Anchor the stable left-hand columns on the exact online
            # sales order number, then map strongly typed trailing columns.
            normalized_online_order = normalize_header(
                "온라인판매 주문번호"
            )
            try:
                header_anchor = headers.index(normalized_online_order)
                row_anchor = selected_row.index(query_value)
            except ValueError:
                header_anchor = -1
                row_anchor = -1
            if header_anchor >= 0 and row_anchor >= 0:
                stable_right_headers = {
                    normalize_header(value)
                    for value in (
                        "온라인판매 주문번호",
                        "모델명",
                        "건수",
                        "수량",
                        "판매금액",
                        "구매자",
                    )
                }
                for header_index, header in enumerate(headers):
                    row_index = row_anchor + header_index - header_anchor
                    if (
                        0 <= row_index < len(selected_row)
                        and (
                            header_index <= header_anchor
                            or header in stable_right_headers
                        )
                    ):
                        pairs[header] = selected_row[row_index]

                long_numbers = [
                    value
                    for index, value in enumerate(selected_row)
                    if index > row_anchor
                    and re.fullmatch(r"\d{8,20}", value)
                    and value != query_value
                ]
                if len(long_numbers) >= 2:
                    pairs[normalize_header("DPS판매번호")] = long_numbers[-2]
                    pairs[normalize_header("전자주문번호")] = long_numbers[-1]

                status_header = normalize_header("상태")
                if status_header in headers:
                    trailing_value = selected_row[-1]
                    if (
                        trailing_value not in long_numbers
                        and not re.fullmatch(
                            r"\d{4}[-./]\d{1,2}[-./]\d{1,2}",
                            trailing_value,
                        )
                    ):
                        pairs[status_header] = trailing_value
        texts = [
            " ".join(str(value).split())
            for value in snapshot.get("raw_result_texts", [])
            if str(value).strip()
        ]
        aliases = {
            "order_id": (
                "네이버 주문번호",
                "주문번호",
                "일반 주문번호",
                "온라인판매 주문번호",
                "온라인 주문번호",
                "외부 주문번호",
            ),
            "product_order_id": (
                "상품주문번호",
                "상품 주문번호",
            ),
            "dps_sales_number": ("DPS 판매번호", "판매번호", "DPS판매번호"),
            "dps_order_number": (
                "DPS 주문번호", "전자주문번호", "전자 주문번호", "주문 번호",
            ),
            "product_name": ("상품명", "제품명", "품명"),
            "model_name": ("모델", "모델명", "제품 모델"),
            "quantity": ("수량", "주문수량", "건수"),
            "requested_date": ("희망일", "요청일", "희망 일자"),
            "online_order_created_date": (
                "온라인판매 주문생성일",
                "온라인판매 주문 생성일",
            ),
            "sales_amount": ("판매금액", "판매 금액"),
            "buyer": ("구매자",),
            "recipient": ("인수자",),
            "installation_scheduled_date": (
                "설치예정일", "설치 예정일", "설치일", "설치요청일",
            ),
            "delivery_scheduled_date": (
                "배송예정일", "배송 예정일", "배송일", "출고예정일",
            ),
            "installation_completed_date": (
                "설치완료일", "설치 완료일", "완료일",
            ),
            "engineer_assignment_status": (
                "기사배정", "기사 배정", "기사배정상태", "배정상태",
            ),
            "progress_status": (
                "진행상태", "배송상태", "설치상태",
                "배송/설치 상태", "처리상태",
            ),
            "reception_status": ("접수상태", "주문상태", "상태"),
        }
        known_labels = {
            normalize_header(label)
            for values in aliases.values()
            for label in values
        }
        for index, text in enumerate(texts[:-1]):
            normalized = normalize_header(text)
            next_normalized = normalize_header(texts[index + 1])
            if (
                normalized in known_labels
                and normalized not in pairs
                and next_normalized not in known_labels
            ):
                pairs[normalized] = texts[index + 1]

        def pick(field: str) -> str | None:
            for label in aliases[field]:
                value = pairs.get(normalize_header(label))
                if value and value != label:
                    return value
            return None

        quantity_text = pick("quantity")
        quantity = None
        if quantity_text:
            match = re.search(r"\d+", quantity_text.replace(",", ""))
            quantity = int(match.group()) if match else None
        parsed_order_id = pick("order_id")
        parsed_product_order_id = pick("product_order_id")
        parsed_product_order_id = parsed_product_order_id or product_order_id
        parsed_order_id = parsed_order_id or order_id
        queried_at = datetime.now().astimezone().isoformat(timespec="seconds")
        data = {
            "naver_order_id": query_value,
            "order_id": parsed_order_id or order_id,
            "product_order_id": parsed_product_order_id or product_order_id,
            "dps_query_value": query_value,
            "dps_query_value_type": dps_query_value_type or "order_id",
            "query_fallback_used": bool(query_fallback_used),
            "dps_date_source": dps_date_source,
            "dps_reference_date": dps_reference_date,
            "dps_period_start": dps_period_start,
            "dps_period_end": dps_period_end,
            "dps_sales_number": pick("dps_sales_number"),
            "dps_order_number": pick("dps_order_number"),
            "electronic_order_number": pick("dps_order_number"),
            "product_name": pick("product_name"),
            "model_name": pick("model_name"),
            "quantity": quantity,
            "requested_date": pick("requested_date"),
            "online_order_created_date": pick(
                "online_order_created_date"
            ),
            "sales_amount": pick("sales_amount"),
            "sale_amount": pick("sales_amount"),
            "buyer": pick("buyer"),
            "recipient": pick("recipient"),
            "installation_scheduled_date": pick("installation_scheduled_date"),
            "delivery_scheduled_date": pick("delivery_scheduled_date"),
            "installation_completed_date": pick("installation_completed_date"),
            "engineer_assignment_status": pick("engineer_assignment_status"),
            "progress_status": pick("progress_status"),
            "reception_status": pick("reception_status"),
            "queried_at": queried_at,
            "online_order_id": parsed_order_id or order_id or query_value,
        }
        data.update(
            {
                "installation_date": data["installation_scheduled_date"],
                "delivery_date": data["delivery_scheduled_date"],
                "assignment_status": data["engineer_assignment_status"],
                "delivery_status": data["progress_status"],
                "installation_status": data["progress_status"],
                "receipt_status": data["reception_status"],
                "lookup_at": queried_at,
            }
        )
        if not data["progress_status"] and data["reception_status"]:
            data["progress_status"] = data["reception_status"]
            data["delivery_status"] = data["reception_status"]
            data["installation_status"] = data["reception_status"]
        found = bool(
            selected_row
            or any(
                data[key]
                for key in (
                    "dps_sales_number",
                    "dps_order_number",
                    "product_name",
                    "model_name",
                    "installation_scheduled_date",
                    "delivery_scheduled_date",
                    "engineer_assignment_status",
                    "progress_status",
                )
            )
        )
        warnings: list[str] = []
        if rows and len(matching_rows) > 1:
            warnings.append("query_value에 일치하는 결과 행이 여러 개입니다.")
            found = False
        if selected_row and headers and len(headers) != len(selected_row):
            warnings.append("헤더 수와 결과 열 수가 일치하지 않습니다.")
        if found and not any(
            data.get(field)
            for field in (
                "dps_sales_number", "dps_order_number", "product_name",
                "model_name", "installation_scheduled_date",
                "delivery_scheduled_date", "progress_status",
            )
        ):
            warnings.append("확정 가능한 DPS 결과 헤더가 부족합니다.")
        return {
            "found": found,
            "data": data,
            "diagnostics": {
                "raw_headers": raw_headers,
                "raw_rows": rows,
                "matched_row_index": matched_row_index,
                "matched_row": list(selected_row or []),
                "row_match_basis": row_match_basis,
                "parse_warnings": warnings,
                "query_value_type": dps_query_value_type or "order_id",
                "query_fallback_used": bool(query_fallback_used),
                "dps_date_source": dps_date_source,
                "dps_reference_date": dps_reference_date,
                "dps_period_start": dps_period_start,
                "dps_period_end": dps_period_end,
                "raw_date_values": {
                    key: data.get(key)
                    for key in (
                        "installation_scheduled_date",
                        "delivery_scheduled_date",
                        "installation_completed_date",
                    )
                },
            },
        }

    @staticmethod
    def _legacy_result_aliases(data: dict[str, Any]) -> dict[str, Any]:
        assignment = str(data.get("assignment_status") or "")
        return {
            "source_order_number": data.get("naver_order_id"),
            "dps_sale_number": data.get("dps_sales_number"),
            "electronic_order_number": data.get("dps_order_number"),
            "model_name": data.get("model_name"),
            "installation_status": data.get("installation_status"),
            "scheduled_date": data.get("installation_date"),
            "completed_date": data.get("installation_completed_date"),
            "technician_assigned": (
                True
                if "배정" in assignment and "미배정" not in assignment
                else False
                if "미배정" in assignment
                else None
            ),
            "queried_at": data.get("lookup_at"),
        }

    @staticmethod
    def _result_signature(snapshot: dict[str, Any]) -> tuple[Any, ...]:
        texts = [
            value
            for value in snapshot.get("raw_result_texts", [])
            if not re.fullmatch(r"\d{1,2}:\d{2}", str(value))
        ]
        return (
            tuple(texts),
            tuple(
                tuple(row)
                for row in snapshot.get("table_rows", [])
                if isinstance(row, list)
            ),
        )

    def _row_values(self, element: Any) -> list[str]:
        try:
            children = list(element.children())
        except Exception:
            children = []
        values: list[str] = []
        for child in children:
            control_type = self._safe_element_info_value(child, "control_type")
            if control_type not in {
                "Text",
                "Cell",
                "DataItem",
                "ListItem",
                "Hyperlink",
            }:
                continue
            value = self._response_safe_text(self._name(child))
            # Preserve duplicate and empty cells. DPS exposes all 16 columns,
            # and dropping an empty/duplicate td shifts every following value
            # under the wrong header.
            values.append(value)
        if not values or not any(values):
            value = self._response_safe_text(self._name(element))
            if value:
                values = [value]
        return values

    def _looks_like_table_header(self, element: Any) -> bool:
        parent = self._safe_parent(element)
        searchable = " ".join(
            (
                self._safe_element_info_value(element, "class_name"),
                self._safe_element_info_value(element, "automation_id"),
                self._safe_element_info_value(parent, "class_name")
                if parent is not None
                else "",
                self._safe_element_info_value(parent, "control_type")
                if parent is not None
                else "",
            )
        ).casefold()
        return any(
            hint in searchable
            for hint in ("header", "thead", "columnheader", "grid")
        )

    @staticmethod
    def _infer_headers_from_texts(texts: list[str]) -> list[str]:
        known = (
            "판매번호",
            "주문번호",
            "상품명",
            "모델명",
            "수량",
            "설치예정일",
            "배송예정일",
            "설치상태",
            "진행상태",
        )
        return [value for value in texts if value in known]

    @staticmethod
    def _safe_grid_property(element: Any, attribute: str) -> int | None:
        try:
            value = getattr(element.iface_grid_item, attribute)
            return int(value)
        except Exception:
            return None

    @staticmethod
    def _response_safe_text(value: Any) -> str:
        text = " ".join(str(value or "").split())[:300]
        text = re.sub(
            r"(?<!\d)(01[016789])[-\s]?(\d{3,4})[-\s]?(\d{4})(?!\d)",
            r"\1-****-\3",
            text,
        )
        return text

    @staticmethod
    def _safe_log_text(value: Any) -> str:
        text = DpsUiAutomation._response_safe_text(value)

        def mask(match: re.Match[str]) -> str:
            number = match.group(0)
            if len(number) <= 8:
                return "*" * len(number)
            return f"{number[:4]}****{number[-4:]}"

        return re.sub(r"\d{6,}", mask, text)

    def _mask_diagnostic_record(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: self._mask_diagnostic_record(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._mask_diagnostic_record(item) for item in value]
        if isinstance(value, str):
            return self._safe_log_text(value)
        return value

    def wait_for_result_change(
        self,
        window: Any,
        before: list[str],
        *,
        timeout: float,
    ) -> tuple[list[str], bool]:
        """이전 테스트/호출부 호환용 단순 텍스트 변화 감지입니다."""

        before_set = set(before)
        deadline = time.monotonic() + max(1.0, timeout)
        latest = before
        while time.monotonic() < deadline:
            time.sleep(0.35)
            latest = self.visible_texts(window)
            additions = [text for text in latest if text not in before_set]
            if additions or len(latest) != len(before):
                return latest, True
        return latest, False

    @staticmethod
    def mask_order_number(order_number: str) -> str:
        value = str(order_number or "")
        if len(value) <= 8:
            return "*" * len(value)
        return f"{value[:8]}{'*' * max(4, len(value) - 12)}{value[-4:]}"

    @staticmethod
    def _replace_edit_value(
        edit: Any,
        value: str,
        validate_target: Callable[[], tuple[bool, dict[str, Any]]],
    ) -> None:
        """키보드 이벤트 없이 UIA ValuePattern으로만 기존 값을 교체합니다."""

        DpsUiAutomation._set_uia_edit_value(edit, "")
        still_safe, checks = validate_target()
        if not still_safe:
            raise InputSafetyError(checks)
        DpsUiAutomation._set_uia_edit_value(edit, value)

    @staticmethod
    def _set_uia_edit_value(edit: Any, value: str) -> None:
        first_error: Exception | None = None
        setter = getattr(edit, "set_edit_text", None)
        if callable(setter):
            try:
                setter(str(value))
                return
            except Exception as error:
                first_error = error
        try:
            value_pattern = edit.iface_value
            pattern_setter = getattr(value_pattern, "set_value", None)
            if not callable(pattern_setter):
                pattern_setter = getattr(value_pattern, "SetValue")
            pattern_setter(str(value))
        except Exception:
            if first_error is not None:
                raise first_error
            raise

    def _exact_named_elements(
        self,
        window: Any,
        *,
        name: str,
        control_types: tuple[str, ...],
    ) -> list[Any]:
        matches: list[Any] = []
        seen: set[Any] = set()
        for control_type in control_types:
            for element in self._elements(window, control_type):
                try:
                    is_visible = getattr(element, "is_visible", None)
                    if callable(is_visible) and not is_visible():
                        continue
                except Exception:
                    continue
                element_key = self._element_identity_key(element)
                if element_key in seen:
                    continue
                seen.add(element_key)
                if " ".join(self._name(element).split()) == name:
                    matches.append(element)
        self.navigation_logger.info(
            "exact_navigation_candidates name=%s control_types=%s matches=%s",
            name,
            list(control_types),
            json.dumps(
                [self._diagnostic_identity(item) for item in matches],
                ensure_ascii=False,
            ),
        )
        return matches

    def _wait_for_exact_named_elements(
        self,
        window: Any,
        *,
        name: str,
        control_types: tuple[str, ...],
    ) -> list[Any]:
        matches: list[Any] = []
        for delay in (0.0, 0.1, 0.3, 0.7, 1.5):
            if delay:
                time.sleep(delay)
            matches = self._exact_named_elements(
                window,
                name=name,
                control_types=control_types,
            )
            if len(matches) == 1:
                break
        return matches

    def _wait_for_left_menu_candidate(
        self,
        window: Any,
        *,
        name: str,
        control_types: tuple[str, ...],
        role: str,
    ) -> Any | None:
        candidate = None
        for delay in (0.0, 0.1, 0.3, 0.7, 1.5):
            if delay:
                time.sleep(delay)
            # Always query descendants again: clicking 판매 can replace the
            # document and invalidate every wrapper from the previous UIA tree.
            matches = self._exact_named_elements(
                window,
                name=name,
                control_types=control_types,
            )
            anchor = None
            if role == "purchase_request":
                online_matches = self._exact_named_elements(
                    window,
                    name=ONLINE_SALES_MENU_NAME,
                    control_types=MENU_CLICK_CONTROL_TYPES,
                )
                anchor = self._resolve_left_menu_candidate(
                    online_matches,
                    role="online_sales",
                )
            candidate = self._resolve_left_menu_candidate(
                matches,
                role=role,
                anchor=anchor,
            )
            if candidate is not None:
                break
        return candidate

    def _selected_sales_candidates(self, candidates: list[Any]) -> list[Any]:
        return [
            candidate
            for candidate in candidates
            if self._is_navigation_element_selected(candidate)
        ]

    def _resolve_sales_top_menu(
        self,
        candidates: list[Any],
        *,
        require_selected: bool = False,
        clicked_identity: dict[str, Any] | None = None,
    ) -> Any | None:
        selected = self._selected_sales_candidates(candidates)
        # More than one selected exact match is inherently unsafe. Do not use
        # geometry or RuntimeId to guess between two selected navigation links.
        if len(selected) > 1:
            return None
        if require_selected:
            if len(selected) != 1 or self._is_breadcrumb_candidate(selected[0]):
                return None
            return selected[0]
        if len(selected) == 1 and not self._is_breadcrumb_candidate(selected[0]):
            return selected[0]

        ranked: list[tuple[int, Any]] = []
        for candidate in candidates:
            if self._is_breadcrumb_candidate(candidate):
                continue
            score = self._sales_top_menu_score(
                candidate,
                clicked_identity=clicked_identity,
            )
            ranked.append((score, candidate))
        if not ranked:
            return None
        ranked.sort(key=lambda item: item[0], reverse=True)
        if len(ranked) == 1:
            return ranked[0][1]
        if ranked[0][0] <= ranked[1][0]:
            return None
        return ranked[0][1]

    def _sales_top_menu_score(
        self,
        element: Any,
        *,
        clicked_identity: dict[str, Any] | None,
    ) -> int:
        score = 0
        ancestry = self._ancestor_records(element)
        searchable = " ".join(
            str(value).casefold()
            for record in [self._structural_identity(element), *ancestry]
            for value in (
                record.get("name"),
                record.get("class_name"),
                record.get("automation_id"),
            )
            if value
        )
        if any(hint in searchable for hint in GLOBAL_MENU_HINTS):
            score += 50
        sibling_names = self._sibling_names(element)
        if len((sibling_names | {SALES_TOP_MENU_NAME}) & TOP_MENU_SIBLING_NAMES) >= 3:
            score += 45
        elif len(sibling_names & TOP_MENU_SIBLING_NAMES) >= 1:
            score += 20
        rect = self._rectangle_record(element)
        if rect is not None and rect["top"] < 300:
            score += 15
        if clicked_identity is not None:
            score += self._structural_match_score(
                self._structural_identity(element),
                clicked_identity,
            )
        return score

    def _resolve_left_menu_candidate(
        self,
        candidates: list[Any],
        *,
        role: str,
        anchor: Any | None = None,
    ) -> Any | None:
        ranked: list[tuple[int, Any]] = []
        for candidate in candidates:
            if self._is_breadcrumb_candidate(candidate):
                continue
            score = self._left_menu_score(candidate, anchor=anchor)
            ranked.append((score, candidate))
        if not ranked:
            return None
        ranked.sort(key=lambda item: item[0], reverse=True)
        if len(ranked) == 1:
            return ranked[0][1]
        # With duplicates, require a strictly better structural/left-region
        # candidate. Equal evidence is ambiguous and must not be clicked.
        if ranked[0][0] <= ranked[1][0]:
            return None
        return ranked[0][1]

    def _left_purchase_request_targets(
        self,
        candidates: list[Any],
        *,
        online_sales_menu: Any | None,
    ) -> list[Any]:
        target = self._resolve_left_menu_candidate(
            candidates,
            role="purchase_request",
            anchor=online_sales_menu,
        )
        return [target] if target is not None else []

    def _left_menu_score(self, element: Any, *, anchor: Any | None) -> int:
        score = 0
        records = [self._structural_identity(element), *self._ancestor_records(element)]
        searchable = " ".join(
            str(value).casefold()
            for record in records
            for value in (
                record.get("name"),
                record.get("class_name"),
                record.get("automation_id"),
            )
            if value
        )
        if any(hint in searchable for hint in LEFT_MENU_HINTS):
            score += 50
        rect = self._rectangle_record(element)
        if rect is not None:
            if rect["left"] < 650:
                score += 20
            elif rect["left"] > 900:
                score -= 10
        if anchor is not None:
            if self._is_descendant_of(element, anchor):
                score += 70
            elif self._nearest_shared_ancestor_distance(element, anchor) <= 4:
                score += 35
        return score

    def _is_breadcrumb_candidate(self, element: Any) -> bool:
        # Breadcrumb evidence must be local to the candidate.  Chrome UIA can
        # expose the whole page title/text on distant Document ancestors; the
        # title often contains a "location" breadcrumb and previously caused
        # every 판매/온라인판매 link on the page to be rejected.
        records = [
            self._structural_identity(element),
            *self._ancestor_records(element, limit=3),
        ]
        searchable = " ".join(
            str(value).casefold()
            for record in records
            for value in (
                record.get("name"),
                record.get("class_name"),
                record.get("automation_id"),
            )
            if value
        )
        return any(hint in searchable for hint in BREADCRUMB_HINTS)

    def _structural_identity(self, element: Any) -> dict[str, Any]:
        parent = self._safe_parent(element)
        return {
            "name": " ".join(self._name(element).split()),
            "control_type": self._safe_element_info_value(element, "control_type"),
            "class_name": self._safe_element_info_value(element, "class_name"),
            "automation_id": self._safe_element_info_value(
                element, "automation_id"
            ),
            "parent_name": self._name(parent) if parent is not None else "",
            "parent_control_type": self._safe_element_info_value(
                parent, "control_type"
            ) if parent is not None else "",
            "parent_class_name": self._safe_element_info_value(
                parent, "class_name"
            ) if parent is not None else "",
            "parent_automation_id": self._safe_element_info_value(
                parent, "automation_id"
            ) if parent is not None else "",
            "rectangle": self._rectangle_record(element),
            # RuntimeId is diagnostic/supplementary only. Stable fields above
            # remain the primary identity across document replacements.
            "runtime_id": self._safe_uia_sequence(element, "runtime_id"),
        }

    @staticmethod
    def _structural_match_score(
        current: dict[str, Any],
        reference: dict[str, Any],
    ) -> int:
        score = 0
        for field, weight in (
            ("name", 8),
            ("control_type", 8),
            ("parent_name", 12),
            ("parent_control_type", 8),
            ("parent_class_name", 12),
            ("parent_automation_id", 12),
        ):
            value = current.get(field)
            if value and value == reference.get(field):
                score += weight
        current_rect = current.get("rectangle")
        reference_rect = reference.get("rectangle")
        if current_rect and reference_rect:
            if abs(current_rect["left"] - reference_rect["left"]) <= 80:
                score += 6
            if abs(current_rect["top"] - reference_rect["top"]) <= 80:
                score += 6
        if (
            current.get("runtime_id")
            and current.get("runtime_id") == reference.get("runtime_id")
        ):
            score += 2
        return score

    def _ancestor_records(self, element: Any, *, limit: int = 8) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        current = element
        seen: set[Any] = set()
        for _ in range(limit):
            current = self._safe_parent(current)
            if current is None:
                break
            key = self._element_identity_key(current)
            if key in seen:
                break
            seen.add(key)
            records.append(
                {
                    "name": self._name(current),
                    "control_type": self._safe_element_info_value(
                        current, "control_type"
                    ),
                    "class_name": self._safe_element_info_value(
                        current, "class_name"
                    ),
                    "automation_id": self._safe_element_info_value(
                        current, "automation_id"
                    ),
                    "identity": key,
                }
            )
        return records

    @staticmethod
    def _safe_parent(element: Any) -> Any | None:
        try:
            parent = getattr(element, "parent", None)
            return parent() if callable(parent) else None
        except Exception:
            return None

    def _same_element(self, first: Any | None, second: Any | None) -> bool:
        if first is None or second is None:
            return False
        return self._element_identity_key(first) == self._element_identity_key(second)

    def _sibling_names(self, element: Any) -> set[str]:
        parent = self._safe_parent(element)
        if parent is None:
            return set()
        try:
            children = self._immediate_navigation_children(
                parent,
                context="navigation_candidate_siblings",
            )
        except Exception:
            return set()
        return {" ".join(self._name(child).split()) for child in children}

    @staticmethod
    def _rectangle_record(element: Any) -> dict[str, int] | None:
        try:
            rect = element.rectangle()
            return {
                "left": int(rect.left),
                "top": int(rect.top),
                "right": int(rect.right),
                "bottom": int(rect.bottom),
            }
        except Exception:
            return None

    def _is_descendant_of(self, element: Any, ancestor: Any) -> bool:
        ancestor_key = self._element_identity_key(ancestor)
        return any(
            record.get("identity") == ancestor_key
            for record in self._ancestor_records(element)
        )

    def _nearest_shared_ancestor_distance(self, first: Any, second: Any) -> int:
        first_records = self._ancestor_records(first)
        second_distances = {
            record.get("identity"): index + 1
            for index, record in enumerate(self._ancestor_records(second))
        }
        distances = [
            index + 1 + second_distances[record.get("identity")]
            for index, record in enumerate(first_records)
            if record.get("identity") in second_distances
        ]
        return min(distances) if distances else 999

    def _rejected_exact_navigation_candidates(
        self,
        window: Any,
    ) -> list[dict[str, Any]]:
        rejected: list[dict[str, Any]] = []
        exact_names = {
            SALES_TOP_MENU_NAME,
            ONLINE_SALES_MENU_NAME,
            PURCHASE_REQUEST_LIST_NAME,
        }
        misleading_terms = (
            "구매요청",
            "주문조회",
            "일반주문",
            "MD/소물자동주문",
        )
        for control_type in NAVIGATION_CONTROL_TYPES:
            for element in self._elements(window, control_type):
                identity = self._diagnostic_identity(element)
                candidate_name = str(identity["name"])
                if candidate_name in exact_names:
                    continue
                if any(term in candidate_name for term in misleading_terms):
                    rejected.append(
                        {
                            **identity,
                            "reason": "not_an_exact_navigation_target",
                        }
                    )
        return rejected[:100]

    @staticmethod
    def _click_uia_element(element: Any) -> None:
        try:
            element.invoke()
        except Exception:
            element.click_input()

    def _log_navigation_snapshot(
        self,
        stage: str,
        elements: list[dict[str, Any]],
    ) -> None:
        keyword_candidates = [
            value
            for value in elements
            if any(
                keyword in str(value.get("name") or "")
                for keyword in NAVIGATION_DIAGNOSTIC_KEYWORDS
            )
        ][:30]
        self.navigation_logger.info(
            "navigation_snapshot stage=%s element_count=%d "
            "keyword_candidates=%s elements=%s",
            stage,
            len(elements),
            json.dumps(keyword_candidates, ensure_ascii=False),
            json.dumps(elements, ensure_ascii=False),
        )

    def _log_navigation_diff(
        self,
        before: list[dict[str, Any]],
        after: list[dict[str, Any]],
    ) -> None:
        def identity(value: dict[str, Any]) -> tuple[str, str, str]:
            return (
                str(value.get("control_type") or ""),
                str(value.get("name") or ""),
                str(value.get("automation_id") or ""),
            )

        before_ids = {identity(value) for value in before}
        added = [value for value in after if identity(value) not in before_ids][:40]
        self.navigation_logger.info(
            "navigation_diff stage=after_sales_menu before_count=%d "
            "after_count=%d added=%s",
            len(before),
            len(after),
            json.dumps(added, ensure_ascii=False),
        )
        self._log_navigation_snapshot("after_sales_menu", after)

    def _log_sales_menu_tree(self, elements: list[dict[str, Any]]) -> None:
        keyword_elements = [
            value
            for value in elements
            if any(
                keyword in str(value.get("name") or "")
                for keyword in NAVIGATION_DIAGNOSTIC_KEYWORDS
            )
        ]
        self.navigation_logger.info(
            "sales menu tree: elements=%s",
            json.dumps(elements, ensure_ascii=False),
        )
        self.navigation_logger.info(
            "sales menu keyword elements: keywords=%s elements=%s",
            list(NAVIGATION_DIAGNOSTIC_KEYWORDS),
            json.dumps(keyword_elements, ensure_ascii=False),
        )

    def _log_clicked_identity(self, stage: str, element: Any) -> None:
        self.navigation_logger.info(
            "clicked target identity: stage=%s identity=%s",
            stage,
            json.dumps(self._diagnostic_identity(element), ensure_ascii=False),
        )

    def _log_resulting_page_markers(self, window: Any) -> None:
        texts = self.visible_texts(window)
        markers = [
            marker
            for marker in PURCHASE_PAGE_MARKERS + REJECTED_NAVIGATION_NAMES
            if any(marker in text for text in texts)
        ]
        self.navigation_logger.info(
            "resulting page markers: %s",
            json.dumps(markers, ensure_ascii=False),
        )

    def _diagnostic_identity(self, element: Any) -> dict[str, Any]:
        return {
            "name": self._safe_diagnostic_name(self._name(element)),
            "control_type": self._safe_element_info_value(
                element, "control_type"
            ),
            "automation_id": self._safe_diagnostic_name(
                self._safe_element_info_value(element, "automation_id")
            ),
            "class_name": self._safe_diagnostic_name(
                self._safe_element_info_value(element, "class_name")
            ),
            "window_handle": self._safe_uia_integer(element, "handle"),
            "runtime_id": self._safe_uia_sequence(element, "runtime_id"),
            "native_window_handle": self._safe_uia_integer(element, "handle"),
            "framework_id": self._safe_element_info_value(
                element, "framework_id"
            ),
        }

    @staticmethod
    def _element_identity_key(element: Any) -> Any:
        try:
            runtime_id = tuple(element.element_info.runtime_id or ())
            if runtime_id:
                return ("runtime_id", runtime_id)
        except Exception:
            pass
        try:
            handle = int(element.element_info.handle)
            if handle:
                return ("handle", handle)
        except Exception:
            pass
        return ("object", id(element))

    def _parent_name(self, element: Any) -> str:
        try:
            return self._safe_diagnostic_name(self._name(element.parent()))
        except Exception:
            return ""

    def _element_depth(
        self,
        element: Any,
        *,
        window_identity: Any,
        max_depth: int,
    ) -> int | None:
        depth = 1
        current = element
        seen: set[Any] = set()
        try:
            while depth <= max_depth:
                current = current.parent()
                identity = self._element_identity_key(current)
                if identity == window_identity:
                    return depth
                if identity in seen:
                    return None
                seen.add(identity)
                depth += 1
        except Exception:
            return depth
        return depth

    @staticmethod
    def _invoke_available(element: Any) -> bool:
        try:
            getattr(element, "iface_invoke")
            return True
        except Exception:
            return callable(getattr(element, "invoke", None))

    @staticmethod
    def _selected_state(element: Any) -> bool | None:
        try:
            return bool(element.iface_selection_item.current_is_selected)
        except Exception:
            try:
                return bool(element.is_selected())
            except Exception:
                return None

    @staticmethod
    def _expanded_state(element: Any) -> bool | None:
        try:
            state = int(element.iface_expand_collapse.current_expand_collapse_state)
            return state in {1, 2}
        except Exception:
            return None

    def _is_navigation_element_selected(self, element: Any) -> bool:
        class_name = self._safe_element_info_value(
            element,
            "class_name",
        ).casefold()
        tokens = set(re.split(r"[\s_-]+", class_name))
        if "selected" in class_name or tokens & {"active", "current", "on"}:
            return True
        selected = self._selected_state(element)
        return bool(selected) if selected is not None else False

    def _is_online_sales_expanded(
        self,
        online_sales_menu: Any,
        purchase_request_targets: list[Any],
    ) -> bool:
        expanded = self._expanded_state(online_sales_menu)
        if expanded is not None:
            return expanded
        class_name = self._safe_element_info_value(
            online_sales_menu,
            "class_name",
        ).casefold()
        tokens = set(re.split(r"[\s_-]+", class_name))
        if tokens & {"expanded", "open", "opened", "on"}:
            return True
        # Chrome UIA가 ExpandCollapsePattern을 노출하지 않는 경우에는
        # 정확한 하위 메뉴 하나가 보이는 것만 펼침의 대체 증거로 허용합니다.
        return len(purchase_request_targets) == 1

    def _purchase_request_page_marker_found(self, window: Any) -> bool:
        separators = re.compile(r"\s*(?:>|›|/)\s*")
        for control_type in ("Text", "Document"):
            for element in self._elements(window, control_type):
                name = " ".join(self._name(element).split())
                if name == PURCHASE_REQUEST_LIST_NAME:
                    return True
                if PURCHASE_REQUEST_LIST_NAME in separators.split(name):
                    return True
        return False

    @staticmethod
    def _safe_diagnostic_name(value: str) -> str:
        text = " ".join(str(value or "").split())[:120]
        text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "<email>", text)
        text = re.sub(
            r"(?<!\d)01\d[- ]?\d{3,4}[- ]?\d{4}(?!\d)",
            "<phone>",
            text,
        )
        text = re.sub(r"\d{6,}", "<number>", text)
        return text

    @staticmethod
    def _navigation_failure(
        reason: str,
        *,
        menu_found: bool,
        target_found: bool,
        checks: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if "온라인판매" in reason:
            code = "ONLINE_SALES_MENU_NOT_FOUND"
            message = f"온라인판매 메뉴 처리에 실패했습니다. ({reason})"
            stage = "online_sales_menu"
        elif "판매 메뉴" in reason:
            code = "SALES_MENU_SELECTION_FAILED"
            message = f"판매 메뉴 선택에 실패했습니다. ({reason})"
            stage = "sales_menu"
        elif "구매요청리스트" in reason:
            code = "PURCHASE_REQUEST_LIST_NOT_FOUND"
            message = f"구매요청리스트 메뉴 처리에 실패했습니다. ({reason})"
            stage = "purchase_request_list"
        else:
            code = "PURCHASE_REQUEST_PAGE_NOT_VERIFIED"
            message = f"구매요청리스트 화면을 안전하게 확인하지 못했습니다. ({reason})"
            stage = "purchase_request_page"
        details = {
            "navigation_stage": stage,
            "menu_found": menu_found,
            "target_menu_found": target_found,
            "reason": reason,
        }
        if checks:
            details["safety_checks"] = checks
        return {
            "ok": False,
            "success": False,
            "code": code,
            "error_code": code,
            "message": message,
            "details": details,
        }

    @staticmethod
    def _navigation_stage_failure(
        code: str,
        message: str,
        *,
        stage: str,
        menu_found: bool,
        target_found: bool,
        reason: str,
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "success": False,
            "code": code,
            "error_code": code,
            "message": message,
            "details": {
                "navigation_stage": stage,
                "menu_found": menu_found,
                "target_menu_found": target_found,
                "reason": reason,
            },
        }

    @staticmethod
    def _navigation_target_unknown(
        *,
        stage: str,
        diagnostics: list[dict[str, Any]],
        rejected: list[dict[str, Any]],
    ) -> dict[str, Any]:
        code = "DPS_NAVIGATION_TARGET_UNKNOWN"
        return {
            "ok": False,
            "success": False,
            "code": code,
            "error_code": code,
            "message": (
                "판매 → 온라인판매 → 구매요청리스트의 정확한 이동 대상을 "
                "확인하지 못해 임의 클릭 없이 중단했습니다."
            ),
            "details": {
                "navigation_stage": stage,
                "target_menu_found": False,
                "diagnostic_mode": True,
                "navigation_element_count": len(diagnostics),
                "rejected_candidates": rejected,
                "reason": "정확히 일치하는 이동 대상이 하나가 아님",
            },
        }

    @staticmethod
    def _elements(window: Any, control_type: str) -> list[Any]:
        try:
            return list(window.descendants(control_type=control_type))
        except Exception:
            return []

    @staticmethod
    def _name(element: Any) -> str:
        try:
            return str(element.element_info.name or element.window_text() or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _failure(
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "success": False,
            "code": code,
            "error_code": code,
            "message": message,
            "details": details or {},
            "manual_action_required": True,
        }
