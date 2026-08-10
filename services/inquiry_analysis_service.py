from __future__ import annotations

import re

from answer.inquiry_analysis import (
    AnswerStrategy,
    InquiryAnalysis,
    InquiryType,
    OrderIdStatus,
)
from answer.models import AnswerRequest


GENERAL_ORDER_ID_PATTERN = re.compile(r"(?<!\d)\d{16}(?!\d)")
ANY_LONG_NUMBER_PATTERN = re.compile(r"(?<!\d)\d{10,24}(?!\d)")
VALID_GENERAL_ORDER_ID_PATTERN = re.compile(r"\d{16}")

DELIVERY_SCHEDULE_WORDS = (
    "도착예정",
    "도착 예정",
    "도착예정일",
    "도착 예정일",
    "배송일",
    "배송예정일",
    "배송 일정",
    "배송일정",
    "배송 언제",
    "배송 언제 오",
    "배송 날짜",
    "배송날짜",
    "배송조회",
    "배송 조회",
    "지정일 배송",
    "이번 주에 받",
    "언제 받을",
    "언제쯤 받을",
    "언제쯤받을",
    "받을 수 있을",
    "받을수있을",
    "언제 도착",
    "주문했는데 언제",
    "언제 오",
    "설치일",
    "설치 일정",
    "설치 언제",
    "방문 일정",
    "기사 방문",
    "도착 시간",
    "도착시간",
    "방문 시간",
    "방문시간",
    "몇 시에 도착",
    "몇시에 도착",
    "몇 시쯤 방문",
    "몇시쯤 방문",
    "기사님 언제",
    "기사님 몇 시",
    "기사님 몇시",
    "설치 날짜",
    "설치날짜",
    "설치 예정일",
    "설치예정일",
    "설치 언제",
    "언제 설치",
    "오늘 오",
    "내일 오",
    "일정 확인",
)
NOTIFICATION_POLICY_WORDS = (
    "알림톡",
    "안내 문자",
    "문자 오",
    "전날 연락",
    "연락이 오",
    "연락 오",
)
ORDER_STATUS_WORDS = (
    "주문 상태",
    "주문 확인",
    "주문됐",
    "결제 확인",
    "출고 상태",
)
INSTALLATION_GENERAL_WORDS = (
    "벽걸이",
    "스탠드",
    "타공",
    "설치비",
    "설치 조건",
    "설치 가능",
    "브라켓",
)
PRODUCT_GENERAL_WORDS = (
    "사양",
    "기능",
    "구성품",
    "호환",
    "크기",
    "무게",
    "색상",
    "모델",
    "지원하나요",
    "가능한가요",
)
CANCEL_WORDS = ("취소", "반품", "교환", "환불")
SCHEDULE_CHANGE_WORDS = (
    "설치일 변경",
    "설치 일정 변경",
    "배송일 변경",
    "배송 일정 변경",
    "변경해 주세요",
    "변경해주세요",
)
HIGH_RISK_WORDS = (
    "피해보상",
    "법적",
    "소송",
    "분쟁",
    "화재",
    "감전",
    "부상",
)
PRE_PURCHASE_DELIVERY_WORDS = (
    "오늘주문하면",
    "지금주문하면",
    "주문하면",
    "주문할예정",
    "구매하려",
    "구매예정",
    "구매하면",
    "배송얼마나걸",
    "배송기간",
    "받을수있",
    "도착가능",
    "배송가능",
    "설치가능",
)
POST_PURCHASE_DELIVERY_WORDS = (
    "주문했",
    "주문완료",
    "주문한지",
    "구매했",
    "구매완료",
    "결제했",
    "결제완료",
    "배송조회",
    "배송상태",
    "발송대기",
    "배송준비",
    "출고대기",
    "송장",
    "운송장",
)


class InquiryAnalysisService:
    """Deterministic first-pass analysis used before any provider call."""

    @staticmethod
    def _intent(question: str) -> str | None:
        compact = re.sub(r"[\s\W_]+", "", question).lower()
        # Customer-facing schedule language is a deterministic business rule,
        # not a score.  Keep this block ahead of legacy keyword scoring and
        # provider inference so newly-synced inquiries cannot inherit a stale
        # GENERAL classification.
        notification_policy = any(
            token in compact
            for token in (
                "배송알림톡",
                "알림톡은언제",
                "문자안내",
                "일반배송정책",
                "이벤트배송정책",
            )
        )
        if notification_policy:
            return "NOTIFICATION_POLICY"
        if any(
            token in compact
            for token in (
                "설치기사님몇시",
                "기사님몇시",
                "기사방문시간",
                "방문시간",
                "몇시설치",
            )
        ):
            return "INSTALLATION_TIME"
        if any(
            token in compact
            for token in (
                "몇시에도착",
                "몇시도착",
                "몇시에와",
                "배송시간",
            )
        ):
            return "DELIVERY_TIME"
        if any(
            token in compact
            for token in (
                "설치예정",
                "설치일",
                "설치날짜",
                "설치언제",
                "설치기사님언제",
                "설치기사님은언제",
                "기사님언제",
                "기사님은언제",
            )
        ):
            return "INSTALLATION_DATE"
        if any(
            token in compact
            for token in (
                "배송되나요",
                "배송날짜",
                "배송언제",
                "언제배송",
                "도착",
                "언제와요",
                "언제오나요",
                "언제쯤",
                "받을수",
                "언제받을",
                "주문한지",
                "한달",
                "한달이네요",
                "출고",
                "배송지연",
            )
        ):
            return "DELIVERY_DATE"
        notification = any(
            re.sub(r"\s+", "", word) in compact
            for word in NOTIFICATION_POLICY_WORDS
        )
        explicit_schedule = any(
            token in compact
            for token in (
                "배송일", "배송예정일", "배송일정", "설치일",
                "배송날짜",
                "설치날짜", "설치예정일", "설치일정", "도착예정",
                "도착날짜", "도착시간", "방문시간", "몇시에도착",
                "몇시에오", "몇시쯤방문", "언제도착", "언제받",
                "언제쯤받", "받을수있", "언제올까",
                "언제설치", "배송언제", "설치언제", "오늘오",
                "내일오", "기사님언제", "기사님몇시", "일정확인",
            )
        )
        if notification and not explicit_schedule:
            return "NOTIFICATION_POLICY"
        if any(
            token in compact
            for token in (
                "설치기사님몇시", "설치시간", "방문시간",
                "몇시쯤방문", "기사님몇시",
            )
        ):
            return "INSTALLATION_TIME"
        if any(
            token in compact
            for token in (
                "몇시에도착", "몇시에오", "도착시간", "배송시간",
            )
        ):
            return "DELIVERY_TIME"
        if any(
            token in compact
            for token in (
                "설치일", "설치날짜", "설치예정일", "설치일정",
                "언제설치",
            )
        ):
            return "INSTALLATION_DATE"
        if any(
            token in compact
            for token in (
                "배송일", "배송예정일", "배송일정", "도착예정",
                "배송날짜",
                "도착날짜", "언제도착", "언제받", "배송언제",
                "언제쯤받", "받을수있", "언제올까",
                "오늘오", "내일오", "언제와", "언제오", "일정확인",
            )
        ):
            return "DELIVERY_DATE"
        if any(token in compact for token in ("배송조회", "배송상태", "출고상태")):
            return "DELIVERY_STATUS"
        return None

    @staticmethod
    def _is_pre_purchase_delivery(
        question: str,
        *,
        has_order_evidence: bool,
    ) -> bool:
        """Separate policy/availability questions from an existing order lookup."""

        if has_order_evidence:
            return False
        compact = re.sub(r"[\s\W_]+", "", str(question or "")).lower()
        if any(word in compact for word in POST_PURCHASE_DELIVERY_WORDS):
            return False
        if re.search(r"\d{1,2}(?:월|[./-])\d{1,2}(?:일)?주문", compact):
            return False
        delivery_context = any(
            word in compact
            for word in ("배송", "발송", "도착", "받을", "설치")
        )
        return delivery_context and any(
            word in compact for word in PRE_PURCHASE_DELIVERY_WORDS
        )

    def analyze(self, request: AnswerRequest) -> InquiryAnalysis:
        question = re.sub(r"\s+", " ", str(request.question or "")).strip()
        legacy_type = str(request.inquiry_type or "").strip()
        order_text = str(request.order_id or "").strip()
        order_present = bool(order_text)
        has_order = bool(VALID_GENERAL_ORDER_ID_PATTERN.fullmatch(order_text))
        has_product_order = bool(
            str(request.product_order_id or "").strip()
        )
        detected_intent = self._intent(question)
        candidates = GENERAL_ORDER_ID_PATTERN.findall(question)
        long_numbers = ANY_LONG_NUMBER_PATTERN.findall(question)
        pre_purchase_delivery = self._is_pre_purchase_delivery(
            question,
            has_order_evidence=bool(
                has_order or has_product_order or candidates or long_numbers
            ),
        )

        if has_order:
            order_status = OrderIdStatus.VALIDATED
        elif order_present:
            order_status = OrderIdStatus.INVALID
        elif has_product_order:
            order_status = OrderIdStatus.AMBIGUOUS
        elif len(candidates) == 1:
            order_status = OrderIdStatus.CANDIDATE_FOUND
        elif len(candidates) > 1:
            order_status = OrderIdStatus.AMBIGUOUS
        else:
            order_status = OrderIdStatus.MISSING

        reasons: list[str] = []
        if any(word in question for word in HIGH_RISK_WORDS):
            kind = InquiryType.MANUAL_REVIEW_REQUIRED
            subtype = "HIGH_RISK_OR_DISPUTE"
            requires_order = False
            requires_dps = False
            strategy = AnswerStrategy.MANUAL_REVIEW
            confidence = 0.99
            manual = True
            reasons.append("위험·분쟁 관련 표현이 있어 직원 판단이 필요합니다.")
        elif any(word in question for word in CANCEL_WORDS):
            kind = InquiryType.CANCEL_RETURN_EXCHANGE
            subtype = "CANCEL_RETURN_EXCHANGE"
            requires_order = True
            requires_dps = False
            strategy = (
                AnswerStrategy.MANUAL_REVIEW
                if has_order
                else AnswerStrategy.REQUEST_ORDER_ID
            )
            confidence = 0.97
            manual = has_order
            reasons.append("취소·반품·교환 문의로 분류했습니다.")
        elif any(word in question for word in SCHEDULE_CHANGE_WORDS):
            kind = InquiryType.DELIVERY_INSTALLATION_STATUS
            subtype = "SCHEDULE_CHANGE_REQUEST"
            requires_order = True
            requires_dps = True
            strategy = (
                AnswerStrategy.MANUAL_REVIEW
                if has_order
                else AnswerStrategy.REQUEST_ORDER_ID
            )
            confidence = 0.99
            manual = has_order
            detected_intent = "SCHEDULE_CHANGE"
            reasons.append("일정 변경 요청은 직원 검토가 필요합니다.")
        elif detected_intent == "NOTIFICATION_POLICY":
            kind = InquiryType.INSTALLATION_GENERAL
            subtype = "DELIVERY_NOTIFICATION_POLICY"
            requires_order = False
            requires_dps = False
            strategy = AnswerStrategy.GENERAL_GUIDANCE
            confidence = 0.97
            manual = False
            reasons.append("배송·설치 알림 방식에 대한 일반 정책 문의입니다.")
        elif pre_purchase_delivery:
            kind = InquiryType.INSTALLATION_GENERAL
            subtype = "PRE_PURCHASE_DELIVERY_GUIDANCE"
            requires_order = False
            requires_dps = False
            strategy = AnswerStrategy.GENERAL_GUIDANCE
            confidence = 0.99
            manual = False
            detected_intent = "PRE_PURCHASE_DELIVERY"
            reasons.append(
                "주문 전 배송·설치 가능 여부를 묻는 일반 정책 문의입니다."
            )
        elif detected_intent in {
            "DELIVERY_DATE",
            "DELIVERY_TIME",
            "INSTALLATION_DATE",
            "INSTALLATION_TIME",
            "DELIVERY_STATUS",
        } or any(word in question for word in DELIVERY_SCHEDULE_WORDS):
            kind = InquiryType.DELIVERY_INSTALLATION_STATUS
            subtype = "DELIVERY_OR_INSTALLATION_SCHEDULE"
            requires_order = True
            requires_dps = True
            strategy = (
                AnswerStrategy.DIRECT_FACT_ANSWER
                if has_order
                else AnswerStrategy.REQUEST_ORDER_ID
            )
            confidence = 0.98
            manual = False
            detected_intent = detected_intent or "DELIVERY_DATE"
            reasons.append("배송 또는 설치 일정 확인 표현을 찾았습니다.")
        elif any(word in question for word in ORDER_STATUS_WORDS):
            kind = InquiryType.ORDER_STATUS
            subtype = "ORDER_LOOKUP"
            requires_order = True
            requires_dps = False
            strategy = (
                AnswerStrategy.DIRECT_FACT_ANSWER
                if has_order
                else AnswerStrategy.REQUEST_ORDER_ID
            )
            confidence = 0.96
            manual = False
            reasons.append("주문 상태 확인 표현을 찾았습니다.")
        elif any(word in question for word in INSTALLATION_GENERAL_WORDS):
            kind = InquiryType.INSTALLATION_GENERAL
            subtype = "GENERAL_INSTALLATION_GUIDANCE"
            requires_order = False
            requires_dps = False
            strategy = AnswerStrategy.GENERAL_GUIDANCE
            confidence = 0.94
            manual = False
            reasons.append("특정 주문과 무관한 설치 일반 문의입니다.")
        elif any(word in question for word in PRODUCT_GENERAL_WORDS):
            kind = InquiryType.PRODUCT_GENERAL
            subtype = "PRODUCT_SPEC_OR_FEATURE"
            requires_order = False
            requires_dps = False
            strategy = AnswerStrategy.GENERAL_GUIDANCE
            confidence = 0.92
            manual = False
            reasons.append("제품 사양·기능 관련 일반 문의입니다.")
        elif "배송" in legacy_type:
            kind = InquiryType.DELIVERY_INSTALLATION_STATUS
            subtype = "LEGACY_DELIVERY_CATEGORY"
            requires_order = True
            requires_dps = True
            strategy = (
                AnswerStrategy.DIRECT_FACT_ANSWER
                if has_order
                else AnswerStrategy.REQUEST_ORDER_ID
            )
            confidence = 0.78
            manual = False
            detected_intent = detected_intent or "DELIVERY_STATUS"
            reasons.append(
                "본문 키워드 대신 기존 배송 문의 유형을 보조 근거로 사용했습니다."
            )
        elif "상품" in legacy_type:
            kind = InquiryType.PRODUCT_GENERAL
            subtype = "LEGACY_PRODUCT_CATEGORY"
            requires_order = False
            requires_dps = False
            strategy = AnswerStrategy.GENERAL_GUIDANCE
            confidence = 0.72
            manual = False
            reasons.append(
                "본문 키워드 대신 기존 상품 문의 유형을 보조 근거로 사용했습니다."
            )
        elif not question:
            kind = InquiryType.INFORMATION_INSUFFICIENT
            subtype = "EMPTY_QUESTION"
            requires_order = False
            requires_dps = False
            strategy = AnswerStrategy.REQUEST_ADDITIONAL_INFORMATION
            confidence = 1.0
            manual = True
            reasons.append("문의 내용이 비어 있습니다.")
        else:
            kind = InquiryType.INFORMATION_INSUFFICIENT
            subtype = "UNCLASSIFIED"
            requires_order = False
            requires_dps = False
            strategy = AnswerStrategy.MANUAL_REVIEW
            confidence = 0.45
            manual = True
            detected_intent = detected_intent or "GENERAL"
            reasons.append("결정적인 문의 유형 규칙과 일치하지 않습니다.")

        requires_order_id = requires_order
        validated = has_order
        effective_kind = kind
        if requires_order_id and not validated:
            effective_kind = InquiryType.ORDER_INFO_REQUIRED
            strategy = AnswerStrategy.REQUEST_ORDER_ID
            manual = False
            if has_product_order:
                reasons.append(
                    "상품주문번호만 있으며 DPS에 사용할 일반 주문번호가 없습니다."
                )
            elif candidates:
                reasons.append(
                    "문의 본문의 숫자는 후보일 뿐 API 검증된 주문번호가 아닙니다."
                )
            else:
                reasons.append(
                    "일반 주문번호가 없거나 형식이 유효하지 않습니다."
                    if order_present
                    else "확인된 네이버 일반 주문번호가 없습니다."
                )
        elif not requires_order_id:
            order_status = OrderIdStatus.NOT_REQUIRED

        selected = self._selected_keys(effective_kind, strategy)
        return InquiryAnalysis(
            inquiry_type=effective_kind,
            inquiry_subtype=subtype,
            requires_order_lookup=requires_order,
            # Business requirement and call eligibility are deliberately
            # separate. A missing order id still means DPS is required for a
            # delivery inquiry, but can_execute_dps_lookup remains false.
            requires_dps_lookup=requires_dps,
            requires_order_id=requires_order_id,
            order_id_present=has_order,
            order_id_validated=validated,
            order_id_status=order_status,
            answer_strategy=strategy,
            selected_fact_keys=selected,
            confidence=confidence,
            reasons=tuple(reasons),
            manual_review_required=manual,
            auto_answerable=not manual,
            detected_intent=detected_intent or "GENERAL",
        )

    @staticmethod
    def _selected_keys(
        inquiry_type: InquiryType,
        strategy: AnswerStrategy,
    ) -> tuple[str, ...]:
        if strategy is AnswerStrategy.REQUEST_ORDER_ID:
            return (
                "analysis.requires_order_id",
                "analysis.order_id_status",
                "analysis.private_post_required",
            )
        if strategy is AnswerStrategy.MANUAL_REVIEW:
            return ("rule.answer",)
        if inquiry_type is InquiryType.DELIVERY_INSTALLATION_STATUS:
            return (
                "installation.date",
                "installation.source",
                "installation.installation_date_confirmed",
                "policy.installation_notification_policy",
                "policy.date_may_change",
            )
        if inquiry_type is InquiryType.ORDER_STATUS:
            return ("delivery.status", "order.order_status")
        if inquiry_type in {
            InquiryType.PRODUCT_GENERAL,
            InquiryType.INSTALLATION_GENERAL,
        }:
            return ("product.name", "product.option_name", "rule.answer")
        return ("rule.answer",)
