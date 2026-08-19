from __future__ import annotations

import re
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
        original_question = str(facts.inquiry.get("question") or "")
        guard = classify_product_fact(
            original_question,
            inquiry_type=inquiry.get("inquiry_type") or facts.inquiry.get("type"),
            inquiry_subtype=facts.inquiry.get("inquiry_subtype"),
            product_id=inquiry.get("product_id") or facts.product.get("product_id"),
            product_name=inquiry.get("product_name") or facts.product.get("name"),
            option_name=inquiry.get("option_name") or facts.product.get("option_name"),
        )
        store_code = inquiry.get("store_code")
        product_name = inquiry.get("product_name") or facts.product.get("name")
        inquiry_type = inquiry.get("inquiry_type") or facts.inquiry.get("type")
        # UNDERSTANDING already decomposes compound inquiries.  Search each
        # sub-question independently so one model-specific item cannot force
        # unrelated policy questions through the same product hard filter.
        questions = list(dict.fromkeys(
            str(item).strip() for item in intent.questions if str(item).strip()
        ))
        if len(questions) <= 1:
            questions = [original_question]
        candidate_pool = self.search.repository.candidates(
            store_code=store_code, limit=2000
        )
        repository_candidate_count = len(candidate_pool)
        candidate_diagnostics = self.search.repository.candidate_diagnostics(
            store_code=store_code
        )
        safe_candidate_pool: list[dict[str, Any]] = []
        learning_quality_rejections: dict[str, int] = {}
        for candidate in candidate_pool:
            metadata = candidate.get("metadata_json")
            metadata = metadata if isinstance(metadata, dict) else {}
            eligibility = self.historical.quality_policy.assess(
                question=str(candidate.get("question_original_masked") or ""),
                answer=str(candidate.get("final_answer") or ""),
                stored_quality=float(candidate.get("quality_score") or 0),
                policy_risk=str(metadata.get("policy_risk") or "NONE"),
                active=bool(candidate.get("active")),
                structured_temporary_valid=(
                    str(candidate.get("validity_type") or "PERMANENT").upper()
                    == "TEMPORARY"
                ),
            )
            source = str(candidate.get("learning_source") or "").upper()
            source_origin = str(metadata.get("source_origin") or "").upper()
            explicitly_approved = bool(
                (
                    source in {"APPROVED_EDITED", "APPROVED_UNEDITED"}
                    or metadata.get("human_verified") is True
                )
                and source_origin != "HISTORICAL_PROMOTED"
            )
            approval_overrides_soft_quality = bool(
                explicitly_approved
                and eligibility.status in {
                    "QUESTION_ANSWER_MISMATCH",
                    "LOW_RELEVANCE",
                    "REVIEW_REQUIRED",
                }
            )
            if not eligibility.context_eligible and not approval_overrides_soft_quality:
                learning_quality_rejections[eligibility.status] = (
                    learning_quality_rejections.get(eligibility.status, 0) + 1
                )
                continue
            copied = dict(candidate)
            copied["runtime_eligibility"] = {
                **eligibility.to_dict(),
                "context_eligible": True,
                "status": (
                    "SAFE_REUSABLE_APPROVED"
                    if approval_overrides_soft_quality
                    else eligibility.status
                ),
                "approval_override": approval_overrides_soft_quality,
            }
            safe_candidate_pool.append(copied)
        candidate_pool = safe_candidate_pool
        contexts: list[dict[str, Any]] = []
        subquestion_traces: list[dict[str, Any]] = []
        for question in questions:
            question_guard = classify_product_fact(
                question,
                inquiry_type=inquiry_type,
                inquiry_subtype=facts.inquiry.get("inquiry_subtype"),
                product_id=inquiry.get("product_id") or facts.product.get("product_id"),
                product_name=product_name,
                option_name=inquiry.get("option_name") or facts.product.get("option_name"),
            )
            item_context = self.search.context(
                question,
                store_code=store_code,
                intent=intent_data.get("category") or intent_data.get("primary_intent"),
                product_name=product_name,
                model_code=question_guard.model_code,
                inquiry_type=inquiry_type,
                product_id=question_guard.product_id,
                product_fact_sensitive=question_guard.sensitive,
                limit=2 if len(questions) > 1 else 3,
                candidate_pool=candidate_pool,
                candidate_diagnostics=candidate_diagnostics,
            )
            for key in ("similar_approved_answers", "seller_style_examples"):
                for item in item_context[key]:
                    item["matched_subquestion"] = question
                    item["attached_to_prompt"] = True
                    item["why_selected"] = (
                        "ACTIVE_VALIDITY_AND_RELEVANCE_THRESHOLD"
                    )
            trace = dict(item_context.get("learning_retrieval") or {})
            trace["product_fact_sensitive"] = question_guard.sensitive
            subquestion_traces.append(trace)
            contexts.append(item_context)

        def merged(key: str, limit: int = 6) -> list[dict[str, Any]]:
            by_id: dict[int, dict[str, Any]] = {}
            for item_context in contexts:
                for item in item_context[key]:
                    learning_id = int(item["learning_example_id"])
                    if learning_id not in by_id or float(item.get("relevance") or 0) > float(
                        by_id[learning_id].get("relevance") or 0
                    ):
                        by_id[learning_id] = item
            return sorted(
                by_id.values(), key=lambda item: float(item.get("relevance") or 0),
                reverse=True,
            )[:limit]

        approved = merged("similar_approved_answers", limit=6)
        seller = merged(
            "seller_style_examples", limit=max(0, 6 - len(approved))
        )
        context = {
            "similar_approved_answers": approved,
            "seller_style_examples": seller,
            "oje_style_rules": contexts[0]["oje_style_rules"] if contexts else {},
        }
        confirmed_schedule = bool(
            facts.installation.get("installation_date_confirmed")
            and facts.installation.get("date")
        )
        current_order_present = bool(
            str(facts.order.get("order_id") or "").strip()
        )
        historical_by_id: dict[int, dict[str, Any]] = {}
        historical_traces: list[dict[str, Any]] = []
        for question in questions:
            detailed = self.historical.search_detailed(
                question,
                store_code=store_code,
                product_name=product_name,
                inquiry_type=inquiry_type,
                limit=2 if len(questions) > 1 else 3,
            )
            historical_traces.append({
                key: value for key, value in detailed.items()
                if key != "selected"
            })
            for item in detailed["selected"]:
                copied = dict(item)
                copied["matched_subquestion"] = question
                case_id = int(copied["id"])
                if (
                    case_id not in historical_by_id
                    or float(copied.get("relevance") or 0)
                    > float(historical_by_id[case_id].get("relevance") or 0)
                ):
                    historical_by_id[case_id] = copied
        historical = sorted(
            historical_by_id.values(),
            key=lambda item: float(item.get("relevance") or 0),
            reverse=True,
        )[:3]
        if guard.sensitive:
            historical = [
                item for item in historical
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
        historical = [
            item for item in historical
            if int(item["id"]) not in promoted_case_ids
        ][:3]

        evidence_map: list[dict[str, Any]] = []
        for question in questions:
            historical_ids: list[int] = []
            approved_for_question = [
                item for item in approved
                if item.get("matched_subquestion") == question
            ]
            historical_for_question = [
                item for item in historical
                if item.get("matched_subquestion") == question
            ]
            explicit_current_schedule = bool(
                re.search(
                    r"(?:예정일|도착일|배송일|설치일|말일까지|기다리다|"
                    r"내\s*주문|주문한\s*(?:제품|상품))",
                    question,
                    re.IGNORECASE,
                )
            )
            asks_when = bool(
                re.search(
                    r"언제\s*(?:오|도착|배송|설치)",
                    question,
                    re.IGNORECASE,
                )
            )
            # "구매하면 며칠" 같은 구매 전 일반 배송정책은 Learning으로
            # 답할 수 있다. 현재 주문번호가 있거나 명시적으로 현재 일정/약속
            # 날짜를 묻는 경우만 DPS authoritative fact가 필요하다.
            schedule_specific = bool(
                explicit_current_schedule
                or (current_order_present and asks_when)
            )
            if schedule_specific and not confirmed_schedule:
                status = "NEEDS_DPS"
                evidence_ids: list[int] = []
                source = "CURRENT_DPS_REQUIRED"
            elif confirmed_schedule and schedule_specific:
                status = "ANSWERABLE"
                evidence_ids = []
                source = "CURRENT_DPS"
            elif approved_for_question:
                status = "ANSWERABLE"
                evidence_ids = [
                    int(item["learning_example_id"])
                    for item in approved_for_question
                ]
                source = "ACTIVE_POSITIVE_LEARNING"
            elif historical_for_question:
                status = "ANSWERABLE"
                evidence_ids = []
                historical_ids = [
                    int(item["id"]) for item in historical_for_question
                ]
                source = "SAFE_HISTORICAL_LEARNING"
            else:
                status = "NO_RELIABLE_SOURCE"
                evidence_ids = []
                source = None
                historical_ids = []
            evidence_map.append(
                {
                    "subquestion": question,
                    "status": status,
                    "source": source,
                    "learning_ids": evidence_ids,
                    "historical_case_ids": historical_ids,
                    "answer_required": status == "ANSWERABLE",
                }
            )
        context["subquestion_evidence"] = evidence_map
        context["subquestion_answer_policy"] = {
            "ANSWERABLE": "Answer directly from the mapped evidence.",
            "NEEDS_DPS": "Do not use Learning as the current order date.",
            "NO_RELIABLE_SOURCE": "Only this item may request confirmation.",
            "CONFLICT": "Do not choose between conflicting sources.",
        }
        selected_ids = [
            int(item["learning_example_id"])
            for item in (*context["similar_approved_answers"], *context["seller_style_examples"])
        ]
        retrieval = {
            "query": original_question,
            "product": product_name,
            "inquiry_type": inquiry_type,
            "candidate_count": repository_candidate_count,
            "safe_candidate_count": len(candidate_pool),
            "active_candidates": candidate_diagnostics.get("active_candidates", 0),
            "selected_count": len(selected_ids),
            "selected_learning_ids": selected_ids,
            "selected": [
                {
                    "learning_id": int(item["learning_example_id"]),
                    "matched_subquestion": item.get("matched_subquestion"),
                    "relevance": item.get("relevance"),
                    "why_selected": item.get("why_selected"),
                    "attached_to_prompt": True,
                    "answer_supported": None,
                }
                for item in context["similar_approved_answers"]
            ],
            "subquestion_count": len(questions),
            "subquestions": subquestion_traces,
            "rejection_counts": {
                "FILTERED_BY_VALIDITY": candidate_diagnostics.get("filtered_by_validity", 0),
                "REVOKED": candidate_diagnostics.get("revoked", 0),
                "NEGATIVE_EXCLUDED": candidate_diagnostics.get("negative_excluded", 0),
                "FILTERED_BY_RUNTIME_QUALITY": sum(
                    learning_quality_rejections.values()
                ),
                **learning_quality_rejections,
            },
        }
        context["learning_retrieval"] = retrieval
        context["historical_cases"] = [
            {
                "historical_case_id": int(item["id"]),
                "question": item.get("question"),
                "answer_style_reference": item.get("seller_answer"),
                "answer_reference": item.get("seller_answer"),
                "quality": item.get("quality_score"),
                "policy_risk": item.get("policy_risk"),
                "usage_notice": item.get("usage_notice"),
                "relevance": item.get("relevance"),
                "matched_subquestion": item.get("matched_subquestion"),
                "eligibility": item.get("runtime_eligibility"),
                "attached_to_prompt": True,
                "source": item.get("reference_strength")
                or "HISTORICAL_VERIFIED_LEARNING",
            }
            for item in historical
        ]
        context["historical_case_policy"] = {
            "safe_reusable_knowledge_allowed": True,
            "runtime_eligibility_required": True,
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
            "learning_role": "APPROVED_STABLE_POLICY_AND_PRODUCT_REFERENCE",
            "approved_learning_may_support": [
                "STABLE_POLICY", "INSTALLATION_METHOD", "PRODUCT_GUIDANCE",
                "AFTER_SERVICE_POLICY", "PROMOTION_POLICY",
            ],
            "approved_learning_must_not_supply": [
                "CURRENT_ORDER_STATUS", "CURRENT_DELIVERY_STATUS",
                "CURRENT_INSTALLATION_DATE",
            ],
        }
        context["historical_retrieval"] = {
            "query": original_question,
            "subquestion_count": len(questions),
            "candidate_count": max(
                (int(item.get("candidate_count") or 0) for item in historical_traces),
                default=0,
            ),
            "safe_candidate_count": max(
                (
                    int(item.get("safe_candidate_count") or 0)
                    for item in historical_traces
                ),
                default=0,
            ),
            "selected_count": len(historical),
            "selected_historical_case_ids": [int(item["id"]) for item in historical],
            "subquestions": historical_traces,
        }
        if inquiry_id is not None:
            self.logs.record_inquiry(
                int(inquiry_id),
                "LEARNING_RETRIEVAL_START",
                "ACTIVE Positive Learning 후보 검색을 실행했습니다.",
                details={
                    "query": retrieval.get("query"),
                    "product": retrieval.get("product"),
                    "inquiry_type": retrieval.get("inquiry_type"),
                    "candidate_count": retrieval.get("candidate_count", 0),
                },
            )
            context_run_id = self.provenance.record_context(
                inquiry_id=int(inquiry_id),
                learning=learning_references,
                historical=context["historical_cases"],
            )
            result_count = len(context["similar_approved_answers"]) + len(
                context["seller_style_examples"]
            )
            historical_count = len(context["historical_cases"])
            self.logs.record_inquiry(
                int(inquiry_id),
                "LEARNING_RETRIEVAL_COMPLETED",
                "Learning 후보 선택 결과를 기록했습니다.",
                details={
                    **retrieval,
                    "context_attached": bool(result_count or historical_count),
                    "historical_context_attached": bool(historical_count),
                    "subquestion_evidence": evidence_map,
                },
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
                details={"result_count": result_count, "max_results": 6},
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
                        "selected_count": result_count,
                        "selection_reason": "RELEVANCE_THRESHOLD_AND_ACTIVE_VALIDITY",
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
                        "subquestions": historical_traces,
                    },
                )
        return context
