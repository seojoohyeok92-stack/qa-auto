from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from answer.answer_format import extract_answer_body
from services.historical_learning_quality_service import (
    DATE_RANGE,
    HistoricalLearningQualityService,
    TEMPORARY,
)
from services.learning_privacy_service import LearningPrivacyService
from services.product_fact_guard import classify_product_fact


LONG_IDENTIFIER = re.compile(r"(?<!\d)\d{10,}(?!\d)")
PHONE = re.compile(r"(?<!\d)01[016789][ -]?\d{3,4}[ -]?\d{4}(?!\d)")
ADDRESS = re.compile(
    r"(?:서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|"
    r"전북|전남|경북|경남|제주)[^\n]{0,30}(?:시|군|구|동|읍|면|로|길)"
)
TRACKING_NUMBER = re.compile(r"(?<!\d)\d{3,4}[ -]\d{3,4}[ -]\d{3,4}(?!\d)")
PERSONAL_RESULT = re.compile(
    r"(?:고객님|해당\s*주문|요청하신|문의하신\s*주문).{0,40}"
    r"(?:처리|완료|취소|환불|반품|교환|접수|출고|발송|방문|회수)|"
    r"(?:취소|환불|반품|교환|접수|회수).{0,24}(?:완료|처리되|진행되)|"
    r"(?:기사|택배).{0,20}(?:배정|방문\s*예정|연락\s*완료)",
    re.IGNORECASE,
)
CURRENT_ORDER_FACT = re.compile(
    r"(?:현재|확인\s*결과|조회\s*결과|해당\s*주문).{0,35}"
    r"(?:배송\s*중|배송\s*완료|출고|발송|설치\s*예정|도착\s*예정|"
    r"기사\s*배정|방문\s*예정)|"
    r"(?:배송|도착|설치|방문).{0,12}"
    r"(?:20\d{2}[./-]\d{1,2}[./-]\d{1,2}|\d{1,2}[./-]\d{1,2})",
    re.IGNORECASE,
)
CURRENT_ORDER_QUESTION = re.compile(
    r"(?:배송중|배송\s*완료|배송된다고|출고됐|발송됐|기사.{0,12}(?:연락|방문)|"
    r"(?:배송|설치|방문)\s*예정일).{0,45}(?:언제|오늘|오나요|올까요|확인|부탁)|"
    r"(?:언제|오늘).{0,24}(?:도착|배송|설치|기사\s*방문)",
    re.IGNORECASE,
)
CUSTOMER_IDENTIFIER_FACT = re.compile(
    r"(?:고객님(?:의)?|문의하신|확인(?:되|된|되는)|조회(?:되|된|되는)|해당).{0,35}"
    r"(?:주문\s*번호|송장\s*번호).{0,24}(?:입니다|확인|(?:\*{2,}|<masked-))",
    re.IGNORECASE,
)
SPECIFIC_CALENDAR_DATE = re.compile(
    r"(?:20\d{2}\s*[년./-]\s*\d{1,2}(?:\s*[월./-]\s*\d{1,2})?|"
    r"\d{1,2}\s*월\s*\d{1,2}\s*일|(?<!\d)\d{1,2}\s*[./-]\s*\d{1,2}(?!\d))"
)
ORDER_DATE_CONTEXT = re.compile(
    r"주문(?:건|번호)|배송\s*(?:날짜|일자|예정|일)|설치\s*(?:날짜|일자|예정|일)|"
    r"방문\s*(?:날짜|일자|예정|일)|도착\s*(?:날짜|일자|예정|일)",
    re.IGNORECASE,
)
DAY_OF_MONTH = re.compile(r"(?<!\d)\d{1,2}\s*일(?!\s*(?:간|정도))")
CURRENT_ORDER_ACTION_REQUEST = re.compile(
    r"(?:취소|환불|반품|교환).{0,18}(?:해\s*주|해주세요|부탁(?:드려요|드립니다)?|"
    r"됐나요|되었나요|완료됐나요)",
    re.IGNORECASE,
)
EVENT_CONTEXT = re.compile(
    r"감사제|페스티벌|프로모션|이벤트|라이브\s*방송|특가|사은품|"
    r"포인트\s*당첨|(?:한전\s*)?복지\s*할인|온누리(?:\s*행사|\s*환급|\s*혜택|\s*신청)|"
    r"(?:여름|하계|명절)\s*휴가",
    re.IGNORECASE,
)
KOREAN_CUSTOMER_NAME = re.compile(
    r"(?<![가-힣])([가-힣]{2,4})\s*고객님",
    re.IGNORECASE,
)
CUSTOMER_ACTION_PROMISE = re.compile(
    r"(?:취소|환불|반품|교환|접수|승인).{0,24}(?:처리\s*(?:도와|하겠|진행)|"
    r"도와\s*드리겠|처리해\s*드리겠|진행해\s*드릴)",
    re.IGNORECASE,
)
CONTACT_ONLY = re.compile(
    r"(?:유선|전화|톡톡).{0,18}(?:안내|연락).{0,12}(?:되었|드렸|완료)",
    re.IGNORECASE,
)
CURRENT_ORDER_EXPERIENCE = re.compile(
    r"(?:주문했|구매했|구입했|샀|시킨|시키고|사고\s*시킨).{0,55}"
    r"(?:안\s*오|못\s*받|지연|취소|반품|수거|회수)|"
    r"(?:한\s*달|며칠|몇\s*주).{0,24}(?:안\s*오|못\s*받|배송\s*지연)",
    re.IGNORECASE,
)
ACKNOWLEDGEMENT_ONLY = re.compile(
    r"^(?:네[.!]?\s*)?(?:확인|처리|접수|취소|환불|반품|교환|전달|완료)"
    r"(?:하였|했|되었|됐|해드렸|되었습니다|했습니다|드립니다|완료입니다)"
    r"[.!\s]*$",
    re.IGNORECASE,
)
GREETING_ONLY = re.compile(
    r"^(?:(?:안녕하세요|고객님|감사합니다|문의\s*주셔서\s*감사합니다|"
    r"도움이\s*되셨길\s*바랍니다)[.!\s]*)+$",
    re.IGNORECASE,
)
INTERNAL_OPERATION = re.compile(
    r"담당\s*부서(?:로)?\s*(?:전달|이관)|내부\s*(?:확인|전달|처리)|"
    r"관리자\s*확인|시스템상\s*처리|CS\s*(?:팀|담당)|업무\s*처리",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class HistoricalReauditDecision:
    disposition: str
    primary_reason: str
    secondary_reasons: tuple[str, ...]
    runtime_status: str
    runtime_context_eligible: bool
    alignment_score: float
    trust_score: float
    temporary: bool
    order_specific: bool
    product_scope_uncertain: bool
    personal_information_detected: bool
    masked_question: str
    masked_answer: str
    product_metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["secondary_reasons"] = list(self.secondary_reasons)
        return result


class HistoricalReauditService:
    """Conservative dry-run interpretation of the production Historical gate."""

    SAFE = "SAFE_REUSABLE"
    REVIEW = "REVIEW_REQUIRED"
    UNSAFE = "UNSAFE_NOT_REUSABLE"

    def __init__(self) -> None:
        self.quality = HistoricalLearningQualityService()
        self.privacy = LearningPrivacyService()

    @staticmethod
    def _metadata(row: dict[str, Any]) -> dict[str, Any]:
        value = row.get("metadata_json")
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _product_metadata(row: dict[str, Any]) -> dict[str, Any]:
        metadata = HistoricalReauditService._metadata(row)
        raw = row.get("raw_json")
        raw = raw if isinstance(raw, dict) else {}
        return {
            "product_name": row.get("product_name"),
            "product_id": row.get("product_id") or raw.get("productId"),
            "model_code": row.get("model_code") or metadata.get("model_code"),
            "category": row.get("category") or metadata.get("category"),
            "product_family": metadata.get("product_family"),
            "size_variant": metadata.get("size_variant"),
            "option": row.get("option_name") or metadata.get("option_name"),
            "source_inquiry_product": row.get("source_inquiry_product")
            or row.get("product_name"),
            "provenance": row.get("source_payload_reference")
            or metadata.get("answer_provenance")
            or row.get("source"),
        }

    def assess(self, row: dict[str, Any]) -> HistoricalReauditDecision:
        question = str(row.get("question") or "").strip()
        answer = str(row.get("seller_answer") or row.get("final_answer") or "").strip()
        metadata = self._metadata(row)
        signal = str(
            row.get("learning_signal_type")
            or metadata.get("learning_signal_type")
            or "POSITIVE"
        ).upper()
        active = bool(row.get("active", True))
        customer_names = (
            row.get("customer_display"), row.get("masked_writer_id")
        )
        masked_question = self.privacy.mask(
            question, customer_names=customer_names
        )
        masked_answer = self.privacy.mask(
            answer, customer_names=customer_names
        )
        name_present = bool(KOREAN_CUSTOMER_NAME.search(answer))
        if name_present:
            masked_answer = KOREAN_CUSTOMER_NAME.sub(
                "<masked-name> 고객님", masked_answer
            )
        personal = bool(
            masked_question != question
            or masked_answer != answer
            or PHONE.search(question + "\n" + answer)
            or LONG_IDENTIFIER.search(question + "\n" + answer)
            or TRACKING_NUMBER.search(question + "\n" + answer)
            or ADDRESS.search(question + "\n" + answer)
            or name_present
        )
        body = extract_answer_body(answer).strip() or answer
        runtime = self.quality.assess(
            question=question,
            answer=answer,
            stored_quality=float(row.get("quality_score") or 0.0),
            policy_risk=str(row.get("policy_risk") or "NONE"),
            active=active,
            structured_temporary_valid=bool(row.get("structured_temporary_valid")),
        )
        product = self._product_metadata(row)
        guard = classify_product_fact(
            question,
            inquiry_type=row.get("inquiry_type"),
            product_id=product["product_id"],
            product_name=product["product_name"],
            option_name=product["option"],
        )
        product_uncertain = bool(
            guard.sensitive
            and not product["product_id"]
            and not product["model_code"]
            and not guard.model_code
        )
        question_has_order_date = bool(
            (SPECIFIC_CALENDAR_DATE.search(question) or DAY_OF_MONTH.search(question))
            and ORDER_DATE_CONTEXT.search(question)
        )
        answer_has_order_date = bool(
            SPECIFIC_CALENDAR_DATE.search(answer)
            and (
                ORDER_DATE_CONTEXT.search(question + "\n" + answer)
                or re.search(r"현재\s*확인|예정(?:으로|일)", answer)
            )
        )
        question_temporary = bool(
            TEMPORARY.search(question) or EVENT_CONTEXT.search(question)
        )
        answer_temporary = bool(EVENT_CONTEXT.search(answer))
        question_has_temporary_date = bool(
            question_temporary and SPECIFIC_CALENDAR_DATE.search(question)
        )
        secondary: list[str] = []
        if personal:
            secondary.append("PERSONAL_INFORMATION_PRESENT")
        if CURRENT_ORDER_FACT.search(answer):
            secondary.append("CURRENT_ORDER_FACT")
        if CURRENT_ORDER_QUESTION.search(question):
            secondary.append("CURRENT_ORDER_QUESTION")
        if CUSTOMER_IDENTIFIER_FACT.search(answer):
            secondary.append("CUSTOMER_SPECIFIC_IDENTIFIER")
        if question_has_order_date:
            secondary.append("CURRENT_ORDER_DATE_QUESTION")
        if answer_has_order_date:
            secondary.append("CURRENT_ORDER_DATE_ANSWER")
        if question_temporary:
            secondary.append("QUESTION_TIME_SENSITIVE")
        if answer_temporary:
            secondary.append("ANSWER_TIME_SENSITIVE")
        if PERSONAL_RESULT.search(answer):
            secondary.append("CUSTOMER_SPECIFIC_FACT")
        if runtime.temporary:
            secondary.append("TIME_SENSITIVE")
        if runtime.order_specific:
            secondary.append("ORDER_SPECIFIC_FACT")
        if product_uncertain:
            secondary.append("PRODUCT_SCOPE_UNCERTAIN")
        if INTERNAL_OPERATION.search(body):
            secondary.append("INTERNAL_OPERATION_TEXT")

        if not question:
            disposition, primary = self.UNSAFE, "MISSING_QUESTION"
        elif not answer:
            disposition, primary = self.UNSAFE, "MISSING_ANSWER"
        elif "source_answered" in row and not bool(row.get("source_answered")):
            # The original import explicitly required Naver's answered flag.
            # A later Q&A Auto observation must not be re-labelled as an old
            # employee answer merely because a SELLER_ANSWER row now exists.
            disposition, primary = self.UNSAFE, "SOURCE_NOT_ANSWERED"
        elif signal in {"NEGATIVE", "EXCLUDED"} or not active or bool(
            metadata.get("verification_revoked")
            or metadata.get("effective_exclusion")
        ):
            disposition, primary = self.UNSAFE, "NEGATIVE_EXCLUDED_OR_REVOKED"
        elif GREETING_ONLY.fullmatch(body):
            disposition, primary = self.UNSAFE, "GREETING_ONLY"
        elif CONTACT_ONLY.search(body) and len(body) < 100:
            disposition, primary = self.UNSAFE, "ANSWER_TOO_GENERIC"
        elif ACKNOWLEDGEMENT_ONLY.fullmatch(body) or len(body) < 6:
            disposition, primary = self.UNSAFE, "ACKNOWLEDGEMENT_ONLY"
        elif (
            CURRENT_ORDER_FACT.search(answer)
            or CURRENT_ORDER_QUESTION.search(question)
            or question_has_order_date
            or answer_has_order_date
            or CURRENT_ORDER_ACTION_REQUEST.search(question)
            or CURRENT_ORDER_EXPERIENCE.search(question)
            or PERSONAL_RESULT.search(answer)
            or CUSTOMER_ACTION_PROMISE.search(answer)
            or CUSTOMER_IDENTIFIER_FACT.search(answer)
        ):
            disposition, primary = self.UNSAFE, "CUSTOMER_OR_ORDER_SPECIFIC_FACT"
        elif personal and (
            LONG_IDENTIFIER.search(question + "\n" + answer)
            or TRACKING_NUMBER.search(question + "\n" + answer)
            or PHONE.search(question + "\n" + answer)
            or ADDRESS.search(question + "\n" + answer)
            or name_present
        ):
            disposition, primary = self.UNSAFE, "CUSTOMER_IDENTIFYING_INFORMATION"
        elif runtime.status == "QUESTION_ANSWER_MISMATCH":
            disposition, primary = self.UNSAFE, "QUESTION_ANSWER_MISMATCH"
        elif runtime.status == "ORDER_SPECIFIC":
            disposition, primary = self.UNSAFE, "ORDER_SPECIFIC_FACT"
        elif runtime.status == "POLICY_RISK":
            disposition, primary = self.UNSAFE, "UNSAFE_REUSE"
        elif (
            runtime.status == "TEMPORARY_OR_EXPIRED"
            or question_temporary
            or answer_temporary
        ) and (DATE_RANGE.search(answer) or question_has_temporary_date):
            disposition, primary = self.UNSAFE, "EXPIRED_OR_DATED_TEMPORARY"
        elif (
            runtime.status == "TEMPORARY_OR_EXPIRED"
            or question_temporary
            or answer_temporary
        ):
            disposition, primary = self.REVIEW, "UNBOUNDED_TEMPORARY_EVENT"
        elif INTERNAL_OPERATION.search(body):
            disposition, primary = self.REVIEW, "INTERNAL_OPERATION_TEXT"
        elif runtime.status in {"LOW_RELEVANCE", "REVIEW_REQUIRED"}:
            disposition, primary = self.REVIEW, runtime.status
        elif product_uncertain:
            disposition, primary = self.REVIEW, "PRODUCT_SCOPE_UNCERTAIN"
        elif runtime.status == "SAFE_REUSABLE" and runtime.context_eligible:
            disposition, primary = self.SAFE, "ALIGNED_STABLE_REUSABLE_CONTENT"
        else:
            disposition, primary = self.REVIEW, "UNKNOWN_LEGACY_REASON"

        ordered_secondary = tuple(dict.fromkeys(
            reason for reason in secondary if reason != primary
        ))
        return HistoricalReauditDecision(
            disposition=disposition,
            primary_reason=primary,
            secondary_reasons=ordered_secondary,
            runtime_status=runtime.status,
            runtime_context_eligible=runtime.context_eligible,
            alignment_score=runtime.alignment_score,
            trust_score=runtime.trust_score,
            temporary=runtime.temporary,
            order_specific=runtime.order_specific,
            product_scope_uncertain=product_uncertain,
            personal_information_detected=personal,
            masked_question=masked_question,
            masked_answer=masked_answer,
            product_metadata=product,
        )
