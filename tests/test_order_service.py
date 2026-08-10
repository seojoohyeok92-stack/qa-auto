from services.order_service import extract_order_candidates, summarize_order


def test_extract_candidates_from_fields_and_text() -> None:
    item = {
        "order_id": "2026070699001271",
        "content": "상품주문번호 2026070674022711 확인 부탁드립니다.",
        "product_order_ids": ["2026070674022711"],
    }

    candidates = extract_order_candidates(item)
    assert [candidate.number for candidate in candidates] == [
        "2026070699001271",
        "2026070674022711",
    ]


def test_summarize_order() -> None:
    raw = {
        "order": {"orderId": "2026070699001271"},
        "productOrder": {
            "productOrderId": "2026070674022711",
            "productName": "테스트 상품",
            "productOrderStatus": "DELIVERING",
            "shippingAddress": {"name": "홍길동", "tel1": "01012345678"},
        },
    }

    summary = summarize_order(raw)
    assert summary["order_id"] == "2026070699001271"
    assert summary["product_order_id"] == "2026070674022711"
    assert summary["product_order_status"] == "DELIVERING"
