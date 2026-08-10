from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

from services.naver_post_payload_builder import NaverPostPayload


@dataclass(frozen=True)
class NaverAnswerResponse:
    http_status: int
    response_id: str | None = None
    response_code: str | None = None


class NaverAnswerClientError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        http_status: int | None = None,
        retryable: bool = False,
        uncertain: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status
        self.retryable = retryable
        self.uncertain = uncertain


class NaverAlreadyAnsweredError(NaverAnswerClientError):
    pass


class NaverAnswerClient:
    def __init__(
        self,
        *,
        session: Any = requests,
        timeout: tuple[float, float] = (5.0, 20.0),
    ) -> None:
        self.session = session
        self.timeout = timeout

    @staticmethod
    def _safe_json(response: Any) -> dict[str, Any]:
        try:
            value = response.json()
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def send(
        self,
        request: NaverPostPayload,
        *,
        access_token: str,
    ) -> NaverAnswerResponse:
        try:
            response = self.session.request(
                request.method,
                request.endpoint,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json=request.payload,
                timeout=self.timeout,
            )
        except (requests.Timeout, TimeoutError) as error:
            raise NaverAnswerClientError(
                "API_TIMEOUT",
                retryable=False,
                uncertain=True,
            ) from error
        except requests.RequestException as error:
            raise NaverAnswerClientError(
                "NETWORK_ERROR",
                retryable=True,
                uncertain=False,
            ) from error

        status = int(response.status_code)
        body = self._safe_json(response)
        code = str(body.get("code") or "") or None
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        response_id = next(
            (
                str(value)
                for value in (
                    data.get("inquiryCommentNo"),
                    data.get("answerContentId"),
                    body.get("traceId"),
                )
                if value not in (None, "")
            ),
            None,
        )
        success_statuses = {int(request.expected_success_status)}
        if status in success_statuses:
            return NaverAnswerResponse(status, response_id, code)
        if code == "ERR-NC-101010":
            raise NaverAlreadyAnsweredError(
                "ALREADY_ANSWERED", http_status=status
            )
        if status == 401:
            error_code = "AUTH_FAILED"
        elif status == 403:
            error_code = "PERMISSION_DENIED"
        elif status == 429:
            error_code = "RATE_LIMITED"
        elif 500 <= status < 600:
            error_code = "NAVER_SERVER_ERROR"
        elif status == 404 and request.target_verified:
            error_code = "REMOTE_TARGET_NOT_FOUND"
        elif status in {400, 404}:
            error_code = code or "INVALID_REQUEST"
        else:
            error_code = code or "API_RESPONSE_INVALID"
        raise NaverAnswerClientError(
            error_code,
            http_status=status,
            retryable=status == 429 or 500 <= status < 600,
            uncertain=False,
        )
