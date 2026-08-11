from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.inquiry_processing_plan_service import InquiryProcessingPlanService
from ui.dashboard import UNCLASSIFIED_FILTER_VALUE


def _database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "qa-fix.db")
    database.initialize()
    return database


def _inquiry(
    database: Database,
    key: str,
    question: str,
    *,
    source_type: str = "CUSTOMER_INQUIRY",
    order_id: str | None = "2026080429306501",
) -> dict:
    saved = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": source_type,
            "source_question_id": key,
            "inquiry_type": source_type,
            "title": question,
            "content": question,
            "order_id": order_id,
            "registered_at": "2026-08-10T08:23:35+09:00",
            "raw_json": {
                "queue": "ORDER_LOOKUP_READY",
                "priority": "MEDIUM",
            },
        }
    )
    return InquiryRepository(database).get(saved.inquiry_id) or {}


@pytest.mark.parametrize(
    ("question", "source_type"),
    (
        ("배송날짜 확인 부탁드립니다.", "CUSTOMER_INQUIRY"),
        ("언제 받을 수 있나요?", "CUSTOMER_INQUIRY"),
        ("언제 출고되나요?", "CUSTOMER_INQUIRY"),
        ("배송 예정일 알려주세요.", "PRODUCT_INQUIRY"),
        ("설치/배송 일정 확인해주세요.", "PRODUCT_INQUIRY"),
    ),
)
def test_delivery_schedule_with_general_order_requires_dps(
    tmp_path: Path, question: str, source_type: str,
) -> None:
    database = _database(tmp_path)
    inquiry = _inquiry(database, question, question, source_type=source_type)

    plan = InquiryProcessingPlanService(database).create(inquiry)

    assert plan.normalized_text == question
    assert plan.inquiry_type == source_type
    assert plan.order_id == "2026080429306501"
    assert plan.order_id_status == "VALID"
    assert plan.is_delivery is True
    assert plan.requires_order_lookup is True
    assert plan.requires_dps_lookup is True
    assert plan.dps_lookup_action == "WAIT_FOR_ORDER_LOOKUP"
    assert plan.workflow_dps_status == "WAITING_FOR_ORDER_LOOKUP"


def test_pre_purchase_without_order_keeps_existing_no_dps_policy(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    inquiry = _inquiry(
        database,
        "pre-purchase",
        "이 상품 배송 얼마나 걸려요?",
        source_type="PRODUCT_INQUIRY",
        order_id=None,
    )

    plan = InquiryProcessingPlanService(database).create(inquiry)

    assert plan.detected_intent == "PRE_PURCHASE_DELIVERY"
    assert plan.requires_order_lookup is False
    assert plan.requires_dps_lookup is False
    assert plan.dps_lookup_action == "SKIP"
    assert plan.workflow_dps_status == "SKIPPED"


@pytest.mark.parametrize("page_size", (10, 15, 20, 30))
def test_dashboard_paginates_entire_filter_result(
    tmp_path: Path, page_size: int,
) -> None:
    database = _database(tmp_path)
    repository = InquiryRepository(database)
    for index in range(20):
        _inquiry(
            database,
            f"dashboard-{index:02d}",
            f"일반 문의 {index}",
            source_type="PRODUCT_INQUIRY",
            order_id=None,
        )

    rows, total, total_pages = repository.dashboard_page(
        store_codes=["OJE_PLUS"],
        source="ALL",
        queues=["ORDER_LOOKUP_READY", UNCLASSIFIED_FILTER_VALUE],
        priorities=["MEDIUM", UNCLASSIFIED_FILTER_VALUE],
        answer_status="ALL",
        delivery_only=False,
        search_query="",
        start_date="2026-08-01",
        end_date="2026-08-31",
        kpi_filter=None,
        page=1,
        page_size=page_size,
    )

    assert total == 20
    assert len(rows) == min(page_size, 20)
    assert total_pages == (20 + page_size - 1) // page_size


def test_dashboard_reset_clears_hidden_date_and_kpi_filters() -> None:
    app = AppTest.from_string(
        """
import streamlit as st
from ui.dashboard import render_filter_bar
if not st.session_state.get("qa_fix_seeded"):
    st.session_state["dashboard_kpi_filter"] = "REVIEW"
    st.session_state["dashboard_date_range"] = ("2026-08-10", "2026-08-10")
    st.session_state["dashboard_full_range_v1"] = True
    st.session_state["qa_fix_seeded"] = True
st.session_state["dashboard_available_routes"] = []
render_filter_bar(
    {"OJE_PLUS": "OJE_PLUS"},
    ["UNCLASSIFIED"],
    ["UNCLASSIFIED"],
)
"""
    ).run(timeout=30)
    next(button for button in app.button if button.label == "초기화").click()
    app = app.run(timeout=30)

    assert "dashboard_kpi_filter" not in app.session_state
    assert "dashboard_date_range" not in app.session_state
    assert "dashboard_full_range_v1" not in app.session_state
    page_size = next(
        item for item in app.selectbox if item.label == "페이지 크기"
    )
    assert page_size.value == 15


def test_readability_override_changes_colors_only() -> None:
    css = (
        Path(__file__).parents[1] / "ui" / "dashboard.css"
    ).read_text(encoding="utf-8")
    block = css[
        css.index("Readability-only overrides:") :
        css.index("End readability-only overrides.")
    ]
    assert '[data-testid="stWidgetLabel"] p' in block
    assert '[data-testid="stCaptionContainer"]' in block
    assert '[data-testid="stDataFrame"] [role="columnheader"]' in block
    assert "color: #f5f8fc !important" in block
    assert "color: #b8c6d5 !important" in block
    for layout_property in ("display:", "position:", "width:", "height:"):
        assert layout_property not in block
