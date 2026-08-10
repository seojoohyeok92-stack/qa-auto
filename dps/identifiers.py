from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class DpsQueryIdentifier:
    value: str | None
    type: str | None
    fallback_used: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _text_identifier(value: Any) -> str | None:
    """Keep identifiers as text; never coerce through an integer type."""

    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    text = str(value).strip()
    return text or None


def select_dps_query_identifier(
    order_id: Any = None,
    product_order_id: Any = None,
) -> DpsQueryIdentifier:
    """Select the only identifier accepted by DPS: the Naver order id.

    ``product_order_id`` is intentionally retained in the signature for API
    compatibility, but is display metadata and is never a DPS fallback.
    """

    order_value = _text_identifier(order_id)
    if order_value:
        return DpsQueryIdentifier(order_value, "order_id", False)

    return DpsQueryIdentifier(None, None, False, "DPS_ORDER_ID_MISSING")
