from typing import Any

import requests


API_BASE_URL = "https://api.commerce.naver.com/external"

PRODUCT_ORDER_IDS_URL = (
    API_BASE_URL
    + "/v1/pay-order/seller/orders/{order_id}/product-order-ids"
)

PRODUCT_ORDERS_DETAIL_URL = (
    API_BASE_URL
    + "/v1/pay-order/seller/product-orders/query"
)


def create_headers(access_token: str) -> dict[str, str]:
    """
    네이버 커머스 API 요청에 사용할 공통 헤더를 만듭니다.
    """

    if not access_token:
        raise ValueError("액세스 토큰이 없습니다.")

    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def raise_order_api_error(
    response: requests.Response,
    action: str,
) -> None:
    """
    주문 API 오류를 읽기 쉬운 형태로 변환합니다.
    """

    raise RuntimeError(
        f"{action}에 실패했습니다.\n"
        f"상태 코드: {response.status_code}\n"
        f"응답 내용: {response.text}"
    )


def normalize_product_order_ids(result: Any) -> list[str]:
    """
    상품주문번호 조회 API의 응답에서 상품주문번호 목록을 꺼냅니다.

    API 응답 구조가 리스트이거나 data 안에 들어 있는 경우를
    모두 처리할 수 있도록 작성했습니다.
    """

    if isinstance(result, list):
        return [
            str(item)
            for item in result
            if item is not None
        ]

    if not isinstance(result, dict):
        return []

    possible_values = (
        result.get("data"),
        result.get("productOrderIds"),
    )

    for value in possible_values:
        if isinstance(value, list):
            return [
                str(item)
                for item in value
                if item is not None
            ]

        if isinstance(value, dict):
            nested_ids = value.get("productOrderIds")

            if isinstance(nested_ids, list):
                return [
                    str(item)
                    for item in nested_ids
                    if item is not None
                ]

    return []


def get_product_order_ids(
    access_token: str,
    order_id: str,
) -> list[str]:
    """
    일반 주문번호로 상품주문번호 목록을 조회합니다.
    """

    normalized_order_id = str(order_id).strip()

    if not normalized_order_id:
        raise ValueError("주문번호가 비어 있습니다.")

    if not normalized_order_id.isdigit():
        raise ValueError("주문번호는 숫자만 입력해야 합니다.")

    url = PRODUCT_ORDER_IDS_URL.format(
        order_id=normalized_order_id,
    )

    response = requests.get(
        url,
        headers=create_headers(access_token),
        timeout=20,
    )

    if response.status_code != 200:
        raise_order_api_error(
            response,
            "상품주문번호 조회",
        )

    result = response.json()
    product_order_ids = normalize_product_order_ids(result)

    if not product_order_ids:
        raise RuntimeError(
            "상품주문번호 조회에는 성공했지만 "
            "응답에서 상품주문번호를 찾지 못했습니다.\n"
            f"응답 내용: {result}"
        )

    return product_order_ids


def get_product_orders(
    access_token: str,
    product_order_ids: list[str],
) -> list[dict]:
    """
    상품주문번호로 주문 상세 정보를 조회합니다.
    """

    normalized_ids = [
        str(product_order_id).strip()
        for product_order_id in product_order_ids
        if str(product_order_id).strip()
    ]

    normalized_ids = list(dict.fromkeys(normalized_ids))

    if not normalized_ids:
        raise ValueError(
            "조회할 상품주문번호가 없습니다."
        )

    if len(normalized_ids) > 300:
        raise ValueError(
            "상품주문 상세조회는 한 번에 최대 300건까지 가능합니다."
        )

    request_body = {
        "productOrderIds": normalized_ids,
        "quantityClaimCompatibility": True,
    }

    response = requests.post(
        PRODUCT_ORDERS_DETAIL_URL,
        headers=create_headers(access_token),
        json=request_body,
        timeout=30,
    )

    if response.status_code != 200:
        raise_order_api_error(
            response,
            "상품주문 상세조회",
        )

    result = response.json()

    if isinstance(result, list):
        return result

    if not isinstance(result, dict):
        raise RuntimeError(
            "상품주문 상세조회 응답 형식을 확인할 수 없습니다.\n"
            f"응답 내용: {result}"
        )

    data = result.get("data")

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in (
            "productOrders",
            "productOrdersInfo",
            "contents",
        ):
            value = data.get(key)

            if isinstance(value, list):
                return value

    raise RuntimeError(
        "상품주문 상세조회에는 성공했지만 "
        "응답에서 주문 목록을 찾지 못했습니다.\n"
        f"응답 내용: {result}"
    )


def get_orders_by_order_id(
    access_token: str,
    order_id: str,
) -> list[dict]:
    """
    일반 주문번호 하나로 상품주문 상세 정보까지 조회합니다.
    """

    product_order_ids = get_product_order_ids(
        access_token=access_token,
        order_id=order_id,
    )

    return get_product_orders(
        access_token=access_token,
        product_order_ids=product_order_ids,
    )


def get_order_summary(order_info: dict) -> dict:
    """
    복잡한 주문 상세 응답에서 업무에 필요한 항목만 정리합니다.
    """

    order = order_info.get("order") or {}
    product_order = order_info.get("productOrder") or {}
    shipping_address = (
        product_order.get("shippingAddress") or {}
    )

    return {
        "order_id": order.get("orderId"),
        "order_date": order.get("orderDate"),
        "payment_date": order.get("paymentDate"),
        "product_order_id": product_order.get(
            "productOrderId"
        ),
        "place_order_date": product_order.get(
            "placeOrderDate"
        ),
        "product_name": product_order.get("productName"),
        "product_option": product_order.get(
            "productOption"
        ),
        "quantity": product_order.get("quantity"),
        "product_order_status": product_order.get(
            "productOrderStatus"
        ),
        "place_order_status": product_order.get(
            "placeOrderStatus"
        ),
        "shipping_start_date": product_order.get(
            "shippingStartDate"
        ),
        "shipping_due_date": product_order.get(
            "shippingDueDate"
        ),
        "receiver_name": shipping_address.get("name"),
        "receiver_tel": shipping_address.get("tel1"),
        "base_address": shipping_address.get(
            "baseAddress"
        ),
        "detailed_address": shipping_address.get(
            "detailedAddress"
        ),
        "shipping_memo": product_order.get(
            "shippingMemo"
        ),
    }
