from __future__ import annotations

import re

from dps.dates import parse_date_value
from answer.facts import AnswerFacts
from answer.fact_selection import SelectedFacts
from answer.hybrid_models import (
    DraftResult,
    IntentResult,
    SelfReviewResult,
    ValidationResult,
    ValidationRuleResult,
)
from answer.inquiry_analysis import AnswerStrategy, InquiryAnalysis
from answer.text_utils import PHONE_PATTERN
from services.learning_compatibility_service import LearningCompatibilityService


SECRET_PATTERN = re.compile(
    r"(?i)\b(api[_ -]?key|token|cookie|session|otp|password|secret)\b"
)
SPECULATION_PATTERN = re.compile(
    r"(아마|추측(?:하면|상)|것 같습니다|일 듯|예상해 봅니다)"
)
EMAIL_ANY_PATTERN = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
)
ISO_DATE_PATTERN = re.compile(
    r"(?<!\d)20\d{2}-\d{2}-\d{2}(?!\d)"
)
CUSTOMER_DATE_PATTERN = re.compile(
    r"(?<!\d)(20\d{2})\s*(?:년|[-./])\s*(\d{1,2})\s*"
    r"(?:월|[-./])\s*(\d{1,2})\s*(?:일)?(?!\d)"
)
UNAVAILABLE_DATE_PHRASES = (
    "구체적인 배송·설치 예정일을 확인할 수 없습니다",
    "설치 예정일을 확인할 수 없습니다",
    "설치예정일을 확인할 수 없습니다",
    "예정일이 확인되지 않습니다",
    "일정 확인이 어렵습니다",
)
INTERNAL_CUSTOMER_TERMS = (
    "DPS",
    "요구납기일",
    "AnswerFacts",
    "Rule Engine",
    "GPT",
    "OpenAI",
    "API",
    "DB",
    "내부 조회 시스템",
)
FORBIDDEN_CLAIMS = (
    "배송 완료되었습니다",
    "설치 완료되었습니다",
    "반품 가능합니다",
    "기사님이 방문합니다",
)


class AnswerValidator:
    def validate_route(
        self,
        answer: str,
        *,
        route: str,
        installation_date: str | None = None,
        installation_time: str | None = None,
        product_name: str = "",
        existing: ValidationResult | None = None,
    ) -> ValidationResult:
        """Single validator entry point selected only by the final route."""

        normalized_route = str(route or "").upper()
        if existing is not None:
            result = existing
        elif normalized_route == "TEMPLATE":
            result = self.validate_template_text(answer)
        elif normalized_route == "ORDER_ID_REQUEST":
            result = self.validate_order_id_request(answer)
        elif normalized_route == "PRODUCT_DB":
            result = self.validate_product_db_text(
                answer, product_name=product_name
            )
        elif normalized_route in {
            "ORDER_LOOKUP_FAILED",
            "DELIVERY_ORDER_NOT_FOUND",
            "DPS_LOOKUP_FAILED",
            "DELIVERY_DATE_UNCONFIRMED",
            "DELIVERY_WITH_INSTALLATION_DATE",
            "DELIVERY_DATE_INVALID",
            "DELIVERY_LOOKUP_REQUIRED",
        }:
            result = self.validate_delivery_route(
                answer,
                route=normalized_route,
                installation_date=installation_date,
                installation_time=installation_time,
            )
        elif normalized_route in {
            "GPT_FALLBACK",
            "GPT_DIRECT",
            "SAFE_RULE",
            "REVIEW_REQUIRED_SAFE_DRAFT",
        }:
            result = self.validate_template_text(answer)
        else:
            return ValidationResult(
                passed=False,
                errors=(f"지원되지 않는 답변 route입니다: {normalized_route}",),
                warnings=(),
                checked_facts=(),
                status="FAILED_SYSTEM_ERROR",
                rules=(),
            )

        if not result.passed:
            mapped_status = "FAILED_INVALID_CONTENT"
        elif normalized_route in {
            "ORDER_LOOKUP_FAILED",
            "DELIVERY_ORDER_NOT_FOUND",
            "DPS_LOOKUP_FAILED",
            "DELIVERY_DATE_UNCONFIRMED",
            "DELIVERY_LOOKUP_REQUIRED",
            "REVIEW_REQUIRED_SAFE_DRAFT",
        }:
            mapped_status = "PASS_REVIEW_REQUIRED"
        else:
            mapped_status = "PASS"
        return ValidationResult(
            passed=result.passed,
            errors=result.errors,
            warnings=result.warnings,
            checked_facts=result.checked_facts,
            status=mapped_status,
            rules=result.rules,
        )

    def validate_template_text(self, answer: str) -> ValidationResult:
        """Validate a rendered operational template before it can be saved."""

        text = str(answer or "").strip()
        rules: list[ValidationRuleResult] = []

        def add(code: str, passed: bool, message: str) -> None:
            rules.append(
                ValidationRuleResult(
                    code,
                    "PASS" if passed else "BLOCK",
                    message,
                )
            )

        unresolved = bool(
            re.search(
                r"\{\{[^{}]+\}\}|\$\{[^{}]+\}|(?<!\{)\{[A-Za-z_][^{}]*\}(?!\})",
                text,
            )
        )
        system_message = any(
            phrase in text
            for phrase in (
                "답변을 생성할 수 없습니다",
                "답변 생성에 실패",
                "내부 오류",
                "SYSTEM_ERROR",
            )
        )
        sensitive = bool(
            PHONE_PATTERN.search(text)
            or EMAIL_ANY_PATTERN.search(text)
            or re.search(r"(?<!\d)\d{12,24}(?!\d)", text)
        )
        add("TEMPLATE_NON_EMPTY", bool(text), "템플릿 답변이 비어 있습니다.")
        add(
            "TEMPLATE_PLACEHOLDERS_RESOLVED",
            not unresolved,
            "치환되지 않은 템플릿 Placeholder가 있습니다.",
        )
        add(
            "TEMPLATE_SYSTEM_MESSAGE_BLOCK",
            not system_message,
            "내부 오류 또는 생성 실패 문구는 답변으로 사용할 수 없습니다.",
        )
        add(
            "TEMPLATE_SECRET_BLOCK",
            not bool(SECRET_PATTERN.search(text)),
            "인증정보 형태의 문구가 포함되어 있습니다.",
        )
        add(
            "TEMPLATE_PERSONAL_DATA_BLOCK",
            not sensitive,
            "불필요한 개인정보 형태의 값이 포함되어 있습니다.",
        )
        errors = tuple(
            rule.message for rule in rules if rule.status == "BLOCK"
        )
        return ValidationResult(
            passed=not errors,
            errors=errors,
            warnings=(),
            checked_facts=(),
            status="PASS" if not errors else "BLOCK",
            rules=tuple(rules),
        )

    def validate_product_db_text(
        self,
        answer: str,
        *,
        product_name: str,
    ) -> ValidationResult:
        """Validate a Product DB answer independently from templates/GPT."""

        base = self.validate_template_text(answer)
        rules = list(base.rules)

        def add(code: str, passed: bool, message: str) -> None:
            rules.append(
                ValidationRuleResult(
                    code,
                    "PASS" if passed else "BLOCK",
                    message,
                )
            )

        text = str(answer or "").strip()
        internal_states = re.search(
            r"(?i)(?:\bCONFLICT\b|\bMISSING\b|needs_review|model_mismatch)",
            text,
        )
        add(
            "PRODUCT_DB_PRODUCT_CONTEXT",
            bool(str(product_name or "").strip()),
            "Product DB 답변에 사용할 상품 컨텍스트가 존재합니다.",
        )
        add(
            "PRODUCT_DB_INTERNAL_STATE_BLOCK",
            not bool(internal_states),
            "Product DB 내부 상태값을 고객 답변에 노출하지 않았습니다.",
        )
        add(
            "PRODUCT_DB_SPECULATION_BLOCK",
            not bool(SPECULATION_PATTERN.search(text)),
            "Product DB에 없는 사양을 추측하지 않았습니다.",
        )
        errors = tuple(
            rule.message for rule in rules if rule.status == "BLOCK"
        )
        return ValidationResult(
            passed=not errors,
            errors=errors,
            warnings=(),
            checked_facts=("product.name",),
            status="PASS" if not errors else "BLOCK",
            rules=tuple(rules),
        )

    def validate_order_id_request(self, answer: str) -> ValidationResult:
        """Validate the deterministic customer order-id request route."""

        text = str(answer or "").strip()
        rules: list[ValidationRuleResult] = []

        def add(code: str, passed: bool, message: str) -> None:
            rules.append(
                ValidationRuleResult(
                    code,
                    "PASS" if passed else "BLOCK",
                    message,
                )
            )

        add(
            "ORDER_ID_REQUEST_NON_EMPTY",
            bool(text),
            "주문번호 요청 답변 본문이 비어 있지 않습니다.",
        )
        add(
            "GENERAL_ORDER_ID_REQUIRED",
            "일반 주문번호" in text,
            "일정 조회에 필요한 일반 주문번호를 안내했습니다.",
        )
        add(
            "NAVER_PURCHASE_HISTORY_GUIDANCE",
            "구매내역" in text and "네이버" in text,
            "네이버 구매내역 확인 경로를 안내했습니다.",
        )
        add(
            "PRODUCT_ORDER_ID_DISTINCTION",
            "상품주문번호" in text
            and (
                "상품주문번호가 아닌 일반 주문번호" in text
                or "상품주문번호가 아닌" in text
            ),
            "상품주문번호와 일반 주문번호를 구분했습니다.",
        )
        add(
            "LOOKUP_AFTER_CONFIRMATION",
            "주문번호" in text
            and "확인" in text
            and ("조회" in text or "안내" in text),
            "주문번호 제공 후 일정 조회 가능함을 안내했습니다.",
        )
        invented_schedule = bool(
            CUSTOMER_DATE_PATTERN.search(text)
            or ISO_DATE_PATTERN.search(text)
            or re.search(
                r"(?<!\d)(?:오전|오후)?\s*(?:[01]?\d|2[0-3])"
                r"(?::[0-5]\d|\s*시)(?!\d)",
                text,
            )
        )
        add(
            "NO_INVENTED_SCHEDULE",
            not invented_schedule,
            "검증되지 않은 배송 날짜나 시간을 포함하지 않았습니다.",
        )
        sensitive = bool(
            PHONE_PATTERN.search(text)
            or EMAIL_ANY_PATTERN.search(text)
            or re.search(r"(?<!\d)\d{12,24}(?!\d)", text)
        )
        add(
            "PERSONAL_DATA_EXPOSURE",
            not sensitive,
            "개인정보와 전체 주문번호를 노출하지 않았습니다.",
        )
        add(
            "SECRET_EXPOSURE",
            not bool(SECRET_PATTERN.search(text)),
            "인증정보 형태의 문구를 노출하지 않았습니다.",
        )
        errors = tuple(
            rule.message for rule in rules if rule.status == "BLOCK"
        )
        return ValidationResult(
            passed=not errors,
            errors=errors,
            warnings=(),
            checked_facts=(),
            status="PASS" if not errors else "BLOCK",
            rules=tuple(rules),
        )

    def validate_delivery_route(
        self,
        answer: str,
        *,
        route: str,
        installation_date: str | None = None,
        installation_time: str | None = None,
    ) -> ValidationResult:
        """Validate deterministic delivery answers independently from GPT."""

        text = str(answer or "").strip()
        rules: list[ValidationRuleResult] = []

        def add(code: str, passed: bool, message: str) -> None:
            rules.append(
                ValidationRuleResult(
                    code,
                    "PASS" if passed else "BLOCK",
                    message,
                )
            )

        add("DELIVERY_ANSWER_NON_EMPTY", bool(text), "배송 답변이 비어 있지 않습니다.")
        answer_dates = set(ISO_DATE_PATTERN.findall(text))
        for match in CUSTOMER_DATE_PATTERN.finditer(text):
            parsed = parse_date_value("-".join(match.groups()))
            if parsed is not None:
                answer_dates.add(parsed.isoformat())
        expected_date = parse_date_value(installation_date)
        expected = expected_date.isoformat() if expected_date else None
        if route == "DELIVERY_WITH_INSTALLATION_DATE":
            add(
                "DELIVERY_DATE_REQUIRED",
                bool(expected and answer_dates == {expected}),
                "검증된 설치예정일과 답변 날짜가 일치합니다.",
            )
        else:
            add(
                "UNVERIFIED_DELIVERY_DATE_BLOCK",
                not answer_dates,
                "확인되지 않은 배송·설치 날짜를 포함하지 않았습니다.",
            )
        clock_pattern = re.compile(
            r"(?<!\d)(?:오전|오후)?\s*(?:[01]?\d|2[0-3])"
            r"(?::[0-5]\d|\s*시(?:\s*[0-5]?\d\s*분)?)(?!\d)"
        )
        answer_times = {match.group(0).strip() for match in clock_pattern.finditer(text)}
        add(
            "DELIVERY_TIME_GROUNDED",
            (
                str(installation_time).strip() in text
                if installation_time
                else not answer_times
            ),
            "확인되지 않은 방문 시간을 포함하지 않았습니다.",
        )
        add(
            "DELIVERY_PERSONAL_DATA_BLOCK",
            not bool(PHONE_PATTERN.search(text) or EMAIL_ANY_PATTERN.search(text)),
            "개인정보 형태의 값을 포함하지 않았습니다.",
        )
        add(
            "DELIVERY_SECRET_BLOCK",
            not bool(SECRET_PATTERN.search(text)),
            "인증정보 형태의 문구를 포함하지 않았습니다.",
        )
        leaked = [
            term
            for term in ("DPS", "요구납기일", "AnswerFacts", "Rule Engine", "OpenAI")
            if term in text
        ]
        add(
            "DELIVERY_INTERNAL_TERM_BLOCK",
            not leaked,
            "고객 답변에 내부 시스템 용어를 포함하지 않았습니다.",
        )
        errors = tuple(rule.message for rule in rules if rule.status == "BLOCK")
        return ValidationResult(
            passed=not errors,
            errors=errors,
            warnings=(),
            checked_facts=(("installation.date",) if expected else ()),
            status="PASS" if not errors else "BLOCK",
            rules=tuple(rules),
        )

    def validate(
        self,
        facts: AnswerFacts,
        intent: IntentResult,
        draft: DraftResult,
        review: SelfReviewResult,
        *,
        analysis: InquiryAnalysis | None = None,
        selected_facts: SelectedFacts | None = None,
    ) -> ValidationResult:
        errors: list[str] = []
        warnings: list[str] = list(draft.warnings) + list(review.warnings)
        checked: list[str] = []
        if not draft.answer.strip():
            errors.append("GPT 답변이 비어 있습니다.")
        for path in draft.used_facts:
            value = facts.get_fact(path)
            if value in (None, "", [], {}, ()):
                errors.append(f"존재하지 않는 Fact를 사용했습니다: {path}")
            else:
                checked.append(path)
        if PHONE_PATTERN.search(draft.answer) or EMAIL_ANY_PATTERN.search(
            draft.answer
        ):
            errors.append("답변에 개인정보 형태의 값이 포함되어 있습니다.")
        if SECRET_PATTERN.search(draft.answer):
            errors.append("답변에 인증정보 형태의 문구가 포함되어 있습니다.")
        if SPECULATION_PATTERN.search(draft.answer):
            errors.append("답변에 추측 표현이 포함되어 있습니다.")
        known_dates = {
            str(value)
            for value in (
                facts.installation.get("date"),
                facts.order.get("shipping_due_date"),
            )
            if value
        }
        answer_dates = {
            parsed.isoformat()
            for match in CUSTOMER_DATE_PATTERN.finditer(draft.answer)
            if (
                parsed := parse_date_value("-".join(match.groups()))
            )
            is not None
        }
        answer_dates.update(ISO_DATE_PATTERN.findall(draft.answer))
        for date_text in answer_dates:
            if date_text not in known_dates:
                errors.append(
                    f"Facts에 없는 날짜가 포함되어 있습니다: {date_text}"
                )
        confirmed_installation_date = facts.installation.get("date")
        installation_confirmed = bool(
            facts.installation.get("installation_date_confirmed")
            and confirmed_installation_date
        )
        schedule_text = " ".join(
            (
                str(intent.category or ""),
                *[str(value) for value in intent.questions],
            )
        )
        schedule_question = any(
            token in schedule_text
            for token in ("배송", "설치", "일정", "예정일")
        )
        if installation_confirmed:
            if any(
                phrase in draft.answer
                for phrase in UNAVAILABLE_DATE_PHRASES
            ):
                errors.append(
                    "확정된 설치예정일이 있는데 확인할 수 없다고 답했습니다."
                )
            if schedule_question and str(
                confirmed_installation_date
            ) not in answer_dates:
                errors.append(
                    "일정 문의 답변에 확정된 설치예정일이 누락되었습니다."
                )
            if answer_dates - {str(confirmed_installation_date)}:
                errors.append(
                    "답변 날짜가 현재 문의의 설치예정일과 다릅니다."
                )
        for term in INTERNAL_CUSTOMER_TERMS:
            if re.search(
                rf"(?i)(?<![A-Za-z]){re.escape(term)}(?![A-Za-z])",
                draft.answer,
            ):
                errors.append(
                    f"고객 답변에 내부 시스템 용어가 포함되었습니다: {term}"
                )
        if facts.policy.get("requires_review") and not (
            draft.requires_review or intent.requires_review
        ):
            errors.append("Rule 정책의 직원 검토 요구를 해제했습니다.")
        if not facts.delivery.get("status") and not facts.installation.get(
            "status"
        ):
            for claim in FORBIDDEN_CLAIMS:
                if claim in draft.answer:
                    errors.append(f"근거 없는 확정 문장입니다: {claim}")
        if not review.passed:
            errors.append("GPT 자체 검토를 통과하지 못했습니다.")
        if review.has_speculation or not review.facts_consistent:
            errors.append("GPT 자체 검토에서 사실 불일치를 확인했습니다.")
        if not review.answered_all_questions and intent.questions:
            warnings.append("복합 질문 일부의 답변 누락 가능성이 있습니다.")
        phase9_rules = self._phase9_rules(
            facts,
            draft.answer,
            analysis=analysis,
            selected_facts=selected_facts,
        )
        questions = tuple(
            str(value).strip()
            for value in intent.questions
            if str(value).strip()
        ) or (str(facts.inquiry.get("question") or ""),)
        relevance = LearningCompatibilityService().answer_relevance(
            questions=questions,
            answer=draft.answer,
        )
        relevance_rule = ValidationRuleResult(
            "ANSWER_TOPIC_RELEVANCE",
            relevance.status,
            relevance.reason,
        )
        coverage_rules: tuple[ValidationRuleResult, ...] = ()
        if len(questions) > 1 and draft.subquestion_results:
            answered = {
                str(item.get("subquestion") or "").strip()
                for item in draft.subquestion_results
                if item.get("answered") is True
            }
            missing = [question for question in questions if question not in answered]
            coverage_rules = (
                ValidationRuleResult(
                    "SUBQUESTION_EVIDENCE_COVERAGE",
                    "REVIEW_REQUIRED" if missing else "PASS",
                    (
                        "COMPOUND_QUESTION_PARTIAL_COVERAGE"
                        if missing else "ALL_SUBQUESTIONS_ANSWERED"
                    ),
                ),
            )
        validation_rules = (*phase9_rules, relevance_rule, *coverage_rules)
        errors.extend(
            item.message for item in validation_rules if item.status == "BLOCK"
        )
        warnings.extend(
            item.message
            for item in validation_rules
            if item.status == "REVIEW_REQUIRED"
        )
        status = (
            "BLOCK"
            if errors
            else "REVIEW_REQUIRED"
            if warnings
            else "PASS"
        )
        return ValidationResult(
            passed=status != "BLOCK",
            errors=tuple(dict.fromkeys(errors)),
            warnings=tuple(dict.fromkeys(warnings)),
            checked_facts=tuple(dict.fromkeys(checked)),
            status=status,
            rules=validation_rules,
        )

    @staticmethod
    def _phase9_rules(
        facts: AnswerFacts,
        answer: str,
        *,
        analysis: InquiryAnalysis | None,
        selected_facts: SelectedFacts | None,
    ) -> tuple[ValidationRuleResult, ...]:
        if analysis is None:
            return ()

        rules: list[ValidationRuleResult] = []

        def add(code: str, status: str, message: str) -> None:
            rules.append(ValidationRuleResult(code, status, message))

        allowed = selected_facts.values if selected_facts else {}
        used_paths = set(selected_facts.keys if selected_facts else ())
        add(
            "FACT_GROUNDING",
            "PASS",
            f"선택된 사실 {len(used_paths)}개를 기준으로 검증했습니다.",
        )
        confirmed = str(allowed.get("installation.date") or "")
        answer_dates = set(ISO_DATE_PATTERN.findall(answer))
        for match in re.finditer(
            r"(?<!\d)(20\d{2})년\s*(\d{1,2})월\s*(\d{1,2})일",
            answer,
        ):
            answer_dates.add(
                f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-"
                f"{int(match.group(3)):02d}"
            )
        if confirmed and answer_dates and answer_dates != {confirmed}:
            add(
                "DATE_GROUNDING",
                "BLOCK",
                "답변 날짜가 현재 문의의 확정 설치일과 일치하지 않습니다.",
            )
        else:
            add("DATE_GROUNDING", "PASS", "답변 날짜가 허용된 사실과 일치합니다.")
        if not confirmed and answer_dates:
            add(
                "UNVERIFIED_DATE_BLOCK",
                "BLOCK",
                "확인되지 않은 날짜를 답변에 포함했습니다.",
            )
        else:
            add(
                "UNVERIFIED_DATE_BLOCK",
                "PASS",
                "확인되지 않은 날짜를 생성하지 않았습니다.",
            )

        requesting = (
            analysis.answer_strategy is AnswerStrategy.REQUEST_ORDER_ID
        )
        has_order_request = "주문번호" in answer
        has_private = "비밀글" in answer or "비공개" in answer
        has_order_history = "주문내역" in answer or "구매내역" in answer
        has_followup = "확인 후" in answer and "안내" in answer
        add(
            "ORDER_ID_REQUEST_REQUIRED",
            "PASS" if not requesting or has_order_request else "BLOCK",
            (
                "주문번호 요청을 확인했습니다."
                if not requesting or has_order_request
                else "주문번호가 필요한 답변에 주문번호 요청이 없습니다."
            ),
        )
        add(
            "PRIVATE_POST_GUIDANCE_REQUIRED",
            "PASS" if not requesting or has_private else "BLOCK",
            (
                "비밀글 안내를 확인했습니다."
                if not requesting or has_private
                else "주문번호 요청 답변에 비밀글 안내가 없습니다."
            ),
        )
        wrong_order_type = (
            requesting
            and "상품주문번호" in answer
            and "상품주문번호가 아닌" not in answer
        )
        add(
            "ORDER_ID_TYPE_CORRECTNESS",
            "BLOCK" if wrong_order_type else "PASS",
            (
                "상품주문번호를 일반 주문번호로 잘못 요청했습니다."
                if wrong_order_type
                else "네이버 일반 주문번호 기준을 유지했습니다."
            ),
        )
        internal_terms = (
            "DPS",
            "요구납기일",
            "AnswerFacts",
            "Rule Engine",
            "OpenAI",
            "구매요청 상태",
        )
        leaked = [term for term in internal_terms if term in answer]
        add(
            "INTERNAL_STATUS_LEAK",
            "BLOCK" if leaked else "PASS",
            (
                "고객 답변에 내부 용어가 포함됐습니다."
                if leaked
                else "고객 답변에 내부 상태를 노출하지 않았습니다."
            ),
        )
        current_date = str(facts.installation.get("date") or "")
        contamination = bool(
            answer_dates
            and current_date
            and answer_dates != {current_date}
        )
        add(
            "CROSS_INQUIRY_CONTAMINATION",
            "BLOCK" if contamination else "PASS",
            (
                "다른 문의의 날짜가 혼입됐습니다."
                if contamination
                else "현재 문의의 사실 범위만 사용했습니다."
            ),
        )
        exposes_order = bool(re.search(r"(?<!\d)\d{16,}(?!\d)", answer))
        add(
            "PERSONAL_DATA_EXPOSURE",
            "BLOCK" if exposes_order else "PASS",
            (
                "답변에 전체 주문번호 또는 장문 식별자가 노출됐습니다."
                if exposes_order
                else "불필요한 개인정보 노출이 없습니다."
            ),
        )
        relevant = bool(answer.strip()) and (
            not requesting
            or (has_order_request and has_order_history and has_followup)
        )
        add(
            "ANSWER_RELEVANCE",
            "PASS" if relevant else "REVIEW_REQUIRED",
            (
                "문의 목적에 직접 답변했습니다."
                if relevant
                else "문의 목적과 답변의 직접 관련성을 확인해야 합니다."
            ),
        )
        required_ok = not requesting or (
            has_order_request
            and has_order_history
            and has_followup
            and "일반 주문번호" in answer
            and "상품주문번호가 아닌" in answer
        )
        add(
            "REQUIRED_CONTENT",
            "PASS" if required_ok else "BLOCK",
            (
                "전략별 필수 내용을 포함했습니다."
                if required_ok
                else "답변 전략의 필수 안내가 누락됐습니다."
            ),
        )
        prohibited = requesting and bool(answer_dates)
        add(
            "PROHIBITED_CONTENT",
            "BLOCK" if prohibited else "PASS",
            (
                "주문번호 요청 답변에 금지된 날짜가 포함됐습니다."
                if prohibited
                else "전략별 금지 내용을 포함하지 않았습니다."
            ),
        )
        low_quality = len(answer.strip()) < 5
        add(
            "EMPTY_OR_LOW_QUALITY",
            "BLOCK" if not answer.strip() else (
                "REVIEW_REQUIRED" if low_quality else "PASS"
            ),
            (
                "답변이 비어 있습니다."
                if not answer.strip()
                else "답변이 지나치게 짧습니다."
                if low_quality
                else "답변 길이와 내용이 유효합니다."
            ),
        )
        add(
            "FINAL_ANSWER_PROTECTION",
            "PASS",
            "최종 답변 보호는 draft repository transaction에서 적용됩니다.",
        )
        return tuple(rules)
