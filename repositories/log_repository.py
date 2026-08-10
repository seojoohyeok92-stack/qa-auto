from __future__ import annotations

import json
import re
from typing import Any, Iterable

from repositories.database import Database
from repositories.inquiry_repository import deserialize_json, serialize_json


MAX_MESSAGE_LENGTH = 2_000
MAX_DETAILS_LENGTH = 20_000

EMAIL_PATTERN = re.compile(
    r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])"
)
PHONE_PATTERN = re.compile(
    r"(?<!\d)(?:\+?82[- ]?)?0?1[016789][ -]?\d{3,4}[ -]?\d{4}(?!\d)"
)
LONG_NUMBER_PATTERN = re.compile(r"(?<!\d)(\d{4})\d{4,}(\d{4})(?!\d)")
BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(api[_ -]?key|client[_ -]?secret|access[_ -]?token|"
    r"refresh[_ -]?token|token|authorization|cookie|session|otp|password)\b"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
LABELED_NAME_PATTERN = re.compile(
    r"(?i)(고객명|이름|customer[_ ]?name)(\s*[:=]\s*)([가-힣A-Za-z]{2,20})"
)
ADDRESS_PATTERN = re.compile(
    r"(?:서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|"
    r"전북|전남|경북|경남|제주)[^\n,]{0,50}(?:로|길|동|읍|면)\s*\d+(?:-\d+)?"
)


def mask_sensitive_text(
    value: Any,
    *,
    customer_names: Iterable[str] = (),
) -> str:
    text = str(value)
    text = BEARER_PATTERN.sub("Bearer <masked-token>", text)
    text = SECRET_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<masked-secret>",
        text,
    )
    text = EMAIL_PATTERN.sub("<masked-email>", text)
    text = PHONE_PATTERN.sub("<masked-phone>", text)
    text = LONG_NUMBER_PATTERN.sub(r"\1****\2", text)
    text = ADDRESS_PATTERN.sub("<masked-address>", text)
    text = LABELED_NAME_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<masked-name>",
        text,
    )
    for name in customer_names:
        normalized = str(name).strip()
        if len(normalized) >= 2:
            text = text.replace(normalized, "<masked-name>")
    return text


def mask_sensitive_data(
    value: Any,
    *,
    customer_names: Iterable[str] = (),
) -> Any:
    names = tuple(customer_names)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key).lower().replace("-", "_")
            if any(
                marker in normalized_key
                for marker in (
                    "password",
                    "secret",
                    "token",
                    "authorization",
                    "api_key",
                    "apikey",
                    "otp",
                )
            ):
                result[str(key)] = "<masked-secret>"
            else:
                result[str(key)] = mask_sensitive_data(
                    item,
                    customer_names=names,
                )
        return result
    if isinstance(value, (list, tuple, set)):
        return [
            mask_sensitive_data(item, customer_names=names)
            for item in value
        ]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return mask_sensitive_text(value, customer_names=names)


class LogRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def record(
        self,
        *,
        level: str,
        event_code: str,
        message: Any,
        inquiry_id: int | None = None,
        details: Any = None,
        customer_names: Iterable[str] = (),
    ) -> int:
        masked_message = mask_sensitive_text(
            message,
            customer_names=customer_names,
        )[:MAX_MESSAGE_LENGTH]
        masked_details = mask_sensitive_data(
            details if details is not None else {},
            customer_names=customer_names,
        )
        details_json = serialize_json(masked_details)
        if len(details_json) > MAX_DETAILS_LENGTH:
            details_json = serialize_json(
                {"truncated": True, "preview": details_json[:MAX_DETAILS_LENGTH]}
            )
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO activity_logs (
                    inquiry_id, level, event_code, message, details_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    inquiry_id,
                    str(level).upper()[:20],
                    str(event_code)[:100],
                    masked_message,
                    details_json,
                ),
            )
        return int(cursor.lastrowid)

    def record_inquiry(
        self,
        inquiry_id: int,
        event_code: str,
        message: Any,
        *,
        level: str = "INFO",
        details: Any = None,
        customer_names: Iterable[str] = (),
    ) -> int:
        return self.record(
            inquiry_id=inquiry_id,
            level=level,
            event_code=event_code,
            message=message,
            details=details,
            customer_names=customer_names,
        )

    def record_system(
        self,
        event_code: str,
        message: Any,
        *,
        level: str = "INFO",
        details: Any = None,
        customer_names: Iterable[str] = (),
    ) -> int:
        return self.record(
            level=level,
            event_code=event_code,
            message=message,
            details=details,
            customer_names=customer_names,
        )

    def _recent(
        self,
        *,
        inquiry_id: int | None,
        system_only: bool,
        limit: int,
    ) -> list[dict[str, Any]]:
        if system_only:
            sql = """
                SELECT * FROM activity_logs
                WHERE inquiry_id IS NULL
                ORDER BY created_at DESC, id DESC LIMIT ?
            """
            parameters = (max(1, int(limit)),)
        else:
            sql = """
                SELECT * FROM activity_logs
                WHERE inquiry_id = ?
                ORDER BY created_at DESC, id DESC LIMIT ?
            """
            parameters = (inquiry_id, max(1, int(limit)))
        with self.database.connection() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        results = [dict(row) for row in rows]
        for result in results:
            result["details_json"] = deserialize_json(result["details_json"])
        return results

    def recent_for_inquiry(
        self,
        inquiry_id: int,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return self._recent(
            inquiry_id=inquiry_id,
            system_only=False,
            limit=limit,
        )

    def recent_system(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return self._recent(inquiry_id=None, system_only=True, limit=limit)
