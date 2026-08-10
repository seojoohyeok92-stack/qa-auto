from config import get_configured_stores
from core.business_engine import (
    count_work_items,
    count_work_items_by_store,
)
from services.work_queue_service import load_work_queue


SEPARATOR = "=" * 80
SUB_SEPARATOR = "-" * 80

def print_order_summary(
    summary: dict,
    number: int,
) -> None:
    """
    조회된 상품주문 정보를 출력합니다.
    """

    print(f"[주문 상품 {number}]")

    print(
        "일반 주문번호:",
        summary.get("order_id")
        or "확인되지 않음",
    )

    print(
        "상품주문번호:",
        summary.get("product_order_id")
        or "확인되지 않음",
    )

    print(
        "상품명:",
        summary.get("product_name")
        or "확인되지 않음",
    )

    print(
        "상품 옵션:",
        summary.get("product_option")
        or "없음",
    )

    quantity = summary.get("quantity")

    print(
        "수량:",
        quantity
        if quantity is not None
        else "확인되지 않음",
    )

    print(
        "상품주문 상태:",
        summary.get("product_order_status")
        or "확인되지 않음",
    )

    print(
        "발주 상태:",
        summary.get("place_order_status")
        or "확인되지 않음",
    )

    print(
        "배송 시작일:",
        summary.get("shipping_start_date")
        or "없음",
    )

    print(
        "배송 예정일:",
        summary.get("shipping_due_date")
        or "없음",
    )

    print(
        "수취인:",
        summary.get("receiver_name")
        or "확인되지 않음",
    )

    print(
        "연락처:",
        summary.get("receiver_tel")
        or "확인되지 않음",
    )

    base_address = (
        summary.get("base_address") or ""
    )

    detailed_address = (
        summary.get("detailed_address") or ""
    )

    full_address = (
        f"{base_address} {detailed_address}"
        .strip()
    )

    print(
        "배송지:",
        full_address
        or "확인되지 않음",
    )

    print(
        "배송 메모:",
        summary.get("shipping_memo")
        or "없음",
    )


def print_analysis(
    work_item: dict,
) -> None:
    """
    비즈니스 엔진의 분석 결과를 출력합니다.
    """

    analysis = work_item["analysis"]

    print(SUB_SEPARATOR)
    print("[문의 분석]")

    print(
        "배송·설치 문의:",
        "예"
        if analysis["is_delivery"]
        else "아니오",
    )

    print(
        "분류 점수:",
        analysis["score"],
    )

    matched_keywords = analysis.get(
        "matched_keywords",
        [],
    )

    print(
        "발견 키워드:",
        ", ".join(matched_keywords)
        if matched_keywords
        else "없음",
    )

    number_candidates = analysis.get(
        "number_candidates",
        [],
    )

    print(
        "문의내용 번호 후보:",
        ", ".join(number_candidates)
        if number_candidates
        else "없음",
    )

    print(
        "우선순위:",
        work_item["priority"],
    )


def print_work_item(
    work_item: dict,
    number: int,
) -> None:
    """
    통합 작업 항목을 출력합니다.
    """

    print(SEPARATOR)
    print(f"[통합 작업 {number}]")

    print(
        "스토어:",
        work_item.get("store_name")
        or "알 수 없음",
    )

    print(
        "스토어 코드:",
        work_item.get("store_code")
        or "알 수 없음",
    )

    source = work_item["source"]

    if source == "PRODUCT_INQUIRY":
        print("문의 출처: 상품문의")
    else:
        print("문의 출처: 고객문의")

    print(
        "문의 번호:",
        work_item.get("inquiry_id"),
    )

    print(
        "등록 일시:",
        work_item.get("registered_at"),
    )

    print(
        "문의 제목:",
        work_item.get("title")
        or "없음",
    )

    print(
        "상품 번호:",
        work_item.get("product_id")
        or "없음",
    )

    print(
        "상품명:",
        work_item.get("product_name")
        or "없음",
    )

    print(
        "답변 여부:",
        work_item.get("answered"),
    )

    if source == "PRODUCT_INQUIRY":
        print(
            "작성자 ID:",
            work_item.get("writer_id")
            or "없음",
        )

    else:
        print(
            "문의 카테고리:",
            work_item.get("category")
            or "없음",
        )

        print(
            "고객 ID:",
            work_item.get("customer_id")
            or "없음",
        )

        print(
            "고객명:",
            work_item.get("customer_name")
            or "없음",
        )

        print(
            "연결된 일반 주문번호:",
            work_item.get("order_id")
            or "없음",
        )

        product_order_ids = work_item.get(
            "product_order_ids",
            [],
        )

        print(
            "연결된 상품주문번호:",
            ", ".join(product_order_ids)
            if product_order_ids
            else "없음",
        )

        print(
            "상품 옵션:",
            work_item.get("product_option")
            or "없음",
        )

    print()
    print("[문의 내용]")

    print(
        work_item.get("content")
        or "문의 내용이 없습니다."
    )

    print()

    print_analysis(work_item)

    print()
    print(
        "작업 큐:",
        work_item["queue_label"],
    )

    print(
        "권장 작업:",
        work_item["recommended_action"],
    )

    recommended_message = work_item.get(
        "recommended_message"
    )

    if recommended_message:
        print()
        print("[권장 안내 답변]")
        print(recommended_message)

    lookup_result = work_item.get(
        "lookup_result"
    )

    if lookup_result:
        print()
        print("[주문 조회 결과]")

        if lookup_result["success"]:
            print("조회 결과: 성공")

            print(
                "확인된 번호 종류:",
                lookup_result["number_type"],
            )

            print(
                "조회에 사용한 번호:",
                lookup_result["input_number"],
            )

            print(
                "조회된 상품주문 수:",
                len(work_item["orders"]),
            )

            for order_number, summary in enumerate(
                work_item["orders"],
                start=1,
            ):
                print()

                print_order_summary(
                    summary=summary,
                    number=order_number,
                )

        else:
            print("조회 결과: 실패")

            print()
            print("[조회 오류]")

            print(
                lookup_result.get("error")
                or "오류 내용이 없습니다."
            )

    existing_answer = work_item.get(
        "existing_answer"
    )

    if existing_answer:
        print()
        print("[기존 판매자 답변]")
        print(existing_answer)


def print_queue_summary(
    work_items: list[dict],
) -> None:
    """
    통합 작업 큐 결과를 요약합니다.
    """

    queue_counts = count_work_items(
        work_items
    )

    store_counts = count_work_items_by_store(
        work_items
    )

    product_count = sum(
        1
        for item in work_items
        if item["source"] == "PRODUCT_INQUIRY"
    )

    customer_count = sum(
        1
        for item in work_items
        if item["source"] == "CUSTOMER_INQUIRY"
    )

    high_priority_count = sum(
        1
        for item in work_items
        if item["priority"] == "HIGH"
    )

    print()
    print(SEPARATOR)
    print("[통합 작업 큐 요약]")

    print()
    print("[스토어별 문의]")

    for store_name, count in store_counts.items():
        print(
            f"{store_name}:",
            count,
        )

    print()
    print("[문의 유형]")

    print(
        "상품문의:",
        product_count,
    )

    print(
        "고객문의:",
        customer_count,
    )

    print(
        "전체 문의:",
        len(work_items),
    )

    print()
    print("[작업 큐]")

    print(
        "🔴 높은 우선순위:",
        high_priority_count,
    )

    print(
        "🟢 자동 처리 가능:",
        queue_counts.get(
            "AUTO_PROCESSABLE",
            0,
        ),
    )

    print(
        "🟡 고객 확인 필요:",
        queue_counts.get(
            "CUSTOMER_CONFIRMATION_REQUIRED",
            0,
        ),
    )

    print(
        "🟠 주문 확인 필요:",
        queue_counts.get(
            "ORDER_LOOKUP_FAILED",
            0,
        ),
    )

    print(
        "⚪ 일반 문의:",
        queue_counts.get(
            "GENERAL_INQUIRY",
            0,
        ),
    )

    print(SEPARATOR)


def main() -> None:
    """
    설정된 모든 스토어를 조회하고 하나의 작업 큐로 통합합니다.
    """

    print(
        "네이버 스마트스토어 다중 스토어 "
        "통합 작업 큐를 시작합니다."
    )

    stores = get_configured_stores()

    if not stores:
        print()
        print(
            "활성화되고 인증정보가 등록된 "
            "스토어가 없습니다."
        )

        return

    print()
    print(
        "연결 대상 스토어 수:",
        len(stores),
    )

    for store in stores:
        print(
            f"- {store.name} ({store.code})"
        )

    print()
    work_items, load_errors = load_work_queue(
        stores=stores,
        progress_callback=print,
    )

    if load_errors:
        print()
        print("[조회 중 발생한 오류]")

        for load_error in load_errors:
            inquiry_text = (
                f", 문의 번호: {load_error['inquiry_id']}"
                if load_error["inquiry_id"]
                else ""
            )
            print(
                f"- {load_error['store_name']} / "
                f"{load_error['stage']}{inquiry_text}: "
                f"{load_error['message']}"
            )

    if not work_items:
        print()
        print(
            "조회된 상품문의와 고객문의가 없습니다."
        )

        return

    for number, work_item in enumerate(
        work_items,
        start=1,
    ):
        print_work_item(
            work_item=work_item,
            number=number,
        )

    print_queue_summary(work_items)


if __name__ == "__main__":
    try:
        main()

    except Exception as error:
        print()
        print(
            "프로그램 실행 중 오류가 발생했습니다."
        )
        print(error)
        
