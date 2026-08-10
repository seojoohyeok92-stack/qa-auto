import json
import re
from collections.abc import Mapping
from typing import Any


SOURCE_LABELS = {
    "PRODUCT_INQUIRY": "상품문의",
    "CUSTOMER_INQUIRY": "고객문의",
}

_PHONE_PATTERN = re.compile(
    r"(?<!\d)(?:\+?82[-\s]?)?"
    r"(?:0?1[016789]|0(?:2|[3-6]\d))"
    r"[-\s]?\d{3,4}[-\s]?\d{4}(?!\d)"
)
_CREDENTIAL_PATTERN = re.compile(
    r"(?i)\b(?:bearer\s+\S+|"
    r"(?:access[_\s-]?token|client[_\s-]?id|"
    r"client[_\s-]?secret|authorization)"
    r"\s*[:=]\s*\S+)"
)
_CUSTOMER_ID_PATTERN = re.compile(
    r"(?i)\b(?:customer[_\s-]?id|고객\s*ID)"
    r"\s*[:=]\s*\S+"
)
_KOREAN_ADDRESS_PATTERN = re.compile(
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


def _sanitize_text(value: Any) -> str:
    """자유문에서 전화번호, 주소, 식별자와 인증정보 패턴을 제거합니다."""

    if value in (None, ""):
        return ""

    text = str(value).strip()
    if not text:
        return ""

    safe_lines: list[str] = []

    for line in text.splitlines() or [text]:
        sanitized = _PHONE_PATTERN.sub("[전화번호 제외]", line)
        sanitized = _CREDENTIAL_PATTERN.sub(
            "[인증정보 제외]",
            sanitized,
        )
        sanitized = _CUSTOMER_ID_PATTERN.sub(
            "[고객 식별정보 제외]",
            sanitized,
        )
        address_label = _ADDRESS_LABEL_PATTERN.search(sanitized)
        if address_label:
            sanitized = (
                sanitized[: address_label.start()].rstrip()
                + " [주소 정보 제외]"
            ).strip()
        sanitized = _KOREAN_ADDRESS_PATTERN.sub(
            "[주소 정보 제외]",
            sanitized,
        )
        safe_lines.append(sanitized)

    return "\n".join(safe_lines).strip()


def _safe_value(
    value: Any,
    *,
    empty_text: str = "확인되지 않음",
) -> str:
    if isinstance(value, Mapping):
        return empty_text

    if isinstance(value, (list, tuple, set)):
        safe_items = [
            _sanitize_text(item)
            for item in value
            if not isinstance(item, (Mapping, list, tuple, set))
        ]
        joined = ", ".join(
            item for item in safe_items if item
        )
        return joined or empty_text

    sanitized = _sanitize_text(value)
    return sanitized or empty_text


def _safe_existing_answer(work_item: Mapping[str, Any]) -> str:
    existing_answer = work_item.get("existing_answer")

    if isinstance(existing_answer, Mapping):
        for key in ("answerContent", "answer", "content"):
            answer_text = existing_answer.get(key)
            if not isinstance(
                answer_text,
                (Mapping, list, tuple, set),
            ):
                sanitized = _sanitize_text(answer_text)
                if sanitized:
                    return sanitized
        return "없음"

    return _safe_value(
        existing_answer,
        empty_text="없음",
    )


def _safe_analysis(work_item: Mapping[str, Any]) -> dict[str, Any]:
    analysis = work_item.get("analysis")
    if not isinstance(analysis, Mapping):
        analysis = {}

    matched_keywords = analysis.get("matched_keywords")
    safe_keywords: list[str] = []
    if isinstance(matched_keywords, (list, tuple, set)):
        safe_keywords = [
            _sanitize_text(keyword)
            for keyword in matched_keywords
            if _sanitize_text(keyword)
        ]

    return {
        "배송_설치_문의": bool(
            analysis.get("is_delivery")
            or work_item.get("is_delivery")
        ),
        "분류_상태": _safe_value(
            analysis.get("queue_status"),
        ),
        "분류_점수": (
            analysis.get("score")
            if isinstance(analysis.get("score"), (int, float))
            else 0
        ),
        "발견_키워드": safe_keywords,
        "작업_큐": _safe_value(work_item.get("queue")),
    }


def _safe_orders(work_item: Mapping[str, Any]) -> list[dict[str, Any]]:
    orders = work_item.get("orders")
    if not isinstance(orders, list):
        return []

    safe_orders: list[dict[str, Any]] = []

    for order in orders:
        if not isinstance(order, Mapping):
            continue

        quantity = order.get("quantity")
        safe_orders.append(
            {
                "상품명": _safe_value(order.get("product_name")),
                "상품_옵션": _safe_value(
                    order.get("product_option"),
                ),
                "수량": (
                    quantity
                    if isinstance(quantity, (int, float))
                    else _safe_value(quantity)
                ),
                "상품주문_상태": _safe_value(
                    order.get("product_order_status"),
                ),
                "발주_상태": _safe_value(
                    order.get("place_order_status"),
                ),
                "배송_시작일": _safe_value(
                    order.get("shipping_start_date"),
                ),
                "배송_예정일": _safe_value(
                    order.get("shipping_due_date"),
                ),
            }
        )

    return safe_orders


def build_safe_context(
    work_item: Mapping[str, Any],
) -> dict[str, Any]:
    """
    허용된 필드만 선택해 개인정보가 제거된 상담 컨텍스트를 만듭니다.

    original_data, 인증정보, 고객 식별자, 전화번호와 주소 필드는
    어떤 경우에도 순회하거나 직렬화하지 않습니다.
    """

    source_code = str(work_item.get("source") or "")

    return {
        "문의": {
            "출처": SOURCE_LABELS.get(
                source_code,
                _safe_value(source_code),
            ),
            "유형": _safe_value(
                work_item.get("category")
                or (
                    "배송·설치 문의"
                    if work_item.get("is_delivery")
                    else "일반 문의"
                )
            ),
            "제목": _safe_value(work_item.get("title")),
            "내용": _safe_value(work_item.get("content")),
            "상품명": _safe_value(work_item.get("product_name")),
            "상품_옵션": _safe_value(
                work_item.get("product_option"),
            ),
        },
        "주문_배송_정보": _safe_orders(work_item),
        "설치_및_분석_정보": _safe_analysis(work_item),
        "기존_답변": _safe_existing_answer(work_item),
        "권장_작업": _safe_value(
            work_item.get("recommended_action"),
        ),
    }


def build_answer_prompt(
    work_item: Mapping[str, Any],
) -> str:
    """향후 답변 생성 provider에 전달할 정책과 안전한 컨텍스트를 만듭니다."""

    context = build_safe_context(work_item)
    context_text = json.dumps(
        context,
        ensure_ascii=False,
        indent=2,
    )

    return (
        "다음 상담 컨텍스트를 바탕으로 고객에게 보낼 한국어 답변 "
        "초안을 작성하세요.\n\n"
        "[작성 정책]\n"
        "- 컨텍스트에서 확인된 사실만 사용합니다.\n"
        "- 알 수 없는 배송 또는 설치 일정은 추측하지 않습니다.\n"
        "- 확정되지 않은 내용을 확정적으로 표현하지 않습니다.\n"
        "- 개인정보와 인증정보를 답변에 포함하지 않습니다.\n"
        "- 정중하고 간결한 고객응대 문체를 사용합니다.\n"
        "- 고객이 해야 할 행동이 있다면 명확하게 안내합니다.\n"
        "- 내부 시스템명, 분석 점수, 분석 키워드, 자동 생성 여부를 "
        "답변에 노출하지 않습니다.\n"
        "- 주문정보가 부족하면 담당자 확인이 필요하다는 안전한 "
        "문구를 사용합니다.\n"
        "- 외부 설치 일정 정보가 연동되지 않았으므로 설치일을 "
        "임의로 만들지 않습니다.\n\n"
        "[안전한 상담 컨텍스트]\n"
        f"{context_text}"
    )
