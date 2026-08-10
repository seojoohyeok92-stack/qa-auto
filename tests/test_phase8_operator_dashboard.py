from __future__ import annotations

import pytest

from answer.gpt_pricing import estimate_cost_krw
from services.dps_lookup_orchestrator import DpsLookupOrchestrator
from ui.uat_panel import operator_card_items


def test_dps_dashboard_requires_normal_order_id() -> None:
    with pytest.raises(ValueError, match="일반 주문번호"):
        DpsLookupOrchestrator.request_from_inquiry(
            {"id": 1, "product_order_ids_json": ["P-1"]}
        )


def test_dps_dashboard_rejects_product_order_id() -> None:
    with pytest.raises(ValueError, match="상품주문번호"):
        DpsLookupOrchestrator.request_from_inquiry(
            {
                "id": 1,
                "order_id": "P-1",
                "product_order_ids_json": ["P-1"],
            }
        )


def test_dps_dashboard_keeps_order_id_and_order_date() -> None:
    request = DpsLookupOrchestrator.request_from_inquiry(
        {
            "id": 7,
            "order_id": "O-7",
            "product_order_ids_json": ["P-7"],
            "order_date": "2026-07-30",
        }
    )
    assert request.order_id == "O-7"
    assert request.order_date == "2026-07-30"


def test_operator_uat_has_exactly_five_primary_cards() -> None:
    cards = operator_card_items({"items": []})
    assert [card["title"] for card in cards] == [
        "네이버 연결",
        "DPS 연결",
        "GPT 연결",
        "DB",
        "등록 잠금",
    ]


def test_latest_model_cost_can_be_estimated(monkeypatch) -> None:
    monkeypatch.setenv("QNA_GPT_USD_KRW_RATE", "1400")
    assert estimate_cost_krw(
        "gpt-5.6-sol",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    ) == 49_000
