import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent
ENV_FILE = PROJECT_ROOT / ".env"
# Streamlit and the Windows DPS Agent can be launched from another working
# directory. Always resolve the project environment from the code location.
load_dotenv(ENV_FILE)


BASE_URL = "https://api.commerce.naver.com/external"
TOKEN_URL = f"{BASE_URL}/v1/oauth2/token"
QNA_URL = f"{BASE_URL}/v1/contents/qnas"
CUSTOMER_INQUIRY_URL = f"{BASE_URL}/v1/pay-user/inquiries"
SELLER_ACCOUNT_URL = f"{BASE_URL}/v1/seller/account"
SELLER_CHANNELS_URL = f"{BASE_URL}/v1/seller/channels"


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_list(name: str, default: str = "") -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(
        value.strip()
        for value in raw.replace(";", ",").split(",")
        if value.strip()
    )


@dataclass(frozen=True)
class NaverSyncSettings:
    """Read-only Naver inquiry synchronization safety settings."""

    enabled: bool = False
    lookback_days: int = 7
    page_size: int = 100
    max_pages: int = 100
    connect_timeout: float = 5.0
    read_timeout: float = 20.0
    max_retries: int = 2
    retry_backoff_seconds: float = 0.5
    max_runtime_seconds: float = 120.0
    lock_ttl_seconds: int = 300

    @classmethod
    def from_environment(cls) -> "NaverSyncSettings":
        return cls(
            enabled=get_env_bool("NAVER_SYNC_ENABLED", default=False),
            lookback_days=_env_int(
                "NAVER_SYNC_LOOKBACK_DAYS", 7, minimum=1
            ),
            page_size=min(
                100,
                _env_int("NAVER_SYNC_PAGE_SIZE", 100, minimum=10),
            ),
            max_pages=_env_int("NAVER_SYNC_MAX_PAGES", 100, minimum=1),
            connect_timeout=_env_float(
                "NAVER_SYNC_CONNECT_TIMEOUT", 5.0, minimum=0.1
            ),
            read_timeout=_env_float(
                "NAVER_SYNC_READ_TIMEOUT", 20.0, minimum=0.1
            ),
            max_retries=_env_int(
                "NAVER_SYNC_MAX_RETRIES", 2, minimum=0
            ),
            retry_backoff_seconds=_env_float(
                "NAVER_SYNC_RETRY_BACKOFF", 0.5, minimum=0.0
            ),
            max_runtime_seconds=_env_float(
                "NAVER_SYNC_MAX_RUNTIME_SECONDS", 120.0, minimum=1.0
            ),
            lock_ttl_seconds=_env_int(
                "NAVER_SYNC_LOCK_TTL_SECONDS", 300, minimum=30
            ),
        )


@dataclass(frozen=True)
class NaverPostSettings:
    """Manual answer-posting safety settings; disabled by default."""

    enabled: bool = False
    connect_timeout: float = 5.0
    read_timeout: float = 20.0

    @classmethod
    def from_environment(cls) -> "NaverPostSettings":
        return cls(
            enabled=get_env_bool("NAVER_POST_ENABLED", default=False),
            connect_timeout=_env_float(
                "NAVER_POST_CONNECT_TIMEOUT", 5.0, minimum=0.1
            ),
            read_timeout=_env_float(
                "NAVER_POST_READ_TIMEOUT", 20.0, minimum=0.1
            ),
        )


@dataclass(frozen=True)
class PositiveLearningSettings:
    """Conservative observation window for unchanged Auto Post answers."""

    observation_days: int = 7

    @classmethod
    def from_environment(cls) -> "PositiveLearningSettings":
        return cls(
            observation_days=_env_int(
                "AUTO_POST_UNCHANGED_LEARNING_DAYS", 7, minimum=1
            )
        )


NAVER_AUTO_SYNC_INTERVALS = (5, 10, 15, 30, 60)


def validate_auto_sync_interval(value: int) -> int:
    interval = int(value)
    if interval not in NAVER_AUTO_SYNC_INTERVALS:
        raise ValueError(
            "자동 동기화 간격은 5, 10, 15, 30, 60분 중 하나여야 합니다."
        )
    return interval


@dataclass(frozen=True)
class NaverAutoSyncSettings:
    enabled: bool = False
    interval_minutes: int = 10

    def __post_init__(self) -> None:
        validate_auto_sync_interval(self.interval_minutes)

    @classmethod
    def from_environment(cls) -> "NaverAutoSyncSettings":
        raw = _env_int(
            "NAVER_AUTO_SYNC_INTERVAL_MINUTES", 10, minimum=1
        )
        interval = raw if raw in NAVER_AUTO_SYNC_INTERVALS else 10
        return cls(
            enabled=get_env_bool(
                "NAVER_AUTO_SYNC_ENABLED", default=False
            ),
            interval_minutes=interval,
        )


@dataclass(frozen=True)
class NaverAutoPostSettings:
    """Automatic answer posting remains opt-in and disabled by default."""

    enabled: bool = False
    interval_minutes: int = 10
    max_retries: int = 1

    def __post_init__(self) -> None:
        validate_auto_sync_interval(self.interval_minutes)
        if not 0 <= int(self.max_retries) <= 10:
            raise ValueError("자동등록 재시도 횟수는 0~10 사이여야 합니다.")

    @classmethod
    def from_environment(cls) -> "NaverAutoPostSettings":
        raw_interval = _env_int(
            "NAVER_AUTO_POST_INTERVAL_MINUTES", 10, minimum=1
        )
        interval = (
            raw_interval if raw_interval in NAVER_AUTO_SYNC_INTERVALS else 10
        )
        return cls(
            enabled=get_env_bool("NAVER_AUTO_POST_ENABLED", default=False),
            interval_minutes=interval,
            max_retries=min(
                10,
                _env_int("NAVER_AUTO_POST_MAX_RETRIES", 1, minimum=0),
            ),
        )


@dataclass(frozen=True)
class DpsSessionSettings:
    """DPS Agent session monitoring and conservative keepalive settings."""

    monitor_enabled: bool = False
    keepalive_enabled: bool = False
    monitor_interval_seconds: int = 60
    keepalive_interval_minutes: int = 40
    keepalive_urgent_minutes: int = 55
    keepalive_retry_cooldown_minutes: int = 40
    passive_idle_enabled: bool = True
    passive_monitor_enabled: bool = True
    on_demand_connect_enabled: bool = True

    @classmethod
    def from_environment(cls) -> "DpsSessionSettings":
        idle_minutes = _env_int(
            "DPS_SESSION_IDLE_MINUTES",
            _env_int("DPS_SESSION_KEEPALIVE_INTERVAL_MINUTES", 40, minimum=10),
            minimum=10,
        )
        return cls(
            monitor_enabled=get_env_bool(
                "DPS_SESSION_MONITOR_ENABLED", default=False
            ),
            keepalive_enabled=get_env_bool(
                "DPS_SESSION_KEEPALIVE_ENABLED", default=False
            ),
            monitor_interval_seconds=_env_int(
                "DPS_SESSION_MONITOR_INTERVAL_SECONDS", 60, minimum=30
            ),
            keepalive_interval_minutes=idle_minutes,
            keepalive_urgent_minutes=_env_int(
                "DPS_SESSION_KEEPALIVE_URGENT_MINUTES", 55, minimum=10
            ),
            keepalive_retry_cooldown_minutes=_env_int(
                "DPS_SESSION_KEEPALIVE_RETRY_COOLDOWN_MINUTES", 40, minimum=1
            ),
            passive_idle_enabled=get_env_bool(
                "DPS_PASSIVE_IDLE_ENABLED", default=True
            ),
            passive_monitor_enabled=get_env_bool(
                "DPS_PASSIVE_SESSION_MONITOR_ENABLED", default=True
            ),
            on_demand_connect_enabled=get_env_bool(
                "DPS_ON_DEMAND_CONNECT_ENABLED", default=True
            ),
        )


@dataclass(frozen=True)
class DpsGuiGuardSettings:
    """Low-priority DPS access to shared Windows GUI resources."""

    enabled: bool = True
    recheck_seconds: float = 5.0
    cooldown_seconds: float = 5.0
    max_wait_seconds: float = 600.0
    activity_grace_seconds: float = 15.0
    process_patterns: tuple[str, ...] = ()
    window_patterns: tuple[str, ...] = ("KakaoTalk", "카카오톡")
    activity_paths: tuple[str, ...] = ()

    @classmethod
    def from_environment(cls) -> "DpsGuiGuardSettings":
        return cls(
            enabled=get_env_bool("DPS_GUI_GUARD_ENABLED", default=True),
            recheck_seconds=_env_float(
                "DPS_GUI_GUARD_RECHECK_SECONDS", 5.0, minimum=0.05
            ),
            cooldown_seconds=_env_float(
                "DPS_GUI_GUARD_COOLDOWN_SECONDS", 5.0, minimum=0.0
            ),
            max_wait_seconds=_env_float(
                "DPS_GUI_RESOURCE_MAX_WAIT_SECONDS", 600.0, minimum=0.0
            ),
            activity_grace_seconds=_env_float(
                "DPS_GUI_GUARD_ACTIVITY_GRACE_SECONDS", 15.0, minimum=0.0
            ),
            process_patterns=_env_list("DPS_GUI_GUARD_PROCESS_PATTERNS"),
            window_patterns=_env_list(
                "DPS_GUI_GUARD_WINDOW_PATTERNS", "KakaoTalk,카카오톡"
            ),
            activity_paths=_env_list("DPS_GUI_GUARD_ACTIVITY_PATHS"),
        )


@dataclass(frozen=True)
class StoreConfig:
    """
    네이버 커머스 API에 연결할 스토어 설정입니다.
    """

    code: str
    name: str
    client_id: str
    client_secret: str
    enabled: bool = True

    def validate(self) -> None:
        """
        현재 스토어의 필수 인증정보를 확인합니다.
        """

        missing_values: list[str] = []

        if not self.client_id:
            missing_values.append(
                f"{self.code}_CLIENT_ID"
            )

        if not self.client_secret:
            missing_values.append(
                f"{self.code}_CLIENT_SECRET"
            )

        if missing_values:
            missing_text = ", ".join(missing_values)

            raise ValueError(
                f"{self.name} 인증정보가 없습니다: "
                f"{missing_text}"
            )


def get_env_bool(
    variable_name: str,
    default: bool = True,
) -> bool:
    """
    환경변수의 문자열 값을 True 또는 False로 변환합니다.
    """

    value = os.getenv(variable_name)

    if value is None:
        return default

    return value.strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
        "disabled",
    }


def get_first_env_value(
    *variable_names: str,
) -> str:
    """
    여러 환경변수 중 처음으로 등록된 값을 반환합니다.

    기존 NAVER_CLIENT_ID 형식과 새로운
    OJE_PLUS_CLIENT_ID 형식을 모두 지원하기 위해 사용합니다.
    """

    for variable_name in variable_names:
        value = os.getenv(variable_name)

        if value:
            return value.strip()

    return ""


OJE_PLUS_STORE = StoreConfig(
    code="OJE_PLUS",
    name=os.getenv(
        "OJE_PLUS_STORE_NAME",
        "오제플러스",
    ).strip(),
    client_id=get_first_env_value(
        "OJE_PLUS_CLIENT_ID",
        "NAVER_CLIENT_ID",
    ),
    client_secret=get_first_env_value(
        "OJE_PLUS_CLIENT_SECRET",
        "NAVER_CLIENT_SECRET",
    ),
    enabled=get_env_bool(
        "OJE_PLUS_ENABLED",
        default=True,
    ),
)


SMART_STORE = StoreConfig(
    code="SMART_STORE",
    name=os.getenv(
        "SMART_STORE_NAME",
        "스마트스토어",
    ).strip(),
    client_id=get_first_env_value(
        "SMART_STORE_CLIENT_ID",
    ),
    client_secret=get_first_env_value(
        "SMART_STORE_CLIENT_SECRET",
    ),
    enabled=get_env_bool(
        "SMART_STORE_ENABLED",
        default=True,
    ),
)


ALL_STORES = (
    OJE_PLUS_STORE,
    SMART_STORE,
)


DEFAULT_STORE_CODE = os.getenv(
    "DEFAULT_STORE_CODE",
    "OJE_PLUS",
).strip().upper()


def is_store_configured(
    store: StoreConfig,
) -> bool:
    """
    스토어의 인증정보가 모두 등록되어 있는지 확인합니다.
    """

    return bool(
        store.client_id
        and store.client_secret
    )


def get_configured_stores() -> list[StoreConfig]:
    """
    활성화되어 있고 인증정보가 등록된 스토어만 반환합니다.

    스마트스토어 인증정보가 아직 없다면 오제플러스만 반환됩니다.
    """

    return [
        store
        for store in ALL_STORES
        if store.enabled
        and is_store_configured(store)
    ]


def get_store_config(
    store_code: str | None = None,
) -> StoreConfig:
    """
    스토어 코드에 해당하는 설정을 반환합니다.

    store_code가 없으면 기본 스토어를 반환합니다.
    """

    target_code = (
        store_code or DEFAULT_STORE_CODE
    ).strip().upper()

    for store in ALL_STORES:
        if store.code == target_code:
            return store

    available_codes = ", ".join(
        store.code
        for store in ALL_STORES
    )

    raise ValueError(
        f"등록되지 않은 스토어 코드입니다: "
        f"{target_code}\n"
        f"사용 가능한 코드: {available_codes}"
    )


def validate_store_settings(
    store: StoreConfig,
) -> None:
    """
    지정한 스토어의 설정을 검사합니다.
    """

    if not store.enabled:
        raise ValueError(
            f"{store.name} 스토어가 "
            f"비활성화되어 있습니다."
        )

    store.validate()


def validate_settings() -> None:
    """
    기본 스토어의 필수 환경변수를 확인합니다.

    기존 코드에서 사용하던 validate_settings()와의
    호환성을 유지합니다.
    """

    default_store = get_store_config()

    validate_store_settings(default_store)


# 기존 API 모듈과의 호환성을 유지하기 위한 값입니다.
# 기존 코드가 CLIENT_ID와 CLIENT_SECRET을 직접 가져와도
# 기본 스토어인 오제플러스 설정을 사용합니다.
CLIENT_ID = OJE_PLUS_STORE.client_id
CLIENT_SECRET = OJE_PLUS_STORE.client_secret
