from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LearningManagerPage:
    rows: list[dict[str, Any]]
    total: int
    page: int
    page_size: int


def normalize_manager_search(value: object) -> tuple[str, str]:
    """Return the ordinary and whitespace-insensitive search forms."""

    ordinary = str(value or "").strip().lower()
    compact = re.sub(r"\s+", "", ordinary)
    return ordinary, compact


def manager_search_matches(query: object, searchable_text: object) -> bool:
    ordinary, compact = normalize_manager_search(query)
    if not ordinary:
        return True
    haystack, compact_haystack = normalize_manager_search(searchable_text)
    return ordinary in haystack or bool(compact and compact in compact_haystack)


def manager_search_sql(search_expression: str, query: object) -> tuple[str, list[str]]:
    """Build a bound LIKE predicate without interpolating user input."""

    ordinary, compact = normalize_manager_search(query)
    if not ordinary:
        return "", []
    compact_expression = search_expression
    for whitespace in ("' '", "char(9)", "char(10)", "char(13)"):
        compact_expression = f"replace({compact_expression}, {whitespace}, '')"
    return (
        f"(lower({search_expression}) LIKE ? ESCAPE '\\' "
        f"OR lower({compact_expression}) LIKE ? ESCAPE '\\')",
        [f"%{_escape_like(ordinary)}%", f"%{_escape_like(compact)}%"],
    )


def manager_page_bounds(*, total: int, page: int, page_size: int) -> tuple[int, int, int]:
    safe_size = max(1, min(int(page_size), 100))
    total_pages = max(1, (max(0, int(total)) + safe_size - 1) // safe_size)
    safe_page = min(max(1, int(page)), total_pages)
    return safe_page, safe_size, (safe_page - 1) * safe_size


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
