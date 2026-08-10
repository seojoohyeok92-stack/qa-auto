from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, TypedDict

from api.auth import get_access_token
from api.customer_inquiry import get_customer_inquiries
from api.qna import get_qna_list
from config import StoreConfig, get_configured_stores
from core.time_utils import to_kst
from core.business_engine import (
    analyze_customer_inquiry,
    analyze_product_inquiry,
)


SEARCH_DAYS = 30
PAGE_SIZE = 100
ANSWERED_FILTER: bool | None = None

WorkItem = dict[str, Any]
ProgressCallback = Callable[[str], None]
EventCallback = Callable[..., None]


class WorkQueueError(TypedDict):
    """대시보드와 콘솔에서 공통으로 사용하는 안전한 오류 정보입니다."""

    store_code: str
    store_name: str
    stage: str
    source: str | None
    inquiry_id: str | None
    message: str


def _safe_error_message(error: Exception) -> str:
    """
    응답 본문이나 인증정보를 그대로 노출하지 않고 첫 오류 문장만 반환합니다.
    """

    first_line = next(
        (
            line.strip()
            for line in str(error).splitlines()
            if line.strip()
        ),
        error.__class__.__name__,
    )
    return first_line[:300]


def _create_error(
    store: StoreConfig,
    stage: str,
    error: Exception,
    *,
    source: str | None = None,
    inquiry_id: Any = None,
) -> WorkQueueError:
    return {
        "store_code": store.code,
        "store_name": store.name,
        "stage": stage,
        "source": source,
        "inquiry_id": (
            str(inquiry_id)
            if inquiry_id not in (None, "")
            else None
        ),
        "message": _safe_error_message(error),
    }


def _notify(
    progress_callback: ProgressCallback | None,
    message: str,
) -> None:
    if progress_callback is not None:
        progress_callback(message)


def _emit(
    event_callback: EventCallback | None,
    event_code: str,
    details: dict[str, Any],
    *,
    level: str = "INFO",
) -> None:
    if event_callback is not None:
        event_callback(event_code, details, level=level)


def _page_reaches_watermark(
    values: Sequence[dict[str, Any]],
    *,
    date_key: str,
    watermark: str | None,
) -> bool:
    if not watermark:
        return False
    cutoff = parse_registered_at(watermark)
    if cutoff == datetime.min:
        return False
    dates = [
        parse_registered_at(item.get(date_key))
        for item in values
        if isinstance(item, dict)
    ]
    valid_dates = [value for value in dates if value != datetime.min]
    return bool(valid_dates) and min(valid_dates) <= cutoff


def load_product_inquiries(
    store: StoreConfig,
    access_token: str,
    *,
    days: int = SEARCH_DAYS,
    page_size: int = PAGE_SIZE,
    answered: bool | None = ANSWERED_FILTER,
    event_callback: EventCallback | None = None,
    since_registered_at: str | None = None,
) -> tuple[list[WorkItem], list[WorkQueueError]]:
    """특정 스토어의 상품문의를 조회하고 표준 작업 항목으로 분석합니다."""

    work_items: list[WorkItem] = []
    errors: list[WorkQueueError] = []
    page = 1
    window_end = datetime.now(UTC)
    while True:
        _emit(
            event_callback,
            "NAVER_SYNC_API_REQUEST_STARTED",
            {
                "store_code": store.code,
                "source": "PRODUCT_INQUIRY",
                "page": page,
                "page_size": page_size,
                "days": days,
            },
        )
        result = get_qna_list(
            access_token=access_token,
            days=days,
            page=page,
            size=page_size,
            answered=answered,
            to_date=window_end,
        )
        contents = result.get("contents") or []
        if not isinstance(contents, list):
            raise RuntimeError(
                "상품문의 응답의 contents 형식이 올바르지 않습니다."
            )
        total_pages = max(1, int(result.get("totalPages") or 1))
        _emit(
            event_callback,
            "NAVER_SYNC_API_RESPONSE_RECEIVED",
            {
                "store_code": store.code,
                "source": "PRODUCT_INQUIRY",
                "page": page,
                "page_size": page_size,
                "item_count": len(contents),
                "total_pages": total_pages,
                "total_elements": int(result.get("totalElements") or 0),
                "is_last": bool(result.get("last")),
                "request_window": result.get("_request"),
                "incremental_watermark": since_registered_at,
            },
        )
        watermark_reached = _page_reaches_watermark(
            contents,
            date_key="createDate",
            watermark=since_registered_at,
        )

        for qna in contents:
            if not isinstance(qna, dict):
                errors.append(
                    _create_error(
                        store,
                        "상품문의 분석",
                        TypeError("상품문의 항목 형식이 올바르지 않습니다."),
                        source="PRODUCT_INQUIRY",
                    )
                )
                continue

            try:
                work_items.append(
                    analyze_product_inquiry(
                        qna=qna,
                        access_token=access_token,
                        store_code=store.code,
                        store_name=store.name,
                    )
                )
            except Exception as error:
                errors.append(
                    _create_error(
                        store,
                        "상품문의 분석",
                        error,
                        source="PRODUCT_INQUIRY",
                        inquiry_id=qna.get("questionId"),
                    )
                )
        _emit(
            event_callback,
            "NAVER_SYNC_PAGE_NORMALIZED",
            {
                "store_code": store.code,
                "source": "PRODUCT_INQUIRY",
                "page": page,
                "normalized_count": len(contents),
                "accumulated_count": len(work_items),
                "analysis_error_count": len(errors),
                "watermark_reached": watermark_reached,
            },
        )
        if (
            watermark_reached
            or bool(result.get("last"))
            or page >= total_pages
            or not contents
        ):
            break
        page += 1

    return work_items, errors


def load_customer_inquiries(
    store: StoreConfig,
    access_token: str,
    *,
    days: int = SEARCH_DAYS,
    page_size: int = PAGE_SIZE,
    answered: bool | None = ANSWERED_FILTER,
    event_callback: EventCallback | None = None,
    since_registered_at: str | None = None,
) -> tuple[list[WorkItem], list[WorkQueueError]]:
    """특정 스토어의 고객문의를 조회하고 표준 작업 항목으로 분석합니다."""

    work_items: list[WorkItem] = []
    errors: list[WorkQueueError] = []
    page = 1
    while True:
        _emit(
            event_callback,
            "NAVER_SYNC_API_REQUEST_STARTED",
            {
                "store_code": store.code,
                "source": "CUSTOMER_INQUIRY",
                "page": page,
                "page_size": page_size,
                "days": days,
            },
        )
        result = get_customer_inquiries(
            access_token=access_token,
            days=days,
            page=page,
            size=page_size,
            answered=answered,
        )
        contents = result.get("content") or []
        if not isinstance(contents, list):
            raise RuntimeError(
                "고객문의 응답의 content 형식이 올바르지 않습니다."
            )
        total_pages = max(1, int(result.get("totalPages") or 1))
        _emit(
            event_callback,
            "NAVER_SYNC_API_RESPONSE_RECEIVED",
            {
                "store_code": store.code,
                "source": "CUSTOMER_INQUIRY",
                "page": page,
                "page_size": page_size,
                "item_count": len(contents),
                "total_pages": total_pages,
                "total_elements": int(result.get("totalElements") or 0),
                "is_last": bool(result.get("last")),
                "request_window": result.get("_request"),
                "incremental_watermark": since_registered_at,
            },
        )
        watermark_reached = _page_reaches_watermark(
            contents,
            date_key="inquiryRegistrationDateTime",
            watermark=since_registered_at,
        )

        for inquiry in contents:
            if not isinstance(inquiry, dict):
                errors.append(
                    _create_error(
                        store,
                        "고객문의 분석",
                        TypeError("고객문의 항목 형식이 올바르지 않습니다."),
                        source="CUSTOMER_INQUIRY",
                    )
                )
                continue

            try:
                work_items.append(
                    analyze_customer_inquiry(
                        inquiry=inquiry,
                        access_token=access_token,
                        store_code=store.code,
                        store_name=store.name,
                    )
                )
            except Exception as error:
                errors.append(
                    _create_error(
                        store,
                        "고객문의 분석",
                        error,
                        source="CUSTOMER_INQUIRY",
                        inquiry_id=inquiry.get("inquiryNo"),
                    )
                )
        _emit(
            event_callback,
            "NAVER_SYNC_PAGE_NORMALIZED",
            {
                "store_code": store.code,
                "source": "CUSTOMER_INQUIRY",
                "page": page,
                "normalized_count": len(contents),
                "accumulated_count": len(work_items),
                "analysis_error_count": len(errors),
                "watermark_reached": watermark_reached,
            },
        )
        if (
            watermark_reached
            or bool(result.get("last"))
            or page >= total_pages
            or not contents
        ):
            break
        page += 1

    return work_items, errors


def parse_registered_at(value: Any) -> datetime:
    """Return KST naive datetime for legacy UI sorting/filter compatibility."""

    parsed = to_kst(value)
    return parsed.replace(tzinfo=None) if parsed is not None else datetime.min


def sort_work_items(
    work_items: Sequence[WorkItem],
) -> list[WorkItem]:
    """기존 콘솔과 동일하게 우선순위와 최신 등록일 순으로 정렬합니다."""

    priority_order = {
        "HIGH": 0,
        "MEDIUM": 1,
        "NORMAL": 2,
    }

    def sort_key(item: WorkItem) -> tuple[int, float]:
        registered_at = parse_registered_at(item.get("registered_at"))
        registered_timestamp = (
            -registered_at.timestamp()
            if registered_at != datetime.min
            else float("inf")
        )
        return (
            priority_order.get(str(item.get("priority")), 99),
            registered_timestamp,
        )

    return sorted(work_items, key=sort_key)


def load_store_work_items(
    store: StoreConfig,
    *,
    days: int = SEARCH_DAYS,
    page_size: int = PAGE_SIZE,
    answered: bool | None = ANSWERED_FILTER,
    progress_callback: ProgressCallback | None = None,
    event_callback: EventCallback | None = None,
    since_by_store_source: (
        dict[tuple[str, str], str] | None
    ) = None,
) -> tuple[list[WorkItem], list[WorkQueueError]]:
    """
    특정 스토어의 토큰을 발급하고 두 문의 API를 독립적으로 조회합니다.
    """

    _notify(progress_callback, f"[{store.name} 연결 시작]")

    try:
        _emit(
            event_callback,
            "NAVER_SYNC_TOKEN_REQUEST_STARTED",
            {"store_code": store.code},
        )
        access_token = get_access_token(store=store)
    except Exception as error:
        store_error = _create_error(store, "토큰 발급", error)
        _notify(
            progress_callback,
            f"{store.name} 토큰 발급 실패: {store_error['message']}",
        )
        return [], [store_error]

    _emit(
        event_callback,
        "NAVER_SYNC_TOKEN_RECEIVED",
        {"store_code": store.code, "status": "SUCCESS"},
    )
    _notify(progress_callback, f"{store.name} 토큰 발급 성공")
    work_items: list[WorkItem] = []
    errors: list[WorkQueueError] = []

    try:
        product_items, product_errors = load_product_inquiries(
            store,
            access_token,
            days=days,
            page_size=page_size,
            answered=answered,
            event_callback=event_callback,
            since_registered_at=(since_by_store_source or {}).get(
                (store.code, "PRODUCT_INQUIRY")
            ),
        )
        work_items.extend(product_items)
        errors.extend(product_errors)
        _notify(
            progress_callback,
            f"{store.name} 상품문의 현재 조회된 수: {len(product_items)}",
        )
    except Exception as error:
        product_error = _create_error(
            store,
            "상품문의 조회",
            error,
            source="PRODUCT_INQUIRY",
        )
        errors.append(product_error)
        _notify(
            progress_callback,
            f"{store.name} 상품문의 조회 실패: "
            f"{product_error['message']}",
        )

    try:
        customer_items, customer_errors = load_customer_inquiries(
            store,
            access_token,
            days=days,
            page_size=page_size,
            answered=answered,
            event_callback=event_callback,
            since_registered_at=(since_by_store_source or {}).get(
                (store.code, "CUSTOMER_INQUIRY")
            ),
        )
        work_items.extend(customer_items)
        errors.extend(customer_errors)
        _notify(
            progress_callback,
            f"{store.name} 고객문의 현재 조회된 수: {len(customer_items)}",
        )
    except Exception as error:
        customer_error = _create_error(
            store,
            "고객문의 조회",
            error,
            source="CUSTOMER_INQUIRY",
        )
        errors.append(customer_error)
        _notify(
            progress_callback,
            f"{store.name} 고객문의 조회 실패: "
            f"{customer_error['message']}",
        )

    _notify(
        progress_callback,
        f"{store.name} 통합 조회 완료: {len(work_items)}",
    )
    return work_items, errors


def load_work_queue(
    stores: Sequence[StoreConfig] | None = None,
    *,
    days: int = SEARCH_DAYS,
    page_size: int = PAGE_SIZE,
    answered: bool | None = ANSWERED_FILTER,
    progress_callback: ProgressCallback | None = None,
    event_callback: EventCallback | None = None,
    since_by_store_source: (
        dict[tuple[str, str], str] | None
    ) = None,
) -> tuple[list[WorkItem], list[WorkQueueError]]:
    """
    설정된 모든 스토어를 순회해 정렬된 통합 작업 큐와 오류를 반환합니다.
    """

    target_stores = (
        list(stores)
        if stores is not None
        else get_configured_stores()
    )
    work_items: list[WorkItem] = []
    errors: list[WorkQueueError] = []

    for store in target_stores:
        store_items, store_errors = load_store_work_items(
            store,
            days=days,
            page_size=page_size,
            answered=answered,
            progress_callback=progress_callback,
            event_callback=event_callback,
            since_by_store_source=since_by_store_source,
        )
        work_items.extend(store_items)
        errors.extend(store_errors)

    return sort_work_items(work_items), errors
