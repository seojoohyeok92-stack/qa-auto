from typing import Any

from api.order import (
    get_order_summary,
    get_orders_by_order_id,
    get_product_orders,
)
from core.utils import (
    classify_inquiry,
    create_order_request_message,
    get_queue_label,
)


DELIVERY_QUEUE = "AUTO_PROCESSABLE"
CUSTOMER_CONFIRMATION_QUEUE = "CUSTOMER_CONFIRMATION_REQUIRED"
ORDER_LOOKUP_FAILED_QUEUE = "ORDER_LOOKUP_FAILED"
GENERAL_QUEUE = "GENERAL_INQUIRY"


def find_value_recursively(
    data: Any,
    possible_keys: tuple[str, ...],
) -> Any:
    """
    중첩된 딕셔너리와 리스트에서 원하는 키 값을 재귀적으로 찾습니다.
    """

    if isinstance(data, dict):
        for key in possible_keys:
            value = data.get(key)

            if value not in (None, ""):
                return value

        for value in data.values():
            found = find_value_recursively(
                value,
                possible_keys,
            )

            if found not in (None, ""):
                return found

    elif isinstance(data, list):
        for item in data:
            found = find_value_recursively(
                item,
                possible_keys,
            )

            if found not in (None, ""):
                return found

    return None


def create_safe_order_summary(
    order_info: dict,
) -> dict:
    """
    주문 API 응답을 화면과 대시보드에서 사용할 표준 형식으로 변환합니다.
    """

    summary = get_order_summary(order_info)

    fallback_keys = {
        "order_id": (
            "orderId",
        ),
        "order_date": (
            "orderDate",
            "orderCreatedAt",
        ),
        "order_created_at": (
            "orderCreatedAt",
        ),
        "payment_date": (
            "paymentDate",
            "paymentCompletedAt",
        ),
        "payment_completed_at": (
            "paymentCompletedAt",
        ),
        "place_order_date": (
            "placeOrderDate",
        ),
        "product_order_id": (
            "productOrderId",
        ),
        "product_name": (
            "productName",
        ),
        "product_option": (
            "productOption",
            "optionManageCode",
        ),
        "quantity": (
            "quantity",
        ),
        "product_order_status": (
            "productOrderStatus",
        ),
        "place_order_status": (
            "placeOrderStatus",
        ),
        "shipping_start_date": (
            "shippingStartDate",
        ),
        "shipping_due_date": (
            "shippingDueDate",
        ),
        "receiver_name": (
            "receiverName",
            "name",
        ),
        "receiver_tel": (
            "receiverTel1",
            "tel1",
        ),
        "base_address": (
            "baseAddress",
        ),
        "detailed_address": (
            "detailedAddress",
        ),
        "shipping_memo": (
            "shippingMemo",
        ),
    }

    for field_name, possible_keys in fallback_keys.items():
        if summary.get(field_name) in (None, ""):
            summary[field_name] = find_value_recursively(
                order_info,
                possible_keys,
            )

    return summary


def normalize_number_list(
    value: Any,
) -> list[str]:
    """
    주문번호 값을 중복 없는 문자열 목록으로 변환합니다.
    """

    if value in (None, ""):
        return []

    raw_values: list[Any]

    if isinstance(value, str):
        normalized = (
            value.replace("\n", ",")
            .replace("|", ",")
            .replace(";", ",")
        )

        raw_values = normalized.split(",")

    elif isinstance(value, (list, tuple, set)):
        raw_values = list(value)

    else:
        raw_values = [value]

    result: list[str] = []

    for raw_value in raw_values:
        number = str(raw_value).strip()

        if not number:
            continue

        if number not in result:
            result.append(number)

    return result


def try_order_lookup(
    access_token: str,
    number_candidate: str,
    preferred_type: str | None = None,
) -> dict:
    """
    주문번호 후보를 이용해 주문을 조회합니다.
    """

    lookup_errors: list[str] = []

    lookup_order = [
        "ORDER_ID",
        "PRODUCT_ORDER_ID",
    ]

    if preferred_type == "PRODUCT_ORDER_ID":
        lookup_order = [
            "PRODUCT_ORDER_ID",
            "ORDER_ID",
        ]

    for number_type in lookup_order:
        if number_type == "ORDER_ID":
            try:
                orders = get_orders_by_order_id(
                    access_token=access_token,
                    order_id=number_candidate,
                )

                if not orders:
                    raise RuntimeError(
                        "조회 결과에 주문 정보가 없습니다."
                    )

                return {
                    "success": True,
                    "input_number": number_candidate,
                    "number_type": "일반 주문번호",
                    "number_type_code": "ORDER_ID",
                    "orders": orders,
                    "error": None,
                }

            except Exception as error:
                lookup_errors.append(
                    "일반 주문번호 조회 실패: "
                    + str(error)
                )

        elif number_type == "PRODUCT_ORDER_ID":
            try:
                orders = get_product_orders(
                    access_token=access_token,
                    product_order_ids=[
                        number_candidate,
                    ],
                )

                if not orders:
                    raise RuntimeError(
                        "조회 결과에 주문 정보가 없습니다."
                    )

                return {
                    "success": True,
                    "input_number": number_candidate,
                    "number_type": "상품주문번호",
                    "number_type_code": "PRODUCT_ORDER_ID",
                    "orders": orders,
                    "error": None,
                }

            except Exception as error:
                lookup_errors.append(
                    "상품주문번호 조회 실패: "
                    + str(error)
                )

    return {
        "success": False,
        "input_number": number_candidate,
        "number_type": None,
        "number_type_code": None,
        "orders": [],
        "error": "\n\n".join(lookup_errors),
    }


def lookup_order_candidates(
    access_token: str,
    order_ids: list[str] | None = None,
    product_order_ids: list[str] | None = None,
    unknown_numbers: list[str] | None = None,
) -> dict:
    """
    여러 주문번호 후보를 순서대로 조회합니다.
    """

    candidates: list[tuple[str, str | None]] = []

    for order_id in order_ids or []:
        candidates.append(
            (
                order_id,
                "ORDER_ID",
            )
        )

    for product_order_id in product_order_ids or []:
        candidates.append(
            (
                product_order_id,
                "PRODUCT_ORDER_ID",
            )
        )

    for unknown_number in unknown_numbers or []:
        candidates.append(
            (
                unknown_number,
                None,
            )
        )

    unique_candidates: list[
        tuple[str, str | None]
    ] = []

    used_numbers: set[str] = set()

    for number, preferred_type in candidates:
        if not number:
            continue

        if number in used_numbers:
            continue

        used_numbers.add(number)

        unique_candidates.append(
            (
                number,
                preferred_type,
            )
        )

    failed_results: list[dict] = []

    for number, preferred_type in unique_candidates:
        result = try_order_lookup(
            access_token=access_token,
            number_candidate=number,
            preferred_type=preferred_type,
        )

        if result["success"]:
            result["order_summaries"] = [
                create_safe_order_summary(order)
                for order in result["orders"]
            ]

            return result

        failed_results.append(result)

    error_messages: list[str] = []

    for result in failed_results:
        error_messages.append(
            f"[번호 {result['input_number']}]\n"
            f"{result['error']}"
        )

    return {
        "success": False,
        "input_number": None,
        "number_type": None,
        "number_type_code": None,
        "orders": [],
        "order_summaries": [],
        "error": "\n\n".join(error_messages),
    }


def determine_priority(
    text: str,
    is_delivery: bool,
) -> str:
    """
    문의 내용을 바탕으로 작업 우선순위를 정합니다.
    """

    normalized_text = text.lower()

    urgent_keywords = (
        "취소",
        "환불",
        "반품",
        "사기",
        "신고",
        "책임",
        "화가",
        "답답",
        "불만",
        "연락 안",
        "연락이 안",
        "이번주 안",
        "오늘까지",
        "긴급",
    )

    if any(
        keyword in normalized_text
        for keyword in urgent_keywords
    ):
        return "HIGH"

    if is_delivery:
        return "MEDIUM"

    return "NORMAL"


def create_base_work_item(
    store_code: str,
    store_name: str,
    source: str,
    inquiry_id: Any,
    title: str,
    content: str,
    product_id: Any,
    product_name: str,
    answered: bool | None,
    registered_at: Any,
) -> dict:
    """
    상품문의와 고객문의가 함께 사용할 표준 작업 항목을 만듭니다.
    """

    analysis = classify_inquiry(content)

    return {
        "store_code": store_code,
        "store_name": store_name,
        "source": source,
        "inquiry_id": inquiry_id,
        "title": title,
        "content": content,
        "product_id": product_id,
        "product_name": product_name,
        "answered": answered,
        "registered_at": registered_at,
        "analysis": analysis,
        "is_delivery": analysis["is_delivery"],
        "priority": determine_priority(
            text=content,
            is_delivery=analysis["is_delivery"],
        ),
        "queue": None,
        "queue_label": None,
        "recommended_action": None,
        "recommended_message": None,
        "order_id": None,
        "product_order_ids": [],
        "lookup_result": None,
        "orders": [],
        "original_data": None,
    }


def analyze_product_inquiry(
    qna: dict,
    access_token: str,
    store_code: str,
    store_name: str,
) -> dict:
    """
    상품문의를 표준 작업 항목으로 분석합니다.
    """

    question = qna.get("question") or ""

    work_item = create_base_work_item(
        store_code=store_code,
        store_name=store_name,
        source="PRODUCT_INQUIRY",
        inquiry_id=qna.get("questionId"),
        title="상품 문의",
        content=question,
        product_id=qna.get("productId"),
        product_name=qna.get("productName") or "",
        answered=qna.get("answered"),
        registered_at=qna.get("createDate"),
    )

    work_item["writer_id"] = (
        qna.get("maskedWriterId")
    )
    work_item["existing_answer"] = (
        qna.get("answer")
    )
    work_item["original_data"] = qna

    analysis = work_item["analysis"]

    if not analysis["is_delivery"]:
        queue = GENERAL_QUEUE

        work_item["queue"] = queue
        work_item["queue_label"] = (
            get_queue_label(queue)
        )
        work_item["recommended_action"] = (
            "일반 문의 답변 작성"
        )

        return work_item

    number_candidates = analysis.get(
        "number_candidates",
        [],
    )

    if not number_candidates:
        queue = CUSTOMER_CONFIRMATION_QUEUE

        work_item["queue"] = queue
        work_item["queue_label"] = (
            get_queue_label(queue)
        )
        work_item["recommended_action"] = (
            "고객에게 주문번호 요청"
        )
        work_item["recommended_message"] = (
            create_order_request_message()
        )

        return work_item

    lookup_result = lookup_order_candidates(
        access_token=access_token,
        unknown_numbers=number_candidates,
    )

    work_item["lookup_result"] = lookup_result

    if not lookup_result["success"]:
        queue = ORDER_LOOKUP_FAILED_QUEUE

        work_item["queue"] = queue
        work_item["queue_label"] = (
            get_queue_label(queue)
        )
        work_item["recommended_action"] = (
            "주문번호 수동 확인"
        )

        return work_item

    queue = DELIVERY_QUEUE

    work_item["queue"] = queue
    work_item["queue_label"] = (
        get_queue_label(queue)
    )
    work_item["recommended_action"] = (
        "배송 및 설치 일정 조회"
    )
    work_item["orders"] = lookup_result[
        "order_summaries"
    ]

    return work_item


def analyze_customer_inquiry(
    inquiry: dict,
    access_token: str,
    store_code: str,
    store_name: str,
) -> dict:
    """
    고객문의를 표준 작업 항목으로 분석합니다.
    """

    inquiry_content = (
        inquiry.get("inquiryContent") or ""
    )

    title = inquiry.get("title") or "고객 문의"

    order_ids = normalize_number_list(
        inquiry.get("orderId")
    )

    product_order_ids = normalize_number_list(
        inquiry.get("productOrderIdList")
    )

    work_item = create_base_work_item(
        store_code=store_code,
        store_name=store_name,
        source="CUSTOMER_INQUIRY",
        inquiry_id=inquiry.get("inquiryNo"),
        title=title,
        content=inquiry_content,
        product_id=inquiry.get("productNo"),
        product_name=(
            inquiry.get("productName") or ""
        ),
        answered=inquiry.get("answered"),
        registered_at=inquiry.get(
            "inquiryRegistrationDateTime"
        ),
    )

    work_item["category"] = inquiry.get(
        "category"
    )
    work_item["customer_id"] = inquiry.get(
        "customerId"
    )
    work_item["customer_name"] = inquiry.get(
        "customerName"
    )
    work_item["product_option"] = inquiry.get(
        "productOrderOption"
    )
    work_item["existing_answer"] = inquiry.get(
        "answerContent"
    )
    work_item["answer_registered_at"] = (
        inquiry.get(
            "answerRegistrationDateTime"
        )
    )
    work_item["order_id"] = (
        order_ids[0]
        if order_ids
        else None
    )
    work_item["product_order_ids"] = (
        product_order_ids
    )
    work_item["original_data"] = inquiry

    analysis = work_item["analysis"]

    if not analysis["is_delivery"]:
        queue = GENERAL_QUEUE

        work_item["queue"] = queue
        work_item["queue_label"] = (
            get_queue_label(queue)
        )
        work_item["recommended_action"] = (
            "일반 고객문의 답변 작성"
        )

        return work_item

    if not order_ids and not product_order_ids:
        queue = CUSTOMER_CONFIRMATION_QUEUE

        work_item["queue"] = queue
        work_item["queue_label"] = (
            get_queue_label(queue)
        )
        work_item["recommended_action"] = (
            "고객 주문정보 수동 확인"
        )
        work_item["recommended_message"] = (
            create_order_request_message()
        )

        return work_item

    lookup_result = lookup_order_candidates(
        access_token=access_token,
        order_ids=order_ids,
        product_order_ids=product_order_ids,
    )

    work_item["lookup_result"] = lookup_result

    if not lookup_result["success"]:
        queue = ORDER_LOOKUP_FAILED_QUEUE

        work_item["queue"] = queue
        work_item["queue_label"] = (
            get_queue_label(queue)
        )
        work_item["recommended_action"] = (
            "연결된 주문정보 수동 확인"
        )

        return work_item

    queue = DELIVERY_QUEUE

    work_item["queue"] = queue
    work_item["queue_label"] = (
        get_queue_label(queue)
    )
    work_item["recommended_action"] = (
        "배송 및 설치 일정 조회"
    )
    work_item["orders"] = lookup_result[
        "order_summaries"
    ]

    return work_item


def count_work_items(
    work_items: list[dict],
) -> dict[str, int]:
    """
    표준 작업 항목의 큐별 개수를 계산합니다.
    """

    queue_counts = {
        DELIVERY_QUEUE: 0,
        CUSTOMER_CONFIRMATION_QUEUE: 0,
        ORDER_LOOKUP_FAILED_QUEUE: 0,
        GENERAL_QUEUE: 0,
    }

    for work_item in work_items:
        queue = work_item.get("queue")

        if queue:
            queue_counts[queue] = (
                queue_counts.get(queue, 0) + 1
            )

    return queue_counts


def count_work_items_by_store(
    work_items: list[dict],
) -> dict[str, int]:
    """
    스토어별 작업 항목 개수를 계산합니다.
    """

    store_counts: dict[str, int] = {}

    for work_item in work_items:
        store_name = (
            work_item.get("store_name")
            or work_item.get("store_code")
            or "알 수 없는 스토어"
        )

        store_counts[store_name] = (
            store_counts.get(store_name, 0) + 1
        )

    return store_counts
