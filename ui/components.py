import hashlib
import html
import re
from typing import Any

import streamlit as st
from core.time_utils import format_datetime_kst
from dps.context import (
    DpsLookupContextError,
    create_dps_lookup_context,
)

try:
    import pyperclip
except ImportError:  # pragma: no cover
    pyperclip = None

from api.auth import get_access_token
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.dps_repository import DpsRepository
from repositories.inquiry_repository import InquiryRepository
from answer.source_adapter import answer_request_from_inquiry
from services.answer_service import AnswerService
from services.dps_lookup_policy import DpsLookupPolicy
from services.dps_agent_client import (
    auto_connect_dps_tab,
    confirm_dps_login,
    connect_current_dps_window,
    connect_dps_window,
    list_dps_chrome_windows,
    disconnect_current_dps_window,
    get_dps_agent_status,
    lookup_dps_order,
    mark_dps_logged_out,
    open_dps_browser,
    open_dps_login,
    refresh_dps_session,
    run_dps_diagnostics,
    start_dps_agent,
)
from services.order_service import (
    attach_orders_to_work_item,
    lookup_work_item_orders,
)
from services.work_queue_service import WorkItem
from ui.answer_presenter import build_answer_display
from ui.dps_presenter import build_dps_display


SOURCE_LABELS = {
    "PRODUCT_INQUIRY": "상품문의",
    "CUSTOMER_INQUIRY": "고객문의",
}

PRIORITY_LABELS = {
    "HIGH": "높음",
    "MEDIUM": "보통",
    "NORMAL": "일반",
}

QUEUE_LABELS = {
    "AUTO_PROCESSABLE": "자동 처리 가능",
    "CUSTOMER_CONFIRMATION_REQUIRED": "고객 확인 필요",
    "ORDER_LOOKUP_FAILED": "주문 확인 필요",
    "GENERAL_INQUIRY": "일반 문의",
}


ORDER_STATUS_LABELS = {
    "PAYMENT_WAITING": "결제 대기",
    "PAYED": "결제 완료",
    "DELIVERING": "배송 중",
    "DELIVERED": "배송 완료",
    "PURCHASE_DECIDED": "구매 확정",
    "EXCHANGED": "교환 완료",
    "CANCELED": "취소 완료",
    "RETURNED": "반품 완료",
}


def format_order_status(value: Any) -> str:
    """네이버 주문 상태 코드를 한글 표시값으로 변환합니다."""

    status = display_value(value, empty_text="확인 필요")
    return ORDER_STATUS_LABELS.get(status, status)


def format_date_value(value: Any, *, empty_text: str = "확인 필요") -> str:
    """ISO 형식 날짜를 화면용 날짜 문자열로 간단히 정리합니다."""

    text = display_value(value, empty_text=empty_text)
    if text == empty_text:
        return text

    normalized = text.strip()
    if "T" in normalized:
        return normalized.split("T", 1)[0]
    if len(normalized) >= 10 and normalized[4] == "-" and normalized[7] == "-":
        return normalized[:10]
    return normalized


def display_value(
    value: Any,
    *,
    empty_text: str = "-",
) -> str:
    """빈 값을 대시보드용 문자열로 안전하게 변환합니다."""

    if value is None:
        return empty_text
    if isinstance(value, str) and not value.strip():
        return empty_text
    if isinstance(value, (list, tuple, set)):
        values = [
            str(item).strip()
            for item in value
            if item not in (None, "")
        ]
        return ", ".join(values) if values else empty_text
    return str(value)


def mask_phone_number(value: Any) -> str:
    """연락처의 가운데 숫자를 마스킹합니다."""

    phone = display_value(value, empty_text="확인되지 않음")
    if phone == "확인되지 않음":
        return phone

    digits = re.sub(r"\D", "", phone)

    if len(digits) == 11:
        return f"{digits[:3]}-****-{digits[-4:]}"
    if len(digits) == 10:
        return f"{digits[:3]}-***-{digits[-4:]}"
    if len(digits) >= 7:
        return f"{digits[:3]}-****-{digits[-4:]}"

    return "일부 번호 확인 불가"


def get_work_item_state_key(
    work_item: WorkItem,
    purpose: str,
) -> str:
    """스토어와 문의를 함께 반영한 안정적인 Streamlit 상태 키를 만듭니다."""

    identity_parts = [
        work_item.get("store_code"),
        work_item.get("source"),
        work_item.get("inquiry_id"),
        work_item.get("registered_at"),
        work_item.get("title"),
    ]
    identity = "|".join(
        str(part)
        for part in identity_parts
        if part not in (None, "")
    )
    digest = hashlib.sha256(
        identity.encode("utf-8")
    ).hexdigest()[:16]
    return f"{purpose}_{digest}"


def _write_fields(fields: list[tuple[str, Any]]) -> None:
    """필드 목록을 두 열로 표시합니다."""

    for start in range(0, len(fields), 2):
        columns = st.columns(2)
        for column, field in zip(columns, fields[start : start + 2]):
            label, value = field
            with column:
                st.caption(label)
                st.write(display_value(value))




def _copy_value_button(label: str, value: Any, *, key: str) -> None:
    """로컬 Windows 클립보드로 식별값을 복사하는 작은 버튼입니다."""

    text = display_value(value, empty_text="")
    disabled = not bool(text) or pyperclip is None
    if st.button(
        "📋",
        key=key,
        disabled=disabled,
        help=f"{label}를 클립보드에 복사합니다.",
        width="content",
    ):
        try:
            pyperclip.copy(text)
            st.toast(f"{label} 복사 완료", icon="✅")
        except Exception as error:
            st.warning(f"복사하지 못했습니다: {error.__class__.__name__}")


def _render_identifier_copy_row(
    fields: list[tuple[str, Any]],
    *,
    key_prefix: str,
) -> None:
    """주문번호·문의번호·상품번호를 값과 복사 버튼으로 표시합니다."""

    valid = [(label, value) for label, value in fields if display_value(value, empty_text="")]
    if not valid:
        return

    columns = st.columns(len(valid), gap="small")
    for index, (column, (label, value)) in enumerate(zip(columns, valid)):
        with column:
            with st.container(border=True):
                st.caption(label)
                value_col, button_col = st.columns([4, 1], gap="small", vertical_alignment="center")
                value_col.code(display_value(value), language=None, wrap_lines=True)
                with button_col:
                    _copy_value_button(label, value, key=f"{key_prefix}_{index}")


def render_summary_metrics(work_items: list[WorkItem]) -> None:
    """통합 작업 큐의 상단 요약 지표를 표시합니다."""

    counts = {
        "전체 문의": len(work_items),
        "높은 우선순위": sum(
            item.get("priority") == "HIGH"
            for item in work_items
        ),
        "자동 처리 가능": sum(
            item.get("queue") == "AUTO_PROCESSABLE"
            for item in work_items
        ),
        "고객 확인 필요": sum(
            item.get("queue")
            == "CUSTOMER_CONFIRMATION_REQUIRED"
            for item in work_items
        ),
        "주문 확인 필요": sum(
            item.get("queue") == "ORDER_LOOKUP_FAILED"
            for item in work_items
        ),
        "일반 문의": sum(
            item.get("queue") == "GENERAL_INQUIRY"
            for item in work_items
        ),
    }

    for column, (label, value) in zip(
        st.columns(len(counts)),
        counts.items(),
    ):
        column.metric(label, value)


def _render_order(order: dict[str, Any], number: int) -> None:
    with st.container(border=True):
        st.markdown(f"**주문 {number}**")
        address_parts = [
            display_value(order.get("base_address"), empty_text=""),
            display_value(order.get("detailed_address"), empty_text=""),
        ]
        address = " ".join(
            part for part in address_parts if part
        ) or "확인되지 않음"

        _render_identifier_copy_row(
            [
                ("네이버 주문번호", order.get("order_id")),
                ("상품주문번호", order.get("product_order_id")),
            ],
            key_prefix=f"order_identifier_copy_{number}",
        )

        _write_fields(
            [
                ("네이버 주문번호", order.get("order_id")),
                ("상품주문번호", order.get("product_order_id")),
                (
                    "주문일",
                    format_date_value(
                        order.get("order_date")
                        or order.get("order_created_at"),
                        empty_text="없음",
                    ),
                ),
                ("상품명", order.get("product_name")),
                (
                    "옵션",
                    display_value(
                        order.get("product_option"),
                        empty_text="없음",
                    ),
                ),
                ("수량", order.get("quantity")),
                (
                    "상품주문 상태",
                    format_order_status(
                        order.get("product_order_status")
                    ),
                ),
                ("발주 상태", order.get("place_order_status")),
                (
                    "배송 시작일",
                    format_date_value(
                        order.get("shipping_start_date"),
                        empty_text="없음",
                    ),
                ),
                (
                    "배송 예정일",
                    format_date_value(
                        order.get("shipping_due_date"),
                        empty_text="없음",
                    ),
                ),
                ("수취인", order.get("receiver_name")),
                (
                    "연락처",
                    mask_phone_number(order.get("receiver_tel")),
                ),
                ("주소", address),
                (
                    "배송 메모",
                    display_value(
                        order.get("shipping_memo"),
                        empty_text="없음",
                    ),
                ),
            ]
        )


def _is_meaningful_order(order: Any) -> bool:
    """화면에 표시할 실제 주문 데이터가 있는지 확인합니다."""

    if not isinstance(order, dict):
        return False

    meaningful_fields = (
        "order_id",
        "product_order_id",
        "product_name",
        "product_order_status",
        "order_date",
        "order_created_at",
        "place_order_status",
        "shipping_start_date",
        "shipping_due_date",
        "receiver_name",
        "receiver_tel",
        "base_address",
        "detailed_address",
        "shipping_memo",
    )

    return any(
        order.get(field) not in (None, "", [], {})
        for field in meaningful_fields
    )


def _valid_orders(work_item: WorkItem) -> list[dict[str, Any]]:
    """빈 주문 객체를 제외하고 표시 가능한 주문만 반환합니다."""

    orders = work_item.get("orders")

    if not isinstance(orders, list):
        return []

    return [
        order
        for order in orders
        if _is_meaningful_order(order)
    ]


def _render_order_summary(
    work_item: WorkItem,
    orders: list[dict[str, Any]],
) -> None:
    """주문 상세을 열기 전 핵심 주문 정보를 요약해 표시합니다."""

    inquiry_order_id = display_value(
        work_item.get("order_id"),
        empty_text="확인되지 않음",
    )
    inquiry_product_order_ids = display_value(
        work_item.get("product_order_ids"),
        empty_text="확인되지 않음",
    )

    if not orders:
        _render_detail_fields(
            [
                ("네이버 주문번호", inquiry_order_id),
                ("상품주문번호", inquiry_product_order_ids),
                ("연결 주문 수", "0건"),
                ("조회 상태", "주문 정보 확인 필요"),
            ]
        )

        st.markdown(
            '<div class="detail-empty">'
            '<strong>연결된 주문 상세 정보를 확인하지 못했습니다.</strong><br>'
            '문의에 주문번호 또는 상품주문번호가 포함되어 있는지 확인하고, '
            '필요하면 고객에게 주문정보를 요청해 주세요.'
            '</div>',
            unsafe_allow_html=True,
        )
        return

    first_order = orders[0]

    _render_detail_fields(
        [
            ("연결 주문 수", f"{len(orders)}건"),
            ("네이버 주문번호", first_order.get("order_id")),
            ("상품주문번호", first_order.get("product_order_id")),
            (
                "상품주문 상태",
                format_order_status(
                    first_order.get("product_order_status")
                ),
            ),
            (
                "주문일",
                format_date_value(
                    first_order.get("order_date")
                    or first_order.get("order_created_at")
                ),
            ),
            (
                "배송 예정일",
                format_date_value(
                    first_order.get("shipping_due_date")
                ),
            ),
        ]
    )


def _render_order_section(work_item: WorkItem) -> None:
    """주문 정보를 자동 조회하고 현재 주문 패널 안에 표시합니다."""

    result_key = get_work_item_state_key(
        work_item,
        "order_lookup_result",
    )
    looked_up_key = get_work_item_state_key(
        work_item,
        "order_lookup_completed",
    )
    refresh_key = get_work_item_state_key(
        work_item,
        "order_lookup_refresh",
    )

    existing_orders = _valid_orders(work_item)
    lookup_result = st.session_state.get(result_key)
    lookup_completed = bool(
        st.session_state.get(looked_up_key, False)
    )

    if not existing_orders and not lookup_completed:
        with st.spinner("네이버 주문 정보를 조회하고 있습니다..."):
            try:
                access_token = get_access_token()
                lookup_result = lookup_work_item_orders(
                    access_token,
                    dict(work_item),
                )
            except Exception as error:
                lookup_result = {
                    "success": False,
                    "lookup_number": None,
                    "lookup_type": None,
                    "orders": [],
                    "error_code": "ORDER_UI_ERROR",
                    "error_message": (
                        "주문 정보를 조회하는 중 오류가 발생했습니다: "
                        f"{error.__class__.__name__}"
                    ),
                    "cached": False,
                    "queried_at": "확인되지 않음",
                }

        st.session_state[result_key] = lookup_result
        st.session_state[looked_up_key] = True

    effective_item: WorkItem = work_item
    if isinstance(lookup_result, dict):
        effective_item = attach_orders_to_work_item(
            dict(work_item),
            lookup_result,
        )

    orders = _valid_orders(effective_item)

    _render_order_summary(
        effective_item,
        orders,
    )

    if isinstance(lookup_result, dict):
        if lookup_result.get("success"):
            cache_text = (
                "캐시 사용"
                if lookup_result.get("cached")
                else "API 조회"
            )
            st.caption(
                "주문 조회 완료 · "
                f"{cache_text} · "
                f"조회번호 {display_value(lookup_result.get('lookup_number'))}"
            )
        else:
            st.warning(
                display_value(
                    lookup_result.get("error_message"),
                    empty_text="주문 정보를 확인하지 못했습니다.",
                )
            )

    if st.button(
        "↻ 주문 정보 새로고침",
        key=refresh_key,
        type="primary",
        help="5분 캐시를 무시하고 네이버 주문 API를 다시 조회합니다.",
    ):
        with st.spinner("주문 정보를 다시 조회하고 있습니다..."):
            try:
                access_token = get_access_token()
                refreshed_result = lookup_work_item_orders(
                    access_token,
                    dict(work_item),
                    force_refresh=True,
                )
            except Exception as error:
                refreshed_result = {
                    "success": False,
                    "lookup_number": None,
                    "lookup_type": None,
                    "orders": [],
                    "error_code": "ORDER_UI_ERROR",
                    "error_message": (
                        "주문 정보를 다시 조회하는 중 오류가 발생했습니다: "
                        f"{error.__class__.__name__}"
                    ),
                    "cached": False,
                    "queried_at": "확인되지 않음",
                }

        st.session_state[result_key] = refreshed_result
        st.session_state[looked_up_key] = True
        st.rerun()

    if not orders:
        return

    show_orders = st.checkbox(
        "수취인·주소·배송 메모 등 주문 상세 정보 보기",
        key=get_work_item_state_key(
            work_item,
            "show_order_details",
        ),
    )

    if not show_orders:
        st.caption(
            "체크하면 수취인, 연락처, 주소, 배송 일정과 배송 메모를 확인할 수 있습니다."
        )
        return

    for number, order in enumerate(
        orders,
        start=1,
    ):
        _render_order(
            order,
            number,
        )


def _render_ai_answer_draft(work_item: WorkItem) -> None:
    """명시적인 버튼 클릭으로만 문의별 답변 초안을 생성합니다."""

    st.markdown("**자동 Q&A 답변 초안**")
    st.caption(
        "기존 자동 Q&A 규칙만 사용하며 OpenAI와 네이버 등록 API는 "
        "호출하지 않습니다. 생성된 프로그램 답변은 반드시 검토해 주세요."
    )

    result_key = get_work_item_state_key(
        work_item,
        "answer_generation_result",
    )
    running_key = get_work_item_state_key(
        work_item,
        "answer_generation_running",
    )
    generate_key = get_work_item_state_key(
        work_item,
        "answer_generate",
    )
    database = Database()
    inquiry = None
    latest_draft = None
    latest_dps = None
    dps_decision = None
    posted = False
    try:
        database.initialize()
        inquiry = InquiryRepository(database).get_by_source(
            str(work_item.get("store_code") or ""),
            str(work_item.get("source") or ""),
            str(work_item.get("inquiry_id") or ""),
        )
        if inquiry is not None:
            answer_repository = AnswerRepository(database)
            latest_draft = answer_repository.latest_for_inquiry(
                inquiry["id"]
            )
            posted = answer_repository.is_inquiry_posted(inquiry["id"])
            request = answer_request_from_inquiry(inquiry)
            dps_decision = DpsLookupPolicy().decide(request)
            if dps_decision.lookup_required and dps_decision.order_id:
                latest_dps = DpsRepository(
                    database
                ).get_preferred_for_inquiry_and_order(
                    inquiry["id"], dps_decision.order_id
                )
    except Exception:
        st.warning(
            "답변 저장소를 확인하지 못했습니다. 기존 문의 조회 기능은 "
            "계속 사용할 수 있습니다."
        )
        return

    if inquiry is None:
        st.info(
            "문의가 아직 작업 저장소에 동기화되지 않아 답변을 생성할 수 "
            "없습니다. 데이터를 새로고침한 뒤 다시 시도해 주세요."
        )
        return

    running = bool(st.session_state.get(running_key, False))
    if dps_decision is not None:
        dps_display = build_dps_display(
            lookup_required=dps_decision.lookup_required,
            order_id=dps_decision.order_id,
            latest_row=latest_dps,
            pending_status=dps_decision.status.value,
        )
        st.markdown("##### DPS 답변 연동 상태")
        required_col, status_col, cache_col = st.columns(3)
        required_col.metric(
            "DPS 조회 필요",
            "예" if dps_display["lookup_required"] else "아니요",
        )
        status_col.metric("DPS 조회 상태", dps_display["status_label"])
        cache_col.metric(
            "캐시 사용", "예" if dps_display["cache_used"] else "아니요"
        )
        st.caption(
            "사용 주문번호: "
            + (str(dps_display["order_id"]) if dps_display["order_id"] else "없음")
        )
        if dps_display["queried_at"]:
            st.caption(
                f"마지막 조회: {dps_display['queried_at']} · "
                f"소요시간: {dps_display['elapsed_seconds'] or 0}초"
            )
        detail_values = (
            ("배송 상태", dps_display["delivery_status"]),
            ("설치 상태", dps_display["installation_status"]),
            (
                "설치 예정일",
                dps_display["installation_date_value"]
                or dps_display["installation_date_status_message"],
            ),
            ("DPS 판매번호", dps_display["sales_number"]),
        )
        if any(value for _, value in detail_values):
            st.write(
                " · ".join(
                    f"{label}: {value or '확인되지 않음'}"
                    for label, value in detail_values
                )
            )
            st.caption(dps_display["installation_date_help"])
            with st.expander("설치예정일 데이터 상세", expanded=False):
                st.json(
                    {
                        "원본 필드": "요구납기일",
                        "원본 값": dps_display[
                            "raw_required_delivery_date"
                        ],
                        "정규화 값": dps_display[
                            "required_delivery_date"
                        ],
                        "파싱 상태": dps_display["date_parse_status"],
                        "데이터 출처": dps_display[
                            "installation_date_source"
                        ],
                    }
                )
        if (
            dps_decision.lookup_required
            and not dps_decision.order_id
        ):
            st.warning(
                "상품주문번호만으로는 DPS를 조회할 수 없습니다. "
                "네이버 일반 주문번호가 필요합니다."
            )
        if dps_display["error_message"]:
            st.warning(str(dps_display["error_message"]))
        for warning in dps_display["warnings"]:
            if warning != dps_display["error_message"]:
                st.warning(str(warning))

        if dps_decision.lookup_required:
            lookup_col, refresh_col = st.columns(2)
            dps_disabled = posted or running or not dps_decision.order_id
            lookup_requested = lookup_col.button(
                "DPS 조회",
                key=get_work_item_state_key(
                    work_item, "integrated_dps_lookup"
                ),
                disabled=dps_disabled,
                width="stretch",
            )
            refresh_requested = refresh_col.button(
                "DPS 재조회",
                key=get_work_item_state_key(
                    work_item, "integrated_dps_refresh"
                ),
                disabled=dps_disabled,
                width="stretch",
            )
            if lookup_requested or refresh_requested:
                st.session_state[running_key] = True
                try:
                    with st.spinner("DPS 배송·설치 정보를 조회하는 중입니다..."):
                        AnswerService(database).enrich_dps_for_inquiry(
                            inquiry["id"],
                            force_refresh=bool(refresh_requested),
                        )
                    st.success("DPS 조회 결과를 통합 DB에 저장했습니다.")
                    st.rerun()
                except Exception as error:
                    st.error(
                        str(error).splitlines()[0][:300]
                        or "DPS 조회에 실패했습니다."
                    )
                finally:
                    st.session_state[running_key] = False

    button_label = (
        "답변 초안 다시 생성"
        if latest_draft is not None
        else "답변 초안 생성"
    )
    if dps_decision is not None and dps_decision.lookup_required:
        button_label = "DPS 결과를 반영하여 답변 초안 생성"
    generation_requested = st.button(
        button_label,
        key=generate_key,
        disabled=posted or running,
    )

    if posted:
        st.info("이미 네이버에 등록된 문의이므로 초안을 다시 생성할 수 없습니다.")

    if generation_requested:
        st.session_state[running_key] = True
        try:
            with st.spinner("자동 Q&A 규칙으로 답변 초안을 생성하는 중입니다..."):
                outcome = AnswerService(database).generate_for_inquiry(
                    inquiry["id"]
                )
            st.session_state[result_key] = outcome.result.to_dict()
            latest_draft = outcome.draft
            st.success("프로그램 답변 초안을 생성해 DB에 저장했습니다.")
        except Exception as error:
            st.error(
                str(error).splitlines()[0][:300]
                or "답변 초안을 생성하지 못했습니다."
            )
        finally:
            st.session_state[running_key] = False

    if latest_draft is None:
        st.caption("아직 생성된 답변 초안이 없습니다.")
        return

    display = build_answer_display(
        latest_draft,
        st.session_state.get(result_key),
    )
    status_col, category_col, provider_col = st.columns(3)
    status_col.metric("답변 상태", display["status_label"])
    category_col.metric("분류", display["category"])
    provider_col.metric("Provider", display["provider"])

    check_col, review_col = st.columns(2)
    check_col.write(
        "자동답변 가능: "
        + ("예" if display["auto_answerable"] else "아니요")
    )
    review_col.write(
        "직원 검토 필요: "
        + ("예" if display["needs_review"] else "아니요")
    )
    st.caption(f"판단 사유: {display['reason']}")

    if display["answer"]:
        st.text_area(
            "프로그램 원본 답변",
            value=display["answer"],
            height=260,
            disabled=True,
            key=get_work_item_state_key(
                work_item,
                f"program_answer_{latest_draft['id']}",
            ),
        )
    else:
        st.info("자동 생성 가능한 답변 본문이 없어 직원 확인이 필요합니다.")

    for warning in display["warnings"]:
        st.warning(warning)
    st.caption(f"생성 시각: {display['created_at']}")
    st.caption(
        "실제 네이버 답변 등록과 직원 수정·학습 기능은 아직 비활성입니다."
    )


def _selected_order_snapshot(work_item: WorkItem) -> dict[str, Any]:
    """Return the one order displayed and used by the DPS button."""

    orders = _valid_orders(work_item)
    work_order_id = str(work_item.get("order_id") or "").strip()
    matching = [
        order
        for order in orders
        if work_order_id
        and str(order.get("order_id") or "").strip() == work_order_id
    ]
    if len(matching) == 1:
        snapshot = dict(matching[0])
    elif len(orders) == 1:
        snapshot = dict(orders[0])
    else:
        unique_order_ids = {
            str(order.get("order_id") or "").strip()
            for order in orders
            if str(order.get("order_id") or "").strip()
        }
        snapshot = dict(orders[0]) if len(unique_order_ids) == 1 else {}
    if snapshot.get("order_id") in (None, ""):
        snapshot["order_id"] = work_item.get("order_id")
    if snapshot.get("product_order_id") in (None, ""):
        values = work_item.get("product_order_ids")
        if isinstance(values, (list, tuple)):
            snapshot["product_order_id"] = next(
                (value for value in values if str(value).strip()),
                None,
            )
        elif values not in (None, ""):
            snapshot["product_order_id"] = values
    return snapshot


def _dps_query_context(work_item: WorkItem) -> dict[str, Any]:
    snapshot = _selected_order_snapshot(work_item)
    try:
        context = create_dps_lookup_context(
            snapshot,
            selected_inquiry_id=(
                work_item.get("inquiry_id")
                or get_work_item_state_key(work_item, "inquiry")
            ),
        )
    except DpsLookupContextError as error:
        return {
            "error": error.code,
            "message": str(error),
            "order_id": str(snapshot.get("order_id") or "").strip() or None,
            "product_order_id": (
                str(snapshot.get("product_order_id") or "").strip() or None
            ),
            "dps_query_value": None,
            "dps_query_value_type": None,
            "query_fallback_used": False,
            "date_warnings": [],
        }
    result = context.to_dict()
    result["query_fallback_used"] = False
    return result


def _dps_order_number(work_item: WorkItem) -> str | None:
    """Deprecated compatibility helper."""

    return _dps_query_context(work_item)["dps_query_value"]


def _format_remaining(seconds: Any) -> str:
    if not isinstance(seconds, int):
        return "사용 전"
    minutes, remainder = divmod(max(0, seconds), 60)
    return f"{minutes}분 {remainder}초"


def _dps_lookup_is_disabled(status: dict[str, Any]) -> bool:
    """A saved tab connection is enough; lookup reselects and verifies it."""

    if status.get("connected"):
        return False
    state = status.get("login_state") or status.get("login_status")
    return str(state or "") != "LOGGED_IN"


def _format_dps_money(value: Any) -> str:
    if value in (None, ""):
        return "확인되지 않음"
    digits = re.sub(r"[^\d-]", "", str(value))
    if not digits or digits == "-":
        return display_value(value, empty_text="확인되지 않음")
    try:
        return f"{int(digits):,}원"
    except ValueError:
        return display_value(value, empty_text="확인되지 않음")


def _dps_summary_values(
    data: dict[str, Any],
    result: dict[str, Any],
) -> list[tuple[str, str]]:
    date_status = data.get("delivery_date_status")
    if date_status in {"DATE_CONFLICT", "MULTIPLE_DATES", "PARTIALLY_CONFIRMED"}:
        date_value = "날짜 확인 필요"
    else:
        date_value = format_date_value(
            data.get("delivery_scheduled_date"),
            empty_text="확인되지 않음",
        )
    quantity = data.get("quantity")
    quantity_value = (
        f"{quantity}대"
        if quantity not in (None, "")
        else "확인되지 않음"
    )
    return [
        ("배송 예정일", date_value),
        (
            "진행 상태",
            display_value(
                data.get("progress_status")
                or data.get("installation_status")
                or result.get("installation_status"),
                empty_text="확인되지 않음",
            ),
        ),
        (
            "모델명",
            display_value(
                data.get("model_name") or result.get("model_name"),
                empty_text="확인되지 않음",
            ),
        ),
        ("수량", quantity_value),
        (
            "DPS 판매번호",
            display_value(
                data.get("dps_sales_number")
                or result.get("dps_sale_number"),
                empty_text="확인되지 않음",
            ),
        ),
        (
            "전자주문번호",
            display_value(
                data.get("dps_order_number")
                or data.get("electronic_order_number")
                or result.get("electronic_order_number"),
                empty_text="확인되지 않음",
            ),
        ),
    ]


def _render_dps_summary_cards(
    data: dict[str, Any],
    result: dict[str, Any],
) -> None:
    values = _dps_summary_values(data, result)
    for offset in (0, 3):
        columns = st.columns(3, gap="small")
        for column, (label, value) in zip(
            columns, values[offset : offset + 3]
        ):
            column.metric(label, value)


def _render_dps_installation_info(work_item: WorkItem) -> None:
    """일반 Chrome과 Windows UI Automation 기반 DPS 상태 및 조회 화면입니다."""

    st.markdown("**삼성 DPS 설치정보**")
    auto_connection = auto_connect_dps_tab(select_tab=False)
    status = get_dps_agent_status()
    logged_in = bool(status.get("logged_in"))
    login_status = str(status.get("login_status") or "")
    connected = bool(status.get("connected"))
    current_page_label = display_value(
        status.get("current_page_label"),
        empty_text="알 수 없음",
    )

    connection_status = str(status.get("connection_status") or "")
    if login_status == "AGENT_RESTART_REQUIRED":
        status_text = "🟠 DPS Agent 재시작 필요"
        status_help = status.get("message") or "이전 Agent를 한 번 종료해 주세요."
    elif login_status == "AGENT_OFFLINE":
        status_text = "⚪ DPS Agent 중지"
        status_help = "DPS Agent를 시작하지 못했습니다. 로그를 확인해 주세요."
    elif status.get("lookup_in_progress"):
        status_text = "🔵 조회 중"
        status_help = "검증된 Samsung DPS 탭에서 조회하고 있습니다."
    elif login_status == "LOGIN_REQUIRED":
        status_text = "🔴 DPS 로그인 필요"
        status_help = status.get("login_reason") or "DPS 로그인 화면이 확인되었습니다."
    elif connected:
        status_text = "🟢 DPS 탭 연결됨"
        status_help = "저장된 DPS 탭을 조회할 때 자동으로 선택하고 검증합니다."
    elif login_status == "LOGIN_UNCERTAIN":
        status_text = "🟡 DPS 탭 연결됨 · 로그인 확인 불확실"
        status_help = status.get("login_reason") or "DPS 로그인 상태 확인 중"
    elif login_status == "DPS_TAB_NOT_FOUND":
        status_text = "⚪ DPS 탭 없음"
        status_help = "Chrome에서 Samsung DPS 탭을 열어 주세요."
    elif login_status == "DPS_PAGE_INVALID":
        status_text = "🟠 DPS 페이지 확인 실패"
        status_help = status.get("login_reason") or "현재 URL과 DPS 페이지를 확인해 주세요."
    elif connection_status == "LOOKUP_COMPLETE":
        status_text = "✅ 조회 완료"
        status_help = "최근 DPS 조회를 안전하게 완료했습니다."
    elif connection_status == "LOOKUP_FAILED":
        status_text = "❌ 조회 실패"
        status_help = status.get("last_error") or "진단 정보와 로그를 확인해 주세요."
    elif connection_status == "PAGE_VERIFICATION_FAILED":
        status_text = "🟡 DPS 탭은 찾았지만 페이지 검증 실패"
        status_help = "DPS 구매요청 화면을 열었는지 확인해 주세요."
    elif login_status == "LOGGED_IN":
        status_text = "🟢 DPS 로그인됨"
        status_help = f"현재 화면: {current_page_label}"
    elif connection_status == "SEARCHING":
        status_text = "🔵 DPS 탭 자동 탐색 중"
        status_help = "일반 Chrome의 탭 목록을 확인하고 있습니다."
    elif connection_status == "CHROME_NOT_FOUND":
        status_text = "⚪ Chrome을 찾지 못함"
        status_help = "일반 Google Chrome을 실행해 주세요."
    elif connection_status == "TAB_CLOSED":
        status_text = "🟠 연결된 탭이 닫힘"
        status_help = "Samsung DPS 탭을 다시 열면 자동으로 재탐색합니다."
    elif connection_status == "DPS_TAB_NOT_FOUND":
        status_text = "⚪ DPS 탭을 찾지 못함"
        status_help = "Chrome에서 Samsung DPS 2.0 탭을 열어 주세요."
    else:
        status_text = "⚪ DPS 탭 미연결"
        status_help = auto_connection.get("message") or "Samsung DPS 탭을 열어 주세요."

    last_lookup = format_datetime_kst(
        status.get("last_lookup_at"), empty="없음"
    )
    recent_order = display_value(status.get("last_order_number"), empty_text="없음")
    connected_title = display_value(status.get("connected_window_title"), empty_text="없음")
    connected_tab_title = display_value(status.get("connected_tab_title"), empty_text="없음")
    last_connected = format_datetime_kst(
        status.get("last_connected_at"), empty="없음"
    )
    auto_connect_text = "켜짐" if status.get("auto_connect", True) else "꺼짐"
    st.markdown(
        '<div class="dps-status-card">'
        f'<strong>{html.escape(status_text)}</strong>'
        f'<span>{html.escape(str(status_help))}</span>'
        f'<small>Chrome 창 · {html.escape(connected_title)} &nbsp;|&nbsp; DPS 탭 · {html.escape(connected_tab_title)}'
        f'<br>현재 화면 · {html.escape(current_page_label)}'
        f'<br>최근 연결 · {html.escape(last_connected)} &nbsp;|&nbsp; 최근 조회 · {html.escape(last_lookup)}'
        f' &nbsp;|&nbsp; 최근 주문 · {html.escape(recent_order)} &nbsp;|&nbsp; 자동 연결 · {auto_connect_text}</small>'
        '</div>',
        unsafe_allow_html=True,
    )

    login_col, confirm_col, browser_col = st.columns(3, gap="small")
    if login_col.button(
        "로그인" if not logged_in else "DPS 로그인 화면",
        key=get_work_item_state_key(work_item, "dps_login"),
        width="stretch",
    ):
        response = open_dps_login()
        st.session_state[get_work_item_state_key(work_item, "dps_action_message")] = response
        st.rerun()

    if confirm_col.button(
        "로그인 상태 다시 확인" if not logged_in else "↻ 세션 새로고침",
        key=get_work_item_state_key(work_item, "dps_login_confirm_or_refresh"),
        width="stretch",
        help="현재 DPS URL과 로그인 화면 신호를 다시 확인합니다." if not logged_in else "연결된 DPS 창을 새로고침하고 로그인 상태를 다시 확인합니다.",
        type="primary" if logged_in else "secondary",
    ):
        response = confirm_dps_login() if not logged_in else refresh_dps_session()
        st.session_state[get_work_item_state_key(work_item, "dps_action_message")] = response
        st.rerun()

    if browser_col.button(
        "DPS 창 보기",
        key=get_work_item_state_key(work_item, "dps_browser_open"),
        width="stretch",
    ):
        response = open_dps_browser()
        st.session_state[get_work_item_state_key(work_item, "dps_action_message")] = response
        st.rerun()

    st.markdown("##### 수동 연결 (자동 탐색 실패 시)")
    st.caption("실제 주소가 dps2u.co.kr로 확인된 탭이 있는 Chrome 창만 표시됩니다.")

    windows_response = list_dps_chrome_windows()
    chrome_windows = windows_response.get("windows") if isinstance(windows_response, dict) else []
    chrome_windows = chrome_windows if isinstance(chrome_windows, list) else []
    window_by_label: dict[str, int] = {}
    window_labels: list[str] = []
    for index, item in enumerate(chrome_windows, start=1):
        if not isinstance(item, dict) or not item.get("hwnd"):
            continue
        title = display_value(item.get("title"), empty_text="제목 없는 Chrome 창")
        recommended = " ⭐ DPS 추천" if item.get("recommended") else ""
        tabs_value = item.get("tabs")
        safe_tabs = tabs_value if isinstance(tabs_value, list) else []
        tab_names = [
            display_value(tab.get("title"))
            for tab in safe_tabs
            if isinstance(tab, dict) and tab.get("dps_url_verified")
        ]
        tab_hint = f" · 탭: {', '.join(tab_names)}" if tab_names else ""
        label = f"{index}. {title}{tab_hint}{recommended}"
        window_labels.append(label)
        window_by_label[label] = int(item["hwnd"])

    selected_label = None
    if window_labels:
        selected_label = st.selectbox(
            "연결할 Chrome 창",
            options=window_labels,
            key=get_work_item_state_key(work_item, "dps_window_selector"),
            label_visibility="collapsed",
        )
    else:
        st.warning("실제 주소가 dps2u.co.kr인 Chrome 탭을 찾지 못했습니다.")

    connect_col, refresh_windows_col, disconnect_col = st.columns(3, gap="small")
    if connect_col.button(
        "🔗 선택 창의 DPS 탭 연결",
        key=get_work_item_state_key(work_item, "dps_connect_selected_window"),
        type="primary",
        disabled=not selected_label,
        width="stretch",
    ):
        response = connect_dps_window(window_by_label[selected_label])
        st.session_state[get_work_item_state_key(work_item, "dps_action_message")] = response
        st.rerun()

    if refresh_windows_col.button(
        "↻ 창 목록 새로고침",
        key=get_work_item_state_key(work_item, "dps_refresh_window_list"),
        width="stretch",
    ):
        st.rerun()

    if disconnect_col.button(
        "연결 해제",
        key=get_work_item_state_key(work_item, "dps_disconnect_current_window"),
        disabled=not connected,
        width="stretch",
    ):
        response = disconnect_current_dps_window()
        st.session_state[get_work_item_state_key(work_item, "dps_action_message")] = response
        st.rerun()

    action_message = st.session_state.pop(get_work_item_state_key(work_item, "dps_action_message"), None)
    if isinstance(action_message, dict):
        message = action_message.get("message") or action_message.get("error_message")
        if message:
            (st.success if action_message.get("success") else st.warning)(message)

    with st.expander("DPS 환경 진단 및 로그인 상태 관리", expanded=False):
        diag_col, logout_col = st.columns(2, gap="small")
        if diag_col.button("DPS 환경 진단", key="dps_global_diagnostics_v5", width="stretch"):
            st.session_state["dps_global_diagnostics_result"] = run_dps_diagnostics()
        if logout_col.button("미로그인으로 변경", key="dps_global_mark_logged_out", width="stretch"):
            st.session_state[get_work_item_state_key(work_item, "dps_action_message")] = mark_dps_logged_out()
            st.rerun()

        diagnostic = st.session_state.get("dps_global_diagnostics_result")
        if isinstance(diagnostic, dict):
            checks = diagnostic.get("checks") if isinstance(diagnostic.get("checks"), list) else []
            for check in checks:
                icon = "🟢" if check.get("ok") else "🔴"
                st.write(f"{icon} **{check.get('name', '항목')}** · {check.get('detail', '')}")
            diagnostic_texts = diagnostic.get("diagnostic_texts")
            if isinstance(diagnostic_texts, list) and diagnostic_texts:
                st.caption("현재 DPS UI Automation 텍스트 (결과 파서 진단용)")
                st.code("\n".join(str(value) for value in diagnostic_texts[:80]))

    st.caption("Agent가 Chrome 창 안의 Samsung DPS 탭을 직접 선택하고, foreground 창·선택된 TabItem·탭 제목·DPS 페이지 요소를 조회 직전에 재검증합니다. 하나라도 실패하면 주문번호를 입력하지 않습니다.")

    query_context = _dps_query_context(work_item)
    query_value = query_context["dps_query_value"]
    if not query_value:
        st.warning("DPS 조회에 사용할 주문 식별자가 없습니다. 먼저 주문 패널에서 주문조회를 완료해 주세요.")
        return

    query_label = "네이버 주문번호"
    st.caption(f"DPS 입력 예정 번호: {query_value}")
    st.caption(f"DPS 조회 기준: {query_label}")
    st.info("DPS 온라인판매 주문번호 조회에는 네이버 주문번호를 사용합니다.")
    if not query_context["dps_reference_date"]:
        st.warning(
            "주문일을 확인하지 못해 DPS 조회 기간을 계산할 수 없습니다."
        )
        return
    date_source_labels = {
        "order_date": "네이버 주문일",
        "order_created_at": "네이버 주문 생성일",
        "payment_date": "결제일",
        "payment_completed_at": "결제 완료일",
        "place_order_date": "발주일",
        "shipping_due_date": "배송 예정일 fallback",
    }
    st.caption(
        "기간 기준: "
        f"{date_source_labels.get(query_context['dps_date_source'], query_context['dps_date_source'])}"
        f" · {query_context['dps_reference_date']}"
    )
    st.caption(
        "조회 기간: "
        f"{query_context['dps_period_start']} ~ "
        f"{query_context['dps_period_end']}"
    )
    if query_context["date_warnings"]:
        st.warning(
            "날짜 진단: "
            + ", ".join(query_context["date_warnings"])
        )
    result_key = get_work_item_state_key(work_item, "dps_agent_result")
    result = st.session_state.get(result_key)
    active_key = get_work_item_state_key(
        work_item, "dps_active_request"
    )
    active_request = st.session_state.get(active_key)
    active_matches = bool(
        isinstance(active_request, dict)
        and active_request.get("order_id") == query_context["order_id"]
        and active_request.get("selected_inquiry_id")
        == query_context["selected_inquiry_id"]
    )
    if active_matches:
        query_context["request_id"] = active_request["request_id"]
    lookup_col, refresh_col = st.columns(2, gap="small")
    lookup_disabled = _dps_lookup_is_disabled(status) or active_matches
    with lookup_col.container(
        key=f"dps_lookup_action_v6_{get_work_item_state_key(work_item, 'button')}"
    ):
        lookup_requested = st.button(
            "설치정보 조회",
            key=get_work_item_state_key(work_item, "dps_agent_lookup"),
            type="primary",
            disabled=lookup_disabled,
            width="stretch",
            help="DPS 탭은 조회 시 자동으로 탐색·선택·검증합니다.",
        )
    refresh_requested = refresh_col.button(
        "↻ DPS 실제 새로고침",
        key=get_work_item_state_key(work_item, "dps_agent_refresh"),
        type="primary",
        disabled=lookup_disabled,
        help="10분 조회 캐시를 무시하고 검증된 DPS 탭에서 다시 조회합니다.",
        width="stretch",
    )

    if lookup_requested or refresh_requested:
        st.session_state[active_key] = {
            "request_id": query_context["request_id"],
            "order_id": query_context["order_id"],
            "selected_inquiry_id": query_context[
                "selected_inquiry_id"
            ],
            "force_refresh": bool(refresh_requested),
        }
        with st.status("네이버 주문정보 확인 중", expanded=True) as progress:
            st.write("DPS 탭 확인 중")
            st.write("구매요청리스트 이동 중")
            current_status = start_dps_agent()
            if not current_status.get("agent_running"):
                progress.update(label="DPS Agent 연결 실패", state="error")
                result = current_status
            else:
                st.write("네이버 주문번호 입력 중")
                st.write("조회 기간 설정 중")
                st.write("구매요청리스트 조회 중")
                st.write("결과 행 확인 중")
                st.write("DPS 판매번호 확인 중")
                st.write("판매 상세정보 열기")
                st.write("배송 예정일 확인 중")
                st.write("품목정보 확인 중")
                st.write("상세 창 닫는 중")
                stage_labels = {
                    "REQUEST_ACCEPTED": "DPS Agent가 요청을 접수했습니다.",
                    "NAVIGATING": "구매요청리스트로 이동하고 있습니다.",
                    "ORDER_ID_INPUT": "네이버 주문번호를 입력하고 있습니다.",
                    "DATE_RANGE_SETTING": "조회 기간을 설정하고 있습니다.",
                    "LIST_QUERY_EXECUTED": "구매요청리스트를 조회하고 있습니다.",
                    "LIST_RESULT_FOUND": "DPS 판매번호를 확인했습니다.",
                    "DETAIL_LINK_OPENING": "판매 상세정보를 여는 중입니다.",
                    "DETAIL_OPENED": "판매 상세정보를 확인하고 있습니다.",
                    "DETAIL_PARSING": "배송 예정일과 품목정보를 확인하고 있습니다.",
                    "DETAIL_CLOSING": "상세 창을 닫고 있습니다.",
                    "COMPLETED": "결과를 정리했습니다.",
                }

                def report_stage(stage: str, message: str) -> None:
                    st.write(stage_labels.get(stage, message or stage))

                result = lookup_dps_order(
                    request_id=query_context["request_id"],
                    selected_inquiry_id=query_context[
                        "selected_inquiry_id"
                    ],
                    order_id=query_context["order_id"],
                    product_order_id=query_context["product_order_id"],
                    dps_query_value=query_value,
                    dps_query_value_type=query_context["dps_query_value_type"],
                    order_date=query_context["order_date"],
                    order_created_at=query_context["order_created_at"],
                    payment_date=query_context["payment_date"],
                    payment_completed_at=query_context["payment_completed_at"],
                    place_order_date=query_context["place_order_date"],
                    shipping_due_date=query_context["shipping_due_date"],
                    dps_date_source=query_context["dps_date_source"],
                    dps_reference_date=query_context["dps_reference_date"],
                    dps_period_start=query_context["dps_period_start"],
                    dps_period_end=query_context["dps_period_end"],
                    force_refresh=refresh_requested,
                    progress_callback=report_stage,
                )
                if result.get("success"):
                    if result.get("found") is False:
                        progress.update(
                            label="해당 기간의 DPS 구매요청 결과가 없습니다.",
                            state="complete",
                        )
                    else:
                        progress.update(label="조회 완료", state="complete")
                elif result.get("login_required"):
                    progress.update(label="DPS 재로그인이 필요합니다", state="error")
                else:
                    progress.update(label="❌ 조회 실패", state="error")
            st.session_state[result_key] = result
            if not (
                isinstance(result, dict)
                and result.get("code") == "AGENT_READ_TIMEOUT"
            ):
                st.session_state.pop(active_key, None)

    if not isinstance(result, dict):
        if login_status == "LOGIN_REQUIRED":
            st.info("DPS 로그인과 SMS 인증을 완료한 뒤 로그인 상태를 다시 확인해 주세요.")
        elif login_status == "LOGIN_UNCERTAIN":
            st.info("DPS 탭을 선택한 상태에서 로그인 상태 다시 확인 또는 환경 진단을 실행해 주세요.")
        elif not logged_in:
            st.info("Samsung DPS 탭과 현재 페이지 상태를 확인해 주세요.")
        else:
            st.info("설치정보 조회 시 홈 화면에서도 구매요청리스트로 안전하게 이동합니다.")
        return
    if not result.get("success"):
        st.warning(display_value(result.get("error_message") or result.get("message"), empty_text="DPS 설치정보를 확인하지 못했습니다."))
        return

    message = result.get("message")
    if message:
        if result.get("recovered_after_timeout"):
            st.success("DPS 조회가 완료되어 결과를 불러왔습니다.")
        else:
            st.info(message)

    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    order_id = data.get("order_id") or result.get("order_id") or query_context["order_id"]
    product_order_id = (
        data.get("product_order_id")
        or result.get("product_order_id")
        or query_context["product_order_id"]
    )
    dps_sales_number = data.get("dps_sales_number") or result.get("dps_sale_number")
    dps_order_number = data.get("dps_order_number") or result.get("electronic_order_number")
    _render_identifier_copy_row(
        [
            ("네이버 주문번호", order_id),
            ("상품주문번호", product_order_id),
            ("DPS 판매번호", dps_sales_number),
            ("DPS 주문번호", dps_order_number),
        ],
        key_prefix=get_work_item_state_key(work_item, "dps_identifier_copy"),
    )
    if result.get("found") is False:
        return

    status_code = str(result.get("status") or "")
    if status_code == "RESULT_FOUND_WITH_DETAIL":
        st.success("DPS 판매 상세정보를 확인했습니다.")
    elif status_code == "DETAIL_DATE_CONFLICT":
        st.warning(
            "상세 화면의 요구납기일 정보가 서로 달라 확인이 필요합니다."
        )
    elif status_code in {
        "RESULT_FOUND_DETAIL_PARTIAL",
        "DETAIL_CLOSE_FAILED",
    }:
        st.warning(
            "DPS 주문은 확인했지만 일부 상세정보를 읽지 못했습니다."
        )
    _render_dps_summary_cards(data, result)

    st.markdown("##### 배송 요약")
    _write_fields(
        [
            (
                "배송 예정일",
                (
                    "날짜 확인 필요"
                    if data.get("delivery_date_status")
                    in {"DATE_CONFLICT", "MULTIPLE_DATES"}
                    else format_date_value(
                        data.get("delivery_scheduled_date"),
                        empty_text="확인되지 않음",
                    )
                ),
            ),
            ("배달시간", data.get("delivery_time")),
            ("배송 메모", data.get("delivery_note")),
            (
                "판매금액",
                _format_dps_money(
                    data.get("sale_amount")
                    or data.get("sales_amount")
                    or data.get("order_amount")
                ),
            ),
        ]
    )

    with st.expander("수취 및 배송 정보 보기", expanded=False):
        _write_fields(
            [
                ("구매자", data.get("buyer_name") or data.get("buyer")),
                (
                    "인수자",
                    data.get("recipient_name") or data.get("recipient"),
                ),
                (
                    "연락처",
                    data.get("recipient_phone")
                    or data.get("buyer_phone"),
                ),
                ("주소", data.get("delivery_address")),
                ("배송정보", data.get("delivery_note")),
            ]
        )

    detail_items = [
        item
        for item in data.get("detail_items", [])
        if isinstance(item, dict)
    ]
    if detail_items:
        with st.expander(
            f"품목 상세 {len(detail_items)}건",
            expanded=False,
        ):
            st.dataframe(
                [
                    {
                        "모델": display_value(item.get("model_name")),
                        "수량": item.get("quantity"),
                        "판매금액": _format_dps_money(
                            item.get("sale_amount")
                        ),
                        "요구납기일": format_date_value(
                            item.get("requested_delivery_date"),
                            empty_text="확인되지 않음",
                        ),
                        "배달시간": display_value(
                            item.get("delivery_time"),
                            empty_text="확인되지 않음",
                        ),
                    }
                    for item in detail_items
                ],
                hide_index=True,
                width="stretch",
            )

    diagnostics = (
        result.get("diagnostics")
        if isinstance(result.get("diagnostics"), dict)
        else {}
    )
    with st.expander("DPS 조회 진단 정보", expanded=False):
        st.json(
            {
                "status": status_code,
                "query_value_type": data.get("dps_query_value_type"),
                "period": (
                    data.get("dps_period_start")
                    or result.get("dps_period_start"),
                    data.get("dps_period_end")
                    or result.get("dps_period_end"),
                ),
                "matched_row_index": diagnostics.get(
                    "matched_row_index"
                ),
                "row_match_basis": diagnostics.get("row_match_basis"),
                "delivery_date_status": data.get(
                    "delivery_date_status"
                ),
                "delivery_date_source": data.get(
                    "delivery_date_source"
                ),
                "detail_lookup": result.get("detail_lookup")
                or data.get("detail_lookup"),
                "parse_warnings": diagnostics.get(
                    "parse_warnings", []
                ),
                "cache_hit": bool(
                    result.get("cached") or result.get("cache_hit")
                ),
            }
        )

def _render_detail_section_title(
    title: str,
    description: str = "",
    *,
    icon: str = "•",
) -> None:
    """문의 상세 화면의 카드형 섹션 제목을 표시합니다."""

    description_html = (
        f'<p>{description}</p>'
        if description
        else ""
    )

    section_html = (
        '<div class="detail-section-header">'
        f'<div class="detail-section-icon">{icon}</div>'
        '<div class="detail-section-copy">'
        f'<h3>{title}</h3>'
        f'{description_html}'
        '</div>'
        '</div>'
    )

    st.markdown(
        section_html,
        unsafe_allow_html=True,
    )


def _render_detail_fields(
    fields: list[tuple[str, Any]],
) -> None:
    """상세 정보를 반응형 카드형 필드로 표시합니다."""

    for start in range(0, len(fields), 2):
        columns = st.columns(
            2,
            gap="medium",
        )

        for column, field in zip(
            columns,
            fields[start:start + 2],
        ):
            label, value = field

            field_html = (
                '<div class="detail-field-card">'
                f'<span class="detail-field-label">{label}</span>'
                f'<strong class="detail-field-value">'
                f'{display_value(value)}'
                '</strong>'
                '</div>'
            )

            with column:
                st.markdown(
                    field_html,
                    unsafe_allow_html=True,
                )


def _render_message_card(
    label: str,
    value: Any,
    *,
    tone: str = "default",
) -> None:
    """문의 제목, 내용, 안내문 등을 메시지 카드로 표시합니다."""

    message_html = (
        f'<div class="detail-message-card {tone}">'
        f'<span class="detail-message-label">{label}</span>'
        f'<p>{display_value(value)}</p>'
        '</div>'
    )

    st.markdown(
        message_html,
        unsafe_allow_html=True,
    )


def _render_panel_marker(panel_name: str) -> None:
    """CSS가 각 상세 영역을 독립 카드로 꾸밀 수 있도록 마커를 추가합니다."""

    st.markdown(
        f'<div class="detail-panel-marker {panel_name}"></div>',
        unsafe_allow_html=True,
    )


def _render_toggle_panel(
    work_item: WorkItem,
    *,
    label: str,
    purpose: str,
    anchor_name: str,
    description: str,
    icon: str,
    renderer: Any,
) -> None:
    """고정 툴바와 연동되는 독립 업무 패널을 표시합니다."""

    toggle_key = get_work_item_state_key(work_item, purpose)
    item_token = get_work_item_state_key(work_item, "detail").rsplit("_", 1)[-1]

    st.markdown(
        f'<div id="detail-{anchor_name}-{item_token}" class="detail-section-anchor"></div>',
        unsafe_allow_html=True,
    )

    with st.container(border=True):
        _render_panel_marker(f"panel-{purpose}")
        _render_detail_section_title(label, description, icon=icon)

        opened = bool(st.session_state.get(toggle_key, False))
        if opened:
            st.markdown(
                '<div class="detail-toggle-content"></div>',
                unsafe_allow_html=True,
            )
            renderer(work_item)
        else:
            st.markdown(
                '<div class="detail-toggle-placeholder">'
                '위 고정 툴바의 버튼을 누르면 이 항목이 열립니다.'
                '</div>',
                unsafe_allow_html=True,
            )


def render_work_item(work_item: WorkItem) -> None:
    """문의 상세를 독립 카드와 접이식 업무 패널로 표시합니다."""

    source_code = str(work_item.get("source") or "")
    priority_code = str(work_item.get("priority") or "")
    queue_code = str(work_item.get("queue") or "")

    analysis = work_item.get("analysis")
    analysis_data = analysis if isinstance(analysis, dict) else {}

    answered_text = (
        "답변 완료"
        if work_item.get("answered") is True
        else "미답변"
        if work_item.get("answered") is False
        else "확인되지 않음"
    )

    classification = (
        "배송·설치 문의"
        if analysis_data.get("is_delivery")
        else "일반 문의"
    )

    overview_html = (
        '<div class="detail-overview-bar">'
        '<div class="detail-overview-badges">'
        f'<span class="detail-status-badge '
        f'{"complete" if work_item.get("answered") is True else "pending"}">'
        f'{answered_text}</span>'
        f'<span class="detail-priority-badge {priority_code.lower()}">'
        f'{PRIORITY_LABELS.get(priority_code, "알 수 없음")}</span>'
        '</div>'
        '<div class="detail-overview-meta">문의번호 '
        f'<strong>{display_value(work_item.get("inquiry_id"))}</strong>'
        '</div>'
        '</div>'
    )
    st.markdown(overview_html, unsafe_allow_html=True)

    _render_identifier_copy_row(
        [
            ("문의번호", work_item.get("inquiry_id")),
            ("상품번호", work_item.get("product_id")),
            ("네이버 주문번호", _dps_order_number(work_item)),
        ],
        key_prefix=get_work_item_state_key(work_item, "detail_identifier_copy"),
    )

    with st.container(border=True):
        _render_panel_marker("panel-basic")
        _render_detail_section_title(
            "문의 기본 정보",
            "고객, 상품, 문의 출처를 빠르게 확인합니다.",
            icon="▦",
        )
        _render_detail_fields(
            [
                ("스토어명", work_item.get("store_name")),
                ("문의 번호", work_item.get("inquiry_id")),
                (
                    "문의 출처",
                    SOURCE_LABELS.get(
                        source_code,
                        source_code or "확인되지 않음",
                    ),
                ),
                (
                    "등록일",
                    format_datetime_kst(work_item.get("registered_at")),
                ),
                (
                    "고객명",
                    display_value(
                        work_item.get("customer_name"),
                        empty_text="확인되지 않음",
                    ),
                ),
                (
                    "작성자 ID",
                    work_item.get("writer_id")
                    or work_item.get("customer_id"),
                ),
                ("상품 번호", work_item.get("product_id")),
                ("상품명", work_item.get("product_name")),
                (
                    "상품 옵션",
                    display_value(
                        work_item.get("product_option"),
                        empty_text="없음",
                    ),
                ),
                ("답변 상태", answered_text),
            ]
        )

    with st.container(border=True):
        _render_panel_marker("panel-message")
        _render_detail_section_title(
            "문의 내용",
            "고객이 작성한 제목과 원문을 확인합니다.",
            icon="✉",
        )
        _render_message_card("문의 제목", work_item.get("title"))
        _render_message_card("문의 내용", work_item.get("content"))

    with st.container(border=True):
        _render_panel_marker("panel-analysis")
        _render_detail_section_title(
            "분류 및 작업 정보",
            "분석 결과와 권장 처리 방향을 한곳에서 확인합니다.",
            icon="⌁",
        )
        _render_detail_fields(
            [
                ("분류 결과", classification),
                ("분류 점수", analysis_data.get("score")),
                (
                    "발견된 키워드",
                    display_value(
                        analysis_data.get("matched_keywords"),
                        empty_text="없음",
                    ),
                ),
                (
                    "우선순위",
                    PRIORITY_LABELS.get(
                        priority_code,
                        priority_code or "확인되지 않음",
                    ),
                ),
                (
                    "작업 큐",
                    QUEUE_LABELS.get(
                        queue_code,
                        queue_code or "확인되지 않음",
                    ),
                ),
                ("권장 작업", work_item.get("recommended_action")),
            ]
        )

        recommended_message = work_item.get("recommended_message")
        if recommended_message:
            _render_message_card(
                "권장 안내 문구",
                recommended_message,
                tone="info",
            )

        existing_answer = work_item.get("existing_answer")
        if existing_answer:
            _render_message_card(
                "기존 판매자 답변",
                existing_answer,
                tone="success",
            )

    st.markdown(
        '<div class="detail-workspace-heading">'
        '<div><strong>업무 도구</strong>'
        '<span>필요한 항목만 열어서 확인할 수 있습니다.</span>'
        '</div></div>',
        unsafe_allow_html=True,
    )

    _render_toggle_panel(
        work_item,
        label="주문 정보",
        purpose="open_order_panel",
        anchor_name="order",
        description="문의와 연결된 네이버 주문 정보를 확인합니다.",
        icon="▣",
        renderer=_render_order_section,
    )

    _render_toggle_panel(
        work_item,
        label="삼성 DPS 설치정보",
        purpose="open_dps_panel",
        anchor_name="dps",
        description="설치 일정과 기사 배정 상태를 조회합니다.",
        icon="◈",
        renderer=_render_dps_installation_info,
    )

    _render_toggle_panel(
        work_item,
        label="AI 답변 초안",
        purpose="open_ai_panel",
        anchor_name="ai",
        description="문의 내용을 기반으로 초안을 생성하고 수정합니다.",
        icon="✦",
        renderer=_render_ai_answer_draft,
    )


def create_expander_title(work_item: WorkItem) -> str:
    """문의 목록에 사용할 간결한 제목을 만듭니다."""

    priority_code = str(work_item.get("priority") or "")
    source_code = str(work_item.get("source") or "")
    queue_code = str(work_item.get("queue") or "")
    subject = (
        work_item.get("product_name")
        or work_item.get("title")
        or "제목 없음"
    )
    subject_text = str(subject).replace("\n", " ").strip()
    if len(subject_text) > 60:
        subject_text = f"{subject_text[:57]}..."

    return " | ".join(
        [
            PRIORITY_LABELS.get(priority_code, priority_code or "-"),
            display_value(work_item.get("store_name")),
            SOURCE_LABELS.get(source_code, source_code or "-"),
            QUEUE_LABELS.get(queue_code, queue_code or "-"),
            subject_text,
            format_datetime_kst(work_item.get("registered_at")),
        ]
    )
