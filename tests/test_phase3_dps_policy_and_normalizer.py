from __future__ import annotations

from answer.models import AnswerRequest
from services.dps_lookup_policy import (
    DpsLookupPolicy,
    DpsLookupStatus,
    DpsSettings,
    split_question_segments,
)
from services.dps_result_normalizer import (
    normalize_date,
    normalize_dps_result,
    sanitize_raw_result,
)


def request(question: str, *, order_id: str = "ORDER-1") -> AnswerRequest:
    return AnswerRequest(question=question, order_id=order_id)


def test_general_question_does_not_require_dps() -> None:
    decision = DpsLookupPolicy().decide(request("넷플릭스 시청이 가능한가요?"))
    assert decision.status is DpsLookupStatus.NOT_REQUIRED
    assert decision.lookup_required is False


def test_delivery_question_with_order_is_pending() -> None:
    decision = DpsLookupPolicy().decide(request("배송은 언제 오나요?"))
    assert decision.status is DpsLookupStatus.PENDING
    assert decision.order_id == "ORDER-1"


def test_product_order_only_never_becomes_dps_identifier() -> None:
    value = AnswerRequest(
        question="설치는 언제 오나요?",
        order_id="",
        product_order_id="PRODUCT-ONLY",
    )
    decision = DpsLookupPolicy().decide(value)
    assert decision.status is DpsLookupStatus.WAITING_FOR_ORDER_ID
    assert decision.order_id is None


def test_change_request_is_always_marked() -> None:
    decision = DpsLookupPolicy().decide(
        request("설치일을 변경해 주세요.")
    )
    assert decision.lookup_required is True
    assert decision.change_request is True


def test_mixed_question_is_split() -> None:
    question = "넷플릭스 되나요? 설치는 언제 오나요?"
    assert split_question_segments(question) == (
        "넷플릭스 되나요?",
        "설치는 언제 오나요?",
    )
    decision = DpsLookupPolicy().decide(request(question))
    assert decision.general_segments == ("넷플릭스 되나요?",)
    assert decision.dps_segments == ("설치는 언제 오나요?",)


def test_timeout_defaults_are_safe(monkeypatch) -> None:
    for key in (
        "DPS_CONNECT_TIMEOUT_SECONDS",
        "DPS_READ_TIMEOUT_SECONDS",
        "DPS_TOTAL_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = DpsSettings.from_environment()
    assert settings.connect_timeout_seconds == 7
    assert settings.read_timeout_seconds == 100
    assert settings.total_timeout_seconds == 120


def test_timeout_environment_values_are_configurable(monkeypatch) -> None:
    monkeypatch.setenv("DPS_CONNECT_TIMEOUT_SECONDS", "8")
    monkeypatch.setenv("DPS_READ_TIMEOUT_SECONDS", "105")
    monkeypatch.setenv("DPS_TOTAL_TIMEOUT_SECONDS", "125")
    settings = DpsSettings.from_environment()
    assert (
        settings.connect_timeout_seconds,
        settings.read_timeout_seconds,
        settings.total_timeout_seconds,
    ) == (8, 105, 125)


def test_success_result_is_normalized_with_iso_date() -> None:
    result = normalize_dps_result(
        {
            "success": True,
            "found": True,
            "status": "RESULT_FOUND_WITH_DETAIL",
            "data": {
                "dps_sales_number": "SALE-1",
                "progress_status": "배송 준비 중",
                "required_delivery_date": "2026년 8월 3일",
                "raw_required_delivery_date": "2026년 8월 3일",
                "installation_date_source": (
                    "DPS_ITEM_DETAIL_REQUIRED_DELIVERY_DATE"
                ),
                "date_parse_status": "PARSED",
            },
        },
        order_id="ORDER-1",
        elapsed_seconds=53.4,
    )
    assert result["lookup_status"] == "SUCCESS"
    assert result["installation_date"] == "2026-08-03"
    assert result["sales_number"] == "SALE-1"


def test_not_found_timeout_offline_and_parse_are_distinct() -> None:
    cases = (
        ({"success": True, "found": False}, "NOT_FOUND"),
        ({"success": False, "code": "AGENT_READ_TIMEOUT"}, "TIMEOUT"),
        ({"success": False, "code": "AGENT_CONNECTION_FAILED"}, "AGENT_OFFLINE"),
        ({"success": False, "code": "DETAIL_PARSE_FAILED"}, "PARSE_ERROR"),
        ({"success": False, "code": "ORDER_INPUT_NOT_FOUND"}, "AUTOMATION_ERROR"),
        ({"success": True, "status": "DETAIL_CLOSE_FAILED"}, "AUTOMATION_ERROR"),
    )
    for raw, expected in cases:
        assert normalize_dps_result(
            raw, order_id="ORDER-1", elapsed_seconds=1
        )["lookup_status"] == expected


def test_empty_markers_and_invalid_dates_become_none() -> None:
    assert normalize_date("-") is None
    assert normalize_date("N/A") is None
    assert normalize_date("2026-02-30") is None


def test_raw_result_removes_sensitive_fields() -> None:
    sanitized = sanitize_raw_result(
        {
            "buyer_phone": "010-1234-5678",
            "delivery_address": "서울시 어딘가",
            "data": {"status": "정상"},
        }
    )
    assert sanitized["buyer_phone"] is None
    assert sanitized["delivery_address"] is None
    assert sanitized["data"]["status"] == "정상"
