from __future__ import annotations

import json
import logging
import os
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONNECTION_FILE = PROJECT_ROOT / "data" / "dps_connection.json"
DEFAULT_AGENT_STATE_FILE = PROJECT_ROOT / "data" / "dps_agent_state.json"
DEFAULT_TAB_TITLE_KEYWORDS = [
    "Samsung DPS 2.0",
    "Samsung DPS",
    "삼성 DPS",
    "DPS 2.0",
]

DEFAULT_CONFIG: dict[str, Any] = {
    "browser": "chrome",
    "tab_title_keywords": DEFAULT_TAB_TITLE_KEYWORDS,
    "last_window_title": None,
    "last_tab_title": None,
    "last_connected_at": None,
    "auto_connect": True,
}


class ConnectionStore:
    """DPS 연결 설정을 저장합니다. HWND와 UIA 요소는 절대로 디스크에 저장하지 않습니다."""

    def __init__(
        self,
        path: Path | None = None,
        state_path: Path | None = None,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        configured = os.getenv("DPS_CONNECTION_FILE", "").strip()
        self.path = path or (Path(configured) if configured else DEFAULT_CONNECTION_FILE)
        self.state_path = state_path or DEFAULT_AGENT_STATE_FILE
        self.logger = logger or logging.getLogger(__name__)
        self._lock = threading.RLock()

    def load(self) -> dict[str, Any]:
        with self._lock:
            config = deepcopy(DEFAULT_CONFIG)
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("설정 파일의 최상위 값이 객체가 아닙니다.")
            except FileNotFoundError:
                self.save(config)
                return config
            except Exception as error:
                self.logger.warning(
                    "DPS 연결 설정 파일이 손상되어 기본값을 사용합니다: %s",
                    error.__class__.__name__,
                )
                self._backup_corrupt_file()
                self.save(config)
                return config

            if raw.get("browser") == "chrome":
                config["browser"] = "chrome"
            keywords = raw.get("tab_title_keywords")
            if isinstance(keywords, list):
                cleaned = [str(value).strip() for value in keywords if str(value).strip()]
                if cleaned:
                    config["tab_title_keywords"] = cleaned
            for key in (
                "last_window_title",
                "last_tab_title",
                "last_connected_at",
            ):
                value = raw.get(key)
                config[key] = str(value).strip() if value not in (None, "") else None
            config["auto_connect"] = bool(raw.get("auto_connect", True))
            return config

    def save(self, config: dict[str, Any]) -> None:
        """같은 폴더의 임시 파일에 쓴 뒤 교체하여 부분 JSON 저장을 방지합니다."""

        safe = deepcopy(DEFAULT_CONFIG)
        safe.update(
            {
                key: config.get(key)
                for key in safe
                if key in config
            }
        )
        safe.pop("connected_hwnd", None)
        safe.pop("hwnd", None)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(self.path.suffix + ".tmp")
            temp.write_text(
                json.dumps(safe, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temp, self.path)

    def update(self, **changes: Any) -> dict[str, Any]:
        with self._lock:
            config = self.load()
            allowed = set(DEFAULT_CONFIG)
            config.update({key: value for key, value in changes.items() if key in allowed})
            self.save(config)
            return config

    def load_agent_state(self) -> dict[str, Any]:
        """로그인 타이머와 진단 메타데이터만 읽습니다. 연결 핸들은 포함하지 않습니다."""

        with self._lock:
            try:
                value = json.loads(self.state_path.read_text(encoding="utf-8"))
                return value if isinstance(value, dict) else {}
            except FileNotFoundError:
                return {}
            except Exception as error:
                self.logger.warning(
                    "DPS Agent 상태 파일을 읽지 못했습니다: %s",
                    error.__class__.__name__,
                )
                return {}

    def save_agent_state(self, state: dict[str, Any]) -> None:
        allowed = {
            "login_confirmed_at",
            "last_activity_at",
            "last_lookup_at",
            "last_successful_lookup_at",
            "last_order_number_masked",
            "last_error",
            "error_type",
            "connection_status",
            "session_status",
            "last_checked_at",
            "last_ready_at",
            "last_keepalive_at",
            "last_keepalive_attempt_at",
            "consecutive_keepalive_failures",
            "keepalive_lock_skips",
            "last_monitor_event",
            "last_passive_monitor_at",
            "last_gui_operation_at",
            "last_gui_operation_type",
        }
        safe = {key: state.get(key) for key in allowed if key in state}
        with self._lock:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            temp.write_text(
                json.dumps(safe, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temp, self.state_path)

    def _backup_corrupt_file(self) -> None:
        if not self.path.exists():
            return
        backup = self.path.with_suffix(self.path.suffix + ".corrupt")
        try:
            os.replace(self.path, backup)
        except OSError:
            self.logger.exception("손상된 DPS 연결 설정 파일 백업에 실패했습니다.")
