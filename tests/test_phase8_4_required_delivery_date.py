from __future__ import annotations

import json

import pytest

from answer.facts import build_answer_facts
from answer.models import AnswerRequest, AnswerResult, AnswerStatus
from dps.sales_detail import (
    map_detail_items,
    merge_list_and_detail,
    parse_required_delivery_date,
    resolve_item_required_delivery_date,
)
from repositories.database import Database
from repositories.dps_repository import DpsRepository
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from services.dps_enrichment_service import DpsEnrichmentService
from services.dps_result_normalizer import normalize_dps_result
from ui.dps_presenter import build_dps_display, installation_date_display


SOURCE = "DPS_ITEM_DETAIL_REQUIRED_DELIVERY_DATE"


@pytest.mark.parametrize(
    ("raw", "normalized", "status"),
    [
        ("2026-07-31", "2026-07-31", "PARSED"),
        ("2026.07.31", "2026-07-31", "PARSED"),
        ("20260731", "2026-07-31", "PARSED"),
        ("2026/07/31", "2026-07-31", "PARSED"),
        ("  2026-07-31  ", "2026-07-31", "PARSED"),
        ("", None, "MISSING"),
        (None, None, "MISSING"),
        ("2026-02-30", None, "PARSE_FAILED"),
    ],
)
def test_required_delivery_date_normalization(raw, normalized, status):
    result = parse_required_delivery_date(raw)
    assert result["required_delivery_date"] == normalized
    assert result["date_parse_status"] == status
    assert result["raw_required_delivery_date"] == (
        None if raw is None else str(raw)
    )


def test_required_delivery_header_maps_same_row_by_index():
    items = map_detail_items(
        ["항번", "모델", "수량", "요구납기일", "비고"],
        [["10", "LH50BEFHLGFXKR", "1", "2026-07-31", ""]],
    )
    assert items[0]["model_name"] == "LH50BEFHLGFXKR"
    assert items[0]["required_delivery_date"] == "2026-07-31"
    assert items[0]["raw_required_delivery_date"] == "2026-07-31"


def test_same_dates_map_to_installation_date():
    result = resolve_item_required_delivery_date(
        [
            {
                "required_delivery_date": "2026-07-31",
                "raw_required_delivery_date": "2026.07.31",
                "date_parse_status": "PARSED",
            },
            {
                "required_delivery_date": "2026-07-31",
                "raw_required_delivery_date": "20260731",
                "date_parse_status": "PARSED",
            },
        ]
    )
    assert result["required_delivery_date"] == "2026-07-31"
    assert result["installation_date"] == "2026-07-31"
    assert result["installation_date_source"] == SOURCE
    assert result["date_parse_status"] == "PARSED"


def test_different_dates_require_human_review_without_earliest_or_latest():
    result = resolve_item_required_delivery_date(
        [
            {
                "required_delivery_date": "2026-07-30",
                "raw_required_delivery_date": "2026-07-30",
                "date_parse_status": "PARSED",
            },
            {
                "required_delivery_date": "2026-07-31",
                "raw_required_delivery_date": "2026-07-31",
                "date_parse_status": "PARSED",
            },
        ]
    )
    assert result["installation_date"] is None
    assert result["date_parse_status"] == "CONFLICT"
    assert result["requires_human_review"] is True


def test_explicit_online_order_representative_item_has_priority():
    result = resolve_item_required_delivery_date(
        [
            {
                "required_delivery_date": "2026-07-30",
                "raw_required_delivery_date": "2026-07-30",
                "date_parse_status": "PARSED",
            },
            {
                "required_delivery_date": "2026-07-31",
                "raw_required_delivery_date": "2026-07-31",
                "date_parse_status": "PARSED",
                "matches_online_order": True,
            },
        ]
    )
    assert result["installation_date"] == "2026-07-31"


def test_single_tv_item_has_priority_over_non_installation_accessory():
    result = resolve_item_required_delivery_date(
        [
            {
                "model_name": "ACCESSORY-1",
                "required_delivery_date": "2026-07-30",
                "raw_required_delivery_date": "2026-07-30",
                "date_parse_status": "PARSED",
            },
            {
                "model_name": "LH50BEFHLGFXKR",
                "required_delivery_date": "2026-07-31",
                "raw_required_delivery_date": "2026-07-31",
                "date_parse_status": "PARSED",
            },
        ]
    )
    assert result["installation_date"] == "2026-07-31"


def test_merge_never_falls_back_to_list_or_customer_dates():
    merged = merge_list_and_detail(
        {"requested_date": "2026-07-29", "order_date": "2026-07-28"},
        {
            "customer_info": {"requested_delivery_date": "2026-07-30"},
            "detail_items": [],
        },
        detail_lookup={"parsed": True},
    )
    assert merged["required_delivery_date"] is None
    assert merged["installation_date"] is None
    assert merged["date_parse_status"] == "MISSING"


def test_normalizer_accepts_supported_installation_date_alias():
    wrong_fallbacks = normalize_dps_result(
        {
            "success": True,
            "found": True,
            "data": {
                "delivery_scheduled_date": "2026-07-20",
                "scheduled_date": "2026-07-21",
                "installation_date": "2026-07-22",
                "requested_date": "2026-07-23",
            },
        },
        order_id="ORDER-1",
        elapsed_seconds=0,
    )
    assert wrong_fallbacks["installation_date"] == "2026-07-22"
    assert wrong_fallbacks["installation_date_source"] == SOURCE
    assert wrong_fallbacks["date_parse_status"] == "PARSED"

    confirmed = normalize_dps_result(
        {
            "success": True,
            "found": True,
            "data": {
                "required_delivery_date": "2026/07/31",
                "raw_required_delivery_date": "2026/07/31",
                "installation_date_source": SOURCE,
                "date_parse_status": "PARSED",
                "required_delivery_date_row_count": 1,
            },
        },
        order_id="ORDER-1",
        elapsed_seconds=0,
    )
    assert confirmed["installation_date"] == "2026-07-31"
    assert confirmed["installation_date_source"] == SOURCE


def _inquiry(database: Database) -> int:
    return InquiryRepository(database).upsert_work_item(
        {
            "store_code": "STORE",
            "source_type": "CUSTOMER_INQUIRY",
            "source_question_id": "PHASE-8-4",
            "content": "설치예정일 문의",
        }
    ).inquiry_id


def test_repository_saves_required_date_columns(tmp_path):
    database = Database(tmp_path / "phase84.db")
    database.initialize()
    inquiry_id = _inquiry(database)
    row = DpsRepository(database).create_lookup_result(
        inquiry_id=inquiry_id,
        order_id="ORDER-1",
        lookup_status="SUCCESS",
        raw_result={},
        normalized_result={
            "lookup_status": "SUCCESS",
            "required_delivery_date": "2026-07-31",
            "installation_date": "2026-07-31",
            "installation_date_source": SOURCE,
            "raw_required_delivery_date": "2026.07.31",
            "date_parse_status": "PARSED",
        },
    )
    assert row["required_delivery_date"] == "2026-07-31"
    assert row["installation_date"] == "2026-07-31"
    assert row["installation_date_source"] == SOURCE
    assert row["raw_required_delivery_date"] == "2026.07.31"
    assert row["date_parse_status"] == "PARSED"


def test_dashboard_states_and_developer_fields():
    assert installation_date_display({}, queried=False) == (
        "아직 DPS 조회를 실행하지 않았습니다."
    )
    assert installation_date_display(
        {"date_parse_status": "MISSING"}, queried=True
    ) == "DPS 상세에 요구납기일이 없습니다."
    assert installation_date_display(
        {"date_parse_status": "PARSE_FAILED"}, queried=True
    ) == "요구납기일 형식을 확인할 수 없습니다."
    assert installation_date_display(
        {"date_parse_status": "CONFLICT"}, queried=True
    ) == "복수 품목의 요구납기일이 달라 확인이 필요합니다."

    display = build_dps_display(
        lookup_required=True,
        order_id="ORDER-1",
        latest_row={
            "lookup_status": "SUCCESS",
            "normalized_result_json": {
                "installation_date": "2026-07-31",
                "required_delivery_date": "2026-07-31",
                "raw_required_delivery_date": "2026.07.31",
                "installation_date_source": SOURCE,
                "date_parse_status": "PARSED",
            },
        },
    )
    assert display["installation_date_display"] == "2026-07-31"
    assert display["installation_date_help"] == (
        "DPS 품목상세내역의 요구납기일 기준"
    )


def _rule_result() -> AnswerResult:
    return AnswerResult(
        status=AnswerStatus.GENERATED,
        category="배송",
        reason="rule",
        answer="확인했습니다.",
        provider="rules",
        auto_answerable=True,
        needs_review=False,
    )


def test_answer_facts_only_exposes_confirmed_required_date():
    confirmed = AnswerRequest(
        question="설치일은 언제인가요?",
        order_id="ORDER-1",
        metadata={
            "dps": {
                "installation_date": "2026-07-31",
                "required_delivery_date": "2026-07-31",
                "installation_date_source": SOURCE,
                "date_parse_status": "PARSED",
            }
        },
    )
    facts = build_answer_facts(confirmed, _rule_result())
    assert facts.installation["date"] == "2026-07-31"
    assert facts.installation["source"] == SOURCE

    conflict = AnswerRequest(
        question="설치일은 언제인가요?",
        order_id="ORDER-1",
        metadata={
            "dps": {
                "installation_date": "2026-07-31",
                "installation_date_source": SOURCE,
                "date_parse_status": "CONFLICT",
                "requires_human_review": True,
            }
        },
    )
    conflict_facts = build_answer_facts(conflict, _rule_result())
    assert conflict_facts.installation["date"] is None
    assert conflict_facts.policy["requires_review"] is True


def test_required_date_activity_logs_are_minimal_and_masked(tmp_path):
    database = Database(tmp_path / "logs.db")
    database.initialize()
    inquiry_id = _inquiry(database)
    service = DpsEnrichmentService(database, client=lambda **_: {})
    service._record_required_date_events(
        inquiry_id,
        order_id="2026072912345678",
        correlation_id="corr-1",
        normalized={
            "date_parse_status": "PARSED",
            "installation_date": "2026-07-31",
            "installation_date_source": SOURCE,
            "required_delivery_date_row_count": 1,
        },
    )
    logs = LogRepository(database).recent_for_inquiry(inquiry_id)
    assert {log["event_code"] for log in logs} == {
        "DPS_REQUIRED_DATE_FOUND",
        "DPS_INSTALLATION_DATE_SAVED",
    }
    for log in logs:
        details = log["details_json"]
        assert set(details) == {
            "masked_order_id",
            "correlation_id",
            "status",
            "normalized_date",
            "source",
            "row_count",
        }
        assert details["masked_order_id"] != "2026072912345678"
        assert "2026072912345678" not in json.dumps(details)
