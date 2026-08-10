from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import streamlit as st

from services.work_queue_service import (
    WorkItem,
    parse_registered_at,
)
from ui.components import (
    PRIORITY_LABELS,
    QUEUE_LABELS,
    SOURCE_LABELS,
)
from ui.dashboard import render_inquiry_table


def _search_text(work_item: WorkItem) -> str:
    """
    문의 관리 검색에 사용할 문자열을 생성합니다.
    """

    search_fields: list[Any] = [
        work_item.get("title"),
        work_item.get("content"),
        work_item.get("product_name"),
        work_item.get("product_option"),
        work_item.get("order_id"),
        work_item.get("product_order_ids"),
        work_item.get("inquiry_id"),
        work_item.get("customer_name"),
        work_item.get("customer_id"),
        work_item.get("writer_id"),
        work_item.get("store_name"),
    ]

    orders = work_item.get("orders")

    if isinstance(orders, list):
        for order in orders:
            if not isinstance(order, dict):
                continue

            search_fields.extend(
                [
                    order.get("order_id"),
                    order.get("product_order_id"),
                    order.get("product_name"),
                    order.get("receiver_name"),
                ]
            )

    return " ".join(
        str(value)
        for value in search_fields
        if value not in (None, "")
    ).lower()


def _filter_items(
    work_items: list[WorkItem],
    *,
    search_query: str,
    answer_status: str,
    source: str,
    stores: list[str],
    queues: list[str],
    priorities: list[str],
    delivery_only: bool,
    start_date: date,
    end_date: date,
) -> list[WorkItem]:
    """
    문의 관리 화면의 필터 조건을 적용합니다.
    """

    normalized_query = search_query.strip().lower()
    filtered: list[WorkItem] = []

    for item in work_items:
        registered_at = parse_registered_at(
            item.get("registered_at")
        )

        if (
            registered_at != datetime.min
            and not (
                start_date
                <= registered_at.date()
                <= end_date
            )
        ):
            continue

        if (
            stores
            and item.get("store_code")
            not in stores
        ):
            continue

        if (
            source != "ALL"
            and item.get("source") != source
        ):
            continue

        if (
            queues
            and item.get("queue") not in queues
        ):
            continue

        if (
            priorities
            and item.get("priority")
            not in priorities
        ):
            continue

        if (
            answer_status == "UNANSWERED"
            and item.get("answered") is not False
        ):
            continue

        if (
            answer_status == "ANSWERED"
            and item.get("answered") is not True
        ):
            continue

        analysis = item.get("analysis")
        analysis_data = (
            analysis
            if isinstance(analysis, dict)
            else {}
        )

        if (
            delivery_only
            and not (
                item.get("is_delivery")
                or analysis_data.get("is_delivery")
            )
        ):
            continue

        if (
            normalized_query
            and normalized_query
            not in _search_text(item)
        ):
            continue

        filtered.append(item)

    return filtered


def _sort_items(
    work_items: list[WorkItem],
    sort_mode: str,
) -> list[WorkItem]:
    """
    선택된 방식으로 문의 목록을 정렬합니다.
    """

    if sort_mode == "오래된 등록일 순":
        return sorted(
            work_items,
            key=lambda item: parse_registered_at(
                item.get("registered_at")
            ),
        )

    if sort_mode == "높은 우선순위 순":
        priority_order = {
            "HIGH": 0,
            "MEDIUM": 1,
            "NORMAL": 2,
        }

        return sorted(
            work_items,
            key=lambda item: (
                priority_order.get(
                    str(item.get("priority") or ""),
                    99,
                ),
                -parse_registered_at(
                    item.get("registered_at")
                ).timestamp()
                if parse_registered_at(
                    item.get("registered_at")
                ) != datetime.min
                else 0,
            ),
        )

    return sorted(
        work_items,
        key=lambda item: parse_registered_at(
            item.get("registered_at")
        ),
        reverse=True,
    )


def _reset_filters() -> None:
    """
    문의 관리 전용 필터 상태를 초기화합니다.
    """

    keys = [
        "inquiry_search",
        "inquiry_answer_status",
        "inquiry_source",
        "inquiry_sort_mode",
        "inquiry_date_range",
        "inquiry_stores",
        "inquiry_queues",
        "inquiry_priorities",
        "inquiry_delivery_only",
        "inquiry_display_limit",
        "selected_inquiry_key",
    ]

    for key in keys:
        st.session_state.pop(
            key,
            None,
        )


def _render_page_header(
    total_count: int,
    filtered_count: int,
) -> None:
    """
    문의 관리 페이지 상단 제목을 표시합니다.
    """

    header_html = (
        '<div class="dashboard-heading">'
        '<h1 class="page-title">문의 관리</h1>'
        '<p class="page-subtitle">'
        '전체 문의를 검색하고 상세 내용과 처리 상태를 확인하세요.'
        '</p>'
        '</div>'
    )

    st.markdown(
        header_html,
        unsafe_allow_html=True,
    )

    metric_columns = st.columns(
        4,
        gap="medium",
    )

    unanswered_count = 0
    answered_count = 0

    metric_values = [
        (
            "전체 문의",
            total_count,
        ),
        (
            "검색 결과",
            filtered_count,
        ),
        (
            "미답변",
            unanswered_count,
        ),
        (
            "답변 완료",
            answered_count,
        ),
    ]

    for column, (label, value) in zip(
        metric_columns,
        metric_values,
    ):
        column.metric(
            label,
            value,
        )


def render_inquiries_page(
    work_items: list[WorkItem],
    configured_stores: list[Any],
) -> None:
    """
    전체 문의 조회 및 상세 확인 화면을 렌더링합니다.
    """

    available_stores = {
        str(item.get("store_code")):
        str(item.get("store_name"))
        for item in work_items
        if item.get("store_code")
    }

    if not available_stores:
        available_stores = {
            store.code: store.name
            for store in configured_stores
        }

    available_queues = sorted(
        {
            str(item.get("queue"))
            for item in work_items
            if item.get("queue")
        }
    )

    available_priorities = [
        code
        for code in (
            "HIGH",
            "MEDIUM",
            "NORMAL",
        )
        if any(
            item.get("priority") == code
            for item in work_items
        )
    ]

    top_columns = st.columns(
        [3.2, 1.25, 1.25, 1.4, 1.75, 0.8],
        gap="small",
        vertical_alignment="center",
    )

    with top_columns[0]:
        search_query = st.text_input(
            "문의 검색",
            placeholder=(
                "주문번호, 문의번호, 상품명, "
                "고객명 또는 문의 내용 검색"
            ),
            label_visibility="collapsed",
            key="inquiry_search",
        )

    with top_columns[1]:
        answer_label = st.selectbox(
            "답변 상태",
            options=[
                "전체 상태",
                "미답변",
                "답변 완료",
            ],
            label_visibility="collapsed",
            key="inquiry_answer_status",
        )

    with top_columns[2]:
        source_label = st.selectbox(
            "문의 유형",
            options=[
                "전체 유형",
                *SOURCE_LABELS.values(),
            ],
            label_visibility="collapsed",
            key="inquiry_source",
        )

    with top_columns[3]:
        sort_mode = st.selectbox(
            "정렬 방식",
            options=[
                "최신 등록일 순",
                "오래된 등록일 순",
                "높은 우선순위 순",
            ],
            label_visibility="collapsed",
            key="inquiry_sort_mode",
        )

    with top_columns[4]:
        selected_dates = st.date_input(
            "조회 기간",
            value=(
                date.today() - timedelta(days=365),
                date.today(),
            ),
            label_visibility="collapsed",
            key="inquiry_date_range",
        )

    with top_columns[5]:
        reset_requested = st.button(
            "↻ 초기화",
            width="stretch",
            key="inquiry_filter_reset",
        )

    if reset_requested:
        _reset_filters()
        st.rerun()

    if (
        isinstance(selected_dates, tuple)
        and len(selected_dates) == 2
    ):
        start_date = selected_dates[0]
        end_date = selected_dates[1]

    elif isinstance(selected_dates, date):
        start_date = selected_dates
        end_date = selected_dates

    else:
        start_date = (
            date.today()
            - timedelta(days=365)
        )
        end_date = date.today()

    with st.expander(
        "고급 필터",
        expanded=False,
    ):
        filter_columns = st.columns(
            4,
            gap="medium",
        )

        with filter_columns[0]:
            stores = st.multiselect(
                "스토어",
                options=list(available_stores),
                default=list(available_stores),
                format_func=lambda code: (
                    available_stores.get(
                        code,
                        code,
                    )
                ),
                key="inquiry_stores",
            )

        with filter_columns[1]:
            queues = st.multiselect(
                "작업 큐",
                options=available_queues,
                default=available_queues,
                format_func=lambda code: (
                    QUEUE_LABELS.get(
                        code,
                        code,
                    )
                ),
                key="inquiry_queues",
            )

        with filter_columns[2]:
            priorities = st.multiselect(
                "우선순위",
                options=available_priorities,
                default=available_priorities,
                format_func=lambda code: (
                    PRIORITY_LABELS.get(
                        code,
                        code,
                    )
                ),
                key="inquiry_priorities",
            )

        with filter_columns[3]:
            delivery_only = st.checkbox(
                "배송·설치 문의만 보기",
                key="inquiry_delivery_only",
            )

            display_limit = st.selectbox(
                "표시 개수",
                options=[
                    20,
                    50,
                    100,
                    200,
                ],
                index=1,
                key="inquiry_display_limit",
            )

    source_code = "ALL"

    for code, label in SOURCE_LABELS.items():
        if label == source_label:
            source_code = code
            break

    answer_code = {
        "전체 상태": "ALL",
        "미답변": "UNANSWERED",
        "답변 완료": "ANSWERED",
    }[answer_label]

    filtered_items = _filter_items(
        work_items,
        search_query=search_query,
        answer_status=answer_code,
        source=source_code,
        stores=stores,
        queues=queues,
        priorities=priorities,
        delivery_only=delivery_only,
        start_date=start_date,
        end_date=end_date,
    )

    filtered_items = _sort_items(
        filtered_items,
        sort_mode,
    )

    unanswered_count = sum(
        item.get("answered") is False
        for item in filtered_items
    )

    answered_count = sum(
        item.get("answered") is True
        for item in filtered_items
    )

    header_html = (
        '<div class="dashboard-heading">'
        '<h1 class="page-title">문의 관리</h1>'
        '<p class="page-subtitle">'
        '전체 문의를 검색하고 상세 내용과 처리 상태를 확인하세요.'
        '</p>'
        '</div>'
    )

    st.markdown(
        header_html,
        unsafe_allow_html=True,
    )

    summary_columns = st.columns(
        4,
        gap="medium",
    )

    summary_values = [
        (
            "전체 문의",
            len(work_items),
        ),
        (
            "검색 결과",
            len(filtered_items),
        ),
        (
            "미답변",
            unanswered_count,
        ),
        (
            "답변 완료",
            answered_count,
        ),
    ]

    for column, (label, value) in zip(
        summary_columns,
        summary_values,
    ):
        column.metric(
            label,
            value,
        )

    visible_items = filtered_items[
        :display_limit
    ]

    if not visible_items:
        st.info(
            "현재 조건에 맞는 문의가 없습니다."
        )
        return

    render_inquiry_table(
        visible_items,
        len(filtered_items),
    )

    if not st.session_state.get(
        "selected_inquiry_key"
    ):
        st.caption(
            "오른쪽의 ‘보기’ 버튼을 누르면 "
            "선택한 문의 행 바로 아래에서 "
            "상세 내용을 확인할 수 있습니다."
        )