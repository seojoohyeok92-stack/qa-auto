from __future__ import annotations

import re
from collections import Counter
from difflib import SequenceMatcher
from typing import Any

from repositories.learning_repository import LearningRepository
from services.learning_privacy_service import LearningPrivacyService
from services.product_fact_guard import same_stable_product


TOKEN = re.compile(r"[가-힣A-Za-z0-9]{2,}")


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
        return 0.65 * jaccard + 0.35 * sequence

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
    ) -> list[dict[str, Any]]:
        query = normalize_learning_question(self.privacy.mask(question))
        ranked: list[tuple[float, dict[str, Any]]] = []
        for item in self.repository.candidates(store_code=store_code):
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
        ranked.sort(
            key=lambda pair: (
                self._source_priority(pair[1]), pair[0],
                pair[1]["rating"], pair[1]["created_at"],
            ),
            reverse=True,
        )
        selected = [item for _, item in ranked[: max(0, min(limit, 3))]]
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
        }
