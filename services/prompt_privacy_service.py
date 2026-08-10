from __future__ import annotations

import re
from typing import Any

from answer.governance_models import PrivacySanitizationResult
from answer.prompt_builder import ADDRESS_PATTERN, EMAIL_ANY_PATTERN
from answer.text_utils import PHONE_PATTERN


SENSITIVE_KEYS = {
    "order_id",
    "product_order_id",
    "inquiry_id",
    "question_id",
    "customer_id",
    "customer_display",
    "customer_name",
    "phone",
    "email",
    "address",
    "sales_number",
    "otp",
    "password",
    "api_key",
    "access_token",
    "refresh_token",
    "authorization",
    "cookie",
    "session",
}
ORDER_PATTERN = re.compile(r"(?<!\d)\d{12,}(?!\d)")
SECRET_PATTERN = re.compile(
    r"(?i)\b(otp|password|api[_ -]?key|token|access[_ -]?token|"
    r"refresh[_ -]?token|authorization|cookie|session)"
    r"\s*[:=]\s*\S+"
)
INTERNAL_URL_PATTERN = re.compile(
    r"(?i)\b(?:https?://)?(?:localhost|127\.0\.0\.1|10\.\d+\.\d+\.\d+|"
    r"192\.168\.\d+\.\d+|[\w.-]+\.internal)(?::\d+)?(?:/\S*)?"
)
WINDOWS_PATH_PATTERN = re.compile(r"(?i)\b[A-Z]:\\(?:[^\\\r\n]+\\)*[^\\\r\n]*")


class PromptPrivacyService:
    def sanitize(self, payload: Any) -> PrivacySanitizationResult:
        removed: list[str] = []
        masked: list[str] = []
        blocking: list[str] = []

        def visit(value: Any, path: str = "") -> Any:
            if isinstance(value, dict):
                result: dict[str, Any] = {}
                for key, item in value.items():
                    key_text = str(key)
                    key_path = f"{path}.{key_text}".strip(".")
                    if key_text.strip().lower() in SENSITIVE_KEYS:
                        removed.append(key_path)
                        if key_text.strip().lower() in {
                            "otp",
                            "password",
                            "api_key",
                            "access_token",
                            "refresh_token",
                            "authorization",
                            "cookie",
                            "session",
                        }:
                            blocking.append(f"민감 인증 필드: {key_path}")
                        continue
                    result[key_text] = visit(item, key_path)
                return result
            if isinstance(value, (list, tuple)):
                return [visit(item, f"{path}[]") for item in value]
            if not isinstance(value, str):
                return value
            text = value
            substitutions = (
                (EMAIL_ANY_PATTERN, "<masked-email>", "email"),
                (PHONE_PATTERN, "<masked-phone>", "phone"),
                (ADDRESS_PATTERN, "<masked-address>", "address"),
                (ORDER_PATTERN, "<masked-order>", "order-number"),
            )
            for pattern, replacement, label in substitutions:
                if pattern.search(text):
                    masked.append(label)
                    text = pattern.sub(replacement, text)
            if SECRET_PATTERN.search(text):
                masked.append("authentication-secret")
                blocking.append("문의 본문에 인증정보 형태가 포함되어 있습니다.")
                text = SECRET_PATTERN.sub("<masked-secret>", text)
            if INTERNAL_URL_PATTERN.search(text):
                masked.append("internal-url")
                blocking.append("내부 시스템 URL이 포함되어 있습니다.")
                text = INTERNAL_URL_PATTERN.sub("<masked-internal-url>", text)
            if WINDOWS_PATH_PATTERN.search(text):
                masked.append("internal-file-path")
                blocking.append("내부 파일 경로가 포함되어 있습니다.")
                text = WINDOWS_PATH_PATTERN.sub("<masked-file-path>", text)
            return text

        sanitized = visit(payload)
        return PrivacySanitizationResult(
            sanitized_payload=sanitized,
            removed_fields=tuple(dict.fromkeys(removed)),
            masked_patterns=tuple(dict.fromkeys(masked)),
            blocking_issues=tuple(dict.fromkeys(blocking)),
            safe_to_send=not blocking,
        )
