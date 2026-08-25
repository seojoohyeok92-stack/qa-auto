from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, TypedDict

from api.order import (
    OrderNotFoundError,
    get_order_summary,
    get_orders_by_order_id,
    get_product_orders,
)
from services.cache_manager import TTLCache


ORDER_NUMBER_PATTERN = re.compile(r"(?<!\d)\d{16}(?!\d)")
DEFAULT_CACHE_TTL_SECONDS = 300


class OrderLookupResult(TypedDict):
    success: bool
    lookup_number: str | None
    lookup_type: str | None
    orders: list[dict[str, Any]]
    error_code: str | None
    error_message: str | None
    cached: bool
    queried_at: str


@dataclass(frozen=True, slots=True)
class OrderCandidate:
    number: str
    source: str


_order_cache: TTLCache[OrderLookupResult] = TTLCache(
    default_ttl_seconds=DEFAULT_CACHE_TTL_SECONDS
)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _normalize_number(value: Any) -> str | None:
    if value is None:
        return None
    number = re.sub(r"\D", "", str(value))
    return number if len(number) == 16 else None


def _iter_values(value: Any) -> Iterable[Any]:
    if isinstance(value, (list, tuple, set)):
        yield from value
    elif value not in (None, ""):
        yield value


def extract_order_candidates(work_item: dict[str, Any]) -> list[OrderCandidate]:
    """Extract unique 16-digit order candidates from structured fields and text."""

    found: list[OrderCandidate] = []
    seen: set[str] = set()

    def add(value: Any, source: str) -> None:
        number = _normalize_number(value)
        if number and number not in seen:
            seen.add(number)
            found.append(OrderCandidate(number=number, source=source))

    add(work_item.get("order_id"), "order_id")

    for value in _iter_values(work_item.get("product_order_ids")):
        add(value, "product_order_ids")

    orders = work_item.get("orders")
    if isinstance(orders, list):
        for order in orders:
            if not isinstance(order, dict):
                continue
            add(order.get("order_id"), "orders.order_id")
            add(order.get("product_order_id"), "orders.product_order_id")

    for field_name in ("title", "content", "recommended_message"):
        text = work_item.get(field_name)
        if not isinstance(text, str):
            continue
        for match in ORDER_NUMBER_PATTERN.findall(text):
            add(match, field_name)

    return found


def _find_value_recursively(data: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if value not in (None, ""):
                return value
        for value in data.values():
            found = _find_value_recursively(value, keys)
            if found not in (None, ""):
                return found
    elif isinstance(data, list):
        for item in data:
            found = _find_value_recursively(item, keys)
            if found not in (None, ""):
                return found
    return None


def summarize_order(order_info: dict[str, Any]) -> dict[str, Any]:
    """Convert a raw Naver order response into the dashboard's stable schema."""

    summary = get_order_summary(order_info)
    fallback_keys: dict[str, tuple[str, ...]] = {
        "order_id": ("orderId",),
        "order_date": ("orderDate", "orderCreatedAt"),
        "order_created_at": ("orderCreatedAt",),
        "payment_date": ("paymentDate", "paymentCompletedAt"),
        "payment_completed_at": ("paymentCompletedAt",),
        "place_order_date": ("placeOrderDate",),
        "product_order_id": ("productOrderId",),
        "product_name": ("productName",),
        "product_option": ("productOption", "optionManageCode"),
        "quantity": ("quantity",),
        "product_order_status": ("productOrderStatus",),
        "place_order_status": ("placeOrderStatus",),
        "shipping_start_date": ("shippingStartDate",),
        "shipping_due_date": ("shippingDueDate",),
        "receiver_name": ("receiverName", "name"),
        "receiver_tel": ("receiverTel1", "tel1"),
        "base_address": ("baseAddress",),
        "detailed_address": ("detailedAddress",),
        "shipping_memo": ("shippingMemo",),
    }

    for field_name, keys in fallback_keys.items():
        if summary.get(field_name) in (None, ""):
            summary[field_name] = _find_value_recursively(order_info, keys)

    return summary


def _cache_key(store_code: str | None, number: str) -> str:
    return f"{store_code or 'DEFAULT'}:{number}"


def _success_result(
    *,
    number: str,
    lookup_type: str,
    raw_orders: list[dict[str, Any]],
    cached: bool,
) -> OrderLookupResult:
    summaries = [
        summarize_order(order)
        for order in raw_orders
        if isinstance(order, dict)
    ]
    return {
        "success": bool(summaries),
        "lookup_number": number,
        "lookup_type": lookup_type,
        "orders": summaries,
        "error_code": None if summaries else "EMPTY_RESULT",
        "error_message": None if summaries else "주문 조회 결과가 비어 있습니다.",
        "cached": cached,
        "queried_at": _now_iso(),
    }


def lookup_order_number(
    access_token: str,
    number: str,
    *,
    store_code: str | None = None,
    force_refresh: bool = False,
) -> OrderLookupResult:
    """Try a 16-digit number as an order ID, then as a product-order ID."""

    normalized = _normalize_number(number)
    if normalized is None:
        return {
            "success": False,
            "lookup_number": None,
            "lookup_type": None,
            "orders": [],
            "error_code": "INVALID_NUMBER",
            "error_message": "주문번호는 16자리 숫자여야 합니다.",
            "cached": False,
            "queried_at": _now_iso(),
        }

    key = _cache_key(store_code, normalized)
    if not force_refresh:
        cached_result = _order_cache.get(key)
        if cached_result is not None:
            cached_result["cached"] = True
            return cached_result

    errors: list[str] = []

    try:
        raw_orders = get_orders_by_order_id(
            access_token=access_token,
            order_id=normalized,
        )
        result = _success_result(
            number=normalized,
            lookup_type="ORDER_ID",
            raw_orders=raw_orders,
            cached=False,
        )
        if result["success"]:
            _order_cache.set(key, result)
            return result
        errors.append(result["error_message"] or "일반 주문번호 조회 결과 없음")
    except Exception as error:
        errors.append(f"일반 주문번호 조회 실패: {str(error).splitlines()[0][:300]}")

    try:
        raw_orders = get_product_orders(
            access_token=access_token,
            product_order_ids=[normalized],
        )
        result = _success_result(
            number=normalized,
            lookup_type="PRODUCT_ORDER_ID",
            raw_orders=raw_orders,
            cached=False,
        )
        if result["success"]:
            _order_cache.set(key, result)
            return result
        errors.append(result["error_message"] or "상품주문번호 조회 결과 없음")
    except Exception as error:
        errors.append(f"상품주문번호 조회 실패: {str(error).splitlines()[0][:300]}")

    return {
        "success": False,
        "lookup_number": normalized,
        "lookup_type": None,
        "orders": [],
        "error_code": "ORDER_LOOKUP_FAILED",
        "error_message": " / ".join(errors),
        "cached": False,
        "queried_at": _now_iso(),
    }


def lookup_general_order_id(
    access_token: str,
    number: str,
    *,
    store_code: str | None = None,
    force_refresh: bool = False,
) -> OrderLookupResult:
    """Look up a validated general order ID without product-ID fallback."""

    normalized = _normalize_number(number)
    if normalized is None:
        return {
            "success": False,
            "lookup_number": None,
            "lookup_type": None,
            "orders": [],
            "error_code": "INVALID_NUMBER",
            "error_message": "주문번호는 16자리 숫자여야 합니다.",
            "cached": False,
            "queried_at": _now_iso(),
        }
    key = _cache_key(store_code, f"ORDER:{normalized}")
    if not force_refresh:
        cached_result = _order_cache.get(key)
        if cached_result is not None:
            cached_result["cached"] = True
            return cached_result
    try:
        result = _success_result(
            number=normalized,
            lookup_type="ORDER_ID",
            raw_orders=get_orders_by_order_id(
                access_token=access_token,
                order_id=normalized,
            ),
            cached=False,
        )
    except OrderNotFoundError:
        # Naver answered, and its answer was "no such order". That is a fact
        # about the number the customer gave us, not a failure of ours -- and
        # collapsing it into ORDER_LOOKUP_FAILED told staff the API was down
        # every time somebody mistyped an order number.
        return {
            "success": False,
            "lookup_number": normalized,
            "lookup_type": "ORDER_ID",
            "orders": [],
            "error_code": "ORDER_NOT_FOUND",
            "error_message": "일반 주문번호에 해당하는 주문 결과가 없습니다.",
            "cached": False,
            "queried_at": _now_iso(),
        }
    except Exception:
        return {
            "success": False,
            "lookup_number": normalized,
            "lookup_type": "ORDER_ID",
            "orders": [],
            "error_code": "ORDER_LOOKUP_FAILED",
            "error_message": "네이버 주문 조회가 원활하지 않습니다.",
            "cached": False,
            "queried_at": _now_iso(),
        }
    if not result["success"]:
        result["error_code"] = "ORDER_NOT_FOUND"
        result["error_message"] = "일반 주문번호에 해당하는 주문 결과가 없습니다."
        return result
    _order_cache.set(key, result)
    return result


def lookup_work_item_orders(
    access_token: str,
    work_item: dict[str, Any],
    *,
    force_refresh: bool = False,
) -> OrderLookupResult:
    """Look up the first valid order candidate found in a work item."""

    candidates = extract_order_candidates(work_item)
    if not candidates:
        return {
            "success": False,
            "lookup_number": None,
            "lookup_type": None,
            "orders": [],
            "error_code": "NO_ORDER_NUMBER",
            "error_message": "문의에서 16자리 주문번호를 찾지 못했습니다.",
            "cached": False,
            "queried_at": _now_iso(),
        }

    last_result: OrderLookupResult | None = None
    store_code = str(work_item.get("store_code") or "") or None

    for candidate in candidates:
        result = lookup_order_number(
            access_token,
            candidate.number,
            store_code=store_code,
            force_refresh=force_refresh,
        )
        if result["success"]:
            return result
        last_result = result

    assert last_result is not None
    return last_result


def attach_orders_to_work_item(
    work_item: dict[str, Any],
    result: OrderLookupResult,
) -> dict[str, Any]:
    """Return a copied work item with lookup metadata and normalized orders."""

    enriched = dict(work_item)
    enriched["order_lookup"] = dict(result)
    enriched["orders"] = list(result["orders"])

    if result["orders"]:
        first_order = result["orders"][0]
        enriched["order_id"] = first_order.get("order_id")
        enriched["product_order_ids"] = [
            order.get("product_order_id")
            for order in result["orders"]
            if order.get("product_order_id")
        ]

    return enriched


def clear_order_cache() -> None:
    _order_cache.clear()
