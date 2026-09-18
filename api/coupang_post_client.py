"""Answer one Coupang online inquiry, and nothing else.

The read client next door is deliberately GET-only, so this is where the one
documented write lives:

    POST /v2/providers/openapi/apis/api/v4/vendors/{vendorId}
         /onlineInquiries/{inquiryId}/replies

with ``{"content", "vendorId", "replyBy"}`` and a ``{"code": "200"}`` body on
success.  ``replyBy`` is the Seller Portal (WING) account id, which Coupang
checks against ``vendorId``; the two must come from the same seller account,
which is why the caller passes a resolved account rather than loose strings.

Authorization reuses ``build_authorization`` from the read client unchanged --
it already takes the method, so signing a POST needs no new scheme.

No retries.  A reply that may or may not have been accepted must be looked at
by a person, not sent again: Coupang rejects a duplicate reply, and a retry
that races the first attempt would turn one answer into two.  The caller
records the attempt either way.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

import requests

from api.coupang_read_client import COUPANG_API_BASE_URL, build_authorization

REPLY_PATH = (
    "/v2/providers/openapi/apis/api/v4/vendors/{vendor_id}"
    "/onlineInquiries/{inquiry_id}/replies"
)

# What the documented 400s mean, kept as codes so the screen and the attempt
# row say the same thing.  The text Coupang returns is recorded beside them.
REPLY_ERROR_HINTS: dict[str, str] = {
    "REPLY_BY_INVALID": "replyBy(WING ID)가 해당 vendorId와 일치하지 않습니다.",
    "INQUIRY_DELETED": "삭제된 문의에는 답변할 수 없습니다.",
    "CONTENT_REQUIRED": "답변 내용이 비어 있습니다.",
    "DUPLICATE_REPLY": "이미 동일한 답변이 등록되어 있습니다.",
    "CONTENT_NOT_PARSABLE": "답변 본문에 JSON으로 보낼 수 없는 제어문자가 있습니다.",
}


class CoupangPostError(RuntimeError):
    """A reply that did not succeed, with the outcome named."""

    def __init__(
        self,
        code: str,
        *,
        http_status: int | None = None,
        message: str = "",
        retryable: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status
        self.message = message
        self.retryable = retryable

    def __str__(self) -> str:
        return f"{self.code}: {self.message}" if self.message else self.code


@dataclass(frozen=True)
class CoupangReplyResult:
    http_status: int
    code: str
    message: str


def build_reply_path(*, vendor_id: object, inquiry_id: object) -> str:
    """The documented reply path for one inquiry.

    Both ids are required and are not guessed from each other: an empty one
    would otherwise produce a syntactically valid path addressing nothing.
    """

    vendor = str(vendor_id or "").strip()
    inquiry = str(inquiry_id or "").strip()
    if not vendor:
        raise CoupangPostError("VENDOR_ID_REQUIRED", message="vendorId가 없습니다.")
    if not inquiry:
        raise CoupangPostError("INQUIRY_ID_REQUIRED", message="inquiryId가 없습니다.")
    return REPLY_PATH.format(vendor_id=vendor, inquiry_id=inquiry)


def build_reply_payload(
    *, content: object, vendor_id: object, reply_by: object
) -> dict[str, str]:
    """The documented request body, with every field required."""

    body = str(content or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    vendor = str(vendor_id or "").strip()
    replier = str(reply_by or "").strip()
    if not body:
        raise CoupangPostError("CONTENT_REQUIRED", message=REPLY_ERROR_HINTS["CONTENT_REQUIRED"])
    if not vendor:
        raise CoupangPostError("VENDOR_ID_REQUIRED", message="vendorId가 없습니다.")
    if not replier:
        raise CoupangPostError(
            "WING_ID_REQUIRED",
            message="replyBy(WING ID)가 설정되지 않았습니다.",
        )
    return {"content": body, "vendorId": vendor, "replyBy": replier}


def _classify(status: int, payload: Mapping[str, Any] | None, text: str) -> str:
    """Name the documented failure, from the response rather than a guess."""

    message = str((payload or {}).get("message") or text or "")
    lowered = message.lower()
    if status == 400:
        if "replyby" in lowered or "wing" in lowered:
            return "REPLY_BY_INVALID"
        if "delet" in lowered or "삭제" in message:
            return "INQUIRY_DELETED"
        if "duplicate" in lowered or "중복" in message or "이미" in message:
            return "DUPLICATE_REPLY"
        if "content" in lowered or "내용" in message:
            return "CONTENT_REQUIRED"
        if "json" in lowered or "parse" in lowered:
            return "CONTENT_NOT_PARSABLE"
        return "BAD_REQUEST"
    if status in {401, 403}:
        return "AUTH_FAILED"
    if status == 404:
        return "INQUIRY_NOT_FOUND"
    if status == 429:
        return "RATE_LIMITED"
    if status >= 500:
        return "SERVER_ERROR"
    return f"HTTP_{status}"


class CoupangPostClient:
    """One request, one answer, no retry."""

    def __init__(
        self,
        *,
        access_key: str,
        secret_key: str,
        transport: Any | None = None,
        timeout: tuple[float, float] = (5.0, 15.0),
        now: Any | None = None,
    ) -> None:
        self.access_key = str(access_key or "").strip()
        self.secret_key = str(secret_key or "").strip()
        self.transport = transport or requests
        self.timeout = timeout
        self.now = now

    def reply(
        self,
        *,
        vendor_id: str,
        inquiry_id: str,
        content: str,
        reply_by: str,
    ) -> CoupangReplyResult:
        if not self.access_key or not self.secret_key:
            raise CoupangPostError(
                "COUPANG_CREDENTIALS_MISSING",
                message="Coupang API 키가 설정되지 않았습니다.",
            )
        path = build_reply_path(vendor_id=vendor_id, inquiry_id=inquiry_id)
        payload = build_reply_payload(
            content=content, vendor_id=vendor_id, reply_by=reply_by
        )
        authorization = build_authorization(
            "POST", path, "", self.access_key, self.secret_key,
            *( (self.now(),) if callable(self.now) else () ),
        )
        try:
            response = self.transport.request(
                "POST",
                f"{COUPANG_API_BASE_URL}{path}",
                headers={
                    "Content-Type": "application/json;charset=UTF-8",
                    "Authorization": authorization,
                },
                # ``json.dumps`` rather than ``json=`` so the escaping Coupang
                # documents as a 400 cause is done once, here, and the body
                # that was signed is the body that is sent.
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                timeout=self.timeout,
            )
        except (requests.RequestException, OSError, TimeoutError) as error:
            # Whether Coupang received it is unknown, so this is never retried
            # automatically; the caller records POST_UNKNOWN for a person.
            raise CoupangPostError(
                "NETWORK_ERROR", message=str(error)[:200], retryable=False
            ) from error
        status = int(response.status_code)
        text = ""
        body: dict[str, Any] | None = None
        try:
            text = response.text or ""
            parsed = response.json()
            body = parsed if isinstance(parsed, dict) else None
        except (ValueError, AttributeError):
            body = None
        if status == 200 and str((body or {}).get("code") or "200") in {"200", "OK"}:
            return CoupangReplyResult(
                http_status=status,
                code=str((body or {}).get("code") or "200"),
                message=str((body or {}).get("message") or "OK"),
            )
        code = _classify(status, body, text)
        raise CoupangPostError(
            code,
            http_status=status,
            message=REPLY_ERROR_HINTS.get(code)
            or str((body or {}).get("message") or text or "")[:200],
        )
