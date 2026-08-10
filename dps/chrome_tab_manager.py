from __future__ import annotations

import ctypes
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

try:
    from pywinauto import Desktop
except ImportError:  # pragma: no cover - 진단 응답에서 처리
    Desktop = None


DEFAULT_DPS_KEYWORDS = (
    "Samsung DPS 2.0",
    "Samsung DPS",
    "삼성 DPS",
    "DPS 2.0",
)


@dataclass(slots=True)
class TabCandidate:
    hwnd: int
    window: Any
    window_title: str
    tab: Any
    tab_title: str
    score: int
    current_url: str = ""


@dataclass(slots=True)
class RuntimeConnection:
    """프로세스 수명 동안만 유지하는 재탐색 가능한 연결 힌트입니다."""

    hwnd: int
    window_title: str
    tab_title: str
    connected_at: str
    tab: Any | None = None
    current_url: str = ""


@dataclass(slots=True)
class PreviousUiContext:
    foreground_hwnd: int | None
    window_title: str
    selected_tab_title: str | None


@dataclass(slots=True)
class AddressReadResult:
    raw_url: str
    normalized_url: str
    url_source: str
    element_name: str = ""
    control_type: str = ""


class ChromeTabManager:
    def __init__(
        self,
        *,
        keywords: Iterable[str] = DEFAULT_DPS_KEYWORDS,
        allowed_hosts: Iterable[str] = ("dps2u.co.kr",),
        logger: logging.Logger | None = None,
        desktop_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.keywords = tuple(str(value).strip() for value in keywords if str(value).strip())
        self.allowed_hosts = tuple(
            str(value).strip().casefold().lstrip(".")
            for value in allowed_hosts
            if str(value).strip()
        )
        self.logger = logger or logging.getLogger(__name__)
        self._desktop_factory = desktop_factory or Desktop
        self.last_connection_failure_reason = ""
        self.last_tab_scan_failed = False

    @staticmethod
    def dps_title_score(title: str) -> int:
        """요청된 우선순위가 뒤집히지 않도록 상호 배타적인 기본 점수를 사용합니다."""

        normalized = " ".join(str(title or "").casefold().split())
        if "samsung dps 2.0" in normalized:
            return 400
        if "samsung dps" in normalized:
            return 300
        if "삼성 dps" in normalized:
            return 200
        if "dps 2.0" in normalized:
            return 150
        if "dps" in normalized:
            return 100
        return 0

    @classmethod
    def is_dps_title(cls, title: str) -> bool:
        return cls.dps_title_score(title) > 0

    def candidate_score(self, title: str) -> int:
        score = self.dps_title_score(title)
        if score:
            return score
        normalized = " ".join(str(title or "").casefold().split())
        for index, keyword in enumerate(self.keywords):
            if " ".join(keyword.casefold().split()) in normalized:
                return max(10, 90 - index)
        return 0

    def matches_dps_title(self, title: str) -> bool:
        return self.candidate_score(title) > 0

    def chrome_windows(self) -> list[Any]:
        self.logger.info("Chrome 창 탐색 시작")
        if self._desktop_factory is None or os.name != "nt":
            self.logger.warning("Windows UI Automation을 사용할 수 없습니다.")
            return []
        try:
            windows = self._desktop_factory(backend="uia").windows()
        except Exception:
            self.logger.exception("Chrome 최상위 창 목록을 읽지 못했습니다.")
            return []

        chrome: list[Any] = []
        for window in windows:
            try:
                class_name = str(window.element_info.class_name or "")
                if "chrome_widgetwin" not in class_name.casefold():
                    continue
                process_name = self.process_name_for_window(int(window.handle))
                if process_name and process_name.casefold() != "chrome.exe":
                    continue
                chrome.append(window)
                self.logger.info(
                    "Chrome 창 발견: title=%r hwnd=%s process=%r",
                    self.window_title(window),
                    int(window.handle),
                    process_name,
                )
            except Exception:
                continue
        self.logger.info("발견한 Chrome 창 수: %d", len(chrome))
        return chrome

    @staticmethod
    def process_name_for_window(hwnd: int) -> str:
        """Chrome_WidgetWin을 사용하는 VS Code 등 다른 앱을 제외합니다."""

        if os.name != "nt" or hwnd <= 0:
            return ""
        process_handle = None
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            process_id = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
            if not process_id.value:
                return ""
            process_handle = kernel32.OpenProcess(
                0x1000,  # PROCESS_QUERY_LIMITED_INFORMATION
                False,
                process_id.value,
            )
            if not process_handle:
                return ""
            size = ctypes.c_ulong(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(
                process_handle,
                0,
                buffer,
                ctypes.byref(size),
            ):
                return ""
            return os.path.basename(buffer.value)
        except Exception:
            return ""
        finally:
            if process_handle:
                try:
                    ctypes.windll.kernel32.CloseHandle(process_handle)
                except Exception:
                    pass

    def tabs_in_window(self, window: Any) -> list[Any]:
        self.last_tab_scan_failed = False
        try:
            tabs = list(window.descendants(control_type="TabItem"))
        except Exception:
            self.last_tab_scan_failed = True
            self.logger.exception("Chrome 탭 목록 조사 실패: %r", self.window_title(window))
            return []
        for tab in tabs:
            self.logger.info(
                "Chrome 탭 발견: window=%r tab=%r",
                self.window_title(window),
                self.element_name(tab),
            )
        self.logger.info(
            "Chrome 전체 탭: hwnd=%s titles=%s",
            getattr(window, "handle", None),
            [self.element_name(tab) for tab in tabs],
        )
        return tabs

    def find_candidates(self, windows: Iterable[Any] | None = None) -> list[TabCandidate]:
        """모든 TabItem을 실제로 선택해 DPS 호스트가 확인된 탭만 반환합니다."""

        result: list[TabCandidate] = []
        previous = self.capture_previous_context()
        last_hwnd = 0
        try:
            for window in list(windows) if windows is not None else self.chrome_windows():
                try:
                    hwnd = int(window.handle)
                except Exception:
                    continue
                last_hwnd = hwnd
                for tab in self.tabs_in_window(window):
                    tab_title = self.element_name(tab)
                    address = self.address_for_selected_tab(window, tab, hwnd=hwnd)
                    host_matches = self.address_matches_dps(address.normalized_url)
                    self.logger.info(
                        "Chrome TabItem URL 후보 검사: hwnd=%s tab=%r raw_url=%r "
                        "url_source=%s host=%r dps_host_matches=%s",
                        hwnd,
                        tab_title,
                        address.raw_url,
                        address.url_source,
                        self._host_from_address(address.normalized_url),
                        host_matches,
                    )
                    if not host_matches:
                        continue
                    result.append(
                        TabCandidate(
                            hwnd=hwnd,
                            window=window,
                            window_title=self.window_title(window),
                            tab=tab,
                            tab_title=tab_title,
                            score=1000,
                            current_url=address.normalized_url,
                        )
                    )
        finally:
            if previous.foreground_hwnd:
                restored = self.restore_previous_context(
                    previous,
                    previous.foreground_hwnd,
                )
                self.logger.info(
                    "original tab restored: restored=%s hwnd=%s tab=%r",
                    restored,
                    previous.foreground_hwnd,
                    previous.selected_tab_title,
                )
            elif last_hwnd:
                self.logger.info(
                    "후보 탐색 전 포그라운드 창이 없어 복원을 생략했습니다: last_hwnd=%s",
                    last_hwnd,
                )
        result.sort(key=lambda candidate: candidate.tab_title.casefold())
        self.logger.info(
            "verified candidate count: %d identities=%s",
            len(result),
            [
                {
                    "hwnd": candidate.hwnd,
                    "tab_title": candidate.tab_title,
                    "url": candidate.current_url,
                }
                for candidate in result
            ],
        )
        return result

    def address_for_selected_tab(
        self,
        window: Any,
        tab: Any,
        *,
        hwnd: int,
        timeout: float = 2.0,
    ) -> AddressReadResult:
        """TabItem을 선택한 뒤 그 탭의 주소창 값을 읽습니다."""

        if not self.activate_window(window):
            return AddressReadResult("", "", "WINDOW_FOREGROUND_FAILED")
        try:
            try:
                tab.select()
            except Exception:
                tab.click_input()
        except Exception:
            self.logger.exception(
                "Chrome TabItem URL 검사용 선택 실패: hwnd=%s tab=%r",
                hwnd,
                self.element_name(tab),
            )
            return AddressReadResult("", "", "TAB_SELECT_FAILED")

        expected_title = self.element_name(tab)
        deadline = time.monotonic() + max(0.5, timeout)
        while time.monotonic() < deadline:
            if self.foreground_hwnd() != hwnd or not self.is_tab_selected(tab):
                time.sleep(0.05)
                continue
            selected_title = self.selected_tab_title(window)
            if (
                not selected_title
                or selected_title.casefold() != expected_title.casefold()
            ):
                time.sleep(0.05)
                continue
            address = self.current_address_details(window)
            if address.normalized_url:
                return address
            time.sleep(0.05)
        return self.current_address_details(window)

    def find_best_candidate(
        self,
        *,
        preferred_hwnd: int | None = None,
        preferred_tab_title: str | None = None,
    ) -> TabCandidate | None:
        candidates = self.find_candidates()
        if not candidates:
            self.logger.warning("Samsung DPS 탭 후보를 찾지 못했습니다.")
            return None
        if preferred_hwnd:
            same_window = [item for item in candidates if item.hwnd == preferred_hwnd]
            if preferred_tab_title:
                same_title = [
                    item
                    for item in same_window
                    if item.tab_title.casefold() == preferred_tab_title.casefold()
                ]
                if same_title:
                    return same_title[0]
            if same_window:
                return same_window[0]
        return candidates[0]

    def candidate_for_connection(self, connection: RuntimeConnection) -> TabCandidate | None:
        if not self.is_window(connection.hwnd):
            self.last_connection_failure_reason = "WINDOW_CLOSED"
            self.logger.info(
                "connection validation result: valid=False reason=WINDOW_CLOSED hwnd=%s",
                connection.hwnd,
            )
            return None
        window = self.window_from_handle(connection.hwnd)
        if window is None:
            self.last_connection_failure_reason = "WINDOW_NOT_FOUND"
            self.logger.info(
                "connection validation result: valid=False reason=WINDOW_NOT_FOUND hwnd=%s",
                connection.hwnd,
            )
            return None
        tabs = self.tabs_in_window(window)
        if self.last_tab_scan_failed:
            self.last_connection_failure_reason = "UIA_READ_FAILED"
            self.logger.warning(
                "connection validation result: valid=unknown "
                "reason=UIA_READ_FAILED hwnd=%s",
                connection.hwnd,
            )
            return None
        if not tabs and connection.tab is not None:
            try:
                if connection.tab.exists(timeout=0):
                    tabs = [connection.tab]
                    self.logger.info(
                        "connection validation result: valid=True "
                        "reason=STORED_TAB_ELEMENT_PRESENT_AFTER_EMPTY_SCAN hwnd=%s",
                        connection.hwnd,
                    )
            except Exception:
                self.last_connection_failure_reason = "UIA_READ_FAILED"
                self.logger.warning(
                    "connection validation result: valid=unknown "
                    "reason=UIA_READ_FAILED_STORED_TAB hwnd=%s",
                    connection.hwnd,
                )
                return None
        matched_tab = None
        if connection.tab is not None:
            matched_tab = next((tab for tab in tabs if tab is connection.tab), None)
        for tab in tabs:
            if matched_tab is not None:
                break
            tab_title = self.element_name(tab)
            if tab_title.casefold() != connection.tab_title.casefold():
                continue
            matched_tab = tab
            break
        if matched_tab is None:
            self.last_connection_failure_reason = "TAB_CLOSED"
            self.logger.info(
                "connection validation result: valid=False reason=TAB_CLOSED "
                "hwnd=%s tab=%r",
                connection.hwnd,
                connection.tab_title,
            )
            return None
        tab_title = self.element_name(matched_tab)
        candidate = TabCandidate(
            hwnd=connection.hwnd,
            window=window,
            window_title=self.window_title(window),
            tab=matched_tab,
            tab_title=tab_title,
            score=1000,
            current_url=connection.current_url,
        )
        self.last_connection_failure_reason = ""
        self.logger.info(
            "connection validation result: valid=True reason=TAB_PRESENT "
            "hwnd=%s tab=%r selected=%s",
            connection.hwnd,
            tab_title,
            self.is_tab_selected(matched_tab),
        )
        return candidate

    def select_candidate(self, candidate: TabCandidate, timeout: float = 5.0) -> tuple[bool, str]:
        before_title = self.selected_tab_title(candidate.window)
        self.logger.info(
            "기존 DPS TabItem 선택 시작: hwnd=%s all_tabs=%s selected_item=%r "
            "item_control_type=%s before_active_title=%r",
            candidate.hwnd,
            [self.element_name(tab) for tab in self.tabs_in_window(candidate.window)],
            self.element_name(candidate.tab),
            getattr(candidate.tab.element_info, "control_type", ""),
            before_title,
        )
        if not self.activate_window(candidate.window):
            self.logger.warning("DPS 탭 창 활성화 실패: %r", candidate.window_title)
            return False, "WINDOW_FOREGROUND_FAILED"
        try:
            try:
                candidate.tab.select()
            except Exception:
                candidate.tab.click_input()
            self.logger.info("DPS 탭 클릭 실행: %r", candidate.tab_title)
        except Exception:
            self.logger.exception("DPS 탭 클릭 실패: %r", candidate.tab_title)
            return False, "DPS_TAB_SELECT_FAILED"

        deadline = time.monotonic() + max(0.5, timeout)
        while time.monotonic() < deadline:
            if self.foreground_hwnd() != candidate.hwnd:
                time.sleep(0.1)
                continue
            selected_title = self.selected_tab_title(candidate.window)
            selected_identity_matches = bool(
                selected_title
                and selected_title.casefold() == candidate.tab_title.casefold()
            )
            address_result = self.current_address_details(candidate.window)
            address = address_result.normalized_url
            ui_hits = self.page_ui_dps_hits(candidate.window)
            if (
                self.is_tab_selected(candidate.tab)
                and selected_identity_matches
                and self.address_matches_dps(address)
            ):
                self.logger.info(
                    "기존 DPS 탭 선택 검증 성공: hwnd=%s before=%r after=%r "
                    "tabitem=%r selected=%s raw_url=%r url_element=%r "
                    "url_control_type=%s home_ui_hits=%s",
                    candidate.hwnd,
                    before_title,
                    selected_title,
                    candidate.tab_title,
                    self.is_tab_selected(candidate.tab),
                    address_result.raw_url,
                    address_result.element_name,
                    address_result.control_type,
                    ui_hits,
                )
                return True, selected_title
            time.sleep(0.1)
        foreground = self.foreground_hwnd()
        selected_title = self.selected_tab_title(candidate.window)
        selected_identity_matches = bool(
            selected_title
            and selected_title.casefold() == candidate.tab_title.casefold()
        )
        address_result = self.current_address_details(candidate.window)
        address = address_result.normalized_url
        ui_hits = self.page_ui_dps_hits(candidate.window)
        ui_matches = self.page_ui_matches_dps(candidate.window, hits=ui_hits)
        checks = {
            "target_hwnd": candidate.hwnd,
            "target_hwnd_valid": self.is_window(candidate.hwnd),
            "foreground_hwnd": foreground,
            "target_window_foreground": foreground == candidate.hwnd,
            "candidate_tab_title": candidate.tab_title,
            "tab_element_selected": self.is_tab_selected(candidate.tab),
            "selected_tab_title": selected_title,
            "selected_tab_identity_matches": selected_identity_matches,
            "selected_tab_title_matches": bool(
                selected_title and self.matches_dps_title(selected_title)
            ),
            "current_url": address,
            "selected_page_address_matches": self.address_matches_dps(address),
            "selected_page_ui_matches": ui_matches,
            "dps_home_ui_hits": ui_hits,
            "raw_url": address_result.raw_url,
            "url_source": address_result.url_source,
            "url_element_name": address_result.element_name,
            "url_element_control_type": address_result.control_type,
            "active_tab_before": before_title,
            "active_tab_after": selected_title,
            "selected_page_host": self._host_from_address(address),
            "allowed_hosts": list(self.allowed_hosts),
        }
        self.logger.warning(
            "활성 탭 검증 실패: reason=%s checks=%s",
            "foreground/선택 상태/DPS 제목/DPS URL 조건 중 하나 이상 불일치",
            checks,
        )
        return False, "DPS_TAB_VERIFICATION_FAILED"

    def validate_selected_candidate(self, candidate: TabCandidate) -> tuple[bool, dict[str, Any]]:
        foreground = self.foreground_hwnd()
        selected_title = self.selected_tab_title(candidate.window)
        address = self.current_address(candidate.window)
        ui_matches = self.page_ui_matches_dps(candidate.window)
        selected_identity_matches = bool(
            selected_title
            and selected_title.casefold() == candidate.tab_title.casefold()
        )
        checks = {
            "target_hwnd_valid": self.is_window(candidate.hwnd),
            "target_window_foreground": foreground == candidate.hwnd,
            "tab_element_selected": self.is_tab_selected(candidate.tab),
            "selected_tab_title": selected_title,
            "selected_tab_identity_matches": selected_identity_matches,
            "selected_tab_title_matches": bool(
                selected_title and self.matches_dps_title(selected_title)
            ),
            "selected_page_address_matches": self.address_matches_dps(address),
            "selected_page_ui_matches": ui_matches,
            "selected_page_host": self._host_from_address(address),
        }
        ok = all(
            checks[key]
            for key in (
                "target_hwnd_valid",
                "target_window_foreground",
                "tab_element_selected",
            )
        ) and bool(
            checks["selected_page_address_matches"]
        )
        self.logger.info("DPS 입력 전 탭 검증: ok=%s checks=%s", ok, checks)
        return ok, checks

    def current_address(self, window: Any) -> str:
        """여러 UIA 경로로 선택된 Chrome 탭 URL을 읽습니다."""

        return self.current_address_details(window).normalized_url

    def current_address_details(self, window: Any) -> AddressReadResult:
        """AddressBar, Document, LegacyIAccessible, ValuePattern 순으로 시도합니다."""

        try:
            edits = list(window.descendants(control_type="Edit"))
        except Exception:
            edits = []
        address_bars: list[Any] = []
        for edit in edits:
            try:
                rect = edit.rectangle()
                name = str(getattr(edit.element_info, "name", "") or "")
                semantic = any(
                    hint in name.casefold()
                    for hint in (
                        "address and search",
                        "address bar",
                        "주소 및 검색",
                        "주소창",
                    )
                )
                if semantic or (rect.top <= 145 and rect.width() >= 250):
                    address_bars.append(edit)
            except Exception:
                continue

        attempts: list[tuple[str, Any, Any]] = []
        # AddressBar는 의미/위치로 식별한 Edit 자체가 노출하는 텍스트입니다.
        for edit in address_bars:
            attempts.extend(
                [
                    ("AddressBar", edit, lambda item=edit: item.window_text()),
                    (
                        "AddressBar",
                        edit,
                        lambda item=edit: getattr(item.element_info, "value", ""),
                    ),
                ]
            )
        try:
            documents = list(window.descendants(control_type="Document"))
        except Exception:
            documents = []
        for document in documents:
            attempts.extend(
                [
                    (
                        "Document",
                        document,
                        lambda item=document: getattr(item.element_info, "url", ""),
                    ),
                    ("Document", document, lambda item=document: item.window_text()),
                    (
                        "Document",
                        document,
                        lambda item=document: item.legacy_properties().get("Value"),
                    ),
                ]
            )
        for edit in address_bars or edits:
            attempts.append(
                (
                    "LegacyIAccessible",
                    edit,
                    lambda item=edit: item.legacy_properties().get("Value"),
                )
            )
        for edit in address_bars or edits:
            attempts.append(("ValuePattern", edit, lambda item=edit: item.get_value()))

        for source, element, reader in attempts:
            try:
                raw = str(reader() or "").strip()
            except Exception:
                continue
            normalized = self.normalize_address(raw)
            if not normalized:
                continue
            result = AddressReadResult(
                raw,
                normalized,
                source,
                self.element_name(element),
                str(getattr(element.element_info, "control_type", "") or ""),
            )
            self.logger.info(
                "Chrome URL 읽기:\nraw_url: %s\nnormalized_url: %s\nurl_source: %s"
                "\nelement_name: %s\ncontrol_type: %s",
                result.raw_url,
                result.normalized_url,
                result.url_source,
                result.element_name,
                result.control_type,
            )
            return result
        self.logger.warning(
            "Chrome URL 읽기:\nraw_url: \nnormalized_url: \nurl_source: NONE"
        )
        return AddressReadResult("", "", "NONE")

    @staticmethod
    def normalize_address(address: str) -> str:
        value = str(address or "").strip()
        if not value or any(character.isspace() for character in value):
            return ""
        parsed = urlparse(value if "://" in value else f"https://{value}")
        host = str(parsed.hostname or "")
        if "." not in host and host.casefold() != "localhost":
            return ""
        return value

    def page_ui_dps_hits(self, window: Any) -> list[str]:
        markers = (
            "Samsung DPS",
            "로그아웃",
            "경영정보",
            "주문/배송",
            "고객관리",
            "구매요청리스트",
        )
        try:
            elements = window.descendants()
        except Exception:
            return []
        hits: set[str] = set()
        for element in elements:
            try:
                control_type = str(element.element_info.control_type or "")
                if control_type not in {
                    "Document",
                    "Pane",
                    "Text",
                    "Menu",
                    "MenuItem",
                    "Hyperlink",
                    "Button",
                }:
                    continue
                name = str(
                    element.element_info.name or element.window_text() or ""
                ).casefold()
                hits.update(marker for marker in markers if marker.casefold() in name)
            except Exception:
                continue
        return [marker for marker in markers if marker in hits]

    def page_ui_matches_dps(
        self,
        window: Any,
        *,
        hits: list[str] | None = None,
    ) -> bool:
        """URL이 없을 때에만 충분한 실제 DPS UI를 탭 검증 근거로 사용합니다."""

        resolved = hits if hits is not None else self.page_ui_dps_hits(window)
        return (
            "로그아웃" in resolved
            or "Samsung DPS" in resolved
            or len(resolved) >= 2
        )

    def address_matches_dps(self, address: str) -> bool:
        host = self._host_from_address(address)
        return any(
            host == allowed or host.endswith(f".{allowed}")
            for allowed in self.allowed_hosts
        )

    @staticmethod
    def _host_from_address(address: str) -> str:
        value = str(address or "").strip()
        if not value:
            return ""
        parsed = urlparse(value if "://" in value else f"https://{value}")
        return str(parsed.hostname or "").casefold().rstrip(".")

    def click_reload_button(self, window: Any) -> bool:
        """키 입력 없이 Chrome의 주소창 영역 새로고침 버튼을 직접 실행합니다."""

        names = {
            "reload this page",
            "reload",
            "새로고침",
            "이 페이지 새로고침",
        }
        try:
            buttons = window.descendants(control_type="Button")
        except Exception:
            return False
        for button in buttons:
            try:
                name = self.element_name(button).casefold()
                rect = button.rectangle()
                if rect.top > 180 or name not in names:
                    continue
                try:
                    button.invoke()
                except Exception:
                    button.click_input()
                return True
            except Exception:
                continue
        return False

    def capture_previous_context(self) -> PreviousUiContext:
        hwnd = self.foreground_hwnd()
        window = self.window_from_handle(hwnd) if hwnd else None
        return PreviousUiContext(
            foreground_hwnd=hwnd,
            window_title=self.window_title(window) if window is not None else "",
            selected_tab_title=self.selected_tab_title(window) if window is not None else None,
        )

    def restore_previous_context(self, context: PreviousUiContext, target_hwnd: int) -> bool:
        """다른 창이면 창만, 같은 Chrome 창이면 이전 TabItem을 다시 선택합니다."""

        if not context.foreground_hwnd or not self.is_window(context.foreground_hwnd):
            self.logger.warning("원래 창 복귀 생략: 원래 HWND가 유효하지 않습니다.")
            return False
        original = self.window_from_handle(context.foreground_hwnd)
        if original is None:
            return False
        if context.foreground_hwnd == target_hwnd and context.selected_tab_title:
            for tab in self.tabs_in_window(original):
                if self.element_name(tab).casefold() != context.selected_tab_title.casefold():
                    continue
                try:
                    try:
                        tab.select()
                    except Exception:
                        tab.click_input()
                    restored = self.activate_window(original)
                    self.logger.info("원래 Chrome 탭 복귀 결과: %s", restored)
                    return restored
                except Exception:
                    self.logger.exception("원래 Chrome 탭 복귀 실패")
                    return False
        restored = self.activate_window(original)
        self.logger.info("원래 최상위 창 복귀 결과: %s", restored)
        return restored

    def selected_tab_title(self, window: Any) -> str | None:
        if window is None:
            return None
        for tab in self.tabs_in_window(window):
            if self.is_tab_selected(tab):
                return self.element_name(tab) or None
        return None

    @staticmethod
    def is_tab_selected(tab: Any) -> bool:
        try:
            return bool(tab.iface_selection_item.CurrentIsSelected)
        except Exception:
            pass
        try:
            return bool(tab.element_info.get_current_property_value(30079))
        except Exception:
            return False

    @staticmethod
    def element_name(element: Any) -> str:
        try:
            return str(element.element_info.name or element.window_text() or "").strip()
        except Exception:
            try:
                return str(element.window_text() or "").strip()
            except Exception:
                return ""

    @staticmethod
    def window_title(window: Any) -> str:
        if window is None:
            return ""
        try:
            return str(window.window_text() or "").strip()
        except Exception:
            return ""

    def window_from_handle(self, hwnd: int | None) -> Any | None:
        if self._desktop_factory is None or not hwnd or not self.is_window(hwnd):
            return None
        try:
            return self._desktop_factory(backend="uia").window(handle=int(hwnd))
        except Exception:
            return None

    @staticmethod
    def is_window(hwnd: int | None) -> bool:
        if os.name != "nt" or not isinstance(hwnd, int) or hwnd <= 0:
            return False
        try:
            return bool(ctypes.windll.user32.IsWindow(hwnd))
        except Exception:
            return False

    @staticmethod
    def foreground_hwnd() -> int | None:
        if os.name != "nt":
            return None
        try:
            value = int(ctypes.windll.user32.GetForegroundWindow())
            return value or None
        except Exception:
            return None

    def activate_window(self, window: Any) -> bool:
        if os.name != "nt":
            return False
        try:
            hwnd = int(window.handle)
        except Exception:
            return False
        if not self.is_window(hwnd):
            return False
        try:
            user32 = ctypes.windll.user32
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.BringWindowToTop(hwnd)
            try:
                window.set_focus()
            except Exception:
                user32.SetForegroundWindow(hwnd)
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if self.foreground_hwnd() == hwnd:
                    return True
                user32.SetForegroundWindow(hwnd)
                time.sleep(0.1)
        except Exception:
            self.logger.exception("Chrome 창 활성화 중 오류")
        return False
