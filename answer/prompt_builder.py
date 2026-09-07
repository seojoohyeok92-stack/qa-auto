from __future__ import annotations

import json
import re
from typing import Any

from answer.fact_selection import SelectedFacts
from answer.facts import AnswerFacts
from answer.inquiry_analysis import InquiryAnalysis
from answer.text_utils import mask_personal_information


FORBIDDEN_KEYS = {
    "address",
    "주소",
    "phone",
    "telephone",
    "otp",
    "token",
    "api_key",
    "access_token",
    "refresh_token",
    "authorization",
    "cookie",
    "session",
    "password",
    "secret",
    "customer_display",
}
SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_ -]?key|token|cookie|session|otp|password|secret)"
    r"\s*[:=]\s*\S+"
)
ADDRESS_PATTERN = re.compile(
    r"(?:서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충[북남]|"
    r"전[북남]|경[북남]|제주)[^\n,]{0,40}(?:로|길|동)\s*\d+(?:-\d+)?"
)
EMAIL_ANY_PATTERN = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
)


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _sanitize(item)
            for key, item in value.items()
            if str(key).strip().lower() not in FORBIDDEN_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        masked = mask_personal_information(value)
        masked = EMAIL_ANY_PATTERN.sub("<masked-email>", masked)
        masked = SECRET_ASSIGNMENT.sub("<masked-secret>", masked)
        return ADDRESS_PATTERN.sub("<masked-address>", masked)
    return value


class PromptBuilder:
    COMPANY_TONE = (
        "정중하고 간결한 한국어 고객응대 말투를 사용한다. "
        "확인된 사실과 확인이 필요한 내용을 명확히 구분한다."
    )
    PROHIBITIONS = (
        "Facts에 없는 배송일, 주문상태, 설치일, 상품정보, 정책, "
        "기사 방문시간, 반품 가능 여부를 추측하지 않는다. "
        "전화번호, 주소, OTP, 인증정보, 토큰, Cookie, Session을 출력하지 않는다. "
        "인사말(예: 안녕하세요)과 마무리 인사(예: 감사합니다)는 별도 Template이 "
        "자동으로 추가하므로 답변 본문에 포함하지 않는다."
    )
    # negative_corrections carry what a member of staff wrote down after
    # rejecting a past answer: which claim was wrong, and what should have
    # been said. They constrain the answer; they never license one. The last
    # two lines matter most -- a Negative was saved about one claim, and
    # widening it into "avoid this whole subject" throws away correct
    # knowledge, which is the failure mode this section exists to prevent.
    NEGATIVE_CORRECTION_INSTRUCTIONS: tuple[str, ...] = (
        "negative_corrections의 bad_patterns에 있는 잘못된 내용을 "
        "다시 답변하지 않는다.",
        "관련된 corrections의 교정 방향을 답변에 반영한다.",
        "corrections에 적혀 있지 않은 교정 내용을 만들어내지 않는다.",
        "negative_corrections는 제약이며, 그 자체로 답변 가능 여부를 "
        "결정하지 않는다.",
        "교정 범위는 지적된 claim에만 적용한다. 같은 주제의 다른 "
        "올바른 사실까지 취소하지 않는다.",
        "Approved Positive 근거가 맞다면 그 사실은 유지하고, 잘못된 "
        "부분만 corrections에 따라 바로잡는다.",
    )
    # What to check before leaning on a retrieved candidate. These are
    # questions, not a ranking: retrieval already ranked, and its score is a
    # measure of wording overlap rather than of whether the candidate answers
    # this customer. Each rule names something the candidate's own provenance
    # can settle -- which question it was written for, which product, when,
    # and whether the claim it carries is tied to one order or holds for the
    # product generally.
    EVIDENCE_JUDGEMENT_RULES: tuple[str, ...] = (
        "retrieval이 가져온 후보는 '검토 대상'이지 '승인된 근거'가 아니다.",
        "각 후보의 relevance/answer_support/rank는 검색 신호이며,"
        " 낮다고 해서 사용 금지를 뜻하지 않는다. 표현이 달라도 같은 사실을"
        " 말하는 후보는 사용할 수 있다.",
        "후보의 source_question(그 답변이 원래 어떤 질문에 쓰였는지)과"
        " source_product를 읽고, 지금 질문과 지금 상품에 적용되는지 판단한다.",
        "시간에 의존하는 사실(특정 주문의 배송일·설치일·처리 상태)은"
        " 다른 주문에 재사용하지 않는다. 그 값은 현재 Order/DPS 결과에서만 온다.",
        "상품에 대해 일반적으로 성립하는 안정적 운영 지식(설치 방식, A/S 절차,"
        " 상시 정책)은 과거 문의에서 나왔더라도 현재 상품에 적용되면 사용할 수 있다.",
        "만료되었거나 다른 모델/변형을 가리키는 후보는 사용하지 않는다.",
        "후보들이 서로 충돌하면 한쪽을 고르지 말고 unresolved로 남긴다.",
        "근거가 실제로 말하지 않는 것을 확장하지 않는다."
        " 예: installation_method=PROFESSIONAL_TECHNICIAN_REQUIRED 는"
        " '전문 기사 설치'까지만 말하며, 기사의 소속 브랜드는 말하지 않는다.",
        "검색 결과는 후보이며 코드가 관련성을 보증하지 않는다. 각 후보를 직접"
        " 읽고 현재 atomic question 에 실제로 도움이 되는 것만 사용한다.",
        "후보가 검색됐다는 이유만으로 사용하지 않는다.",
        "다른 모델/상품의 사양을 현재 상품의 사실로 전환하지 않는다.",
        "LISTING METADATA(판매 페이지 표기)와 VERIFIED PRODUCT CATALOG FACTS"
        "(검증 사양)를 구분한다. product_information_tiers 를 따른다.",
        "사용한 후보와 사용하지 않은 후보를 모두 이유와 함께 보고한다.",
        "어떤 질문에 대해 쓸 수 있는 근거가 없으면 그 질문만 unresolved로"
        " 남기고, 답할 수 있는 다른 질문까지 회피하지 않는다.",
    )

    # What each product block in ``input`` means, and how far each may be
    # trusted. Keys match the block names the retrieval side emits.
    PRODUCT_TIER_RULES: dict[str, str] = {
        "listing_metadata": (
            "allowed_facts.product 은 현재 네이버 판매 페이지에 표시된 상품"
            " 정보입니다. 어떤 상품에 대한 문의인지 이해하는 데 사용하고,"
            " 판매 페이지 표기 자체를 인용할 수는 있으나(예: '판매 페이지에는"
            " 4K UHD로 표기되어 있습니다'), 검증된 사양과 같은 신뢰도로"
            " 단정하지 마세요."
        ),
        "product_catalog": (
            "현재 상품과 안전하게 식별된 Product Catalog 의 검증 사양입니다."
            " 확정 사실로 사용할 수 있습니다."
        ),
        "product_candidates": (
            "정확한 상품 identity 가 확정되지 않아 남은 후보 모델입니다."
            " 다른 모델의 사양을 현재 상품의 확정 사실로 전환하지 마세요."
        ),
        "precedence": (
            "product_catalog > listing_metadata > product_candidates."
            " 서로 어긋나면 확정하지 말고 unresolved 로 남기세요."
        ),
    }

    OUTPUT_CONTRACTS: dict[str, dict[str, Any]] = {
        "UNDERSTANDING": {
            "category": "string",
            "questions": ["string"],
            "emotion": {
                "enum": [
                    "NORMAL",
                    "CONFUSED",
                    "URGENT",
                    "ANGRY",
                    "THANKFUL",
                    "FOLLOW_UP",
                ]
            },
            "urgency": {"enum": ["NORMAL", "HIGH"]},
            "confidence": "number between 0 and 1",
            "requires_review": "boolean",
            "reason": "string",
        },
        "DRAFT": {
            "answer": "string in polite Korean",
            "confidence": "number between 0 and 1",
            "used_facts": ["fact path string"],
            "missing_information": ["string"],
            "requires_review": "boolean",
            "warnings": ["string"],
            "learning_usage": [
                {
                    "learning_id": "integer",
                    "matched_subquestion": "string",
                    "answer_supported": "boolean",
                    "reason": "string",
                }
            ],
            "historical_usage": [
                {
                    "historical_case_id": "integer",
                    "matched_subquestion": "string",
                    "answer_supported": "boolean",
                    "reason": "string",
                }
            ],
            "feedback_signal_usage": [
                {
                    "signal_id": "integer",
                    "matched_subquestion": "string",
                    "answer_supported": "boolean",
                    "reason": "string",
                }
            ],
            "subquestion_results": [
                {
                    "subquestion": "string",
                    "status": {
                        "enum": [
                            "ANSWERABLE", "NEEDS_DPS",
                            "NO_RELIABLE_SOURCE", "CONFLICT",
                            "DELIVERY_SCHEDULE_REVIEW",
                        ]
                    },
                    "learning_ids": ["integer"],
                    "answered": "boolean",
                }
            ],
            # Which candidates were actually leaned on, and which were read and
            # put aside. Both halves matter: the dashboard used to show "6
            # selected / 0 used" whenever the code had emptied the evidence map,
            # so an operator could not tell a model that found nothing useful
            # from a pipeline that had forbidden everything.
            "evidence_decisions": [
                {
                    "kind": {
                        "enum": [
                            "TEMPLATE", "PRODUCT_FACT", "LEARNING",
                            "HISTORICAL", "FEEDBACK_SIGNAL",
                        ]
                    },
                    "id": "string or integer identifying the candidate",
                    "decision": {"enum": ["USED", "IGNORED"]},
                    "matched_subquestion": "string",
                    "reason": "string",
                }
            ],
            "used_template_ids": ["template id string"],
            "used_product_facts": ["product fact field_key string"],
            "used_learning_ids": ["integer"],
            "used_historical_ids": ["integer"],
            "ignored_evidence": [
                {"kind": "string", "id": "string or integer",
                 "reason": "string"}
            ],
            "unresolved": ["subquestion string with no usable evidence"],
            "can_auto_post": "boolean",
            "reason": "string",
        },
        "SELF_REVIEW": {
            "passed": "boolean",
            "answered_all_questions": "boolean",
            "has_speculation": "boolean",
            "facts_consistent": "boolean",
            "requires_review": "boolean",
            "reason": "string",
            "warnings": ["string"],
        },
    }

    def build(
        self,
        *,
        task: str,
        facts: AnswerFacts,
        extra: dict[str, Any] | None = None,
        analysis: InquiryAnalysis | None = None,
        selected_facts: SelectedFacts | None = None,
    ) -> str:
        normalized_task = str(task).upper()
        fact_payload = (
            dict(selected_facts.values)
            if selected_facts is not None
            else facts.to_prompt_dict()
        )
        allowed_fact_paths = (
            list(selected_facts.keys)
            if selected_facts is not None
            else sorted(
                f"{section}.{key}"
                for section, values in fact_payload.items()
                if isinstance(values, dict)
                for key, value in values.items()
                if value not in (None, "", [], {}, ())
            )
        )
        installation_date = facts.installation.get("date")
        installation_confirmed = bool(
            installation_date
            and facts.installation.get(
                "installation_date_confirmed"
            )
        )
        confirmed_facts = {
            "installation_date": (
                installation_date if installation_confirmed else None
            ),
            "required_delivery_date": (
                facts.installation.get("required_delivery_date")
                if installation_confirmed
                else None
            ),
            "installation_date_source": (
                facts.installation.get("source")
                if installation_confirmed
                else None
            ),
            "installation_date_status": (
                "CONFIRMED" if installation_confirmed else "UNCONFIRMED"
            ),
        }
        payload = {
            "task": normalized_task,
            "system_policy": {
                "facts_only": True,
                "customer_question_first": True,
                "do_not_expose_internal_status": True,
                "do_not_guess_dates": True,
                "do_not_request_excess_personal_information": True,
                "active_positive_learning_is_grounded_reference": True,
                "answer_supported_parts_even_if_other_parts_are_missing": True,
            },
            "customer_inquiry": facts.inquiry.get("question"),
            # Three tiers of product information, deliberately not merged.
            #
            # The listing is what the seller wrote on the page the customer is
            # reading; the catalogue is what the operator verified for one
            # identified model. They are usually consistent and occasionally
            # not, and the difference matters to the customer -- so the model is
            # told which is which rather than being handed one blended block.
            #
            # The listing tier exists because withholding it produced worse
            # answers than showing it: an inquiry whose catalogue lookup failed
            # was left with no idea which product was being asked about at all.
            "product_information_tiers": dict(self.PRODUCT_TIER_RULES),
            "inquiry_analysis": (
                analysis.to_dict() if analysis is not None else {}
            ),
            "answer_strategy": (
                analysis.answer_strategy.value if analysis is not None else ""
            ),
            "allowed_facts": fact_payload,
            "facts": fact_payload,
            "prohibited_claims": [
                self.PROHIBITIONS,
                "Allowed facts에 없는 사실을 만들거나 변경하지 않습니다.",
                "고객이 묻지 않은 구매요청 상태나 내부 처리 코드를 설명하지 않습니다.",
                "DPS, 요구납기일, AnswerFacts, GPT, OpenAI, API, DB를 고객에게 노출하지 않습니다.",
            ],
            "required_content": (
                [
                    "네이버 주문내역에 표시된 주문번호 요청",
                    "비밀글 또는 비공개로 남겨 달라는 안내",
                    "주문번호 확인 후 다시 안내한다는 설명",
                ]
                if analysis is not None
                and analysis.answer_strategy.value == "REQUEST_ORDER_ID"
                else []
            ),
            "tone_and_length": {
                "tone": self.COMPANY_TONE,
                "polite_korean": True,
                "concise": True,
                "answer_only": True,
                "avoid_repetition": True,
                "no_greeting_or_closing": True,
            },
            "confirmed_facts": confirmed_facts,
            "learning_usage_policy": {
                "approved_learning_allowed_for": [
                    "stable policy", "installation method", "product guidance",
                    "after-service policy", "promotion policy",
                ],
                "approved_learning_forbidden_for": [
                    "current order status", "current delivery status",
                    "current installation date",
                ],
                "seller_style_examples_are_facts": False,
                "safe_historical_learning_allowed_for_stable_knowledge": True,
                "historical_learning_forbidden_for_current_order_facts": True,
                "partial_answer_required_for_supported_subquestions": True,
                # Retrieval hands over candidates, not verdicts. The scores
                # beside each one (relevance, answer_support, rank) measure how
                # a search engine found it; they are lexical, so a correct
                # answer worded differently from the question scores zero and a
                # wrong answer sharing the question's words scores well.
                # Reading them as permission is what stopped an approved
                # "해당 상품은 삼성 기사님이 방문하여 설치하는 상품입니다." from
                # answering "삼성기사분이 설치하러 오시나요". Whether a candidate
                # answers *this* question is a judgement, and it is made here.
                "retrieval_candidates_are_not_approved_evidence": True,
                "relevance_and_answer_support_are_hints_not_permission": True,
                "you_decide_which_candidates_apply": True,
                "answerable_items_must_not_be_replaced_by_blanket_uncertainty": True,
                "report_each_learning_id_actually_used": True,
                "report_each_historical_case_id_actually_used": True,
                "report_ignored_candidates_with_reason": True,
            },
            # How to read a candidate. Each one carries its own provenance
            # (source_question, source_product, validity, scope); these say what
            # to check, never which candidate to pick.
            "evidence_judgement_rules": list(self.EVIDENCE_JUDGEMENT_RULES),
            "feedback_signal_policy": {
                "verified_facts_and_corrections_are_factual_evidence": True,
                "corrections_supersede_older_or_conflicting_learning_answers": True,
                "good_patterns_and_bad_patterns_are_style_guidance_only": True,
                "good_patterns_and_bad_patterns_are_never_factual_evidence": True,
                "avoid_bad_pattern_content_and_structure": True,
                "follow_good_pattern_style_where_relevant": True,
                "never_choose_between_conflicting_verified_facts": True,
                "report_each_feedback_signal_id_actually_used": True,
            },
            # Only present when a Negative memo was actually retrieved for this
            # inquiry, so an inquiry with no relevant Negative keeps exactly
            # the prompt it had before this contract existed.
            **(
                {
                    "negative_correction_instructions": list(
                        self.NEGATIVE_CORRECTION_INSTRUCTIONS
                    )
                }
                if (extra or {}).get("negative_corrections")
                else {}
            ),
            "installation_date_instructions": (
                [
                    "확정된 설치예정일이 있으면 고객에게 자연스럽게 안내한다.",
                    "확정 날짜가 있는데 확인할 수 없다고 답하지 않는다.",
                    "확정 날짜와 다른 날짜를 만들거나 변경하지 않는다.",
                    "날짜가 없으면 임의 날짜를 생성하지 않는다.",
                    "고객 답변에는 DPS, 요구납기일, AnswerFacts, GPT, "
                    "OpenAI, API, DB 같은 내부 용어를 노출하지 않는다.",
                    "고객에게는 설치예정일 또는 예정일로 표현한다.",
                    "확정 보장 대신 현재 확인되는 예정일이라고 표현한다.",
                ]
                if normalized_task == "DRAFT"
                else []
            ),
            "input": dict(extra or {}),
            "output_schema": {
                "format": "JSON object only",
                "no_markdown": True,
                "facts_only": True,
                "required_fields": self.OUTPUT_CONTRACTS.get(
                    normalized_task, {}
                ),
                "used_facts_rule": (
                    "used_facts must contain only exact strings from "
                    "allowed_fact_paths"
                ),
                "allowed_fact_paths": allowed_fact_paths,
                "omit_additional_fields": True,
            },
            "output_contract": {
                "format": "JSON object only",
                "no_markdown": True,
                "facts_only": True,
                "required_fields": self.OUTPUT_CONTRACTS.get(
                    normalized_task, {}
                ),
                "allowed_fact_paths": allowed_fact_paths,
            },
        }
        safe = _sanitize(payload)
        return json.dumps(safe, ensure_ascii=False, sort_keys=True)

    def safe_payload(self, payload: Any) -> Any:
        return _sanitize(payload)
