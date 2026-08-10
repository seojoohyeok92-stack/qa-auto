from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

import requests


ERROR_MESSAGES = {
    "AUTH_FAILED": "네이버 인증 정보를 다시 확인해주세요.",
    "TOKEN_FAILED": "네이버 액세스 토큰을 발급하지 못했습니다.",
    "PERMISSION_DENIED": "네이버 문의 조회 권한을 확인해주세요.",
    "RATE_LIMITED": "네이버 API 요청 한도를 초과했습니다. 잠시 후 다시 시도해주세요.",
    "API_TIMEOUT": "네이버 문의 조회 시간이 초과되었습니다.",
    "API_RESPONSE_INVALID": "네이버 문의 응답 형식을 확인할 수 없습니다.",
    "PAGINATION_FAILED": "네이버 문의 페이지 조회를 완료하지 못했습니다.",
    "INVALID_DATE_RANGE": "문의 조회 기간이 올바르지 않습니다.",
    "NORMALIZATION_FAILED": "일부 문의 데이터를 변환하지 못했습니다.",
    "DB_WRITE_FAILED": "문의 데이터를 로컬 저장소에 반영하지 못했습니다.",
    "SYNC_IN_PROGRESS": "동일 스토어의 문의 동기화가 이미 진행 중입니다.",
    "LOCK_FAILED": "동기화 잠금 처리 중 오류가 발생했습니다.",
    "MAX_RUNTIME_EXCEEDED": "문의 동기화 최대 실행 시간을 초과했습니다.",
    "UNKNOWN_ERROR": "네이버 문의 동기화 중 오류가 발생했습니다.",
}


@dataclass
class NaverSyncError(RuntimeError):
    code: str
    user_message: str
    status_code: int | None = None
    endpoint: str | None = None
    retryable: bool = False

    def __str__(self) -> str:
        return self.user_message


def classified_error(
    code: str,
    *,
    status_code: int | None = None,
    endpoint: str | None = None,
    retryable: bool = False,
) -> NaverSyncError:
    return NaverSyncError(
        code=code,
        user_message=ERROR_MESSAGES.get(code, ERROR_MESSAGES["UNKNOWN_ERROR"]),
        status_code=status_code,
        endpoint=endpoint,
        retryable=retryable,
    )


def request_json(
    method: str,
    url: str,
    *,
    session: Any = requests,
    timeout: tuple[float, float] = (5.0, 20.0),
    max_retries: int = 2,
    backoff_seconds: float = 0.5,
    sleeper: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> dict[str, Any]:
    """Execute a bounded read/token request without exposing response secrets."""

    attempts = max(0, int(max_retries)) + 1
    last_error: NaverSyncError | None = None
    for attempt in range(attempts):
        try:
            response = session.request(
                method,
                url,
                timeout=timeout,
                **kwargs,
            )
        except (requests.Timeout, TimeoutError):
            last_error = classified_error(
                "API_TIMEOUT", endpoint=url, retryable=True
            )
        except requests.RequestException:
            last_error = classified_error(
                "UNKNOWN_ERROR", endpoint=url, retryable=True
            )
        else:
            status = int(response.status_code)
            if status in {200, 201}:
                try:
                    payload = response.json()
                except (TypeError, ValueError) as error:
                    raise classified_error(
                        "API_RESPONSE_INVALID",
                        status_code=status,
                        endpoint=url,
                    ) from error
                if not isinstance(payload, dict):
                    raise classified_error(
                        "API_RESPONSE_INVALID",
                        status_code=status,
                        endpoint=url,
                    )
                return payload
            if status == 401:
                raise classified_error(
                    "AUTH_FAILED", status_code=status, endpoint=url
                )
            if status == 403:
                raise classified_error(
                    "PERMISSION_DENIED", status_code=status, endpoint=url
                )
            if status == 429:
                last_error = classified_error(
                    "RATE_LIMITED",
                    status_code=status,
                    endpoint=url,
                    retryable=True,
                )
            elif 500 <= status < 600:
                last_error = classified_error(
                    "UNKNOWN_ERROR",
                    status_code=status,
                    endpoint=url,
                    retryable=True,
                )
            else:
                raise classified_error(
                    "API_RESPONSE_INVALID",
                    status_code=status,
                    endpoint=url,
                )

        if attempt + 1 < attempts and last_error and last_error.retryable:
            sleeper(max(0.0, backoff_seconds) * (2**attempt))
            continue
        break
    raise last_error or classified_error("UNKNOWN_ERROR", endpoint=url)
