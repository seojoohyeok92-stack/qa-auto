from __future__ import annotations

import calendar
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any


DATE_SOURCE_PRIORITY: tuple[tuple[str, ...], ...] = (
    ("order_date", "order_created_at"),
    ("payment_date", "payment_completed_at"),
    ("place_order_date",),
    ("shipping_due_date",),
)


@dataclass(frozen=True, slots=True)
class DpsDateSource:
    source: str | None
    reference_date: date | None
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["reference_date"] = (
            self.reference_date.isoformat()
            if self.reference_date is not None
            else None
        )
        value["warnings"] = list(self.warnings)
        return value


@dataclass(frozen=True, slots=True)
class DpsLookupPeriod:
    reference_date: date
    start: date
    end: date
    effective_reference_date: date
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "dps_reference_date": self.reference_date.isoformat(),
            "dps_period_start": self.start.isoformat(),
            "dps_period_end": self.end.isoformat(),
            "effective_reference_date": (
                self.effective_reference_date.isoformat()
            ),
            "warnings": list(self.warnings),
        }


def parse_date_value(value: Any) -> date | None:
    """Parse Naver/DPS date values without lossy numeric conversion."""

    if value is None or isinstance(value, (Mapping, list, tuple, set)):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value).strip()
    if not text:
        return None
    iso_candidate = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso_candidate).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    separated = re.fullmatch(
        r"\s*(\d{4})\D+(\d{1,2})\D+(\d{1,2})(?:\D.*)?",
        text,
    )
    if separated:
        try:
            return date(
                int(separated.group(1)),
                int(separated.group(2)),
                int(separated.group(3)),
            )
        except ValueError:
            return None

    digits = re.sub(r"\D", "", text)
    if len(digits) >= 8:
        try:
            return date(
                int(digits[:4]),
                int(digits[4:6]),
                int(digits[6:8]),
            )
        except ValueError:
            return None
    return None


def select_dps_date_source(
    order: Mapping[str, Any] | None,
) -> DpsDateSource:
    """Select the first valid date using the shared DPS source priority."""

    if not isinstance(order, Mapping):
        return DpsDateSource(None, None, ("DATE_SOURCE_MISSING",))
    warnings: list[str] = []
    for aliases in DATE_SOURCE_PRIORITY:
        for field in aliases:
            raw_value = order.get(field)
            if raw_value in (None, ""):
                continue
            parsed = parse_date_value(raw_value)
            if parsed is not None:
                return DpsDateSource(field, parsed, tuple(warnings))
            warnings.append(f"{field}:INVALID_DATE")
    warnings.append("DATE_SOURCE_MISSING")
    return DpsDateSource(None, None, tuple(warnings))


def calculate_dps_lookup_period(
    reference_date: Any,
    today: date | datetime | None = None,
) -> DpsLookupPeriod:
    """Calculate one DPS month: month start to month end, or today."""

    parsed_reference = parse_date_value(reference_date)
    if parsed_reference is None:
        raise ValueError("DATE_SOURCE_MISSING")
    if isinstance(today, datetime):
        current_date = today.date()
    elif isinstance(today, date):
        current_date = today
    else:
        current_date = datetime.now().astimezone().date()

    warnings: list[str] = []
    effective_reference = parsed_reference
    if parsed_reference > current_date:
        # A future Naver timestamp is abnormal. Clamp the effective month to
        # today so the automation never selects a future end date.
        effective_reference = current_date
        warnings.append("REFERENCE_DATE_IN_FUTURE_CLAMPED_TO_TODAY")

    start = effective_reference.replace(day=1)
    last_day = calendar.monthrange(
        effective_reference.year,
        effective_reference.month,
    )[1]
    end = effective_reference.replace(day=last_day)
    if (
        effective_reference.year == current_date.year
        and effective_reference.month == current_date.month
    ):
        end = current_date
    return DpsLookupPeriod(
        reference_date=parsed_reference,
        start=start,
        end=end,
        effective_reference_date=effective_reference,
        warnings=tuple(warnings),
    )


def validate_dps_lookup_period(
    start_value: Any,
    end_value: Any,
    *,
    today: date | datetime | None = None,
) -> tuple[bool, str, date | None, date | None]:
    start = parse_date_value(start_value)
    end = parse_date_value(end_value)
    if start is None:
        return False, "DATE_START_VERIFY_FAILED", start, end
    if end is None:
        return False, "DATE_END_VERIFY_FAILED", start, end
    if isinstance(today, datetime):
        current_date = today.date()
    elif isinstance(today, date):
        current_date = today
    else:
        current_date = datetime.now().astimezone().date()
    if (
        start > end
        or start.year != end.year
        or start.month != end.month
        or end > current_date
    ):
        return False, "DATE_RANGE_INVALID", start, end
    return True, "DATE_RANGE_READY", start, end
