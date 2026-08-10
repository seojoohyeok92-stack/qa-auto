from __future__ import annotations

from datetime import date, datetime, timedelta
from html import escape
import os
from pathlib import Path
from typing import Any

import streamlit as st
import streamlit.components.v1 as components

from core.time_utils import format_datetime_kst
from repositories.approval_repository import ApprovalRepository
from repositories.database import Database
from services.work_queue_service import WorkItem, parse_registered_at
from ui.components import (
    PRIORITY_LABELS,
    QUEUE_LABELS,
    SOURCE_LABELS,
    display_value,
    get_work_item_state_key,
    render_work_item,
)
from ui.session_identity import current_identity

CSS_PATH = Path(__file__).with_name("dashboard.css")
UNCLASSIFIED_FILTER_VALUE = "UNCLASSIFIED"


def metadata_filter_matches(value: Any, selected: list[str]) -> bool:
    if not selected:
        return True
    normalized = (
        UNCLASSIFIED_FILTER_VALUE
        if value in (None, "")
        else str(value)
    )
    return normalized in selected


def load_dashboard_css() -> None:
    if CSS_PATH.exists():
        st.markdown(f"<style>{CSS_PATH.read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)


def render_header(
    items: list[WorkItem] | None = None,
    database: Database | None = None,
    states: list[dict[str, Any]] | None = None,
) -> tuple[date, date]:
    states = states if states is not None else (
        ApprovalRepository(database).dashboard_states()
        if database is not None
        else []
    )
    alert_count = sum(
        _state_matches_filter(state, "ATTENTION") for state in states
    )
    identity = current_identity()
    configured_environment = os.getenv("QNA_ENVIRONMENT", "").strip()
    mode = os.getenv("QNA_GPT_MODE", "").strip().upper()
    environment = configured_environment or (
        "테스트" if mode in {"FAKE", "SHADOW"} else "운영"
    )
    item_dates = [
        parsed.date()
        for item in (items or [])
        if (parsed := parse_registered_at(item.get("registered_at")))
        != datetime.min
    ]
    default_start = min(item_dates) if item_dates else date.today() - timedelta(days=30)
    default_end = max([date.today(), *item_dates]) if item_dates else date.today()
    if not st.session_state.get("dashboard_full_range_v1"):
        st.session_state["dashboard_date_range"] = (
            default_start,
            default_end,
        )
        st.session_state["dashboard_full_range_v1"] = True
    title_col, date_col, profile_col = st.columns(
        [4.8, 2.5, 3.2], gap="medium", vertical_alignment="center"
    )
    with title_col:
        st.markdown(
            '<div class="dashboard-heading"><h1 class="page-title">CS 운영 Dashboard</h1>'
            '<p class="page-subtitle">문의부터 승인까지 한 화면에서 처리합니다.</p></div>',
            unsafe_allow_html=True,
        )
    with date_col:
        selected = st.date_input(
            "조회 기간",
            label_visibility="collapsed",
            key="dashboard_date_range",
        )
    with profile_col:
        display_name = escape(str(identity.get("display_name") or "사용자"))
        role = escape(str(identity.get("role") or "USER"))
        avatar = escape(display_name[:1] or "U")
        st.markdown(
            '<div class="operations-top-status">'
            f'<span class="environment-chip">환경: <b>{escape(environment)}</b></span>'
            '<span class="post-lock-chip">운영 보호 적용</span>'
            f'<span class="alert-chip">알림 <b>{alert_count}</b></span>'
            f'<span class="operator-avatar">{avatar}</span>'
            f'<span class="operator-copy"><b>{display_name}</b><small>{role}</small></span>'
            "</div>",
            unsafe_allow_html=True,
        )
    if isinstance(selected, tuple) and len(selected) == 2:
        return selected[0], selected[1]
    if isinstance(selected, date):
        return selected, selected
    return default_start, default_end


def _kpi_icon_svg(code: str) -> str:
    paths = {
        "NEW": '<path d="M5 6h14v9H9l-4 4V6Z"/><circle cx="10" cy="10.5" r="1"/><circle cx="14" cy="10.5" r="1"/>',
        "DRAFTED": '<circle cx="12" cy="12" r="8"/><path d="m8.5 12 2.3 2.3 4.8-5"/>',
        "REVIEW": '<circle cx="12" cy="12" r="8"/><path d="M12 7v5l3 2"/>',
        "APPROVED": '<path d="m4 5 16 7-16 7 3-7-3-7Z"/><path d="M7 12h8"/>',
        "ATTENTION": '<path d="M12 3 2.8 20h18.4L12 3Z"/><path d="M12 9v5"/><circle cx="12" cy="17" r=".7"/>',
    }
    return (
        '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" '
        'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
        f'stroke-linejoin="round">{paths.get(code, paths["NEW"])}</svg>'
    )


def _seven_day_counts(
    items: list[WorkItem],
    state_by_key: dict[tuple[str, str, str], dict[str, Any]],
    code: str,
) -> list[int]:
    start = date.today() - timedelta(days=6)
    counts = [0] * 7
    for item in items:
        parsed = parse_registered_at(item.get("registered_at"))
        if (
            parsed == datetime.min
            or not start <= parsed.date() <= date.today()
        ):
            continue
        state = state_by_key.get(_work_item_state_key(item))
        if state is not None and _state_matches_filter(state, code):
            counts[(parsed.date() - start).days] += 1
    return counts


def _sparkline_svg(values: list[int]) -> str:
    if not any(values):
        return '<span class="kpi-no-trend">최근 7일 추세 데이터 없음</span>'
    maximum = max(values) or 1
    points = " ".join(
        f"{index * 16.5:.1f},{28 - (value / maximum * 22):.1f}"
        for index, value in enumerate(values)
    )
    return (
        '<svg class="kpi-sparkline" viewBox="0 0 100 32" '
        'preserveAspectRatio="none" aria-label="최근 7일 추세">'
        f'<polyline points="{points}"/></svg>'
    )


def _analysis(item: WorkItem) -> dict[str, Any]:
    value = item.get("analysis")
    return value if isinstance(value, dict) else {}


def _is_today(item: WorkItem) -> bool:
    parsed = parse_registered_at(item.get("registered_at"))
    return parsed != datetime.min and parsed.date() == date.today()


def _age_hours(item: WorkItem) -> float:
    parsed = parse_registered_at(item.get("registered_at"))
    if parsed == datetime.min:
        return 0.0
    now = datetime.now(parsed.tzinfo) if parsed.tzinfo else datetime.now()
    return max(0.0, (now - parsed).total_seconds() / 3600)


def dashboard_metrics(items: list[WorkItem]) -> list[dict[str, Any]]:
    today_items = [item for item in items if _is_today(item)]
    unanswered = [item for item in items if item.get("answered") is False]
    answered_today = [item for item in today_items if item.get("answered") is True]
    installation_wait = [item for item in unanswered if item.get("is_delivery") or _analysis(item).get("is_delivery")]
    ai_wait = [item for item in unanswered if item.get("queue") == "AUTO_PROCESSABLE"]
    urgent = [item for item in unanswered if item.get("priority") == "HIGH" or _age_hours(item) >= 24]
    processing_rate = round(len(answered_today) / len(today_items) * 100) if today_items else 0
    return [
        {"title": "신규 문의", "subtitle": "오늘 새로 들어온 문의", "value": len(today_items), "unit": "건", "icon": "•••", "tone": "green", "delta": "+ 3", "delta_text": "어제보다 증가"},
        {"title": "설치 대기", "subtitle": "설치 일정 확인이 필요한 문의", "value": len(installation_wait), "unit": "건", "icon": "▣", "tone": "amber", "delta": "–", "delta_text": "변동 없음"},
        {"title": "AI 답변 대기", "subtitle": "AI 초안 생성 가능한 문의", "value": len(ai_wait), "unit": "건", "icon": "◎", "tone": "mint", "delta": "↓ 2", "delta_text": "어제보다 감소"},
        {"title": "긴급 문의", "subtitle": "24시간 이상 미답변 문의", "value": len(urgent), "unit": "건", "icon": "!", "tone": "red", "delta": "↑ 1", "delta_text": "어제보다 증가"},
        {"title": "답변 완료", "subtitle": "오늘 답변을 완료한 문의", "value": len(answered_today), "unit": "건", "icon": "✓", "tone": "blue", "delta": "↑ 12", "delta_text": "어제보다 증가"},
        {"title": "오늘 처리율", "subtitle": "전체 문의 대비 처리 완료율", "value": int(processing_rate), "unit": "%", "icon": "◔", "tone": "violet", "delta": "↑ 8%", "delta_text": "어제보다 증가"},
    ]


def _state_matches_filter(state: dict[str, Any], code: str) -> bool:
    workflow = str(state.get("workflow_status") or "")
    approval = str(state.get("approval_status") or "PENDING")
    post = str(state.get("post_status") or "")
    has_draft = bool(state.get("has_draft"))
    if code == "NEW":
        return workflow == "NEW"
    if code == "DRAFTED":
        return has_draft and approval == "PENDING" and post != "POSTED"
    if code == "REVIEW":
        return (
            has_draft
            and approval == "PENDING"
            and workflow in {"REVIEW_PENDING", "NEEDS_ATTENTION"}
        )
    if code == "APPROVED":
        return approval == "APPROVED"
    if code == "ATTENTION":
        return workflow in {"NEEDS_ATTENTION", "FAILED"}
    return True


def _state_key(state: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(state.get("store_code") or ""),
        str(state.get("source_type") or ""),
        str(state.get("source_question_id") or ""),
    )


def _work_item_state_key(item: WorkItem) -> tuple[str, str, str]:
    return (
        str(item.get("store_code") or ""),
        str(item.get("source") or item.get("source_type") or ""),
        str(item.get("inquiry_id") or item.get("source_question_id") or ""),
    )


def apply_kpi_filter(
    items: list[WorkItem],
    database: Database | None,
    code: str | None,
    *,
    states: list[dict[str, Any]] | None = None,
) -> list[WorkItem]:
    if database is None or not code:
        return items
    state_map = {
        _state_key(state): state
        for state in (
            states
            if states is not None
            else ApprovalRepository(database).dashboard_states()
        )
    }
    return [
        item
        for item in items
        if (
            (state := state_map.get(_work_item_state_key(item))) is not None
            and _state_matches_filter(state, code)
        )
    ]


def render_kpi_cards(
    items: list[WorkItem],
    database: Database | None = None,
    *,
    states: list[dict[str, Any]] | None = None,
    value_counts: dict[str, int] | None = None,
) -> str | None:
    states = states if states is not None else (
        ApprovalRepository(database).dashboard_states()
        if database is not None
        else []
    )
    state_by_key = {_state_key(state): state for state in states}
    definitions = (
        ("NEW", "신규 문의", "새로 수신된 문의", "blue"),
        ("DRAFTED", "답변 초안 완료", "Program Answer 생성", "green"),
        ("REVIEW", "검토 대기", "직원 확인 필요", "amber"),
        ("APPROVED", "승인 완료", "Final Answer 승인", "violet"),
        ("ATTENTION", "오류/주의", "확인 또는 재시도 필요", "red"),
    )
    active = st.session_state.get("dashboard_kpi_filter")
    columns = st.columns(5, gap="medium")
    for column, (code, title, subtitle, tone) in zip(
        columns, definitions
    ):
        value = (
            int(value_counts.get(code, 0))
            if value_counts is not None
            else sum(_state_matches_filter(state, code) for state in states)
            if states
            else 0
        )
        today_value = sum(
            1
            for item in items
            if _is_today(item)
            and (
                (state := state_by_key.get(_work_item_state_key(item)))
                is not None
            )
            and _state_matches_filter(state, code)
        )
        with column:
            with st.container(
                key=f"kpi_filter_card_{code.lower()}_{tone}"
            ):
                trend = _seven_day_counts(items, state_by_key, code)
                st.markdown(
                    f'<div class="operations-kpi-card {tone}">'
                    f'<div class="operations-kpi-icon">{_kpi_icon_svg(code)}</div>'
                    '<div class="operations-kpi-copy">'
                    f'<span>{escape(title)}</span><strong>{value}<small>건</small></strong>'
                    f'<p>오늘 {today_value}건 · 전체 {value}건</p>'
                    f'<small>{escape(subtitle)}</small></div>'
                    f'<div class="operations-kpi-trend">{_sparkline_svg(trend)}</div>'
                    "</div>",
                    unsafe_allow_html=True,
                )
                if st.button(
                    "필터 해제" if active == code else "목록 필터",
                    key=f"kpi_filter_{code}",
                    type="primary" if active == code else "secondary",
                    width="stretch",
                ):
                    st.session_state["dashboard_kpi_filter"] = (
                        None if active == code else code
                    )
                    st.rerun()
    return st.session_state.get("dashboard_kpi_filter")


def _render_filter_bar_legacy(available_stores: dict[str, str], available_queues: list[str], available_priorities: list[str]) -> dict[str, Any]:
    keys = {"search": "dashboard_search", "status": "dashboard_answer_status", "source": "dashboard_source", "sort": "dashboard_sort", "stores": "dashboard_stores", "queues": "dashboard_queues", "priorities": "dashboard_priorities", "delivery": "dashboard_delivery_only", "limit": "dashboard_display_limit"}
    if st.session_state.get(keys["limit"]) not in (None, 10, 15, 20, 30):
        st.session_state[keys["limit"]] = 15
    if not st.session_state.get("dashboard_unclassified_filter_v1"):
        for state_key in (keys["queues"], keys["priorities"]):
            current = st.session_state.get(state_key)
            if isinstance(current, list) and UNCLASSIFIED_FILTER_VALUE not in current:
                st.session_state[state_key] = [
                    *current,
                    UNCLASSIFIED_FILTER_VALUE,
                ]
        st.session_state["dashboard_unclassified_filter_v1"] = True
    st.markdown('<div class="filter-shell-marker"></div>', unsafe_allow_html=True)
    search_col, status_col, type_col, sort_col, reset_col = st.columns([2.45, 1.2, 1.2, 1.35, 0.72], gap="small", vertical_alignment="center")
    with search_col:
        search_query = st.text_input("문의 검색", placeholder="⌕  주문번호, 상품명, 고객명 검색", label_visibility="collapsed", key=keys["search"])
    with status_col:
        answer_status = st.selectbox("답변 상태", ["전체 상태", "미답변", "답변 완료"], label_visibility="collapsed", key=keys["status"])
    with type_col:
        source_label = st.selectbox("문의 유형", ["전체 유형", *SOURCE_LABELS.values()], label_visibility="collapsed", key=keys["source"])
    with sort_col:
        sort_mode = st.selectbox("정렬 방식", ["최신 등록일 순", "기존 작업 큐 순서"], label_visibility="collapsed", key=keys["sort"])
    with reset_col:
        reset_requested = st.button("↻ 초기화", width="stretch", key="dashboard_filter_reset")
    with st.expander("고급 필터", expanded=False):
        store_col, queue_col, priority_col, option_col = st.columns(4, gap="medium")
        with store_col:
            stores = st.multiselect("스토어", list(available_stores), default=list(available_stores), format_func=lambda code: available_stores.get(code, code), key=keys["stores"])
        with queue_col:
            queues = st.multiselect("작업 큐", available_queues, default=available_queues, format_func=lambda code: "미분류" if code == UNCLASSIFIED_FILTER_VALUE else QUEUE_LABELS.get(code, code), key=keys["queues"])
        with priority_col:
            priorities = st.multiselect("우선순위", available_priorities, default=available_priorities, format_func=lambda code: "미분류" if code == UNCLASSIFIED_FILTER_VALUE else PRIORITY_LABELS.get(code, code), key=keys["priorities"])
        with option_col:
            delivery_only = st.checkbox("배송·설치 문의만 보기", key=keys["delivery"])
            display_limit = st.selectbox(
                "페이지 크기", [10, 15, 20, 30], index=1, key=keys["limit"]
            )
    if reset_requested:
        for key in [
            *keys.values(),
            "selected_inquiry_key",
            "dashboard_kpi_filter",
            "dashboard_date_range",
            "dashboard_full_range_v1",
        ]:
            st.session_state.pop(key, None)
        st.rerun()
    source_code = next((code for code, label in SOURCE_LABELS.items() if label == source_label), "ALL")
    answer_code = {"전체 상태": "ALL", "미답변": "UNANSWERED", "답변 완료": "ANSWERED"}[answer_status]
    return {"search_query": search_query, "answer_status": answer_code, "source": source_code, "sort_mode": sort_mode, "stores": stores, "queues": queues, "priorities": priorities, "delivery_only": delivery_only, "display_limit": display_limit}


def render_filter_bar(
    available_stores: dict[str, str],
    available_queues: list[str],
    available_priorities: list[str],
) -> dict[str, Any]:
    available_routes = st.session_state.get("dashboard_available_routes", [])
    routes = sorted(
        {
            str(value)
            for value in (
                available_routes if isinstance(available_routes, list) else []
            )
            if value
        }
    )
    keys = {
        "search": "dashboard_search",
        "status": "dashboard_answer_status",
        "source": "dashboard_source",
        "sort": "dashboard_sort",
        "stores": "dashboard_stores",
        "queues": "dashboard_queues",
        "priorities": "dashboard_priorities",
        "delivery": "dashboard_delivery_only",
        "limit": "dashboard_display_limit",
        "route": "dashboard_route",
    }
    if st.session_state.get(keys["limit"]) not in (None, 10, 15, 20, 30):
        st.session_state[keys["limit"]] = 15
    if not st.session_state.get("dashboard_unclassified_filter_v1"):
        for state_key in (keys["queues"], keys["priorities"]):
            current = st.session_state.get(state_key)
            if isinstance(current, list) and UNCLASSIFIED_FILTER_VALUE not in current:
                st.session_state[state_key] = [*current, UNCLASSIFIED_FILTER_VALUE]
        st.session_state["dashboard_unclassified_filter_v1"] = True

    search_col, store_col, status_col, route_col, refresh_col, reset_col = st.columns(
        [2.5, 1.5, 1.25, 1.45, 0.82, 0.72],
        gap="small",
        vertical_alignment="bottom",
    )
    with search_col:
        search_query = st.text_input(
            "문의 검색", placeholder="주문번호, 상품명, 문의 검색", key=keys["search"]
        )
    with store_col:
        stores = st.multiselect(
            "Store",
            list(available_stores),
            default=list(available_stores),
            format_func=lambda code: available_stores.get(code, code),
            key=keys["stores"],
        )
    with status_col:
        answer_status = st.selectbox(
            "문의 상태", ["전체 상태", "미답변", "답변 완료"], key=keys["status"]
        )
    with route_col:
        selected_route = st.selectbox(
            "Route", ["ALL", *routes], key=keys["route"]
        )
    with refresh_col:
        refresh_requested = st.button(
            "새로고침", width="stretch", key="dashboard_filter_refresh"
        )
    with reset_col:
        reset_requested = st.button(
            "초기화", width="stretch", key="dashboard_filter_reset"
        )
    if refresh_requested:
        st.rerun()

    with st.expander("고급 필터", expanded=False):
        source_col, queue_col, priority_col, option_col = st.columns(4, gap="medium")
        with source_col:
            source_label = st.selectbox(
                "문의 유형", ["전체 유형", *SOURCE_LABELS.values()], key=keys["source"]
            )
        with queue_col:
            queues = st.multiselect(
                "작업 큐", available_queues, default=available_queues,
                format_func=lambda code: "미분류" if code == UNCLASSIFIED_FILTER_VALUE else QUEUE_LABELS.get(code, code),
                key=keys["queues"],
            )
        with priority_col:
            priorities = st.multiselect(
                "우선순위", available_priorities, default=available_priorities,
                format_func=lambda code: "미분류" if code == UNCLASSIFIED_FILTER_VALUE else PRIORITY_LABELS.get(code, code),
                key=keys["priorities"],
            )
        with option_col:
            delivery_only = st.checkbox(
                "배송·설치 문의만 보기", key=keys["delivery"]
            )
            display_limit = st.selectbox(
                "페이지 크기", [10, 15, 20, 30], index=1, key=keys["limit"]
            )
            sort_mode = st.selectbox(
                "정렬 방식", ["최신 등록일순", "기존 작업 순서"], key=keys["sort"]
            )
    if reset_requested:
        for key in [
            *keys.values(),
            "selected_inquiry_key",
            "dashboard_kpi_filter",
            "dashboard_date_range",
            "dashboard_full_range_v1",
        ]:
            st.session_state.pop(key, None)
        st.rerun()
    source_code = next(
        (code for code, label in SOURCE_LABELS.items() if label == source_label),
        "ALL",
    )
    answer_code = {
        "전체 상태": "ALL",
        "미답변": "UNANSWERED",
        "답변 완료": "ANSWERED",
    }[answer_status]
    return {
        "search_query": search_query,
        "answer_status": answer_code,
        "source": source_code,
        "sort_mode": sort_mode,
        "stores": stores,
        "queues": queues,
        "priorities": priorities,
        "delivery_only": delivery_only,
        "display_limit": display_limit,
        "route": selected_route,
    }


def _type_label(item: WorkItem) -> tuple[str, str]:
    if item.get("is_delivery") or _analysis(item).get("is_delivery"):
        return "설치문의", "purple"
    if item.get("source") == "PRODUCT_INQUIRY":
        return "제품문의", "red"
    return "기타문의", "green"


def _status_label(item: WorkItem) -> tuple[str, str]:
    if item.get("answered") is True:
        return "답변 완료", "green"
    if item.get("queue") == "AUTO_PROCESSABLE":
        return "답변 대기", "amber"
    return "미답변", "red"


def _masked_customer(item: WorkItem) -> str:
    name = display_value(item.get("customer_name") or item.get("writer_id") or item.get("customer_id"), empty_text="-")
    return name if name == "-" or len(name) <= 1 else f"{name[0]}*{name[-1]}"


def _item_key(item: WorkItem) -> str:
    return "|".join([
        display_value(item.get("store_code"), empty_text="NO_STORE"),
        display_value(item.get("source"), empty_text="NO_SOURCE"),
        display_value(item.get("inquiry_id"), empty_text="NO_ID"),
        display_value(item.get("registered_at"), empty_text="NO_DATE"),
    ])


def _valid_order_rows(item: WorkItem) -> list[dict[str, Any]]:
    """문의 항목에서 실제 값이 있는 주문만 반환합니다."""

    orders = item.get("orders")
    if not isinstance(orders, list):
        return []

    meaningful_fields = (
        "order_id",
        "product_order_id",
        "product_name",
        "product_order_status",
        "shipping_start_date",
        "shipping_due_date",
    )

    return [
        order
        for order in orders
        if isinstance(order, dict)
        and any(
            order.get(field) not in (None, "", [], {})
            for field in meaningful_fields
        )
    ]


def _order_table_info(item: WorkItem) -> tuple[str, str, str]:
    """목록에 표시할 주문 상태, 색상, 배송 예정일을 계산합니다."""

    orders = _valid_order_rows(item)
    if not orders:
        has_candidate = bool(
            item.get("order_id")
            or item.get("product_order_ids")
        )
        if has_candidate:
            return "조회 필요", "amber", "-"
        return "주문 없음", "muted", "-"

    first_order = orders[0]
    raw_status = display_value(
        first_order.get("product_order_status"),
        empty_text="확인 필요",
    )
    status_labels = {
        "PAYMENT_WAITING": "결제 대기",
        "PAYED": "결제 완료",
        "DELIVERING": "배송 중",
        "DELIVERED": "배송 완료",
        "PURCHASE_DECIDED": "구매 확정",
        "EXCHANGED": "교환 완료",
        "CANCELED": "취소 완료",
        "RETURNED": "반품 완료",
    }
    status_text = status_labels.get(raw_status, raw_status)

    if raw_status in {"DELIVERED", "PURCHASE_DECIDED"}:
        tone = "green"
    elif raw_status in {"CANCELED", "RETURNED"}:
        tone = "red"
    elif raw_status in {"DELIVERING", "PAYED"}:
        tone = "blue"
    else:
        tone = "amber"

    due_date = display_value(
        first_order.get("shipping_due_date"),
        empty_text="-",
    )
    if due_date != "-" and "T" in due_date:
        due_date = due_date.split("T", 1)[0]
    elif due_date != "-" and len(due_date) >= 10:
        due_date = due_date[:10]

    return status_text, tone, due_date


def render_inquiry_table(items: list[WorkItem], total_count: int) -> None:
    st.session_state.setdefault("selected_inquiry_key", None)
    heading_col, view_all_col = st.columns([8, 1], vertical_alignment="center")
    with heading_col:
        st.markdown(
            f'<div class="table-heading-copy"><h3>최근 문의 목록</h3>'
            f'<span>{total_count}건 · 주문 상태 자동 표시</span></div>',
            unsafe_allow_html=True,
        )
    with view_all_col:
        if st.button("전체 보기 ›", key="show_all_inquiries", width="stretch"):
            st.session_state["current_page"] = "inquiries"
            st.session_state["selected_inquiry_key"] = None
            st.rerun()

    column_widths = [1.18, 0.88, 2.25, 1.05, 1.02, 1.0, 1.05, 1.02, 0.68]
    header_labels = [
        "문의번호",
        "유형",
        "제목",
        "고객명",
        "등록일",
        "답변",
        "주문 상태",
        "배송 예정",
        "상세",
    ]

    headers = st.columns(column_widths, gap="small")
    for column, label in zip(headers, header_labels):
        column.markdown(
            f'<div class="table-header-cell">{escape(label)}</div>',
            unsafe_allow_html=True,
        )

    for index, item in enumerate(items):
        key = _item_key(item)
        selected = st.session_state.get("selected_inquiry_key") == key
        type_text, type_tone = _type_label(item)
        status_text, status_tone = _status_label(item)
        order_text, order_tone, due_date = _order_table_info(item)
        registered_text = format_datetime_kst(
            item.get("registered_at")
        )
        inquiry_id = display_value(item.get("inquiry_id"), empty_text=f"문의-{index + 1}")
        title = display_value(item.get("title") or item.get("content"), empty_text="제목 없음")

        row = st.columns(column_widths, gap="small", vertical_alignment="center")
        row[0].markdown(f'<div class="table-cell table-id-cell">{escape(inquiry_id)}</div>', unsafe_allow_html=True)
        row[1].markdown(f'<div class="table-cell"><span class="badge {type_tone}">{escape(type_text)}</span></div>', unsafe_allow_html=True)
        row[2].markdown(f'<div class="table-cell title-cell" title="{escape(title)}">{escape(title)}</div>', unsafe_allow_html=True)
        row[3].markdown(f'<div class="table-cell">{escape(_masked_customer(item))}</div>', unsafe_allow_html=True)
        row[4].markdown(f'<div class="table-cell table-date-cell">{escape(registered_text)}</div>', unsafe_allow_html=True)
        row[5].markdown(f'<div class="table-cell"><span class="badge {status_tone}">{escape(status_text)}</span></div>', unsafe_allow_html=True)
        row[6].markdown(f'<div class="table-cell"><span class="badge {order_tone}">{escape(order_text)}</span></div>', unsafe_allow_html=True)
        row[7].markdown(f'<div class="table-cell table-date-cell order-date-cell">{escape(due_date)}</div>', unsafe_allow_html=True)

        if row[8].button(
            "닫기" if selected else "보기",
            key=f"inquiry_{index}_{abs(hash(key))}",
            width="stretch",
        ):
            st.session_state["selected_inquiry_key"] = None if selected else key
            st.rerun()

        if selected:
            item_token = get_work_item_state_key(item, "detail").rsplit("_", 1)[-1]
            panel_keys = {
                "order": get_work_item_state_key(item, "open_order_panel"),
                "dps": get_work_item_state_key(item, "open_dps_panel"),
                "ai": get_work_item_state_key(item, "open_ai_panel"),
            }

            def activate_panel(panel_name: str) -> None:
                target_key = panel_keys[panel_name]
                will_open = not bool(st.session_state.get(target_key, False))
                for state_key in panel_keys.values():
                    st.session_state[state_key] = False
                if will_open:
                    st.session_state[target_key] = True
                    st.session_state["detail_scroll_target"] = f"detail-{panel_name}-{item_token}"
                else:
                    st.session_state["detail_scroll_target"] = f"detail-top-{item_token}"

            st.markdown(
                f'<div id="detail-top-{item_token}" class="inline-detail-anchor"></div>',
                unsafe_allow_html=True,
            )

            with st.container(key=f"detail_toolbar_shell_{item_token}"):
                title_col, order_col, dps_col, ai_col, close_col = st.columns(
                    [6.4, 1.0, 0.9, 0.9, 1.05],
                    gap="small",
                    vertical_alignment="center",
                )
                with title_col:
                    st.markdown(
                        f'<div class="detail-toolbar-marker"><div class="detail-toolbar-copy">'
                        f'<span>선택한 문의</span><strong>{escape(inquiry_id)}</strong>'
                        f'<small>{escape(title)}</small></div></div>',
                        unsafe_allow_html=True,
                    )
                with order_col:
                    if st.button(
                        "주문",
                        key=f"toolbar_order_{index}_{abs(hash(key))}",
                        width="stretch",
                        type="primary" if st.session_state.get(panel_keys["order"], False) else "secondary",
                    ):
                        activate_panel("order")
                        st.rerun()
                with dps_col:
                    if st.button(
                        "DPS",
                        key=f"toolbar_dps_{index}_{abs(hash(key))}",
                        width="stretch",
                        type="primary" if st.session_state.get(panel_keys["dps"], False) else "secondary",
                    ):
                        activate_panel("dps")
                        st.rerun()
                with ai_col:
                    if st.button(
                        "AI",
                        key=f"toolbar_ai_{index}_{abs(hash(key))}",
                        width="stretch",
                        type="primary" if st.session_state.get(panel_keys["ai"], False) else "secondary",
                    ):
                        activate_panel("ai")
                        st.rerun()
                with close_col:
                    if st.button(
                        "상세 닫기",
                        key=f"toolbar_close_{index}_{abs(hash(key))}",
                        width="stretch",
                    ):
                        st.session_state["selected_inquiry_key"] = None
                        st.session_state.pop("detail_scroll_target", None)
                        st.rerun()

            st.markdown('<div class="detail-toolbar-spacer" aria-hidden="true"></div>', unsafe_allow_html=True)

            st.markdown('<div class="detail-panel-shell">', unsafe_allow_html=True)
            render_work_item(item)
            st.markdown('</div><div class="inline-detail-end"></div>', unsafe_allow_html=True)

            scroll_target = st.session_state.pop("detail_scroll_target", None)
            if scroll_target:
                components.html(
                    f"""
                    <script>
                    const targetId = {scroll_target!r};
                    const scrollToTarget = () => {{
                      const target = window.parent.document.getElementById(targetId);
                      if (target) {{
                        target.scrollIntoView({{behavior: 'smooth', block: 'start'}});
                      }}
                    }};
                    window.setTimeout(scrollToTarget, 180);
                    window.setTimeout(scrollToTarget, 500);
                    </script>
                    """,
                    height=0,
                    width=0,
                )
