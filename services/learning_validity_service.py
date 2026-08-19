from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any, Mapping

from core.time_utils import KST, parse_datetime_aware


VALIDITY_TYPES = ("PERMANENT", "TEMPORARY")
VALIDITY_STATUSES = ("ACTIVE", "SCHEDULED", "EXPIRED", "DISABLED")


def _metadata_bool(value: object, *, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(value)


def validity_type_for(learning: Mapping[str, Any]) -> str:
    value = str(learning.get("validity_type") or "PERMANENT").upper()
    return value if value in VALIDITY_TYPES else "PERMANENT"


def _boundary(value: object, *, end_of_day: bool = False) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        local_time = time.max if end_of_day else time.min
        return datetime.combine(value, local_time, tzinfo=KST)
    text = str(value).strip()
    if len(text) == 10:
        try:
            parsed_date = date.fromisoformat(text)
        except ValueError:
            return None
        local_time = time.max if end_of_day else time.min
        return datetime.combine(parsed_date, local_time, tzinfo=KST)
    parsed = parse_datetime_aware(value)
    return parsed.astimezone(KST) if parsed is not None else None


def validity_status(
    learning: Mapping[str, Any], *, now: datetime | None = None
) -> str:
    current = now or datetime.now(KST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=KST)
    else:
        current = current.astimezone(KST)
    if not _metadata_bool(learning.get("active"), default=True):
        return "DISABLED"
    if not _metadata_bool(learning.get("validity_active"), default=True):
        return "DISABLED"
    if validity_type_for(learning) == "PERMANENT":
        return "ACTIVE"
    valid_from = _boundary(learning.get("valid_from"))
    valid_until = _boundary(learning.get("valid_until"), end_of_day=True)
    # An incomplete TEMPORARY policy must never enter a new answer context.
    if valid_from is None or valid_until is None or valid_until < valid_from:
        return "DISABLED"
    if current < valid_from:
        return "SCHEDULED"
    if current > valid_until:
        return "EXPIRED"
    return "ACTIVE"


def is_learning_usable(
    learning: Mapping[str, Any], *, now: datetime | None = None
) -> bool:
    return validity_status(learning, now=now) == "ACTIVE"


def validity_summary(
    learning: Mapping[str, Any], *, now: datetime | None = None
) -> str:
    if validity_type_for(learning) == "PERMANENT":
        return "영구" if validity_status(learning, now=now) == "ACTIVE" else "수동 비활성"
    return {
        "ACTIVE": "기간성 · 활성",
        "SCHEDULED": "기간성 · 시작 전",
        "EXPIRED": "기간성 · 만료",
        "DISABLED": "수동 비활성",
    }[validity_status(learning, now=now)]


def normalize_validity_update(
    *,
    validity_type: str,
    event_name: str | None = None,
    valid_from: object = None,
    valid_until: object = None,
    validity_active: bool = True,
    validity_note: str | None = None,
    condition: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_type = str(validity_type or "").upper()
    if normalized_type not in VALIDITY_TYPES:
        raise ValueError("학습 유효성은 PERMANENT 또는 TEMPORARY여야 합니다.")
    if normalized_type == "PERMANENT":
        return {
            "validity_type": "PERMANENT",
            "event_name": None,
            "valid_from": None,
            "valid_until": None,
            "validity_active": bool(validity_active),
            "validity_note": str(validity_note or "").strip()[:2_000] or None,
            "condition_json": dict(condition or {}),
        }
    clean_event = str(event_name or "").strip()
    start = _boundary(valid_from)
    end = _boundary(valid_until, end_of_day=True)
    if not clean_event:
        raise ValueError("기간성 Learning의 이벤트명을 입력해 주세요.")
    if start is None or end is None:
        raise ValueError("기간성 Learning의 유효 시작일과 종료일을 입력해 주세요.")
    if end < start:
        raise ValueError("유효 종료일은 시작일보다 빠를 수 없습니다.")
    return {
        "validity_type": "TEMPORARY",
        "event_name": clean_event[:200],
        "valid_from": start.astimezone(UTC).isoformat(timespec="milliseconds"),
        "valid_until": end.astimezone(UTC).isoformat(timespec="milliseconds"),
        "validity_active": bool(validity_active),
        "validity_note": str(validity_note or "").strip()[:2_000] or None,
        "condition_json": dict(condition or {}),
    }
