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
        "전화번호, 주소, OTP, 인증정보, 토큰, Cookie, Session을 출력하지 않는다."
    )
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
            "subquestion_results": [
                {
                    "subquestion": "string",
                    "status": {
                        "enum": [
                            "ANSWERABLE", "NEEDS_DPS",
                            "NO_RELIABLE_SOURCE", "CONFLICT",
                        ]
                    },
                    "learning_ids": ["integer"],
                    "answered": "boolean",
                }
            ],
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
                "partial_answer_required_for_supported_subquestions": True,
                "subquestion_evidence_is_binding": True,
                "answerable_items_must_not_be_replaced_by_blanket_uncertainty": True,
                "report_each_learning_id_actually_used": True,
            },
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
