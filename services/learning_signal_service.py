from __future__ import annotations

import hashlib
from typing import Any

from answer.evidence_support import apply_answer_support
from answer.learning_signal import (
    FACTUAL_SIGNAL_KINDS,
    GUIDANCE_SIGNAL_KINDS,
    OriginKind,
    SignalKind,
    facts_conflict,
    normalize_fact_scope,
    normalize_signal_kind,
)
from repositories.database import Database
from repositories.learning_signal_repository import LearningSignalRepository
from services.learning_compatibility_service import (
    LearningCompatibilityService,
    extract_product_identity,
    profile_knowledge,
)
from services.learning_privacy_service import LearningPrivacyService
from services.similar_answer_service import (
    SimilarAnswerService,
    normalize_learning_question,
)


class LearningSignalService:
    """Turns an operator memo into a retrievable Structured Learning Signal.

    A memo only becomes a signal (and only becomes retrievable) when the
    operator explicitly classifies it as GOOD_PATTERN / BAD_PATTERN /
    CORRECTION / VERIFIED_FACT and supplies distinct content for it.  A
    plain evaluation reason (the default) stays exactly as it is today --
    recorded on ``learning_feedback``/``learning_examples`` for audit, never
    promoted into a retrievable signal.  This keeps every inquiry with no
    such memo byte-for-byte unaffected (Acceptance Case J).
    """

    def __init__(self, database: Database) -> None:
        self.database = database
        self.repository = LearningSignalRepository(database)
        self.privacy = LearningPrivacyService()
        self.compatibility = LearningCompatibilityService()

    @staticmethod
    def _source_key(*parts: object) -> str:
        return hashlib.sha256(
            "|".join(str(part or "") for part in parts).encode("utf-8")
        ).hexdigest()

    def capture(
        self,
        *,
        origin_kind: str | OriginKind,
        signal_kind: str | SignalKind,
        content_text: str = "",
        inquiry: dict[str, Any] | None = None,
        learning_feedback_id: int | None = None,
        learning_example_id: int | None = None,
        historical_case_id: int | None = None,
        question: str = "",
        product_name: str | None = None,
        model_code: str | None = None,
        product_id: str | None = None,
        option_name: str | None = None,
        fact_scope: str | None = None,
        actor: str = "직원",
    ) -> dict[str, Any] | None:
        kind = normalize_signal_kind(signal_kind)
        text = str(content_text or "").strip()
        if kind is SignalKind.REASON or not text:
            # No structured signal to retrieve later -- the plain reason/note
            # already lives on learning_feedback / learning_examples.
            return None
        origin = (
            origin_kind
            if isinstance(origin_kind, OriginKind)
            else OriginKind(str(origin_kind))
        )
        inquiry = inquiry or {}
        inquiry_id = inquiry.get("id") or inquiry.get("inquiry_id")
        masked_question = self.privacy.mask(question) if question else None
        masked_text = self.privacy.mask(text)
        identity = extract_product_identity(
            product_id=product_id or inquiry.get("product_id"),
            product_name=product_name or inquiry.get("product_name"),
            model_code=model_code,
            option=option_name or inquiry.get("option_name"),
        )
        profile = profile_knowledge(
            question=masked_question or "",
            answer=masked_text,
            identity=identity,
        )
        scope = normalize_fact_scope(fact_scope) or profile.scope
        source_key = self._source_key(
            origin.value, kind.value, inquiry_id, learning_feedback_id,
            learning_example_id, historical_case_id, masked_text,
        )
        signal = {
            "source_key": source_key,
            "signal_kind": kind.value,
            "origin_kind": origin.value,
            "learning_feedback_id": learning_feedback_id,
            "learning_example_id": learning_example_id,
            "historical_case_id": historical_case_id,
            "inquiry_id": int(inquiry_id) if inquiry_id is not None else None,
            "store_code": inquiry.get("store_code"),
            "question_masked": masked_question,
            "content_text": masked_text,
            "product_scope": scope,
            "topics_json": list(profile.topics),
            "product_identity_json": identity.to_dict(),
            "metadata_json": {"actor": str(actor or "직원")},
            "active": True,
            "actor": str(actor or "직원"),
        }
        return self.repository.upsert(signal)

    def retrieve(
        self,
        question: str,
        *,
        store_code: str | None = None,
        product_name: str | None = None,
        model_code: str | None = None,
        product_id: str | None = None,
        option_name: str | None = None,
        inquiry_type: str | None = None,
        limit: int = 3,
        minimum_relevance: float = 0.24,
    ) -> dict[str, Any]:
        """Rank ACTIVE signals compatible with this question/product/topic.

        Returns FACTUAL signals (VERIFIED_FACT/CORRECTION) split into a
        usable ``verified_facts``/``corrections`` bucket and a ``conflicts``
        bucket for scope-matched signals whose polarity flatly disagrees --
        those are withheld from evidence so GPT is never asked to pick a
        side (Acceptance Case F).  GUIDANCE signals (GOOD_PATTERN/
        BAD_PATTERN) are ranked the same way but never treated as evidence.
        """

        query = normalize_learning_question(self.privacy.mask(question))
        current_product = extract_product_identity(
            product_id=product_id, product_name=product_name,
            model_code=model_code, option=option_name,
        )
        candidates = self.repository.candidates(
            store_code=store_code,
            signal_kinds=tuple(
                kind.value for kind in (*FACTUAL_SIGNAL_KINDS, *GUIDANCE_SIGNAL_KINDS)
            ),
        )
        ranked: list[tuple[float, dict[str, Any]]] = []
        rejection_counts: dict[str, int] = {}
        for item in candidates:
            # The signal's own product identity (captured once, at write
            # time) is authoritative. The join to `inquiries` only fills a
            # gap for legacy/historical-only signals that never carried an
            # inquiry_id -- it must never override a value the signal
            # already has.
            stored_identity = item.get("product_identity_json")
            stored_identity = stored_identity if isinstance(stored_identity, dict) else {}
            candidate_product = extract_product_identity(
                product_id=stored_identity.get("product_id")
                or item.get("source_product_id"),
                product_name=stored_identity.get("product_name")
                or item.get("source_product_name"),
                model_code=stored_identity.get("model_code"),
                option=stored_identity.get("option")
                or item.get("source_option_name"),
                metadata={"product_scope": item.get("product_scope")},
            )
            compatibility = self.compatibility.evaluate(
                current_question=question,
                current_product=current_product,
                candidate_question=item.get("question_masked"),
                candidate_answer=item.get("content_text"),
                candidate_product=candidate_product,
                candidate_metadata={"product_scope": item.get("product_scope")},
                authority="VERIFIED_SIGNAL",
            )
            if not compatibility.eligible:
                reason = str(compatibility.reject_reason or "COMPATIBILITY_REJECTED")
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
                continue
            relevance = SimilarAnswerService._similarity(
                query, normalize_learning_question(item.get("question_masked"))
            )
            relevance += compatibility.score_adjustment
            relevance, support = apply_answer_support(
                relevance, query, item.get("content_text")
            )
            if relevance < minimum_relevance:
                rejection_counts["BELOW_SIMILARITY_THRESHOLD"] = (
                    rejection_counts.get("BELOW_SIMILARITY_THRESHOLD", 0) + 1
                )
                continue
            safe = dict(item)
            safe["relevance"] = round(relevance, 4)
            safe["answer_support"] = round(support, 4)
            safe["compatibility"] = compatibility.to_dict()
            ranked.append((relevance, safe))
        ranked.sort(key=lambda pair: pair[0], reverse=True)

        def bucket(kind: SignalKind) -> list[dict[str, Any]]:
            return [item for _, item in ranked if item["signal_kind"] == kind.value]

        verified_facts = bucket(SignalKind.VERIFIED_FACT)
        corrections = bucket(SignalKind.CORRECTION)
        good_patterns = bucket(SignalKind.GOOD_PATTERN)[:limit]
        bad_patterns = bucket(SignalKind.BAD_PATTERN)[:limit]

        factual = verified_facts + corrections
        conflicts: list[dict[str, Any]] = []
        conflicting_ids: set[int] = set()
        for i, left in enumerate(factual):
            for right in factual[i + 1:]:
                if int(left["id"]) == int(right["id"]):
                    continue
                if left.get("product_scope") != right.get("product_scope"):
                    continue
                if not set(left.get("topics_json") or []) & set(
                    right.get("topics_json") or []
                ):
                    continue
                if facts_conflict(left.get("content_text"), right.get("content_text")):
                    conflicting_ids.add(int(left["id"]))
                    conflicting_ids.add(int(right["id"]))
                    conflicts.append(
                        {
                            "left_signal_id": int(left["id"]),
                            "right_signal_id": int(right["id"]),
                            "left_text": left.get("content_text"),
                            "right_text": right.get("content_text"),
                            "product_scope": left.get("product_scope"),
                        }
                    )
        verified_facts = [
            item for item in verified_facts if int(item["id"]) not in conflicting_ids
        ][:limit]
        corrections = [
            item for item in corrections if int(item["id"]) not in conflicting_ids
        ][:limit]
        conflicting_signals = [
            item for item in factual if int(item["id"]) in conflicting_ids
        ]

        return {
            "verified_facts": verified_facts,
            "corrections": corrections,
            "good_patterns": good_patterns,
            "bad_patterns": bad_patterns,
            "conflicts": conflicts,
            "conflicting_signals": conflicting_signals,
            "trace": {
                "query": query,
                "candidate_count": len(candidates),
                "above_threshold_count": len(ranked),
                "rejection_counts": rejection_counts,
                "conflict_count": len(conflicts),
            },
        }
