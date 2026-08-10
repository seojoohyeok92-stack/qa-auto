from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")
UTC_ZONE = ZoneInfo("UTC")
KST_DISPLAY_FORMAT = "%Y-%m-%d %H:%M:%S"


def parse_datetime_aware(
    value: Any,
    *,
    naive_timezone: ZoneInfo = UTC_ZONE,
) -> datetime | None:
    """Parse an ISO value as aware datetime; legacy naive values mean UTC."""

    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            text = str(value).strip()
            if not text:
                return None
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=naive_timezone)
    return parsed


def to_kst(value: Any) -> datetime | None:
    parsed = parse_datetime_aware(value)
    return parsed.astimezone(KST) if parsed is not None else None


def format_datetime_kst(
    value: Any,
    *,
    empty: str = "-",
    include_timezone: bool = False,
) -> str:
    parsed = to_kst(value)
    if parsed is None:
        return empty
    rendered = parsed.strftime(KST_DISPLAY_FORMAT)
    return f"{rendered} KST" if include_timezone else rendered


def format_datetime_minute_kst(value: Any, *, empty: str = "-") -> str:
    """Compact Dashboard timestamp without changing stored timezone data."""
    parsed = to_kst(value)
    return parsed.strftime("%Y-%m-%d %H:%M") if parsed is not None else empty


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")
