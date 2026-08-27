from __future__ import annotations

import re
from dataclasses import dataclass

from .answer_format import combine_answer_bodies, format_auto_answer, korean_date
from .config_loader import (
    VALIDITY_ACTIVE,
    AnswerConfig,
    active_install_schedule_rules,
    install_schedule_status,
    load_answer_config,
)
from .exceptions import AnswerGenerationError
from .models import (
    AnswerRequest,
    AnswerResult as PublicAnswerResult,
    AnswerStatus,
)
from .providers.base import AnswerProvider
from .providers.rule_provider import RuleProvider
from .text_utils import (
    CURRENT_DELIVERY_SCHEDULE_QUERY,
    CURRENT_INSTALLATION_SCHEDULE_QUERY,
    NOTICE_SUBJECT_QUERY,
    compact,
    is_delivery_deadline_question,
    is_delivery_notice_question,
    is_general_delivery_policy_question,
    is_package_contents_question,
    is_seller_identity_question,
    is_weekend_delivery_policy_question,
    estimate_question_count,
    find_any,
    has_any,
    has_date_or_order_hint,
    normalize_space,
)


# A body that already opens with a figure ("1~2주", "3영업일") or with an
# immediacy statement has answered the timing question itself.
_DIRECT_TIMING_ANSWER = re.compile(
    r"\d+\s*(?:~|-|에서)?\s*\d*\s*(?:영업일|일|주|주일|시간|개월)"
    r"|바로\s*배송|즉시|당일\s*(?:발송|배송|출고)"
)


@dataclass
class AnswerResult:
    status: str
    answer: str
    reason: str
    category: str
    question_count: int = 0
    question_breakdown: str = ""
    provider: str = "rules"
    # Which matcher produced this result. FIXED_* kinds are deterministic
    # policy/catalog answers whose wording is meant to be exact; KEYWORD_*
    # kinds only matched on substring keywords and must not decide the final
    # customer answer on their own (see answer_service._template_may_answer).
    match_kind: str = "UNKNOWN"


class AnswerEngine:
    def __init__(
        self,
        config: AnswerConfig | None = None,
        provider: AnswerProvider | None = None,
    ):
        self.config = config or load_answer_config()
        self.provider = provider or RuleProvider()
        self.model_catalog = self._load_model_catalog()
        self.learned_rules = list(self.config.learned_rules)
        self.install_schedule_rules = list(
            self.config.install_schedule_rules
        )

    def generate(self, request: AnswerRequest) -> PublicAnswerResult:
        if not isinstance(request, AnswerRequest):
            raise TypeError("request must be an AnswerRequest")
        result = self.provider.generate(request, self._generate_with_rules)
        if not isinstance(result, PublicAnswerResult):
            raise AnswerGenerationError(
                "답변 provider가 유효한 AnswerResult를 반환하지 않았습니다."
            )
        if (
            result.status is AnswerStatus.GENERATED
            and not result.answer.strip()
        ):
            raise AnswerGenerationError(
                "답변 가능 결과에 답변 본문이 없습니다."
            )
        return self._apply_dps_context(request, result)

    def _apply_dps_context(
        self,
        request: AnswerRequest,
        result: PublicAnswerResult,
    ) -> PublicAnswerResult:
        dps = request.metadata.get("dps")
        if not isinstance(dps, dict) or not dps.get("lookup_required"):
            return result

        status = str(dps.get("lookup_status") or "")
        change_request = bool(dps.get("change_request"))
        general_segments = [
            str(segment)
            for segment in dps.get("general_segments", [])
            if str(segment).strip()
        ]
        general_answers: list[str] = []
        subquestions: list[dict[str, str]] = []
        for segment in general_segments:
            source = self.answer(
                request.product_name, segment, request.option_name
            )
            subquestions.append(
                {
                    "question": segment,
                    "kind": "GENERAL",
                    "status": source.status,
                    "category": source.category,
                }
            )
            if source.answer.strip():
                general_answers.append(source.answer)
        for segment in dps.get("dps_segments", []):
            subquestions.append(
                {
                    "question": str(segment),
                    "kind": "DPS",
                    "status": status,
                    "category": "배송/설치현황",
                }
            )

        delivery_status = str(dps.get("delivery_status") or "").strip()
        installation_status = str(
            dps.get("installation_status") or ""
        ).strip()
        installation_date = str(
            dps.get("installation_date") or ""
        ).strip()
        warnings = list(result.warnings)
        if status == "SUCCESS":
            parts: list[str] = []
            current_status = delivery_status or installation_status
            if current_status:
                parts.append(
                    f"고객님의 주문은 현재 {current_status} 상태로 확인됩니다."
                )
            if installation_date:
                parts.append(
                    "DPS 기준 설치 예정일은 "
                    f"{korean_date(installation_date)}로 확인됩니다."
                )
            if change_request:
                parts.append(
                    "설치일·배송일 변경 요청은 담당자 확인이 필요하며, "
                    "현재 조회된 일정만으로 변경 완료를 안내드릴 수 없습니다."
                )
            dps_body = " ".join(parts)
            if not dps_body:
                warnings.append(
                    "DPS 조회는 성공했지만 답변에 사용할 배송·설치 정보가 없습니다."
                )
        else:
            dps_body = str(dps.get("error_message") or "").strip()
            if not dps_body:
                dps_body = "배송·설치 일정은 담당자 확인이 필요합니다."

        answer = combine_answer_bodies(*general_answers, dps_body)
        has_confirmed_data = bool(
            status == "SUCCESS"
            and (delivery_status or installation_status or installation_date)
        )
        needs_review = (
            change_request
            or status != "SUCCESS"
            or not has_confirmed_data
            or bool(dps.get("warnings"))
        )
        metadata = dict(result.metadata)
        metadata.update(
            {
                "dps": dict(dps),
                "subquestions": subquestions,
                "base_rule_status": result.status.value,
                "base_rule_category": result.category,
            }
        )
        return PublicAnswerResult(
            status=(
                AnswerStatus.NEEDS_REVIEW
                if needs_review
                else AnswerStatus.GENERATED
            ),
            category=(
                "배송/설치일변경요청"
                if change_request
                else "배송/설치현황"
            ),
            reason=(
                "DPS 결과와 요청 내용을 확인한 직원 검토가 필요합니다."
                if needs_review
                else "DPS의 배송·설치 현황을 답변에 반영했습니다."
            ),
            answer=answer,
            provider=result.provider,
            auto_answerable=not needs_review,
            needs_review=needs_review,
            matched_rule="DPS_ENRICHED",
            warnings=tuple(warnings)
            + tuple(str(item) for item in dps.get("warnings", [])),
            metadata=metadata,
        )

    def _generate_with_rules(
        self,
        request: AnswerRequest,
    ) -> PublicAnswerResult:
        source_result = self.answer(
            request.product_name,
            request.question,
            request.option_name,
        )
        warnings: list[str] = []
        if not request.question.strip():
            warnings.append("문의 내용이 비어 있습니다.")
        if not request.product_name.strip():
            warnings.append("상품명이 없어 적용 가능한 규칙이 제한됩니다.")

        if source_result.status == "답변 가능":
            if not source_result.answer.strip():
                raise AnswerGenerationError(
                    "답변 가능 결과에 답변 본문이 없습니다."
                )
            status = AnswerStatus.GENERATED
            auto_answerable = True
            needs_review = False
        elif source_result.status == "추가정보 필요":
            status = AnswerStatus.NEEDS_REVIEW
            auto_answerable = False
            needs_review = True
        elif source_result.category == "기타/직원확인":
            status = AnswerStatus.NOT_SUPPORTED
            auto_answerable = False
            needs_review = True
        else:
            status = AnswerStatus.NEEDS_REVIEW
            auto_answerable = False
            needs_review = True

        return PublicAnswerResult(
            status=status,
            category=source_result.category,
            reason=source_result.reason,
            answer=source_result.answer,
            provider=self.provider.name,
            auto_answerable=auto_answerable,
            needs_review=needs_review,
            matched_rule=source_result.category,
            warnings=tuple(warnings),
            metadata={
                "source_status": source_result.status,
                "question_count": source_result.question_count,
                "question_breakdown": source_result.question_breakdown,
                "existing_template": (
                    "configuration.xlsx" in source_result.reason
                ),
                "template_match_kind": source_result.match_kind,
            },
        )

    def answer(self, product: str, question: str, option_name: str = "") -> AnswerResult:
        product = normalize_space(product)
        question = normalize_space(question)
        option_name = normalize_space(option_name)
        text = f"{product} {question} {option_name}"
        ctext = compact(text)
        cq = compact(question)

        blocked = self._hard_block(product, question)
        if blocked:
            return self._finalize(
                product, question, blocked, allow_gpt=False,
                match_kind="FIXED_POLICY_HARD_BLOCK",
            )

        learned = self._learned_rule(product, question)
        if learned:
            # Operator-registered rulebook entry, matched by substring
            # keywords only -- suggestive, not a guaranteed exact answer.
            return self._finalize(
                product, question, learned, match_kind="KEYWORD_LEARNED_RULE",
            )

        store_pickup = self._store_pickup(product, question)
        if store_pickup:
            return self._finalize(
                product, question, store_pickup,
                match_kind="FIXED_POLICY_STORE_PICKUP",
            )

        review = self._review_event(product, question)
        if review:
            return self._finalize(
                product, question, review, match_kind="FIXED_EVENT_REVIEW",
            )

        package_code = self._package_code_answer(product, question)
        if package_code:
            return self._finalize(
                product, question, package_code,
                match_kind="FIXED_PACKAGE_CODE",
            )

        onnuri = self._onnuri_or_festival(product, question)
        if onnuri:
            return self._finalize(
                product, question, onnuri, match_kind="FIXED_EVENT_ONNURI",
            )

        shipping = self._shipping(product, question)
        if shipping:
            return self._finalize(
                product, question, shipping, match_kind="FIXED_POLICY_SHIPPING",
            )

        install_common = self._install_common_info(product, question)
        if install_common:
            return self._finalize(
                product, question, install_common,
                match_kind="FIXED_POLICY_INSTALL",
            )

        pickup = self._old_appliance_pickup(product, question)
        if pickup:
            return self._finalize(
                product, question, pickup, match_kind="FIXED_POLICY_PICKUP",
            )

        stand = self._stand_or_battery(product, question)
        if stand:
            return self._finalize(
                product, question, stand, match_kind="FIXED_PRODUCT_ACCESSORY",
            )

        model = self._model_code(product, question)
        if model:
            return self._finalize(
                product, question, model, match_kind="PRODUCT_DB_MODEL_CODE",
            )

        spec = self._model_spec_answer(product, question)
        if spec:
            return self._finalize(
                product, question, spec, match_kind="PRODUCT_DB_MODEL_SPEC",
            )

        simple = self._simple_product_usage(product, question, ctext, cq)
        if simple:
            # Generic "usage" catch-all; never a guaranteed answer to the
            # customer's specific question.
            return self._finalize(
                product, question, simple,
                match_kind="KEYWORD_SIMPLE_PRODUCT_USAGE",
            )

        return self._finalize(
            product,
            question,
            self.no_answer("기타/직원확인", "현재 룰북과 설정만으로는 자동답변 확신이 낮습니다."),
            match_kind="NO_MATCH",
        )

    def yes(self, category: str, body: str, reason: str) -> AnswerResult:
        return AnswerResult("답변 가능", format_auto_answer(body), reason, category)

    def no_answer(self, category: str, reason: str) -> AnswerResult:
        return AnswerResult("답변하지 않음", "", reason, category)

    def need_info(self, category: str, body: str, reason: str) -> AnswerResult:
        return AnswerResult("추가정보 필요", format_auto_answer(body), reason, category)

    def _load_model_catalog(self) -> dict:
        return self.config.model_catalog

    def _learned_rule(self, product: str, question: str) -> AnswerResult | None:
        if not self.learned_rules:
            return None
        product_text = compact(product)
        question_text = compact(question)
        sorted_rules = sorted(self.learned_rules, key=lambda row: int(row.get("우선순위") or 100))
        for rule in sorted_rules:
            product_keywords = split_keywords(rule.get("상품키워드"))
            question_keywords = split_keywords(rule.get("질문키워드"))
            if product_keywords and not all(compact(keyword) in product_text for keyword in product_keywords):
                continue
            if question_keywords and not all(compact(keyword) in question_text for keyword in question_keywords):
                continue
            body = str(rule.get("답변본문") or "").strip()
            if body:
                return self.yes(str(rule.get("카테고리") or "학습답변룰"), body, "configuration.xlsx 학습답변룰 시트에 채택된 답변입니다.")
        return None

    def _finalize(
        self,
        product: str,
        question: str,
        result: AnswerResult,
        allow_gpt: bool = True,
        match_kind: str = "UNKNOWN",
    ) -> AnswerResult:
        count, breakdown = estimate_question_count(question)
        result.question_count = count
        result.question_breakdown = breakdown
        result.provider = "rules"
        result.match_kind = match_kind
        return result

    def _hard_block(self, product: str, question: str) -> AnswerResult | None:
        q = compact(question)
        schedule_change_words = ["변경", "바꾸", "미루", "연기", "지정", "요청", "부탁", "받을수가없", "받을수없"]
        schedule_target_words = ["배송일", "설치일", "설치날짜", "설치일정", "이사날짜", "8.1이후", "8월1일이후", "이후설치"]
        if any(k in q for k in schedule_change_words) and any(k in q for k in schedule_target_words):
            return self.no_answer("배송/설치일변경요청", "배송일/설치일 변경, 지정, 연기 요청은 직원이 실제 주문 업무를 진행해야 하므로 자동답변하지 않습니다.")

        chatbot_complaint_words = ["챗봇말고", "쳇봇말고", "챗봇상담말고", "쳇봇상담말고", "챗봇상담", "쳇봇상담", "챗봇답변", "쳇봇답변", "챗봇", "쳇봇", "답글도안달", "사람이답변", "상담원"]
        if any(k in q for k in chatbot_complaint_words):
            return self.no_answer("고객불만/직원응대", "기존 챗봇 답변에 대한 불만이나 사람 답변 요청이 포함되어 직원이 직접 응대해야 합니다.")

        if "구매내역서" in q:
            return self.yes(
                "증빙/구매내역서확인",
                "구매내역서는 네이버페이 > 결제내역 > 대상제품검색 > 주문 상세정보 캡쳐로 확인 가능합니다.",
                "네이버 구매 고객의 구매내역서는 발급/발송보다 직접 확인 경로 안내가 적절합니다.",
            )

        document_words = ["거래명세서", "거래명새표", "거래명세표", "영수증", "증빙", "구매내역서", "거래내역서"]
        request_words = ["보내주세요", "보내주", "발급", "출력", "캡쳐", "캡처", "메일", "첨부", "요청"]
        if any(k in q for k in document_words) and any(k in q for k in request_words):
            return self.no_answer("증빙/거래명세서요청", "거래명세서/영수증/증빙자료 발급 또는 전달 요청은 직원이 주문 정보를 확인해 처리해야 하므로 자동답변하지 않습니다.")

        structure_keywords = ["돌출", "용도", "목적", "구멍", "홈", "버튼", "브라켓", "꺽쇠", "꺾쇠", "부품", "나사"]
        supply_keywords = ["제공", "동봉", "포함", "따로", "구성품", "들어있", "들어 있", "오나요", "있나요"]
        if any(k in q for k in structure_keywords) and any(k in q for k in supply_keywords):
            return self.no_answer("제품정보/구성품확인", "제품 구조/부품 용도/구성품 제공 여부는 해당 상품의 상세페이지, 설명서, 구성품 DB 등 확정 근거가 있을 때만 답변할 수 있습니다.")

        policy = self.config.answer_policy
        for rule in policy["hard_block_rules"]:
            if any(k in q for k in rule["keywords"]):
                return self.no_answer(rule["category"], rule["reason"])
        return None

    def _store_pickup(self, product: str, question: str) -> AnswerResult | None:
        q = compact(question)
        if not any(k in q for k in ["방문수령", "방문으로", "직접수령", "찾으러", "방문해서", "어디로가"]):
            return None
        if self._is_install_product(product):
            return self.no_answer("방문수령/설치상품", "방문수령 안내는 택배발송 상품에 한해서만 자동답변합니다.")
        if not self._is_parcel_product(product):
            return self.no_answer("방문수령/배송유형확인", "방문수령 안내는 택배발송 상품 여부가 확인되는 경우에만 자동답변합니다.")
        return self.yes(
            "방문수령",
            self.config.shipping["store_pickup_answer"],
            "택배발송 상품 방문수령 주소 안내입니다.",
        )

    def _review_event(self, product: str, question: str) -> AnswerResult | None:
        q = compact(question)
        events = self.config.events["review_event"]
        if any(k in q for k in ["온누리", "감사페스티벌", "감사 페스티벌", "환급"]):
            return None
        if not any(k in q for k in events["keywords"]):
            return None

        if any(k in q for k in events["individual_check_keywords"]):
            return self.no_answer("리뷰이벤트/직원확인", "리뷰 이벤트 개별 접수 상태 확인이 필요합니다.")

        if self._is_moving_style_install_product(product) and any(k in q for k in events["delayed_delivery_keywords"]):
            body = self._with_event_notice(
                events["delayed_delivery_answer"],
                events.get("delayed_delivery_event_notice"),
                events.get("delayed_delivery_event_validity"),
            )
            return self.yes("리뷰이벤트/배송지연", body, "무빙스타일 설치 상품의 리뷰 참여 기간 완화 안내입니다.")

        if any(k in q for k in events["answerable_keywords"]):
            return self.yes("리뷰이벤트", events["general_answer"], "리뷰 이벤트 지급 시점/작성 방법 일반 안내입니다.")

        return None

    def _onnuri_or_festival(self, product: str, question: str) -> AnswerResult | None:
        q = compact(question)
        ev = self.config.events["onnuri"]
        if not any(k in q for k in ev["keywords"]):
            return None

        package_code = self._package_code_answer(product, question)
        if package_code:
            return package_code

        if any(k in q for k in ["기준", "정확하게뭐", "정확히뭐", "설치받은날", "구매는", "구매일", "설치일"]) and any(k in q for k in ["환급", "온누리", "감사페스티벌", "삼성페스타", "페이백"]):
            return self.yes(
                "행사/대상기준",
                "디지털 온누리 상품권 환급 신청 기준은 2026년 6월 8일부터 7월 5일까지 구매한 고객 중, 9월 5일까지 배송 완료된 제품에 한합니다.\n\n신청은 아래 삼성전자 행사 페이지에서 진행해 주세요.\nhttps://www.samsung.com/sec/event/thankyoufestival-benefit/",
                "온누리/감사제 신청 방법이 아니라 대상 기준 문의로 판단했습니다.",
            )

        if any(k in q for k in ["비정품거치대", "비정품", "거치대"]) and any(k in q for k in ["주문번호", "상품주문번호", "수정", "보완"]):
            return self.yes(
                "행사/주문번호보완",
                "삼성페스타 신청 시 TV와 비정품 거치대를 묶어서 신청 가능합니다.\n\n네이버 주문 기준으로는 전체 주문에 부여되는 주문번호와 옵션 주문별로 부여되는 상품주문번호가 각각 조회됩니다. 삼성페스타 신청 시에는 주문번호로 기재해 주세요.\n\n만약 이후에도 계속 보완 요청이 오는 경우, 삼성전자서비스 1588-3366으로 연락하셔서 신청내역을 상담사에게 직접 점검받아보시는 것을 권장드립니다.",
                "삼성페스타 비정품 거치대 포함 신청 및 네이버 주문번호/상품주문번호 구분 안내입니다.",
            )

        if any(k in q for k in ["패키지코드", "패키지코드좀", "반려", "구매처보완", "구매처입력", "정확히구매처"]):
            return self.no_answer("행사/직원확인", "패키지코드 반려/구매처 보완은 행사 신청 상태와 구매 구성을 확인해야 합니다.")

        # "판매처", "판매자", "스토어명" -- the same question, and the guard
        # knew only one of its names. Anything it missed fell through to the
        # generic rebate answer below, which states the application window and
        # an event URL and says nothing about which seller to enter.
        if is_seller_identity_question(question):
            return self.no_answer("행사/직원확인", "감사페스티벌 구매처 입력/보완 문의는 행사 신청 상태 확인이 필요합니다.")

        if any(k in q for k in ["모델코드", "모델명에뭘", "모델코드에뭘", "뭘넣"]):
            return self.yes("행사/모델코드", ev["model_code_label_answer"], "모델코드는 옵션/색상에 따라 달라질 수 있어 라벨 확인만 안내합니다.")

        if any(k in q for k in ["시리얼", "씨리얼", "s/n", "sn번호"]):
            return self.yes("행사/시리얼", ev["serial_answer"], "시리얼번호 위치 안내입니다.")

        if any(k in q for k in ["주문결제", "설치받는", "수취인", "받는분"]):
            return self.yes("행사/신청자", ev["payer_answer"], "주문결제자 기준 신청 안내입니다.")

        if any(k in q for k in ["얼마", "환급금", "금액"]):
            return self.yes("행사/환급금", ev["refund_amount_answer"], "환급 금액은 구매내역 확인으로 안내합니다.")

        if any(k in q for k in ["7월4일", "7/4", "7월5일", "7/5"]):
            return self.yes("행사/신청가능", ev["eligible_order_answer"], "7/5까지 주문건 신청 가능 안내입니다.")

        if any(k in q for k in self.config.shipping["shipping_keywords"]) and self._is_install_product(product):
            body = self._install_existing_order_body(product) + "\n\n" + ev["general_answer"]
            return self.yes("배송/행사복합", body, "비즈니스TV 설치 일정과 온누리 신청 방법 복합 문의입니다.")

        return self.yes("행사/신청방법", ev["general_answer"], "온누리/감사페스티벌 일반 신청 안내입니다.")

    def _package_code_answer(self, product: str, question: str) -> AnswerResult | None:
        q = compact(question)
        p = compact(product)
        if not any(k in q for k in ["패키지코드", "패키지코드좀"]):
            return None
        is_m5_32_white = ("m5" in p or "s32dm501" in p or "ls32dm501" in p) and any(k in p for k in ["32인치", "80cm", "80.1cm", "s32dm501", "ls32dm501"]) and "화이트" in p
        if not is_m5_32_white:
            return self.no_answer("행사/패키지코드확인", "패키지코드는 상품 옵션과 구성에 따라 달라져 확인된 조합 외에는 자동답변하지 않습니다.")
        if any(k in q + p for k in ["삼성정품스탠드", "정품스탠드", "무빙스타일", "ls32dm501e-2wo"]):
            return self.yes(
                "행사/패키지코드",
                "문의하신 M5 32인치 화이트 + 삼성 정품 무빙스타일 스탠드 구성의 패키지 코드는 LS32DM501E-2WO로 안내 가능합니다.",
                "삼성 정품 무빙스타일 스탠드 구성에 한정한 패키지코드 예외 안내입니다.",
            )
        if any(k in q + p for k in ["오베닉", "fms", "유압식", "2in1", "2in1거치대"]):
            return self.yes(
                "행사/패키지코드",
                "문의하신 M5 32인치 화이트 + 오베닉/FMS 스탠드 구성은 모니터 모델코드 LS32DM501EKXKR 기준으로 안내 가능합니다.",
                "오베닉/FMS 구성은 삼성 정품 무빙스타일 스탠드 패키지코드 예외와 구분합니다.",
            )
        return self.no_answer("행사/패키지코드확인", "패키지코드는 스탠드 구성에 따라 달라져 삼성 정품 무빙스타일 스탠드인지 오베닉/FMS 구성인지 확인이 필요합니다.")

    def _shipping(self, product: str, question: str) -> AnswerResult | None:
        q = compact(question)
        if not any(k in q for k in self.config.shipping["shipping_keywords"]):
            return None

        # The keyword gate above is deliberately broad -- "배송", "언제",
        # "받을수", "도착", "설치기사님" all open this block, so that no
        # shipping question is missed. Nothing then checked what the question
        # was *about*, so mentioning delivery in passing was enough to be
        # answered with the delivery policy: "배송 올 때 공구도 같이 오나요?"
        # received "택배배송 상품은 오후 3시 이전 결제 주문에 한해 당일
        # 발송되며..." and that was auto-posted.
        #
        # Here the shipment is the occasion, not the subject. Returning None
        # rather than a refusal hands the question back to the rest of the
        # engine and then to the normal evidence pipeline, where a verified
        # product fact or an approved same-product Learning can answer what
        # actually comes in the box.
        if is_package_contents_question(question):
            return None

        if self._is_order_specific_parcel_shipping(product, question):
            return self.no_answer("배송/개별주문확인", "결제일과 현재 배송상태가 포함된 개별 주문 배송 문의는 실제 주문/출고 이력 확인이 필요합니다.")

        if self._is_install_product(product):
            # Asked whether a date can be met, before any order exists.
            #
            # "혹시 오늘 주문하면 9일까지 받아볼 수 있을까요?" contains
            # "주문하면", so _is_new_install_shipping_question matched it and
            # the new-order body was returned: 결제 확인 후 설치 기사님 일정에
            # 맞춰 진행됩니다. Every word of that is true and none of it says
            # whether the ninth is possible. Because the shipping block is an
            # exact-template match kind, it outranked GPT and was published.
            #
            # Nothing here can confirm a date for an order that does not exist
            # -- there is no order, so no DPS schedule, and standing policy
            # states no lead time. Declining is the only honest answer, and it
            # is what the weekend-policy branch below already does for the same
            # reason. An existing order with a confirmed DPS date never reaches
            # this block; it is answered from that schedule.
            if is_delivery_deadline_question(question):
                return self.no_answer(
                    "배송/기한확인",
                    "특정 날짜까지 배송·설치가 가능한지는 확정된 근거가 없어 "
                    "담당자 확인이 필요합니다.",
                )
            pickup = self._old_appliance_pickup(product, question)
            if pickup and self._is_new_install_shipping_question(question):
                body = self._install_new_order_body(product) + "\n\n" + self.config.shipping["old_appliance_pickup_answer"]
                return self.yes("배송/설치신규+폐가전", body, "방문설치 신규 주문 일정과 폐가전 수거 안내입니다.")
            if any(k in q for k in ["정확한배송날짜", "정확한날짜", "정확한가", "라면서", "앞전문의", "앞전", "제헌절", "공휴일도배송"]):
                return self.no_answer("배송/정확일자확인", "이미 안내받은 날짜의 정확성 확인이나 공휴일 배송 여부 확인은 개별 설치 일정 확인이 필요합니다.")
            if "해피콜" in q:
                body = self._install_existing_order_body(product, happycall=True)
                body = self._append_install_extras(body, question)
                return self.yes("배송/설치기존+해피콜", body, "방문설치 일정 안내와 해피콜 발신인 안내입니다.")
            if self._is_install_schedule_choice_question(question):
                body = self._install_schedule_choice_body(product)
                body = self._append_install_extras(body, question)
                return self.yes("배송/설치일조율", body, "방문설치 신규 주문의 일정 조율 가능 여부 안내입니다.")
            if self._is_new_install_shipping_question(question):
                body = self._install_new_order_body(product)
                if any(k in q for k in ["일주일", "1주일"]):
                    body += "\n\n따라서 현재 기준으로는 수령/설치까지 일주일 이상 소요될 가능성이 높습니다."
                body = self._lead_with_the_question(body, question)
                body = self._append_install_extras(body, question)
                return self.yes("배송/설치신규", body, "방문설치 신규 주문 일정 안내입니다.")
            # Last resort of the shipping block: anything mentioning a
            # shipping word for an install product that was not recognised as
            # a new-order question gets the existing-order notification
            # guidance ("알림톡은 설치일 전날 발송됩니다").
            #
            # That is guidance about *how notice is given*, not an answer to
            # "when is mine coming". Production inquiry 686472270 asked
            # "언제 발송되나요?" with no order number; the classifier did not
            # recognise 발송 as a schedule word, so the processing plan never
            # routed it to ORDER_ID_REQUEST, this fall-through answered it,
            # and the notification guidance was auto-posted to a customer
            # asking about their own shipment.
            #
            # The classifier now recognises that phrasing and routes it before
            # the engine is reached. This is the second line, using the same
            # predicate rather than a second opinion: one definition in
            # text_utils, consulted in both places.
            if not is_delivery_notice_question(question) and (
                CURRENT_DELIVERY_SCHEDULE_QUERY.search(q)
                or CURRENT_INSTALLATION_SCHEDULE_QUERY.search(q)
            ):
                return self.no_answer(
                    "배송/개별주문일정확인",
                    "개별 주문의 배송·설치 일정 문의는 주문 조회가 필요합니다.",
                )
            # Third line, for the two policy questions that name no order at
            # all. "혹시 토요일에도 배달 가능하나요? / 주문시 며칠 소요되나요"
            # matched none of the branches above, so it reached the default
            # below and was answered with the notification template -- which
            # tells the customer when the 알림톡 arrives and answers neither
            # question. Because the shipping block is a FIXED_POLICY_SHIPPING
            # match kind, that default also outranked GPT and became the
            # published answer.
            #
            # A weekend rule is checked first and separately: the shipping
            # config has none, so there is nothing to answer from and the
            # honest move is to decline rather than to pick the nearest
            # template. A duration question does have an answer here, and it
            # is the new-order body -- for an install product the wait is the
            # installer's schedule, which is exactly what that body explains.
            if is_weekend_delivery_policy_question(question):
                return self.no_answer(
                    "배송/주말정책확인",
                    "주말·공휴일 배송 가능 여부는 확정된 운영 기준이 없어 담당자 확인이 필요합니다.",
                )
            if is_general_delivery_policy_question(question):
                body = self._install_new_order_body(product)
                body = self._lead_with_the_question(body, question)
                body = self._append_install_extras(body, question)
                return self.yes(
                    "배송/설치신규",
                    body,
                    "방문설치 상품의 일반 배송 소요기간 안내입니다.",
                )
            # Default deny. This branch used to answer *everything* that got
            # this far with the existing-order notification body, which made a
            # confirmed operational template the fallback for any question
            # containing a shipping word: 보증기간, 캐시백, A/S, even "배송 중
            # 깨진 것 같은데 어떻게 하나요?". 75 corpus questions received it
            # and only 7 were about the notice.
            #
            # Handing the rest back rather than refusing them matters: this
            # block runs before _install_common_info and _old_appliance_pickup,
            # so those rules never got a chance at the questions it swallowed.
            # Returning None lets the A/S and visiting-installer rules answer
            # what is theirs, and sends the remainder to the evidence pipeline
            # -- where a missing basis becomes staff review rather than a
            # confident answer to a question nobody asked.
            if is_delivery_notice_question(question):
                body = self._install_existing_order_body(product)
                body = self._append_install_extras(body, question)
                return self.yes(
                    "배송/설치기존",
                    body,
                    "방문설치 기존 주문의 알림톡·사전연락 안내입니다.",
                )
            return None

        if any(k in q for k in ["송장", "운송장", "분리배송", "스탠드언제", "스탠드는언제"]):
            return self.no_answer("배송/부분배송", "송장번호/부분배송/스탠드 별도배송은 실제 출고 이력 확인이 필요합니다.")

        if self._is_parcel_product(product):
            return self.yes("배송/택배", self.config.shipping["parcel_default_answer"], "택배배송 기본 안내입니다.")

        return None

    # Asserts nothing the confirmed policy does not already say. That policy is
    # "결제 확인 후 설치 기사님 일정에 맞춰 배송·설치가 진행됩니다" -- a
    # scheduled delivery, which is what "not immediate" means. No duration and
    # no date is introduced, because none is confirmed anywhere.
    # Kept to the direct answer alone. Spelling out "결제 확인 후 설치 일정에
    # 맞춰 진행됩니다" here too would say the same thing twice, since that is
    # the sentence the body already opens with.
    _NOT_IMMEDIATE_LEAD = "주문 즉시 바로 배송되는 방식은 아닙니다."

    def _lead_with_the_question(self, body: str, question: str) -> str:
        """Answer what was asked first; keep the notice as the follow-up.

        "주문하면 바로 배송되나요" was answered with the new-order body, which
        opens on installer scheduling and then explains the 알림톡. Both
        sentences are true and neither says, in the customer's own terms, that
        the answer to their question is no. A reply whose first line is about
        the notification has inverted the question and its footnote.

        Only prepended when the customer actually asked about timing *and* the
        body does not already answer it. An active schedule rule opens with a
        real figure -- "배송/설치까지 빠르면 1~2주 정도 소요되고 있습니다" --
        which is a better answer than this sentence; adding it there would say
        the same thing twice and bury the number the customer wanted.
        """

        text = str(body or "").strip()
        if not text or not is_general_delivery_policy_question(question):
            return text
        opening = text.split("\n\n")[0]
        if _DIRECT_TIMING_ANSWER.search(opening):
            return text
        return f"{self._NOT_IMMEDIATE_LEAD}\n\n{text}"

    def _is_new_install_shipping_question(self, question: str) -> bool:
        q = compact(question)
        return any(k in q for k in [
            "오늘주문", "지금주문", "주문하면", "구매하면", "지금구매",
            "기간얼마", "얼마나걸", "언제걸", "오래걸", "많이오래", "5주", "몇주", "몇 주",
        ])

    def _install_new_order_body(self, product: str) -> str:
        return self._install_new_order_source(product)[0]

    def _install_new_order_source(self, product: str) -> tuple[str, bool]:
        """The new-order schedule body and whether a valid rule supplied it."""

        rule = self._install_schedule_rule(product)
        if rule:
            body = str(rule.get("신규주문안내") or "").strip()
            if body:
                return body, True
        if self._is_g9_install_product(product):
            return self.config.shipping["g9_install_answer"], True
        # No schedule is valid today. Fall back to wording that names no date
        # and no event, rather than to the last event notice that happened to
        # be written into the config.
        return self._shipping_default(
            "install_new_order_default_answer", "install_new_order_answer"
        ), False

    def _install_existing_order_body(self, product: str, happycall: bool = False) -> str:
        rule = self._install_schedule_rule(product)
        if rule:
            body = str(rule.get("기존주문안내") or "").strip()
            if body:
                if happycall and "해피콜" not in compact(body):
                    body = body.rstrip() + "\n\n이후 해피콜은 기사님 개인 연락처로 연락이 갈 수 있습니다."
                return body
        if happycall:
            return self.config.shipping["install_existing_happycall_answer"]
        return self.config.shipping["install_existing_order_answer"]

    def _install_schedule_choice_body(self, product: str) -> str:
        new_order_body, from_rule = self._install_new_order_source(product)
        if from_rule:
            return (
                "설치일은 고객님께서 일정 조율 가능합니다.\n\n"
                f"{new_order_body}\n\n"
                "설치 일정 관련 안내는 결제 후 수취인의 카카오톡 알림톡으로 발송되며, 이후 안내에 따라 원하시는 일정으로 조율해 주시면 됩니다."
            )
        return self._shipping_default(
            "install_schedule_choice_default_answer",
            "install_schedule_choice_answer",
        )

    def _with_event_notice(
        self,
        body: str,
        notice: object,
        validity: object,
    ) -> str:
        """Append a time-bound event sentence only while its window is open.

        The permanent part of the answer (the relaxed review window) is always
        correct; the backlog sentence beside it stops being true the moment the
        event ends, so it is gated by the same validity rules the install
        schedules use instead of living inside the fixed wording.
        """

        text = str(notice or "").strip()
        if not text or not isinstance(validity, dict):
            return body
        if install_schedule_status(validity) != VALIDITY_ACTIVE:
            return body
        return body.rstrip() + "\n\n" + text

    def _shipping_default(self, preferred_key: str, legacy_key: str) -> str:
        """Prefer the date-free default, tolerating an older config file."""

        value = str(self.config.shipping.get(preferred_key) or "").strip()
        return value or self.config.shipping[legacy_key]

    def _install_schedule_rule(self, product: str) -> dict | None:
        # Expired and not-yet-started schedules are skipped here rather than at
        # load time, so a long-running process cannot keep serving an event
        # notice that lapsed after it started.
        usable = active_install_schedule_rules(self.install_schedule_rules)
        if not usable:
            return None
        product_text = compact(product)
        normalized = re.sub(r"[^a-z0-9가-힣]", "", str(product or "").lower())
        matched: list[dict] = []
        broad_install_keywords = {"비즈니스tv", "사이니지", "삼성", "tv", "티비"}
        for rule in usable:
            product_keywords = split_keywords(rule.get("상품키워드"))
            model_keywords = split_keywords(rule.get("모델키워드"))
            distinctive_product_keywords = [
                keyword for keyword in product_keywords
                if compact(keyword) and compact(keyword) not in broad_install_keywords
            ]
            product_match = any(compact(keyword) in product_text for keyword in distinctive_product_keywords)
            model_match = any(
                re.sub(r"[^a-z0-9가-힣]", "", str(keyword or "").lower()) in normalized
                for keyword in model_keywords
                if str(keyword or "").strip()
            )
            if product_match or model_match:
                matched.append(rule)
        if not matched:
            return None
        return sorted(matched, key=lambda row: int(row.get("우선순위") or 999))[0]

    def _is_install_schedule_choice_question(self, question: str) -> bool:
        q = compact(question)
        return any(k in q for k in ["설치날짜", "설치일", "설치일정"]) and any(
            k in q for k in ["정할수", "정할 수", "조율", "토요일", "화요일", "오후", "원하"]
        )

    def _append_install_extras(self, body: str, question: str) -> str:
        extras = self._install_extra_paragraphs(question)
        if not extras:
            return body
        return body.rstrip() + "\n\n" + "\n\n".join(extras)

    def _install_extra_paragraphs(self, question: str) -> list[str]:
        q = compact(question)
        raw = str(question or "").lower()
        extras: list[str] = []
        if any(k in q for k in ["설치기사님", "기사님이오", "기사님오", "설치해주시"]):
            extras.append("해당 상품은 삼성 기사님이 방문하여 설치하는 상품입니다.")
        if any(k in q for k in ["정품", "삼성로고", "로고가없"]):
            extras.append("문의하신 제품은 삼성전자 정품입니다.")
        asks_as = any(k in q for k in ["서비스센터", "a/s", "고장", "불량"]) or bool(re.search(r"\ba\s*/?\s*s\b", raw))
        if asks_as:
            extras.append("제품 사용 중 고장이나 불량이 의심되는 경우 삼성전자 고객센터 1588-3366으로 문의해 A/S 접수해 주시면 됩니다.")
        return extras

    def _install_common_info(self, product: str, question: str) -> AnswerResult | None:
        if not self._is_install_product(product):
            return None
        extras = self._install_extra_paragraphs(question)
        if not extras:
            return None
        return self.yes("설치상품/공통안내", "\n\n".join(extras), "설치기사/정품/A/S 관련 일반 안내입니다.")

    def _is_order_specific_parcel_shipping(self, product: str, question: str) -> bool:
        if not self._is_parcel_product(product):
            return False
        q = compact(question)
        has_paid_date = bool(re.search(r"\d{1,2}\s*월\s*\d{1,2}\s*일", question) or re.search(r"\d{1,2}\s*[./-]\s*\d{1,2}", question))
        has_order_action = any(k in q for k in ["결제", "주문", "구매"])
        has_current_status = any(k in q for k in ["배송준비", "발송대기", "상품준비", "준비중", "출고대기"])
        asks_specific_eta = any(k in q for k in ["언제와", "언제오", "언제받", "배송일", "도착", "아직"])
        return has_paid_date and has_order_action and (has_current_status or asks_specific_eta)

    def _old_appliance_pickup(self, product: str, question: str) -> AnswerResult | None:
        q = compact(question)
        if not any(k in q for k in ["폐가전", "기존tv", "기존티비", "수거"]):
            return None
        if self._is_install_product(product):
            return self.yes("폐가전수거", self.config.shipping["old_appliance_pickup_answer"], "방문설치 상품의 폐가전 수거 안내입니다.")
        return self.need_info("폐가전수거", self.config.shipping["old_appliance_pickup_answer"], "방문설치 상품 여부 확인이 필요합니다.")

    def _stand_or_battery(self, product: str, question: str) -> AnswerResult | None:
        q = compact(question)
        p = compact(product)

        if any(k in p for k in ["삼탠바이미", "무탠바이미", "스탠바이미"]) and any(k in q for k in ["무선", "선없이", "선없이사용", "선없이쓸", "배터리를따로", "배터리따로", "배터리사야", "배터리를사야"]):
            return self.yes(
                "배터리/무선사용",
                "삼탠바이미 구성은 배터리가 있어야 전원선을 연결하지 않고 무선으로 사용할 수 있습니다.\n\n배터리가 포함되지 않은 구성이라면 별도 배터리 구매가 필요합니다.",
                "삼탠바이미 무선 사용에는 배터리가 필요합니다.",
            )

        if any(k in p + q for k in ["vipb01", "삼탠배터리", "배터리"]):
            if "m7" in q:
                return self.yes("배터리호환", self.config.models["battery_rules"]["m7_answer"], "M7 배터리 사용 및 PD 단자 사용불가 안내입니다.")
            if any(k in q for k in ["m5", "ls32dm5", "ls27dm5", "ls32fm5", "ls27fm5"]):
                if any(k in q for k in ["안맞", "안 맞", "단자", "케이블"]):
                    return self.no_answer("배터리/케이블", "M5 배터리 단자 불일치는 케이블 오발송 가능성이 있어 직원 응대가 필요합니다.")
                return self.yes("배터리호환", self.config.models["battery_rules"]["m5_answer"], "M5 전 모델 배터리 호환 안내입니다.")

        if any(k in q for k in ["오베닉", "몇세대", "세대"]):
            return self.yes("스탠드모델", self.config.models["stand_rules"]["obenic_fms_answer"], "오베닉/FMS 세대 문의 안내입니다.")

        is_stand_product = any(k in p for k in ["스탠드", "스텐드", "거치대", "fms", "삼탠바이미"])
        if is_stand_product and any(k in q for k in ["앞쪽", "쏠", "기울", "틸트"]):
            return self.yes("스탠드사용법", self.config.models["stand_rules"]["tilt_answer"], "스탠드 틸트 조임 안내입니다.")

        if any(k in q for k in ["호환", "장착", "사용가능", "사용 가능"]):
            mentioned_model = self._extract_question_model_code(question)
            if mentioned_model and not self._is_known_samsung_model_code(mentioned_model):
                return self.no_answer("스탠드호환/타사모델확인", "질문에 별도 타사 모델코드가 포함된 스탠드 호환 문의는 VESA 규격과 무게 확인이 필요하므로 자동답변하지 않습니다.")
            size = self._extract_size(product + " " + question)
            if "삼성" in product + question and size and size <= 32:
                return self.yes("스탠드호환", self.config.models["stand_rules"]["samsung_under_32_answer"], "32인치 이하 삼성 모니터 스탠드 호환 안내입니다.")
            if size == 43:
                return self.need_info("스탠드호환", self.config.models["stand_rules"]["inch_43_answer"], "43인치는 베사 확장브라켓 확인이 필요합니다.")
            if size and size >= 50:
                return self.need_info("스탠드호환", self.config.models["stand_rules"]["inch_50_plus_answer"], "50인치 이상은 무게와 베사 확인이 필요합니다.")

        return None

    def _model_code(self, product: str, question: str) -> AnswerResult | None:
        q = compact(question)
        if any(k in q for k in ["모델코드에뭘", "모델코드뭘", "모델코드에", "모델명에뭘"]):
            return self.yes("모델코드", self.config.events["onnuri"]["model_code_label_answer"], "코드 추정 제공 금지, 라벨 확인 안내입니다.")
        if any(k in q for k in ["dm인가", "fm인가"]):
            return self.no_answer("모델명/직원확인", "DM/FM 구분은 옵션/색상/연식에 따라 달라질 수 있어 직원 확인이 안전합니다.")
        return None

    def _model_spec_answer(self, product: str, question: str) -> AnswerResult | None:
        q = compact(question)
        p = compact(product)

        if self._is_smart_monitor_product(product) and any(k in q for k in ["ott", "넷플릭스", "디즈니", "티빙", "웨이브", "쿠팡플레이", "와이파이", "wifi", "wi-fi", "인터넷선", "유튜브", "tvplus"]):
            return self.yes(
                "스마트모니터/OTT",
                "문의하신 제품은 인터넷만 연결해 주시면 OTT 시청이 가능합니다.\n\n와이파이만 연결해도 사용 가능하며, OTT별 개인 구독은 별도로 필요합니다.",
                "스마트모니터는 사이니지와 달리 OTT 앱 사용이 가능한 상품군입니다.",
            )

        if self._is_smart_monitor_product(product) and any(k in q for k in ["usb", "유에스비", "동영상", "영상파일", "파일재생", "외장하드", "메모리", "코덱", "확장자", "포맷"]):
            return self.yes(
                "스마트모니터/USB재생",
                "문의하신 스마트 M5 모니터는 타이젠(Tizen) OS가 탑재된 스마트 모니터로, 별도 PC 연결 없이 USB 메모리나 외장하드를 모니터 뒷면 USB 포트에 연결해 내장 미디어 플레이어로 동영상 재생이 가능합니다.\n\nUSB 메모리나 외장하드의 포맷 형식은 FAT32, exFAT, NTFS 방식을 모두 지원합니다. 4GB 이상의 고용량 동영상 파일을 담으시려면 exFAT나 NTFS 포맷을 권장합니다.",
                "스마트 M5 USB 저장장치 동영상 재생 및 포맷 형식 안내입니다.",
            )

        # "케이블" was listed bare and meant cable *television*, but it is also
        # the word for the lead in the box: "배송 올 때 케이블도 같이 오나요?"
        # was answered with "이 스마트모니터는 RF 단자가 없어 지상파를 직접
        # 수신할 수 없습니다". Only the broadcast sense opens this rule now;
        # the accessory sense is left to the evidence pipeline.
        if self._is_smart_monitor_product(product) and any(
            k in q for k in [
                "rf", "rf단자", "지상파", "일반티비", "일반tv", "일반방송",
                "케이블방송", "케이블tv", "케이블채널", "유선방송", "채널",
            ]
        ):
            return self.yes(
                "스마트모니터/방송수신",
                "문의하신 스마트모니터는 RF 단자가 없어 일반 TV처럼 지상파 방송을 직접 수신해 시청할 수 없습니다.\n\n인터넷을 통한 OTT 시청으로 이용하시거나, 지상파/케이블 방송 시청이 필요하신 경우 셋톱박스 연결이 필수입니다.",
                "스마트모니터는 RF 단자가 없어 지상파 직접 수신이 불가합니다.",
            )

        if self._is_business_tv_product(product) and any(k in q for k in ["nas", "나스", "공유폴더", "서버", "네트워크드라이브"]):
            return self.yes(
                "비즈니스TV/NAS연결",
                "삼성 스마트 비즈니스TV는 네트워크 기능을 지원하지만, NAS 서버 공유 폴더에 노트북 없이 직접 접속 가능한지는 사용하시는 NAS 방식과 TV에서 지원하는 앱/연결 방식에 따라 달라질 수 있습니다.\n\n정확한 지원 여부와 연결 방법은 삼성전자 고객센터 1588-3366으로 문의해 확인해 주세요.",
                "NAS 직접 접속은 TV 지원 앱/연결 방식과 NAS 방식에 따라 달라져 삼성 고객센터 확인을 안내합니다.",
            )

        # Curated product-family rule: mirroring support is a property of the
        # business-TV line, and this rule states what that line does.
        #
        # It used to fire on "airplay"/"아이폰" as well, and then answered a
        # question it had not been written for. Real inquiry 686394444 asked
        # "아이폰 에어플레이 지원되나요? 와이파이 없이도 미러링 가능한가요?"
        # and received "미러링 기능을 지원합니다 / 아이폰 미러링은 지원하지
        # 않습니다" -- stated with full certainty, with no learning attached
        # and no ``airplay_support`` or ``mirroring_without_wifi`` fact for
        # that product. AirPlay is a distinct feature that varies by model
        # (operational Learning records both "에어플레이 미지원" for one model
        # and AirPlay instructions for another), so this rule cannot settle
        # it. Such a question now falls through to the evidence-based path,
        # which says the specification needs checking when nothing verifies
        # it. A mirroring question still gets the mirroring rule.
        if self._is_business_tv_product(product) and any(
            k in q for k in ["미러링", "화면공유", "화면 공유", "스마트뷰", "smartview", "스마트 뷰"]
        ):
            return self.yes(
                "비즈니스TV/미러링",
                "문의하신 제품은 미러링 기능을 지원합니다.\n\n다만 아이폰 미러링은 지원하지 않는 점 참고 부탁드립니다.",
                "비즈니스TV 미러링 지원 및 아이폰 미러링 미지원 확인 답변입니다.",
            )

        if self._is_business_tv_product(product) and any(k in q for k in ["디즈니", "disney"]):
            return self.yes(
                "사이니지/OTT",
                "문의하신 사이니지/비즈니스TV 계열은 디즈니+ 등 OTT 구독서비스 앱을 지원하지 않습니다.\n\nOTT 이용이 필요하신 경우, OTT가 지원되는 별도의 셋톱박스를 연결해 사용해 주세요.",
                "사이니지 계열은 OTT 구독서비스 앱이 없어 셋톱박스 연결 안내가 필요합니다.",
            )

        if self._is_business_tv_product(product) and any(k in q for k in ["ott", "넷플릭스", "와이파이", "wifi", "wi-fi", "웹브라우저", "인터넷웹", "유튜브"]):
            return self.yes(
                "사이니지/OTT",
                "넷플릭스 등 OTT 앱과 삼성 TV 플러스는 지원하지 않습니다.\n\n와이파이 연결 시 유튜브나 인터넷 웹은 사용 가능합니다.\n\nOTT 이용이 필요하신 경우, OTT가 지원되는 별도의 셋탑박스를 연결해 사용해 주세요.",
                "사이니지는 OTT 구독서비스 앱이 없어 셋톱박스 연결 안내가 필요합니다.",
            )

        if "오토피벗" in q:
            if any(k in p for k in ["s32dm5", "s27dm5"]):
                return self.yes("모델스펙/오토피벗", "문의하신 모델은 오토피벗 기능을 지원합니다.", "DM5 시리즈 오토피벗 지원 룰입니다.")
            if any(k in p for k in ["s32fm5", "s27fm5"]):
                return self.yes("모델스펙/오토피벗", "문의하신 모델은 오토피벗 기능을 지원하지 않습니다.", "FM5 시리즈 오토피벗 미지원 룰입니다.")
            if "m50f" in p:
                return self.yes("모델스펙/오토피벗", "문의하신 M50F 모델은 오토피벗 기능을 지원하지 않습니다.", "M50F는 FM 시리즈로 오토피벗 미지원입니다.")
            if "m5" in p and ("27인치" in p or "68.6cm" in p or "68cm" in p):
                return self.yes("모델스펙/오토피벗", "문의하신 M5 27인치 모델은 오토피벗 기능을 지원하지 않습니다.", "M5 27인치는 현재 FM 시리즈 기준으로 오토피벗 미지원입니다.")
            if "m5" in p and ("32인치" in p or "80cm" in p or "80.1cm" in p):
                return self.yes("모델스펙/오토피벗", "문의하신 M5 32인치 모델은 오토피벗 기능을 지원합니다.", "M50F가 아닌 M5 32인치는 DM 시리즈 기준으로 오토피벗 지원입니다.")

        model_key, item = self._find_model_item(product, question)
        if not item:
            return None
        spec = str(item.get("spec") or "")
        model = str(item.get("model") or model_key)

        if "오토피벗" in q:
            if any(k in model.upper() for k in ["S32DM5", "S27DM5"]):
                return self.yes("모델스펙/오토피벗", "문의하신 모델은 오토피벗 기능을 지원합니다.", "DM5 시리즈 오토피벗 지원 룰입니다.")
            if any(k in model.upper() for k in ["S32FM5", "S27FM5"]):
                return self.yes("모델스펙/오토피벗", "문의하신 모델은 오토피벗 기능을 지원하지 않습니다.", "FM5 시리즈 오토피벗 미지원 룰입니다.")
            if "m50f" in p:
                return self.yes("모델스펙/오토피벗", "문의하신 M50F 모델은 오토피벗 기능을 지원하지 않습니다.", "M50F는 FM 시리즈로 오토피벗 미지원입니다.")
            if "m5" in p and ("27인치" in p or "68.6cm" in p or "68cm" in p):
                return self.yes("모델스펙/오토피벗", "문의하신 M5 27인치 모델은 오토피벗 기능을 지원하지 않습니다.", "M5 27인치는 현재 FM 시리즈 기준으로 오토피벗 미지원입니다.")
            if "m5" in p and ("32인치" in p or "80cm" in p or "80.1cm" in p) and "m50f" not in p:
                return self.yes("모델스펙/오토피벗", "문의하신 M5 32인치 모델은 오토피벗 기능을 지원합니다.", "M50F가 아닌 M5 32인치는 DM 시리즈 기준으로 오토피벗 지원입니다.")

        if any(k in q for k in ["피벗", "세로", "길게화면", "화면돌", "돌려지", "회전"]):
            if "피벗" in spec:
                if any(k in q for k in ["안돌", "안 돌아", "걸려", "막혀", "방법", "어떻게"]):
                    return self.yes(
                        "모델스펙/피벗사용법",
                        "문의하신 모델은 피벗 기능을 지원합니다.\n\n세로 화면으로 회전할 때 화면이 받침대나 책상에 걸리는 경우, 먼저 모니터 화면을 위쪽으로 기울이거나 높이를 올린 뒤 천천히 회전해 주세요.",
                        "피벗 지원 모델의 실제 회전 조작 방법 안내입니다.",
                    )
                return self.yes(
                    "모델스펙/피벗",
                    f"문의하신 {model} 모델은 피벗 기능을 지원하여 화면을 세로 방향으로 돌려 사용할 수 있습니다.",
                    "JSON 모델 스펙에 피벗 기능이 확인됩니다.",
                )
            return self.no_answer("모델스펙/피벗확인", "해당 모델의 피벗 지원 근거가 JSON 스펙에서 확인되지 않아 자동답변하지 않습니다.")

        if any(k in q for k in ["스피커", "내장스피커", "소리나", "소리나오"]):
            speaker = item.get("speaker")
            if speaker is True:
                return self.yes("모델스펙/스피커", f"문의하신 {model} 모델은 스피커가 내장되어 있습니다.", "JSON 모델 스펙의 speaker 값이 true입니다.")
            if speaker is False:
                return self.yes("모델스펙/스피커", f"문의하신 {model} 모델은 스피커 내장형이 아닙니다.", "JSON 모델 스펙의 speaker 값이 false입니다.")

        return None

    def _find_model_item(self, product: str, question: str) -> tuple[str, dict] | tuple[None, None]:
        if not self.model_catalog:
            return None, None
        raw = f"{product} {question}".upper()
        normalized = re.sub(r"[^A-Z0-9]", "", raw)

        candidates = set()
        for match in re.findall(r"L?S\d{2}[A-Z]{1,3}\d{3,4}", normalized):
            candidates.add(match)
            if match.startswith("LS"):
                candidates.add(match[1:])
            if match.startswith("S"):
                candidates.add("L" + match)
        for match in re.findall(r"LH\d{2}BE[A-Z0-9]+", normalized):
            candidates.add(match)

        for key, item in self.model_catalog.items():
            key_norm = re.sub(r"[^A-Z0-9]", "", str(key).upper())
            model_norm = re.sub(r"[^A-Z0-9]", "", str(item.get("model", "")).upper()) if isinstance(item, dict) else ""
            if key_norm in candidates or model_norm in candidates:
                return key, item
            if key_norm and key_norm in normalized:
                return key, item
            if model_norm and model_norm in normalized:
                return key, item
        return None, None

    def _extract_question_model_code(self, question: str) -> str | None:
        text = str(question or "").upper()
        patterns = [
            r"\bBLS\d{2}-\d{2}[A-Z]\b",
            r"\b[A-Z]{2,5}\d{2,4}-\d{2,4}[A-Z]?\b",
            r"\bL?S\d{2}[A-Z]{1,3}\d{3,4}[A-Z0-9]*\b",
            r"\bLH\d{2}BE[A-Z0-9]*\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(0)
        return None

    def _is_known_samsung_model_code(self, model_code: str) -> bool:
        code = re.sub(r"[^A-Z0-9]", "", str(model_code or "").upper())
        if code.startswith(("LS", "S", "LH")):
            return True
        if code.startswith(("BLS", "LG", "OLED", "UN")):
            return False
        return False

    def _simple_product_usage(self, product: str, question: str, ctext: str, cq: str) -> AnswerResult | None:
        if any(k in cq for k in ["pc와연결", "pc연결", "모니터로사용"]) and any(k in cq for k in ["ott", "넷플릭스", "유튜브"]):
            return self.yes("제품사용", "네, PC와 연결해 모니터로 사용하시고 PC를 켜지 않았을 때는 모니터 자체 OTT 기능을 이용해 시청하실 수 있습니다.", "스마트모니터 PC 연결 및 자체 OTT 사용 문의입니다.")
        if any(k in cq for k in ["스탠드도오는", "스탠드포함", "스텐드도오는"]):
            return self.yes("상품구성", "네, 해당 상품 구성에는 이동식 스탠드가 포함되어 있습니다.", "상품 구성 문의입니다.")
        if any(k in cq for k in ["지상파", "일반방송"]):
            return self.yes("방송시청", "일반 지상파 방송은 안테나선 또는 유선방송/셋톱박스 등 시청 환경이 갖춰져 있으면 사용 가능합니다.", "지상파 방송 시청 문의입니다.")
        return None

    def _is_install_product(self, product: str) -> bool:
        p = compact(product)
        if any(k in p for k in ["비즈니스tv", "사이니지", "기사님방문설치"]):
            return True
        if re.search(r"lh\d{2}be", p):
            return True
        if self._is_g9_install_product(product):
            return True
        return self._is_moving_style_install_product(product)

    def _is_business_tv_product(self, product: str) -> bool:
        p = compact(product)
        return any(k in p for k in ["비즈니스tv", "사이니지"]) or bool(re.search(r"lh\d{2}be", p))

    def _is_moving_style_install_product(self, product: str) -> bool:
        p = compact(product)
        if any(k in p for k in ["fms", "vi스탠드", "vi거치대", "오베닉", "유압식", "2in1", "2in1거치대"]):
            return False
        return "무빙스타일" in p or "ls32dm501e-2wo" in p or "ls32dm501e2wo" in p

    def _is_g9_install_product(self, product: str) -> bool:
        p = compact(product)
        normalized = re.sub(r"[^a-z0-9]", "", str(product or "").lower())
        if any(code in normalized for code in ["ls49cg954ekxkr", "ls49dg930skxkr"]):
            return True
        return "오디세이g9" in p and ("49인치" in p or "123.8cm" in p or "49" in p)

    def _is_smart_monitor_product(self, product: str) -> bool:
        p = compact(product)
        if self._is_business_tv_product(product):
            return False
        if any(k in p for k in ["스마트모니터", "m5", "m7", "s32dm", "s32fm", "ls32dm", "ls32fm", "ls27dm", "ls27fm"]):
            return True
        if any(k in p for k in ["스마트tv", "iptv", "유튜브", "넷플릭스", "tvplus"]) and any(k in p for k in ["32인치", "80cm", "80.1cm"]):
            return True
        return False

    def _is_parcel_product(self, product: str) -> bool:
        p = compact(product)
        if self._is_install_product(product):
            return False
        return any(k in p for k in ["배터리", "vipb01", "m5", "m7", "m50f", "스마트모니터", "오디세이"]) or self._is_smart_monitor_product(product)

    def _extract_size(self, text: str) -> int | None:
        m = re.search(r"(\d{2,3})\s*(?:인치|inch)", text, flags=re.I)
        if m:
            return int(m.group(1))
        m = re.search(r"(?:LS|LH|S|KQ|KU|UN)(\d{2})", text.upper())
        if m:
            return int(m.group(1))
        return None


def split_keywords(value) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    return [part.strip() for part in re.split(r"[,/|;\n]+", text) if part and part.strip()]
