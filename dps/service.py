import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from dps.identifiers import select_dps_query_identifier
from dps.models import InstallationInfo
from dps.provider import (
    DeterministicDpsProvider,
    DpsProvider,
)


_PHONE_PATTERN = re.compile(
    r"(?<!\d)(?:\+?82[-\s]?)?"
    r"(?:0?1[016789]|0(?:2|[3-6]\d))"
    r"[-\s]?\d{3,4}[-\s]?\d{4}(?!\d)"
)
_SECRET_PATTERN = re.compile(
    r"(?i)\b(?:bearer\s+\S+|"
    r"(?:access[_\s-]?token|client[_\s-]?secret|"
    r"authorization|session[_\s-]?cookie|cookie|password)"
    r"\s*[:=]\s*\S+)"
)
_ADDRESS_PATTERN = re.compile(
    r"(?:[가-힣]+(?:특별시|광역시|특별자치시|"
    r"특별자치도|도|시)\s+)?"
    r"[가-힣]+(?:시|군|구)\s+"
    r"[가-힣0-9]+(?:로|길|동|읍|면)"
    r"\s*\d+(?:-\d+)?"
    r"(?:\s+[가-힣A-Za-z0-9()동호층-]+){0,4}"
)
_ADDRESS_LABEL_PATTERN = re.compile(
    r"(?i)(?:전체\s*주소|상세\s*주소|배송지|수령지|주소)"
    r"\s*[:=]"
)


def _now_text() -> str:
    return datetime.now().astimezone().isoformat(
        timespec="seconds"
    )


def _safe_text(
    value: Any,
    *,
    default: str | None = None,
) -> str | None:
    if value in (None, ""):
        return default
    if isinstance(value, (Mapping, list, tuple, set)):
        return default

    text = str(value).strip()
    text = _PHONE_PATTERN.sub("[민감정보 제외]", text)
    text = _SECRET_PATTERN.sub("[인증정보 제외]", text)
    address_label = _ADDRESS_LABEL_PATTERN.search(text)
    if address_label:
        text = (
            text[: address_label.start()].rstrip()
            + " [주소정보 제외]"
        ).strip()
    text = _ADDRESS_PATTERN.sub("[주소정보 제외]", text)
    return text or default


def _normalize_numbers(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        raw_values: list[Any] = re.split(
            r"[,;|\s]+",
            value,
        )
    elif isinstance(value, (list, tuple, set)):
        raw_values = list(value)
    else:
        raw_values = [value]

    numbers: list[str] = []
    for raw_value in raw_values:
        if isinstance(raw_value, (Mapping, list, tuple, set)):
            continue
        number = str(raw_value).strip()
        if number and number not in numbers:
            numbers.append(number)
    return numbers


def find_order_reference(
    work_item: Mapping[str, Any],
) -> tuple[str | None, str | None]:
    """표준 work_item에서 DPS 조회에 사용할 주문 참조값을 찾습니다."""

    order_id = None
    product_order_id = None
    orders = work_item.get("orders")
    if isinstance(orders, list):
        for order in orders:
            if not isinstance(order, Mapping):
                continue
            if product_order_id is None:
                values = _normalize_numbers(order.get("product_order_id"))
                product_order_id = values[0] if values else None
            if order_id is None:
                values = _normalize_numbers(order.get("order_id"))
                order_id = values[0] if values else None

    if product_order_id is None:
        values = _normalize_numbers(work_item.get("product_order_ids"))
        product_order_id = values[0] if values else None
    if order_id is None:
        values = _normalize_numbers(work_item.get("order_id"))
        order_id = values[0] if values else None

    selected = select_dps_query_identifier(order_id, product_order_id)
    return selected.value, selected.type.upper() if selected.type else None


def _unavailable_result() -> InstallationInfo:
    return InstallationInfo(
        success=False,
        installation_status="조회 불가",
        scheduled_date=None,
        completed_date=None,
        assignment_status="확인되지 않음",
        technician_assigned=None,
        technician_name=None,
        visit_time_window=None,
        installation_memo=None,
        source_order_number=None,
        queried_at=_now_text(),
        error_code="ORDER_NUMBER_MISSING",
        error_message=(
            "네이버 일반 주문번호가 없어 "
            "설치정보를 조회할 수 없습니다."
        ),
    )


def _failure_result(
    source_order_number: str | None = None,
) -> InstallationInfo:
    return InstallationInfo(
        success=False,
        installation_status="조회 실패",
        scheduled_date=None,
        completed_date=None,
        assignment_status="확인되지 않음",
        technician_assigned=None,
        technician_name=None,
        visit_time_window=None,
        installation_memo=None,
        source_order_number=_safe_text(source_order_number),
        queried_at=_now_text(),
        error_code="DPS_PROVIDER_ERROR",
        error_message=(
            "설치정보 조회 중 오류가 발생했습니다. "
            "잠시 후 다시 시도해 주세요."
        ),
    )


def _sanitize_result(
    result: InstallationInfo,
    *,
    source_order_number: str,
) -> InstallationInfo:
    """provider 결과를 허용된 표준 필드로 다시 구성하고 민감값을 제거합니다."""

    return InstallationInfo(
        success=bool(result.success),
        installation_status=(
            _safe_text(
                result.installation_status,
                default="확인되지 않음",
            )
            or "확인되지 않음"
        ),
        scheduled_date=_safe_text(result.scheduled_date),
        completed_date=_safe_text(result.completed_date),
        assignment_status=(
            _safe_text(
                result.assignment_status,
                default="확인되지 않음",
            )
            or "확인되지 않음"
        ),
        technician_assigned=(
            result.technician_assigned
            if isinstance(
                result.technician_assigned,
                bool,
            )
            else None
        ),
        technician_name=_safe_text(result.technician_name),
        visit_time_window=_safe_text(
            result.visit_time_window
        ),
        installation_memo=_safe_text(
            result.installation_memo
        ),
        source_order_number=_safe_text(source_order_number),
        queried_at=(
            _safe_text(result.queried_at, default=_now_text())
            or _now_text()
        ),
        error_code=_safe_text(result.error_code),
        error_message=_safe_text(result.error_message),
    )


def lookup_installation_status(
    work_item: Mapping[str, Any],
    provider: DpsProvider | None = None,
) -> dict[str, Any]:
    """
    주문 참조값으로 설치정보를 조회합니다.

    provider가 없으면 외부 연결을 하지 않는 deterministic mock을
    사용하며, 모든 provider 오류는 안전한 표준 실패 결과로 격리합니다.
    """

    order_number, number_type = find_order_reference(work_item)
    if not order_number or not number_type:
        return _unavailable_result().to_dict()

    target_provider = provider or DeterministicDpsProvider()

    try:
        provider_result = target_provider.lookup(
            order_number=order_number,
            number_type=number_type,
        )
        if not isinstance(provider_result, InstallationInfo):
            return _failure_result(order_number).to_dict()

        return _sanitize_result(
            provider_result,
            source_order_number=order_number,
        ).to_dict()
    except Exception:
        return _failure_result(order_number).to_dict()


def to_ai_installation_context(
    result: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """향후 AI에 전달 가능한 최소 DPS 정보만 화이트리스트로 변환합니다."""

    if not isinstance(result, Mapping):
        return {
            "설치_상태": "확인되지 않음",
            "설치_예정일": None,
            "방문_예정_시간대": None,
            "설치_완료": False,
        }

    completed_date = _safe_text(result.get("completed_date"))
    installation_status = (
        _safe_text(
            result.get("installation_status"),
            default="확인되지 않음",
        )
        or "확인되지 않음"
    )

    return {
        "설치_상태": installation_status,
        "설치_예정일": _safe_text(
            result.get("scheduled_date")
        ),
        "방문_예정_시간대": _safe_text(
            result.get("visit_time_window")
        ),
        "설치_완료": bool(
            completed_date
            or installation_status == "설치 완료"
        ),
    }
