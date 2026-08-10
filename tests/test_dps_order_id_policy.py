from __future__ import annotations

from datetime import date
from unittest.mock import Mock, patch

import pytest

from dps import agent_server
from dps.context import (
    DpsLookupContextError,
    create_dps_lookup_context,
    identifier_fingerprint,
)
from dps.dps_ui_automation import DpsUiAutomation
from dps.identifiers import select_dps_query_identifier
from services import dps_agent_client


ORDER_ID = "2026062987651931"
PRODUCT_ORDER_ID = "2026062961090761"


def _snapshot() -> dict[str, str]:
    return {
        "order_id": ORDER_ID,
        "product_order_id": PRODUCT_ORDER_ID,
        "order_date": "2026-06-29",
        "shipping_due_date": "2026-07-08",
    }


def test_regression_context_uses_order_id_and_order_month() -> None:
    context = create_dps_lookup_context(
        _snapshot(),
        selected_inquiry_id="inquiry-1",
        request_id="request-1",
        today=date(2026, 7, 28),
    )
    assert context.order_id == ORDER_ID
    assert context.product_order_id == PRODUCT_ORDER_ID
    assert context.dps_query_value == ORDER_ID
    assert context.dps_query_value != PRODUCT_ORDER_ID
    assert context.dps_query_value_type == "order_id"
    assert context.dps_date_source == "order_date"
    assert context.dps_period_start == "2026-06-01"
    assert context.dps_period_end == "2026-06-30"


def test_context_is_frozen() -> None:
    context = create_dps_lookup_context(
        _snapshot(),
        selected_inquiry_id="inquiry-1",
        request_id="request-1",
    )
    with pytest.raises(AttributeError):
        context.order_id = "changed"  # type: ignore[misc]


def test_context_rejects_missing_order_id_without_product_fallback() -> None:
    with pytest.raises(DpsLookupContextError) as caught:
        create_dps_lookup_context(
            {
                "product_order_id": PRODUCT_ORDER_ID,
                "order_date": "2026-06-29",
            },
            selected_inquiry_id="inquiry-1",
        )
    assert caught.value.code == "DPS_ORDER_ID_MISSING"


def test_identifier_never_uses_product_order_id() -> None:
    selected = select_dps_query_identifier(None, PRODUCT_ORDER_ID)
    assert selected.value is None
    assert selected.type is None
    assert selected.error == "DPS_ORDER_ID_MISSING"


def test_client_sends_exact_order_context() -> None:
    with (
        patch.object(
            dps_agent_client,
            "start_dps_agent",
            return_value={"agent_running": True},
        ),
        patch.object(
            dps_agent_client,
            "_request",
            return_value={"success": True},
        ) as request,
    ):
        dps_agent_client.lookup_dps_order(
            request_id="request-1",
            selected_inquiry_id="inquiry-1",
            order_id=ORDER_ID,
            product_order_id=PRODUCT_ORDER_ID,
            dps_query_value=ORDER_ID,
            dps_query_value_type="order_id",
        )
    payload = request.call_args.args[1]
    assert payload["request_id"] == "request-1"
    assert payload["order_id"] == ORDER_ID
    assert payload["dps_query_value"] == ORDER_ID
    assert payload["product_order_id"] == PRODUCT_ORDER_ID


@pytest.mark.parametrize(
    ("query_value", "query_type", "expected_code"),
    [
        (PRODUCT_ORDER_ID, "product_order_id", "INVALID_DPS_QUERY_TYPE"),
        (PRODUCT_ORDER_ID, "order_id", "CLIENT_ORDER_ID_MISMATCH"),
    ],
)
def test_client_blocks_product_or_mismatched_query_before_http(
    query_value: str,
    query_type: str,
    expected_code: str,
) -> None:
    with patch.object(dps_agent_client, "_request") as request:
        result = dps_agent_client.lookup_dps_order(
            request_id="request-1",
            order_id=ORDER_ID,
            product_order_id=PRODUCT_ORDER_ID,
            dps_query_value=query_value,
            dps_query_value_type=query_type,
        )
    assert result["code"] == expected_code
    request.assert_not_called()


def test_agent_rejects_context_mismatch_before_automation() -> None:
    agent = agent_server.DpsWindowsAgent(
        store=Mock(
            load=Mock(
                return_value={
                    "tab_title_keywords": ["DPS"],
                    "allowed_hosts": ["dps2u.co.kr"],
                }
            ),
            load_agent_state=Mock(return_value={}),
        ),
        tab_manager=Mock(),
        ui_automation=Mock(),
    )
    result = agent.lookup(
        request_id="request-1",
        order_id=ORDER_ID,
        product_order_id=PRODUCT_ORDER_ID,
        dps_query_value=PRODUCT_ORDER_ID,
        dps_query_value_type="order_id",
    )
    assert result["code"] == "REQUEST_CONTEXT_MISMATCH"
    agent.ui.perform_lookup.assert_not_called()


def test_agent_rejects_product_query_type_before_automation() -> None:
    agent = agent_server.DpsWindowsAgent(
        store=Mock(
            load=Mock(
                return_value={
                    "tab_title_keywords": ["DPS"],
                    "allowed_hosts": ["dps2u.co.kr"],
                }
            ),
            load_agent_state=Mock(return_value={}),
        ),
        tab_manager=Mock(),
        ui_automation=Mock(),
    )
    result = agent.lookup(
        request_id="request-1",
        order_id=ORDER_ID,
        product_order_id=PRODUCT_ORDER_ID,
        dps_query_value=PRODUCT_ORDER_ID,
        dps_query_value_type="product_order_id",
    )
    assert result["code"] == "INVALID_DPS_QUERY_TYPE"
    agent.ui.perform_lookup.assert_not_called()


def test_order_cache_key_includes_period_and_schema_is_five() -> None:
    assert agent_server.CACHE_SCHEMA_VERSION == 5
    assert agent_server.dps_cache_key(
        ORDER_ID,
        "order_id",
        "2026-06-01",
        "2026-06-30",
    ) == f"order:{ORDER_ID}:2026-06-01:2026-06-30"


def test_product_cache_key_is_forbidden() -> None:
    with pytest.raises(ValueError):
        agent_server.dps_cache_key(
            PRODUCT_ORDER_ID,
            "product_order_id",
            "2026-06-01",
            "2026-06-30",
        )


def test_result_row_matches_online_sales_number_to_order_id_only() -> None:
    parsed = DpsUiAutomation().parse_lookup_result(
        {
            "raw_result_texts": [],
            "table_headers": [
                "온라인판매 주문번호",
                "DPS판매번호",
                "전자주문번호",
            ],
            "table_rows": [
                [PRODUCT_ORDER_ID, "WRONG", "WRONG"],
                [ORDER_ID, "SALE-1", "ELEC-1"],
            ],
        },
        order_id=ORDER_ID,
        product_order_id=PRODUCT_ORDER_ID,
        dps_query_value=ORDER_ID,
        dps_query_value_type="order_id",
    )
    assert parsed["found"] is True
    assert parsed["data"]["order_id"] == ORDER_ID
    assert parsed["data"]["product_order_id"] == PRODUCT_ORDER_ID
    assert parsed["data"]["dps_sales_number"] == "SALE-1"
    assert parsed["data"]["dps_order_number"] == "ELEC-1"
    assert parsed["diagnostics"]["matched_row_index"] == 1


def test_result_does_not_select_single_nonmatching_row() -> None:
    parsed = DpsUiAutomation().parse_lookup_result(
        {
            "raw_result_texts": [],
            "table_headers": ["온라인판매 주문번호", "DPS판매번호"],
            "table_rows": [[PRODUCT_ORDER_ID, "WRONG"]],
        },
        order_id=ORDER_ID,
        product_order_id=PRODUCT_ORDER_ID,
        dps_query_value=ORDER_ID,
        dps_query_value_type="order_id",
    )
    assert parsed["found"] is False
    assert parsed["diagnostics"]["matched_row_index"] is None


def test_actual_dps_sparse_row_aligns_number_model_quantity_and_status() -> None:
    parsed = DpsUiAutomation().parse_lookup_result(
        {
            "raw_result_texts": [],
            "table_headers": [
                "온라인판매",
                "셀러",
                "온라인판매 주문생성일",
                "온라인판매 주문번호",
                "모델명",
                "건수",
                "판매금액",
                "구매자",
                "인수자",
                "DPS판매번호",
                "전자주문번호",
                "희망일",
                "상태",
                "버튼",
                "트레이드인 대상",
                "트레이드인 신청일",
            ],
            "table_rows": [
                [
                    "네이버",
                    "seller",
                    "2026-07-23",
                    ORDER_ID,
                    "LH50BEFHLGFXKR",
                    "1",
                    "799,000",
                    "구매자",
                    "3141154630",
                    "1354801198",
                    "구매요청",
                ]
            ],
        },
        order_id=ORDER_ID,
        product_order_id=PRODUCT_ORDER_ID,
        dps_query_value=ORDER_ID,
        dps_query_value_type="order_id",
    )
    assert parsed["data"]["order_id"] == ORDER_ID
    assert parsed["data"]["model_name"] == "LH50BEFHLGFXKR"
    assert parsed["data"]["quantity"] == 1
    assert parsed["data"]["online_order_created_date"] == "2026-07-23"
    assert parsed["data"]["sales_amount"] == "799,000"
    assert parsed["data"]["dps_sales_number"] == "3141154630"
    assert parsed["data"]["dps_order_number"] == "1354801198"
    assert parsed["data"]["requested_date"] is None
    assert parsed["data"]["reception_status"] == "구매요청"


def test_identifier_log_fingerprint_uses_tail_and_hash_only() -> None:
    fingerprint = identifier_fingerprint(ORDER_ID)
    assert fingerprint["tail"] == "1931"
    assert len(fingerprint["hash"] or "") == 12
    assert ORDER_ID not in str(fingerprint)
