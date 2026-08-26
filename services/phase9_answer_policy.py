from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from answer.answer_format import format_final_answer, korean_date
from answer.inquiry_analysis import AnswerStrategy, InquiryAnalysis
from answer.models import AnswerRequest, AnswerResult, AnswerStatus
from dps.dates import parse_date_value


ORDER_ID_REQUEST_ANSWER = format_final_answer(
    """배송 또는 설치 일정을 확인하려면 네이버 구매내역에 표시된 일반 주문번호가 필요합니다.

네이버 앱 또는 웹에서
쇼핑 → 구매내역 → 해당 상품 주문 상세
경로로 들어가신 뒤 주문번호를 확인하여 문의에 남겨주세요.

네이버쇼핑의 주문·배송 조회 화면에서도 주문번호를 확인하실 수 있습니다.

상품주문번호가 아닌 일반 주문번호를 전달해주셔야 정확한 일정 조회가 가능합니다.

주문번호를 확인해주시면 확인 후 배송 또는 설치 예정일을 조회해 안내드리겠습니다."""
)

DELIVERY_DATE_ANSWER = format_final_answer(
    """확인 결과 설치 예정일은 {delivery_date}입니다.

현재 조회 결과에서 정확한 방문 시간까지는 확인되지 않습니다. 방문 시간은 설치일 전날 또는 당일 담당 기사님이 연락드려 안내할 예정입니다.

기사님의 배차 및 현장 상황에 따라 일정이나 방문 시간이 변경될 수 있는 점 참고 부탁드립니다."""
)

DELIVERY_DATE_TIME_ANSWER = format_final_answer(
    """확인 결과 설치 예정일은 {delivery_date}, 방문 예정 시간은 {installation_time}입니다.

기사님의 배차 및 현장 상황에 따라 일정이나 방문 시간이 변경될 수 있는 점 참고 부탁드립니다."""
)

DELIVERY_DATE_PENDING_ANSWER = format_final_answer(
    """현재 삼성 설치 시스템에는
아직 설치(배송) 예정일이 등록되지 않았습니다.
일정이 등록되면 확인 가능합니다."""
)

DELIVERY_LOOKUP_FAILED_ANSWER = format_final_answer(
    """현재 배송 또는 설치 일정 조회가 원활하지 않아 정확한 예정일을 안내드리기 어렵고 담당자 확인이 필요합니다.

임의의 날짜를 안내하지 않고 담당자가 주문 정보를 다시 확인한 뒤 안내드리겠습니다."""
)

ORDER_LOOKUP_FAILED_ANSWER = format_final_answer(
    """문의하신 배송·설치 일정을 확인하기 위해 주문 정보를 조회했으나, 현재 주문 조회가 원활하지 않아 정확한 일정을 바로 확인하기 어려운 상태입니다.

확인되지 않은 일정을 임의로 안내드릴 수 없어 해당 문의는 직원 확인이 필요한 상태로 처리하겠습니다."""
)

DELIVERY_NOT_FOUND_ANSWER = format_final_answer(
    """남겨주신 주문번호로 배송 또는 설치 일정을 확인하지 못했습니다.

네이버 구매내역에서 상품주문번호가 아닌 일반 주문번호가 맞는지 다시 확인해 주세요.

확인된 일반 주문번호를 남겨주시면 다시 조회해 안내드리겠습니다."""
)

DELIVERY_INVALID_DATE_ANSWER = format_final_answer(
    """배송 또는 설치 일정 조회 결과에 확인이 필요한 날짜 정보가 있어 담당자 검토가 필요합니다.

정확한 일정을 확인한 뒤 안내드리겠습니다."""
)

DELIVERY_LOOKUP_REQUIRED_ANSWER = format_final_answer(
    """배송 또는 설치 일정을 안내하기 위해 주문 조회가 필요합니다.

조회가 완료되기 전에는 일반 안내 문구나 임의의 일정을 답변으로 사용하지 않습니다."""
)

DELIVERY_MANUAL_REVIEW_ANSWER = DELIVERY_LOOKUP_FAILED_ANSWER

DELIVERY_CHANGE_REVIEW_ANSWER = format_final_answer(
    """요청하신 배송·설치 일정 변경은 담당자 확인이 필요합니다.

주문 정보를 확인한 후 안내드리겠습니다."""
)


@dataclass(frozen=True)
class DeliveryAnswerContext:
    inquiry_id: int | None
    inquiry_type: str
    intent: str
    is_delivery: bool
    inquiry_text: str
    order_id: str
    product_order_id: str
    order_id_status: str
    dps_lookup_status: str
    dps_lookup_completed_at: str | None
    installation_date_raw: str | None
    installation_date_display: str | None
    installation_time: str | None
    delivery_status: str | None
    dps_error_code: str | None
    dps_error_message: str | None
    selected_answer_route: str
    selected_template: str | None
    template_variables: dict[str, Any]


def _clock_time(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = re.search(
        r"(?<!\d)(?:오전|오후)?\s*(?:[01]?\d|2[0-3])"
        r"(?::[0-5]\d|\s*시(?:\s*[0-5]?\d\s*분)?)(?!\d)",
        text,
    )
    return match.group(0).strip() if match else None


def _canonical_date(dps: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return a canonical DPS date without treating a past date as corrupt."""

    fields = (
        "installation_date",
        "installation_date_raw",
        "required_delivery_date",
        "requiredDeliveryDate",
        "품목상세내역 요구납기일",
        "expected_installation_date",
        "promised_date",
        "delivery_date",
    )
    raw_value = next(
        (dps.get(key) for key in fields if dps.get(key) not in (None, "")),
        None,
    )
    if raw_value is None:
        return None, None
    raw = str(raw_value).strip()
    parse_status = str(dps.get("date_parse_status") or "").upper()
    if parse_status in {"PARSE_FAILED", "CONFLICT", "INVALID"}:
        return raw, None
    parsed = parse_date_value(raw_value)
    if parsed is None:
        return raw, None
    canonical = parsed.isoformat()
    return canonical, korean_date(canonical)


def build_delivery_answer_context(
    request: AnswerRequest,
    analysis: InquiryAnalysis,
) -> DeliveryAnswerContext:
    dps = (
        dict(request.metadata.get("dps"))
        if isinstance(request.metadata.get("dps"), dict)
        else {}
    )
    raw_date, display_date = _canonical_date(dps)
    return DeliveryAnswerContext(
        inquiry_id=request.inquiry_id,
        inquiry_type=analysis.inquiry_type.value,
        intent=analysis.detected_intent,
        is_delivery=analysis.delivery_question,
        inquiry_text=request.question,
        order_id=request.order_id,
        product_order_id=request.product_order_id,
        order_id_status=analysis.order_id_status.value,
        dps_lookup_status=str(dps.get("lookup_status") or "NOT_RUN").upper(),
        dps_lookup_completed_at=(
            str(
                dps.get("lookup_completed_at")
                or dps.get("lookup_timestamp")
                or dps.get("queried_at")
            )
            if (
                dps.get("lookup_completed_at")
                or dps.get("lookup_timestamp")
                or dps.get("queried_at")
            )
            else None
        ),
        installation_date_raw=raw_date,
        installation_date_display=display_date,
        installation_time=_clock_time(
            dps.get("installation_time")
            or dps.get("installation_time_text")
            or dps.get("visit_time_window")
        ),
        delivery_status=(
            str(dps.get("delivery_status"))
            if dps.get("delivery_status") is not None
            else None
        ),
        dps_error_code=(
            str(dps.get("error_code"))
            if dps.get("error_code") is not None
            else None
        ),
        dps_error_message=(
            str(dps.get("error_message"))
            if dps.get("error_message") is not None
            else None
        ),
        selected_answer_route="UNRESOLVED",
        selected_template=None,
        template_variables={},
    )


def _routing_metadata(
    metadata: dict,
    *,
    answer_type: str,
    answer_source: str,
    order_id_present: bool,
    dps_lookup_attempted: bool,
    delivery_date_found: bool,
    gpt_called: bool = False,
) -> dict:
    routed = dict(metadata)
    routed.update(
        {
            "answer_type": answer_type,
            "answer_source": answer_source,
            "order_id_present": order_id_present,
            "dps_lookup_attempted": dps_lookup_attempted,
            "delivery_date_found": delivery_date_found,
            "gpt_called": gpt_called,
            "draft_created": True,
            "requires_manual_review": answer_type
            == "manual_review_required",
        }
    )
    return routed


def _is_schedule_change(analysis: InquiryAnalysis) -> bool:
    """Whether the customer asked us to *move* the schedule, not read it."""

    return (
        analysis.inquiry_subtype == "SCHEDULE_CHANGE_REQUEST"
        or str(analysis.detected_intent or "").upper() == "SCHEDULE_CHANGE"
    )


def apply_phase9_rule_policy(
    request: AnswerRequest,
    result: AnswerResult,
    analysis: InquiryAnalysis,
) -> AnswerResult:
    """Create a grounded fallback before any provider is invoked."""

    metadata = dict(result.metadata)
    metadata["phase9"] = {"analysis": analysis.to_dict()}
    context = build_delivery_answer_context(request, analysis)

    def routed_result(
        *,
        route: str,
        template: str,
        answer: str,
        answer_type: str,
        source: str = "delivery_template",
        generated: bool = True,
        requires_review: bool = False,
        variables: dict[str, Any] | None = None,
        date_found: bool = False,
    ) -> AnswerResult:
        context_value = asdict(context)
        context_value.update(
            {
                "selected_answer_route": route,
                "selected_template": template,
                "template_variables": dict(variables or {}),
            }
        )
        routed_metadata = _routing_metadata(
            metadata,
            answer_type=answer_type,
            answer_source=source,
            order_id_present=analysis.order_id_validated,
            dps_lookup_attempted=context.dps_lookup_status
            not in {"NOT_RUN", "NOT_REQUIRED", "WAITING_FOR_ORDER_ID"},
            delivery_date_found=date_found,
        )
        routed_metadata["delivery_context"] = context_value
        routed_metadata["selected_answer_route"] = route
        routed_metadata["detected_intent"] = analysis.detected_intent
        return AnswerResult(
            status=(
                AnswerStatus.GENERATED
                if generated
                else AnswerStatus.NEEDS_REVIEW
            ),
            category=analysis.inquiry_type.value,
            reason=f"배송 전용 라우팅: {route}",
            answer=answer,
            provider="phase9_policy",
            auto_answerable=generated and not requires_review,
            needs_review=requires_review or not generated,
            matched_rule=template,
            warnings=tuple(result.warnings),
            metadata=routed_metadata,
        )

    if analysis.answer_strategy is AnswerStrategy.REQUEST_ORDER_ID:
        return routed_result(
            route="ORDER_ID_REQUEST",
            template="PHASE9_REQUEST_ORDER_ID",
            answer=ORDER_ID_REQUEST_ANSWER,
            answer_type="order_id_required",
            source="ORDER_ID_REQUEST",
        )

    order_lookup_status = str(
        request.metadata.get("order_lookup_status") or "SUCCESS"
    ).upper()
    if order_lookup_status == "NOT_FOUND":
        return routed_result(
            route="DELIVERY_ORDER_NOT_FOUND",
            template="PHASE9_DELIVERY_ORDER_NOT_FOUND",
            answer=DELIVERY_NOT_FOUND_ANSWER,
            answer_type="delivery_order_not_found",
            source="SAFE_TEMPLATE",
            requires_review=True,
        )
    if order_lookup_status in {
        "FAILED",
        "TIMEOUT",
        "API_ERROR",
        "AUTH_ERROR",
        "PARSE_ERROR",
        "NETWORK_ERROR",
    }:
        return routed_result(
            route="ORDER_LOOKUP_FAILED",
            template="PHASE9_ORDER_LOOKUP_FAILED",
            answer=ORDER_LOOKUP_FAILED_ANSWER,
            answer_type="manual_review_required",
            source="ORDER_LOOKUP_FAILED",
            requires_review=True,
        )

    dps = (
        request.metadata.get("dps")
        if isinstance(request.metadata.get("dps"), dict)
        else {}
    )
    metadata["dps"] = dict(dps)
    date_value = context.installation_date_display
    if (
        analysis.answer_strategy is AnswerStrategy.DIRECT_FACT_ANSWER
        and analysis.requires_dps_lookup
        and date_value
        and not dps.get("change_request")
    ):
        variables = {"delivery_date": date_value}
        if context.installation_time:
            variables["installation_time"] = context.installation_time
            answer = DELIVERY_DATE_TIME_ANSWER.format(**variables)
        else:
            answer = DELIVERY_DATE_ANSWER.format(**variables)
        return routed_result(
            route="DELIVERY_WITH_INSTALLATION_DATE",
            template=(
                "PHASE9_CONFIRMED_INSTALLATION_DATE_TIME"
                if context.installation_time
                else "PHASE9_CONFIRMED_INSTALLATION_DATE"
            ),
            answer=answer,
            answer_type="delivery_date",
            source="dps",
            variables=variables,
            date_found=True,
        )
    if context.installation_date_raw and not context.installation_date_display:
        return routed_result(
            route="DELIVERY_DATE_INVALID",
            template="PHASE9_DELIVERY_DATE_INVALID",
            answer=DELIVERY_INVALID_DATE_ANSWER,
            answer_type="delivery_date_invalid",
            source="SAFE_TEMPLATE",
            requires_review=True,
        )
    # ``delivery_question`` is the analysis object's own definition of "this
    # asks about a delivery or installation schedule", and it already covers
    # both the subtype set that used to be written out here and the delivery
    # intents. Spelling the subtypes out again silently excluded
    # COMPOUND_MULTI_INTENT -- which is what a customer writes whenever they
    # ask two things at once ("오늘 주문했는데 언제 받아볼 수 있을까요? 대략적인
    # 배송 예정이라도 알 수 없나요?"). Those inquiries matched no branch here,
    # fell through to the tail, and answer_service rejected the unrecognised
    # answer_source by raising -- so a successful DPS lookup produced no draft
    # at all.
    if (
        analysis.delivery_question
        and analysis.order_id_validated
        and str(dps.get("lookup_status") or "").upper() == "SUCCESS"
        and not date_value
    ):
        return routed_result(
            route="DELIVERY_DATE_UNCONFIRMED",
            template="PHASE9_DELIVERY_DATE_PENDING",
            answer=DELIVERY_DATE_PENDING_ANSWER,
            answer_type="delivery_date_pending",
            source="SAFE_TEMPLATE",
        )
    if context.dps_lookup_status == "NOT_FOUND":
        return routed_result(
            route="DELIVERY_ORDER_NOT_FOUND",
            template="PHASE9_DELIVERY_ORDER_NOT_FOUND",
            answer=DELIVERY_NOT_FOUND_ANSWER,
            answer_type="delivery_order_not_found",
            source="SAFE_TEMPLATE",
            requires_review=True,
        )
    if context.dps_lookup_status in {
        "TIMEOUT",
        "AGENT_OFFLINE",
        "PARSE_ERROR",
        "AUTOMATION_ERROR",
        "NETWORK_ERROR",
        "CACHE_CORRUPTION",
        "STALE_CACHE",
        "CANCELLED",
        "FAILED",
    }:
        return routed_result(
            route="DPS_LOOKUP_FAILED",
            template="PHASE9_DELIVERY_LOOKUP_FAILED",
            answer=DELIVERY_LOOKUP_FAILED_ANSWER,
            answer_type="manual_review_required",
            source="SAFE_TEMPLATE",
            requires_review=True,
        )
    if (
        analysis.delivery_question
        and analysis.order_id_validated
        and analysis.requires_dps_lookup
    ):
        return routed_result(
            route="DELIVERY_LOOKUP_REQUIRED",
            # Keyed on the intent as well as the subtype, for the same reason
            # as above: a customer who asks to move the date *and* asks
            # something else is COMPOUND_MULTI_INTENT, and telling them "주문
            # 조회가 필요합니다" answers a question they did not ask.
            template=(
                "PHASE9_DELIVERY_CHANGE_REVIEW"
                if _is_schedule_change(analysis)
                else "PHASE9_DELIVERY_LOOKUP_REQUIRED"
            ),
            answer=(
                DELIVERY_CHANGE_REVIEW_ANSWER
                if _is_schedule_change(analysis)
                else DELIVERY_LOOKUP_REQUIRED_ANSWER
            ),
            answer_type="manual_review_required",
            source="SAFE_TEMPLATE",
            requires_review=True,
        )
    # A delivery-schedule inquiry that matched no branch above is a routing
    # gap, and the caller treats an unrecognised answer_source as fatal --
    # AnswerGenerationError, no draft written, "답변 생성 버튼을 눌러 초안을
    # 생성하세요" left on screen even though the DPS lookup had succeeded.
    #
    # A gap in the routing table is not a reason to produce nothing. The safe
    # template says only that the schedule still needs checking and carries
    # requires_review, so the customer is never told an invented date and a
    # person still sees the inquiry. The guard in answer_service stays exactly
    # as strict; this simply stops delivery inquiries reaching it unrouted.
    #
    # The safe template still has to say the right safe thing. A customer who
    # writes "오늘 주문했는데 해피콜 및 기사님 빠른설치 부탁드릴게요" is asking
    # a person to do something, and the classifier records exactly that --
    # SCHEDULE_CHANGE_REQUEST, manual_review_required, order id MISSING. This
    # guard answered every unrouted delivery inquiry with "주문 조회가
    # 필요합니다", which threw that meaning away and replied only about the
    # order number. Keyed on the same predicate the main branch uses, so the
    # two cannot drift.
    if analysis.delivery_question:
        change_request = _is_schedule_change(analysis)
        return routed_result(
            route="DELIVERY_LOOKUP_REQUIRED",
            template=(
                "PHASE9_DELIVERY_CHANGE_REVIEW"
                if change_request
                else "PHASE9_DELIVERY_LOOKUP_REQUIRED"
            ),
            answer=(
                DELIVERY_CHANGE_REVIEW_ANSWER
                if change_request
                else DELIVERY_LOOKUP_REQUIRED_ANSWER
            ),
            answer_type="manual_review_required",
            source="SAFE_TEMPLATE",
            requires_review=True,
        )
    return AnswerResult(
        status=result.status,
        category=result.category,
        reason=result.reason,
        answer=result.answer,
        provider=result.provider,
        auto_answerable=result.auto_answerable,
        needs_review=result.needs_review,
        matched_rule=result.matched_rule,
        warnings=result.warnings,
        metadata=metadata,
    )
