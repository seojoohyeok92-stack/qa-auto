from __future__ import annotations

import re
from collections import Counter
from difflib import SequenceMatcher
from typing import Any

from repositories.learning_repository import LearningRepository
from services.learning_privacy_service import LearningPrivacyService
from services.product_fact_guard import same_stable_product


TOKEN = re.compile(r"[가-힣A-Za-z0-9]{2,}")
SEMANTIC_CONCEPTS = {
    "SELF_INSTALLATION": (r"자가\s*설치",),
    "TECHNICIAN_INSTALLATION": (
        r"기사(?:님)?[^\n]{0,12}설치",
        r"설치\s*해\s*주",
        r"방문\s*설치",
    ),
    "WALL_MOUNT_INSTALLATION": (r"벽\s*걸이", r"벽걸이"),
    "INSTALLATION_FEE": (r"설치\s*비", r"설치[^\n]{0,12}(?:가격|비용|포함)"),
    "DELIVERY_SCHEDULE": (
        r"(?:배송|도착|설치)[^\n]{0,16}(?:언제|예정|일정|며칠|얼마나)",
        r"(?:언제|며칠|얼마나)[^\n]{0,16}(?:배송|도착|설치|받)",
        r"(?:받|수령)[^\n]{0,16}(?:언제|예정|며칠|얼마나|걸리)",
        r"주문[^\n]{0,20}(?:받|수령|걸리)",
        r"기다리(?:다|고|는|기)",
    ),
    "AFTER_SERVICE": (r"(?:a\s*/?\s*s|에이에스|서비스\s*센터)",),
    "PANEL_TYPE": (r"(?:패널|led|qled|oled)",),
    "POINT_PROMOTION": (r"(?:네이버\s*)?포인트", r"프로모션|이벤트"),
}
CONTEXT_ANCHOR_CONCEPTS = {
    "WALL_MOUNT_INSTALLATION",
    "AFTER_SERVICE",
    "PANEL_TYPE",
    "POINT_PROMOTION",
}


def normalize_learning_question(value: object) -> str:
    return " ".join(TOKEN.findall(str(value or "").lower()))


class SimilarAnswerService:
    def __init__(self, repository: LearningRepository) -> None:
        self.repository = repository
        self.privacy = LearningPrivacyService()

    @staticmethod
    def _similarity(left: str, right: str) -> float:
        left_tokens, right_tokens = set(TOKEN.findall(left)), set(TOKEN.findall(right))
        jaccard = len(left_tokens & right_tokens) / max(len(left_tokens | right_tokens), 1)
        sequence = SequenceMatcher(None, left, right).ratio()
        left_concepts = SimilarAnswerService._semantic_concepts(left)
        right_concepts = SimilarAnswerService._semantic_concepts(right)
        concept_overlap = len(left_concepts & right_concepts) / max(
            min(len(left_concepts), len(right_concepts)), 1
        )
        return 0.65 * jaccard + 0.35 * sequence + 0.18 * concept_overlap

    @staticmethod
    def _semantic_concepts(value: str) -> set[str]:
        return {
            concept
            for concept, patterns in SEMANTIC_CONCEPTS.items()
            if any(re.search(pattern, value, re.IGNORECASE) for pattern in patterns)
        }

    @staticmethod
    def _source_priority(item: dict[str, Any]) -> int:
        """Keep approved operational answers ahead of style-only legacy data."""
        source = str(item.get("learning_source") or "").upper()
        metadata = item.get("metadata_json")
        legacy_source = str(
            metadata.get("legacy_source") if isinstance(metadata, dict) else ""
        ).upper()
        source_origin = str(
            metadata.get("source_origin") if isinstance(metadata, dict) else ""
        ).upper()
        if source_origin == "HISTORICAL_PROMOTED":
            return 1
        if source == "AUTO_POST_CORRECTED":
            return 6
        if source == "APPROVED_EDITED":
            if source_origin == "COPILOT_CORRECTION":
                return 3
            return 5
        if source == "AUTO_POST_REVIEWED_NO_CHANGE":
            if isinstance(metadata, dict) and metadata.get("acceptance_mode") == "AUTO_OBSERVATION":
                return 2
            return 4
        if source == "APPROVED_UNEDITED":
            return 3
        if legacy_source == "LEGACY_RULE":
            return 2
        if legacy_source == "LEGACY_GPT":
            return 1
        return 0

    def search(
        self, question: str, *, store_code: str | None = None,
        intent: str | None = None, product_name: str | None = None,
        model_code: str | None = None, inquiry_type: str | None = None,
        product_id: str | None = None,
        product_fact_sensitive: bool = False,
        limit: int = 3, minimum_relevance: float = 0.24,
        candidate_pool: list[dict[str, Any]] | None = None,
        candidate_diagnostics: dict[str, int] | None = None,
    ) -> list[dict[str, Any]]:
        query = normalize_learning_question(self.privacy.mask(question))
        ranked: list[tuple[float, dict[str, Any]]] = []
        candidates = (
            candidate_pool
            if candidate_pool is not None
            else self.repository.candidates(store_code=store_code, limit=2000)
        )
        diagnostics = candidate_diagnostics or self.repository.candidate_diagnostics(
            store_code=store_code
        )
        rejection_counts = {
            "FILTERED_BY_PRODUCT": 0,
            "BELOW_SIMILARITY_THRESHOLD": 0,
            "CONTEXT_POLICY_REJECTED": 0,
        }
        type_mismatch_count = 0
        query_concepts = self._semantic_concepts(query)
        required_context = query_concepts & CONTEXT_ANCHOR_CONCEPTS
        for item in candidates:
            if inquiry_type and item.get("inquiry_type") != inquiry_type:
                # Inquiry type is intentionally a relevance signal, not a hard
                # equality filter. Taxonomies differ between old and new data.
                type_mismatch_count += 1
            if product_fact_sensitive:
                metadata = item.get("metadata_json")
                metadata = metadata if isinstance(metadata, dict) else {}
                if not metadata.get("human_verified") or not same_stable_product(
                    current_product_id=product_id,
                    candidate_product_id=item.get("source_product_id"),
                    current_model_code=model_code,
                    candidate_model_code=item.get("model_code"),
                ):
                    # Detailed answer bodies from another/unknown product or
                    # unverified examples are not safe facts. Global style
                    # aggregation below remains available.
                    rejection_counts["FILTERED_BY_PRODUCT"] += 1
                    continue
            candidate_concepts = self._semantic_concepts(
                str(item["question_normalized"])
            )
            if required_context and not required_context.issubset(candidate_concepts):
                rejection_counts["CONTEXT_POLICY_REJECTED"] += 1
                continue
            relevance = self._similarity(query, str(item["question_normalized"]))
            relevance += 0.10 if intent and item.get("intent") == intent else 0
            relevance += 0.08 if product_name and item.get("product_name") == product_name else 0
            relevance += 0.08 if model_code and item.get("model_code") == model_code else 0
            relevance += 0.04 if inquiry_type and item.get("inquiry_type") == inquiry_type else 0
            if relevance >= minimum_relevance:
                safe = dict(item)
                safe["relevance"] = round(relevance, 4)
                ranked.append((relevance, safe))
            else:
                rejection_counts["BELOW_SIMILARITY_THRESHOLD"] += 1
        ranked.sort(
            key=lambda pair: (
                pair[0], self._source_priority(pair[1]),
                pair[1]["rating"], pair[1]["created_at"],
            ),
            reverse=True,
        )
        selected = [item for _, item in ranked[: max(0, min(limit, 3))]]
        self.last_trace = {
            "query": query,
            "product": product_name,
            "inquiry_type": inquiry_type,
            "candidate_count": len(candidates),
            "active_candidates": diagnostics["active_candidates"],
            # Keep diagnostic logs bounded. These are the candidates that
            # actually passed relevance ranking, not every repository row.
            "candidate_ids": [int(item[1]["id"]) for item in ranked[:20]],
            "above_threshold_count": len(ranked),
            "selected_count": len(selected),
            "selected_learning_ids": [int(item["id"]) for item in selected],
            "minimum_relevance": minimum_relevance,
            "inquiry_type_mismatch_signal_count": type_mismatch_count,
            "rejection_counts": {
                "FILTERED_BY_VALIDITY": diagnostics["filtered_by_validity"],
                "REVOKED": diagnostics["revoked"],
                "NEGATIVE_EXCLUDED": diagnostics["negative_excluded"],
                **rejection_counts,
            },
        }
        self.repository.mark_used([int(item["id"]) for item in selected])
        return selected

    def context(self, question: str, **filters: Any) -> dict[str, Any]:
        results = self.search(question, **filters)
        approved, seller = [], []
        for item in results:
            payload = {
                "learning_example_id": int(item["id"]),
                "learning_source": item["learning_source"],
                "question": item["question_original_masked"],
                "answer": item["final_answer"],
                "rating": item["rating"],
                "relevance": item["relevance"],
                "source_origin": (
                    (item.get("metadata_json") or {}).get("source_origin")
                    if isinstance(item.get("metadata_json"), dict) else None
                ),
                "historical_case_id": (
                    (item.get("metadata_json") or {}).get("historical_case_id")
                    if isinstance(item.get("metadata_json"), dict) else None
                ),
                "source_product_id": item.get("source_product_id"),
            }
            (seller if item["style_only"] else approved).append(payload)
        features = Counter()
        lengths = []
        for item in self.repository.candidates(store_code=filters.get("store_code"), limit=100):
            if int(item["rating"]) < 4:
                continue
            style = item.get("style_features_json") or {}
            for key in ("greeting", "closing"):
                if style.get(key): features[(key, style[key])] += 1
            if style.get("average_sentence_length"): lengths.append(float(style["average_sentence_length"]))
        return {
            "similar_approved_answers": approved,
            "seller_style_examples": seller,
            "oje_style_rules": {
                "seller_examples_are_style_only": True,
                "facts_priority": ["PRODUCT_DB", "POLICY", "VALIDATOR", "TEMPLATE"],
                "typical_greeting": next((v for (k, v), _ in features.most_common() if k == "greeting"), ""),
                "typical_closing": next((v for (k, v), _ in features.most_common() if k == "closing"), ""),
                "average_sentence_length": round(sum(lengths) / len(lengths), 1) if lengths else None,
            },
            "learning_retrieval": dict(getattr(self, "last_trace", {})),
        }
