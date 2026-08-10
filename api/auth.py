import base64
import time

import bcrypt

from api.naver_read_client import NaverSyncError, classified_error, request_json
from config import (
    TOKEN_URL,
    StoreConfig,
    get_store_config,
    validate_store_settings,
)


def create_client_secret_sign(
    timestamp: str,
    store: StoreConfig | None = None,
) -> str:
    """
    지정한 스토어의 네이버 인증 전자서명을 만듭니다.

    store를 전달하지 않으면 기본 스토어를 사용합니다.
    """

    target_store = (
        store
        if store is not None
        else get_store_config()
    )

    validate_store_settings(target_store)

    password = (
        f"{target_store.client_id}_{timestamp}"
    ).encode("utf-8")

    salt = target_store.client_secret.encode(
        "utf-8"
    )

    hashed_password = bcrypt.hashpw(
        password,
        salt,
    )

    return base64.b64encode(
        hashed_password
    ).decode("utf-8")


def get_access_token(
    store: StoreConfig | None = None,
    store_code: str | None = None,
    *,
    timeout: tuple[float, float] = (5.0, 20.0),
    max_retries: int = 1,
    backoff_seconds: float = 0.5,
    session=None,
    sleeper=None,
) -> str:
    """
    지정한 스토어의 네이버 커머스 API 토큰을 발급받습니다.

    사용 예시:

    기존 방식:
        token = get_access_token()

    스토어 설정 전달:
        token = get_access_token(store=store)

    스토어 코드 전달:
        token = get_access_token(
            store_code="SMART_STORE"
        )
    """

    if store is not None and store_code is not None:
        raise ValueError(
            "store와 store_code는 동시에 "
            "전달할 수 없습니다."
        )

    target_store = (
        store
        if store is not None
        else get_store_config(store_code)
    )

    validate_store_settings(target_store)

    timestamp = str(
        int(time.time() * 1000)
    )

    request_data = {
        "client_id": target_store.client_id,
        "timestamp": timestamp,
        "client_secret_sign": (
            create_client_secret_sign(
                timestamp=timestamp,
                store=target_store,
            )
        ),
        "grant_type": "client_credentials",
        "type": "SELF",
    }

    try:
        request_kwargs = {}
        if session is not None:
            request_kwargs["session"] = session
        if sleeper is not None:
            request_kwargs["sleeper"] = sleeper
        response_data = request_json(
            "POST",
            TOKEN_URL,
            data=request_data,
            timeout=timeout,
            max_retries=max_retries,
            backoff_seconds=backoff_seconds,
            **request_kwargs,
        )
    except NaverSyncError as error:
        raise classified_error(
            "TOKEN_FAILED",
            status_code=error.status_code,
            endpoint=TOKEN_URL,
        ) from error

    access_token = response_data.get(
        "access_token"
    )

    if not access_token:
        raise classified_error("TOKEN_FAILED", endpoint=TOKEN_URL)

    return access_token


def get_store_access_tokens() -> dict[str, str]:
    """
    설정된 모든 스토어의 토큰을 발급합니다.

    반환 예시:
    {
        "OJE_PLUS": "발급된 토큰",
        "SMART_STORE": "발급된 토큰",
    }
    """

    from config import get_configured_stores

    tokens: dict[str, str] = {}

    for store in get_configured_stores():
        tokens[store.code] = get_access_token(
            store=store
        )

    return tokens
