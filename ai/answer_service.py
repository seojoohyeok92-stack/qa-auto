from collections.abc import Mapping
from typing import Any, Protocol

from ai.prompt_builder import (
    build_answer_prompt,
    build_safe_context,
)


class AnswerProvider(Protocol):
    """향후 외부 답변 생성 서비스가 구현해야 하는 최소 인터페이스입니다."""

    def generate(
        self,
        *,
        prompt: str,
        context: Mapping[str, Any],
    ) -> str:
        """안전한 프롬프트와 컨텍스트로 답변 초안을 생성합니다."""


class DeterministicAnswerProvider:
    """API 호출 없이 확인된 정보만 사용하는 기본 모의 provider입니다."""

    def generate(
        self,
        *,
        prompt: str,
        context: Mapping[str, Any],
    ) -> str:
        del prompt

        inquiry = context.get("문의")
        inquiry_data = (
            inquiry
            if isinstance(inquiry, Mapping)
            else {}
        )
        analysis = context.get("설치_및_분석_정보")
        analysis_data = (
            analysis
            if isinstance(analysis, Mapping)
            else {}
        )
        orders = context.get("주문_배송_정보")
        order_list = orders if isinstance(orders, list) else []
        queue = str(analysis_data.get("작업_큐") or "")
        is_delivery = bool(
            analysis_data.get("배송_설치_문의")
        )

        if not is_delivery:
            return (
                "안녕하세요. 문의해 주셔서 감사합니다.\n\n"
                "문의하신 내용을 확인한 후 정확한 내용을 "
                "안내드리겠습니다."
            )

        if queue == "CUSTOMER_CONFIRMATION_REQUIRED":
            return (
                "안녕하세요. 문의해 주셔서 감사합니다.\n\n"
                "정확한 배송 및 설치 일정 확인을 위해 해당 주문의 "
                "주문정보 확인이 필요합니다. 네이버 주문내역에서 "
                "해당 주문을 선택한 뒤 다시 문의해 주세요.\n\n"
                "주문정보가 확인되면 담당자가 확인 후 안내드리겠습니다."
            )

        if queue == "ORDER_LOOKUP_FAILED" or not order_list:
            return (
                "안녕하세요. 문의해 주셔서 감사합니다.\n\n"
                "현재 확인된 정보만으로는 배송 및 설치 일정을 "
                "확정하여 안내드리기 어렵습니다. 담당자가 주문정보를 "
                "확인한 후 정확히 안내드리겠습니다."
            )

        first_order = order_list[0]
        order_data = (
            first_order
            if isinstance(first_order, Mapping)
            else {}
        )
        shipping_start = str(
            order_data.get("배송_시작일")
            or "확인되지 않음"
        )
        shipping_due = str(
            order_data.get("배송_예정일")
            or "확인되지 않음"
        )
        product_name = str(
            inquiry_data.get("상품명")
            or order_data.get("상품명")
            or "문의하신 상품"
        )

        facts: list[str] = []
        if shipping_start != "확인되지 않음":
            facts.append(f"배송 시작일은 {shipping_start}로 확인됩니다")
        if shipping_due != "확인되지 않음":
            facts.append(f"배송 예정일은 {shipping_due}로 확인됩니다")

        if facts:
            fact_text = ". ".join(facts) + "."
            return (
                "안녕하세요. 문의해 주셔서 감사합니다.\n\n"
                f"{product_name} 주문을 확인한 결과, {fact_text}\n"
                "배송 예정일은 실제 진행 상황에 따라 변경될 수 있으며, "
                "설치 일정은 별도 확인이 필요합니다. 확정된 설치 일정은 "
                "담당자 확인 후 안내드리겠습니다."
            )

        return (
            "안녕하세요. 문의해 주셔서 감사합니다.\n\n"
            "주문정보는 확인되었으나 현재 배송 및 설치 일정은 "
            "확정되지 않았습니다. 담당자가 진행 상황을 확인한 후 "
            "정확히 안내드리겠습니다."
        )


def generate_answer_draft(
    work_item: Mapping[str, Any],
    provider: AnswerProvider | None = None,
) -> str:
    """
    버튼 클릭 시 호출할 공개 인터페이스입니다.

    provider를 전달하지 않으면 외부 API나 환경변수를 사용하지 않는
    deterministic fallback으로 답변을 생성합니다.
    """

    safe_context = build_safe_context(work_item)
    prompt = build_answer_prompt(work_item)
    target_provider = provider or DeterministicAnswerProvider()
    draft = target_provider.generate(
        prompt=prompt,
        context=safe_context,
    )

    if not isinstance(draft, str) or not draft.strip():
        raise RuntimeError(
            "답변 생성 provider가 유효한 초안을 반환하지 않았습니다."
        )

    return draft.strip()
