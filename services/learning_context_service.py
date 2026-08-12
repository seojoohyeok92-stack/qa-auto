from __future__ import annotations

from typing import Any

from answer.facts import AnswerFacts
from answer.hybrid_models import IntentResult
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_repository import LearningRepository
from repositories.log_repository import LogRepository
from services.similar_answer_service import SimilarAnswerService
from services.historical_case_service import HistoricalCaseService
from repositories.learning_provenance_repository import LearningProvenanceRepository
from services.product_fact_guard import classify_product_fact, same_stable_product


class LearningContextService:
    """GPT에 제공할 비사실성 참고 문맥만 구성한다."""

    def __init__(self, database: Database) -> None:
        self.inquiries = InquiryRepository(database)
        self.search = SimilarAnswerService(LearningRepository(database))
        self.logs = LogRepository(database)
        self.historical = HistoricalCaseService(database)
        self.provenance = LearningProvenanceRepository(database)

    def build(self, facts: AnswerFacts, intent: IntentResult) -> dict[str, Any]:
        inquiry_id = facts.inquiry.get("inquiry_id")
        inquiry = self.inquiries.get(int(inquiry_id)) if inquiry_id is not None else None
        inquiry = inquiry or {}
        intent_data = intent.to_dict()
        guard = classify_product_fact(
            facts.inquiry.get("question"),
            inquiry_type=inquiry.get("inquiry_type") or facts.inquiry.get("type"),
            inquiry_subtype=facts.inquiry.get("inquiry_subtype"),
            product_id=inquiry.get("product_id") or facts.product.get("product_id"),
            product_name=inquiry.get("product_name") or facts.product.get("name"),
            option_name=inquiry.get("option_name") or facts.product.get("option_name"),
        )
        context = self.search.context(
            str(facts.inquiry.get("question") or ""),
            store_code=inquiry.get("store_code"),
            intent=intent_data.get("category") or intent_data.get("primary_intent"),
            product_name=inquiry.get("product_name") or facts.product.get("name"),
            model_code=guard.model_code,
            inquiry_type=inquiry.get("inquiry_type") or facts.inquiry.get("type"),
            product_id=guard.product_id,
            product_fact_sensitive=guard.sensitive,
        )
        historical = self.historical.search(
            str(facts.inquiry.get("question") or ""),
            store_code=inquiry.get("store_code"),
            product_name=inquiry.get("product_name") or facts.product.get("name"),
            inquiry_type=inquiry.get("inquiry_type") or facts.inquiry.get("type"),
            limit=5,
        )
        if guard.sensitive:
            historical = [
                item
                for item in historical
                if same_stable_product(
                    current_product_id=guard.product_id,
                    candidate_product_id=item.get("product_id"),
                    current_model_code=guard.model_code,
                    candidate_model_code=None,
                )
            ]
        learning_references = [
            *context["similar_approved_answers"],
            *context["seller_style_examples"],
        ]
        promoted_case_ids = {
            int(item["historical_case_id"])
            for item in learning_references
            if item.get("historical_case_id") is not None
        }
        # A legacy-promoted case may be available through both repositories.
        # Include it once, through its existing Learning provenance route.
        historical = [
            item for item in historical
            if int(item["id"]) not in promoted_case_ids
        ][:3]
        context["historical_cases"] = [
            {
                "historical_case_id": int(item["id"]),
                "question": item.get("question"),
                "answer_style_reference": item.get("seller_answer"),
                "quality": item.get("quality_score"),
                "policy_risk": item.get("policy_risk"),
                "usage_notice": item.get("usage_notice"),
                "relevance": item.get("relevance"),
                "source": item.get("reference_strength")
                or "HISTORICAL_VERIFIED_LEARNING",
            }
            for item in historical
        ]
        context["historical_case_policy"] = {
            "reference_only": True,
            "never_use_as_current_fact": True,
            "current_authority_order": [
                "RULE_AND_SAFETY", "CURRENT_ORDER", "CURRENT_DPS",
                "PRODUCT_DB", "VALIDATED_TEMPLATE", "APPROVED_LEARNING",
                "HISTORICAL_VERIFIED_LEARNING",
            ],
            "time_dependent_claims_require_current_facts": True,
        }
        context["product_fact_guard"] = {
            **guard.to_dict(),
            "cross_product_answer_bodies_excluded": guard.sensitive,
            "same_product_detailed_examples_only": guard.sensitive,
            "learning_role": "STYLE_AND_STRUCTURE_ONLY_WITHOUT_CURRENT_FACT",
        }
        if inquiry_id is not None:
            context_run_id = self.provenance.record_context(
                inquiry_id=int(inquiry_id),
                learning=learning_references,
                historical=context["historical_cases"],
            )
            result_count = len(context["similar_approved_answers"]) + len(
                context["seller_style_examples"]
            )
            self.logs.record_inquiry(
                int(inquiry_id),
                (
                    "SIMILAR_ANSWERS_FOUND"
                    if result_count
                    else "SIMILAR_ANSWERS_NOT_FOUND"
                ),
                (
                    "승인된 유사 답변을 찾았습니다."
                    if result_count
                    else "적용 가능한 승인 유사 답변이 없습니다."
                ),
                details={"result_count": result_count, "max_results": 3},
            )
            if result_count:
                self.logs.record_inquiry(
                    int(inquiry_id),
                    "LEARNING_CONTEXT_APPLIED",
                    "승인된 유사 답변을 GPT 참고 Context에 적용했습니다.",
                    details={
                        "result_count": result_count,
                        "facts_authority": "PRODUCT_DB_POLICY_VALIDATOR_FIRST",
                        "context_run_id": context_run_id,
                        "learning_example_ids": [
                            item["learning_example_id"] for item in learning_references
                        ],
                    },
                )
            if historical:
                self.logs.record_inquiry(
                    int(inquiry_id),
                    "HISTORICAL_CONTEXT_APPLIED",
                    "유사한 과거 상담 사례를 표현 참고 Context에 적용했습니다.",
                    details={
                        "result_count": len(historical),
                        "reference_only": True,
                        "facts_authority": "CURRENT_RULE_ORDER_DPS_FIRST",
                        "context_run_id": context_run_id,
                        "historical_case_ids": [
                            item["historical_case_id"] for item in context["historical_cases"]
                        ],
                    },
                )
        return context
