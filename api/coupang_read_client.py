"""Read-only Coupang Open API client.

This module deliberately contains no persistence, normalization, answer, or
posting logic.  Every request is a signed GET and is designed to be exercised
through an injected transport in tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
import hashlib
import hmac
import time
from typing import Any, Callable, Mapping
from urllib.parse import urlencode

import requests


COUPANG_API_BASE_URL = "https://api-gateway.coupang.com"
ONLINE_INQUIRIES_PATH = (
    "/v2/providers/openapi/apis/api/v5/vendors/{vendor_id}/onlineInquiries"
)
CONTACT_CENTER_INQUIRIES_PATH = (
    "/v2/providers/openapi/apis/api/v5/vendors/{vendor_id}/callCenterInquiries"
)


ERROR_MESSAGES = {
    "CONFIGURATION_ERROR": "Coupang Open API credentials are not configured.",
    "INVALID_DATE_RANGE": "Coupang inquiry date range must be 7 days or less.",
    "INVALID_PARAMETER": "Coupang inquiry request parameters are invalid.",
    "AUTH_FAILED": "Coupang authentication or HMAC signature failed.",
    "PERMISSION_DENIED": "Coupang permission or IP allowlist denied the request.",
    "RATE_LIMITED": "Coupang API rate limit was reached.",
    "API_SERVER_ERROR": "Coupang API returned a server error.",
    "NETWORK_ERROR": "Coupang API request could not reach the server.",
    "API_RESPONSE_INVALID": "Coupang API response was not a JSON object.",
}


@dataclass
class CoupangReadError(RuntimeError):
    code: str
    status_code: int | None = None
    endpoint: str | None = None
    retryable: bool = False

    def __str__(self) -> str:
        return ERROR_MESSAGES.get(self.code, "Coupang API read failed.")


def serialize_query(parameters: Mapping[str, Any]) -> str:
    """Return the one encoded query string used for both signing and URL."""

    pairs: list[tuple[str, str]] = []
    for key, value in parameters.items():
        if value is None:
            continue
        if isinstance(value, bool):
            encoded = "true" if value else "false"
        else:
            encoded = str(value)
        pairs.append((str(key), encoded))
    return urlencode(sorted(pairs, key=lambda pair: pair[0]))


def _utc_timestamp(now: datetime | None = None) -> str:
    value = now or datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%y%m%dT%H%M%SZ")


def build_authorization(
    method: str,
    path: str,
    query_string: str,
    access_key: str,
    secret_key: str,
    now: datetime | None = None,
) -> str:
    """Build Coupang's documented HMAC-SHA256 Authorization header for GET."""

    timestamp = _utc_timestamp(now)
    message = f"{timestamp}{method.upper()}{path}{query_string}"
    signature = hmac.new(
        secret_key.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return (
        "CEA algorithm=HmacSHA256, "
        f"access-key={access_key}, "
        f"signed-date={timestamp}, signature={signature}"
    )


def _as_date(value: date | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


class CoupangReadClient:
    """Minimal signed GET client for Coupang inquiry endpoints only."""

    def __init__(
        self,
        *,
        access_key: str,
        secret_key: str,
        vendor_id: str,
        transport: Any = requests,
        now: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        max_retries: int = 1,
        retry_backoff_seconds: float = 0.5,
        min_request_interval_seconds: float = 0.25,
        timeout: tuple[float, float] = (5.0, 20.0),
    ) -> None:
        self.access_key = str(access_key or "")
        self.secret_key = str(secret_key or "")
        self.vendor_id = str(vendor_id or "")
        self.transport = transport
        self.now = now or (lambda: datetime.now(UTC))
        self.sleeper = sleeper
        self.monotonic = monotonic
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
        self.min_request_interval_seconds = max(
            0.0, float(min_request_interval_seconds)
        )
        self.timeout = timeout
        self._last_request_at: float | None = None

    def list_online_inquiries(
        self,
        *,
        inquiry_start_at: date | str,
        inquiry_end_at: date | str,
        page_num: int = 1,
        page_size: int = 50,
        answered_type: str = "ALL",
    ) -> dict[str, Any]:
        self._validate_date_range(inquiry_start_at, inquiry_end_at)
        answered = str(answered_type).upper()
        if answered not in {"ALL", "ANSWERED", "NOANSWER"}:
            raise CoupangReadError("INVALID_PARAMETER")
        if not 1 <= int(page_size) <= 50 or int(page_num) < 1:
            raise CoupangReadError("INVALID_PARAMETER")
        path = ONLINE_INQUIRIES_PATH.format(vendor_id=self.vendor_id)
        return self._get(
            path,
            {
                "vendorId": self.vendor_id,
                "answeredType": answered,
                "inquiryStartAt": _as_date(inquiry_start_at).isoformat(),
                "inquiryEndAt": _as_date(inquiry_end_at).isoformat(),
                "pageNum": int(page_num),
                "pageSize": int(page_size),
            },
        )

    def list_contact_center_inquiries(
        self,
        *,
        inquiry_start_at: date | str,
        inquiry_end_at: date | str,
        page_num: int = 1,
        page_size: int = 30,
        partner_counseling_status: str = "NONE",
    ) -> dict[str, Any]:
        self._validate_date_range(inquiry_start_at, inquiry_end_at)
        status = str(partner_counseling_status).upper()
        if status not in {"NONE", "ANSWER", "NO_ANSWER", "TRANSFER"}:
            raise CoupangReadError("INVALID_PARAMETER")
        if not 1 <= int(page_size) <= 30 or int(page_num) < 1:
            raise CoupangReadError("INVALID_PARAMETER")
        path = CONTACT_CENTER_INQUIRIES_PATH.format(vendor_id=self.vendor_id)
        return self._get(
            path,
            {
                "vendorId": self.vendor_id,
                "partnerCounselingStatus": status,
                "inquiryStartAt": _as_date(inquiry_start_at).isoformat(),
                "inquiryEndAt": _as_date(inquiry_end_at).isoformat(),
                "pageNum": int(page_num),
                "pageSize": int(page_size),
            },
        )

    def _validate_date_range(
        self,
        inquiry_start_at: date | str,
        inquiry_end_at: date | str,
    ) -> None:
        start = _as_date(inquiry_start_at)
        end = _as_date(inquiry_end_at)
        if end < start or (end - start).days > 7:
            raise CoupangReadError("INVALID_DATE_RANGE")

    def _require_configuration(self) -> None:
        if not (self.access_key and self.secret_key and self.vendor_id):
            raise CoupangReadError("CONFIGURATION_ERROR")

    def _wait_for_rate_limit(self) -> None:
        if self._last_request_at is None:
            return
        remaining = self.min_request_interval_seconds - (
            self.monotonic() - self._last_request_at
        )
        if remaining > 0:
            self.sleeper(remaining)

    def _get(self, path: str, parameters: Mapping[str, Any]) -> dict[str, Any]:
        self._require_configuration()
        query_string = serialize_query(parameters)
        url = f"{COUPANG_API_BASE_URL}{path}?{query_string}"
        attempts = self.max_retries + 1
        last_error: CoupangReadError | None = None

        for attempt in range(attempts):
            self._wait_for_rate_limit()
            authorization = build_authorization(
                "GET",
                path,
                query_string,
                self.access_key,
                self.secret_key,
                self.now(),
            )
            try:
                response = self.transport.request(
                    "GET",
                    url,
                    headers={
                        "Content-Type": "application/json;charset=UTF-8",
                        "Authorization": authorization,
                    },
                    timeout=self.timeout,
                )
            except (requests.RequestException, OSError, TimeoutError):
                last_error = CoupangReadError(
                    "NETWORK_ERROR", endpoint=path, retryable=True
                )
            else:
                self._last_request_at = self.monotonic()
                status = int(response.status_code)
                if status == 200:
                    try:
                        payload = response.json()
                    except (TypeError, ValueError) as error:
                        raise CoupangReadError(
                            "API_RESPONSE_INVALID", status_code=status, endpoint=path
                        ) from error
                    if not isinstance(payload, dict):
                        raise CoupangReadError(
                            "API_RESPONSE_INVALID", status_code=status, endpoint=path
                        )
                    return payload
                if status == 400:
                    raise CoupangReadError(
                        "INVALID_PARAMETER", status_code=status, endpoint=path
                    )
                if status == 401:
                    raise CoupangReadError(
                        "AUTH_FAILED", status_code=status, endpoint=path
                    )
                if status == 403:
                    raise CoupangReadError(
                        "PERMISSION_DENIED", status_code=status, endpoint=path
                    )
                if status == 429:
                    last_error = CoupangReadError(
                        "RATE_LIMITED", status_code=status, endpoint=path, retryable=True
                    )
                elif 500 <= status < 600:
                    last_error = CoupangReadError(
                        "API_SERVER_ERROR", status_code=status, endpoint=path, retryable=True
                    )
                else:
                    raise CoupangReadError(
                        "API_RESPONSE_INVALID", status_code=status, endpoint=path
                    )

            if last_error is not None and last_error.retryable and attempt + 1 < attempts:
                self.sleeper(self.retry_backoff_seconds * (2**attempt))
                continue
            break
        raise last_error or CoupangReadError("NETWORK_ERROR", endpoint=path)
