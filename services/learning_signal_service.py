from __future__ import annotations

import hashlib
from typing import Any

from answer.answer_diff_classifier import (
    classify_answer_diff,
    classify_operator_note,
)
from answer.evidence_support import apply_answer_support
from answer.negative_correction import NegativeCorrection, parse_operator_memo
from answer.learning_signal import (
    ConfirmationStatus,
    FACTUAL_SIGNAL_KINDS,
    GUIDANCE_SIGNAL_KINDS,
    GenerationMode,
    OriginKind,
    SignalKind,
    facts_conflict,
    normalize_fact_scope,
    normalize_signal_kind,
)
from config import StructuredSignalAutoLearningSettings
from repositories.database import Database
from repositories.learning_feedback_repository import LearningFeedbackRepository
from repositories.learning_signal_repository import LearningSignalRepository
from services.learning_compatibility_service import (
    GENERIC_TOPICS,
    LearningCompatibilityService,
    extract_product_identity,
    profile_knowledge,
)
from services.learning_evidence_policy import (
    current_schedule_scope_reason,
    order_identifier_request_reason,
)
from services.learning_privacy_service import LearningPrivacyService
from services.similar_answer_service import (
    SimilarAnswerService,
    normalize_learning_question,
)


# Constraints are ranked on a lower bar than evidence, and the asymmetry is
# the point. Getting a marginal *fact* wrong publishes a wrong answer; getting
# a marginal *constraint* wrong makes the answer more careful than it needed to
# be. What stops an unrelated Negative from reaching the prompt is the
# compatibility gate -- product identity and topic scope, the same gate the
# Positive path uses -- not this number.
#
# 0.24 was measured too high for this corpus specifically: two operator memos
# about the same mistake are written in whatever words the operator reached
# for, so "상품권 신청했는데 확인해주세요" and "상품권 신청 얼마 전에 했는데
# 확인 부탁드려요" score 0.21 against each other while plainly being the same
# situation. Against the 30 legacy memos on the server this value changes which
# memos are reachable without letting any of them cross a topic boundary.
NEGATIVE_CORRECTION_RELEVANCE = 0.18


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
        self.feedback = LearningFeedbackRepository(database)
        self.privacy = LearningPrivacyService()
        self.compatibility = LearningCompatibilityService()

    @staticmethod
    def _source_key(*parts: object) -> str:
        return hashlib.sha256(
            "|".join(str(part or "") for part in parts).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _is_eligible(
        item: dict[str, Any], *, settings: StructuredSignalAutoLearningSettings,
    ) -> bool:
        """Whether a signal may be used as retrieval evidence/guidance right now.

        Dynamic, not cached: recomputed from the item's *current* row plus
        its live confirmation count every call, so a cancelled approval
        (which lowers ``live_confirmation_count`` via the live JOIN in
        ``LearningSignalRepository.candidates()``) is reflected immediately
        without touching the signal row itself.
        """

        if item.get("confirmation_status") in {"REJECTED", "SUPERSEDED"}:
            return False
        if item.get("generation_mode") != GenerationMode.AUTO_EXTRACTED.value:
            return True  # MANUAL: unchanged phase-3 behavior.
        if item.get("confirmation_status") == ConfirmationStatus.MANUALLY_PROMOTED.value:
            return True  # Human-in-the-loop override, section 13.
        if item.get("signal_kind") in {
            SignalKind.GOOD_PATTERN.value, SignalKind.BAD_PATTERN.value,
        }:
            # Non-factual guidance goes live after its first confirmation, but
            # revoking the last live source must remove its runtime influence.
            return int(item.get("live_confirmation_count") or 0) > 0
        return (
            settings.auto_verified_promotion_enabled
            and int(item.get("live_confirmation_count") or 0)
            >= settings.min_confirmations_for_promotion
        )

    @staticmethod
    def _normalized_identity_key(
        *,
        store_code: object,
        signal_kind: SignalKind,
        scope: str,
        identity: Any,
        topics: tuple[str, ...],
        content_text: str,
    ) -> str:
        """Group confirmations of the same essential claim onto one signal.

        Anchored on product identity (narrowest available field first) so a
        MODEL-scoped claim about one product never merges with the same
        text stated about a different product (section 8's scope safety
        applies here too, not just at retrieval time).
        """

        anchor = (
            identity.model_code or identity.product_id or identity.family
            or identity.category or "GLOBAL"
        )
        normalized_content = normalize_learning_question(content_text)
        return hashlib.sha256(
            "|".join(
                str(part or "")
                for part in (
                    store_code, signal_kind.value, scope, anchor,
                    "+".join(sorted(topics)), normalized_content,
                )
            ).encode("utf-8")
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

    def negative_corrections(
        self,
        question: str,
        *,
        store_code: str | None = None,
        product_name: str | None = None,
        model_code: str | None = None,
        product_id: str | None = None,
        option_name: str | None = None,
        semantic_goal: dict[str, Any] | None = None,
        limit: int = 2,
        minimum_relevance: float = NEGATIVE_CORRECTION_RELEVANCE,
    ) -> dict[str, Any]:
        """Correction knowledge from Negative memos that predate the signal table.

        Same shape as the guidance the signal path already produces, ranked by
        the same compatibility gate, and deliberately *not* factual evidence:
        a Negative memo says which claim was wrong and what to say instead, it
        does not license a sub-question to become answerable on its own. That
        keeps the existing conflict/coverage safety exactly where it is --
        nothing here can flip a NO_RELIABLE_SOURCE into an answer.

        A Negative with no memo never appears. There is nothing to read, and
        inventing a reason it was rejected is the failure this whole path
        exists to avoid; those rows keep their existing exclusion behaviour
        and nothing else.
        """

        semantic_goal = semantic_goal or {}
        query_variants = self._query_variants(question, semantic_goal)
        if not query_variants:
            return {"selected": [], "trace": {"candidate_count": 0}}
        order_evidence_required = semantic_goal.get("order_evidence_required")
        current_product = extract_product_identity(
            product_id=product_id, product_name=product_name,
            model_code=model_code, option=option_name,
        )
        candidates = self.feedback.legacy_memo_candidates(store_code=store_code)
        ranked: list[tuple[float, dict[str, Any]]] = []
        rejection_counts: dict[str, int] = {}

        def reject(reason: str) -> None:
            rejection_counts[reason] = rejection_counts.get(reason, 0) + 1

        for item in candidates:
            parsed = parse_operator_memo(
                item.get("correction_note"),
                reason_code=item.get("correction_reason"),
                feedback_id=item.get("id"),
            )
            if parsed is None:
                reject("NO_OPERATOR_MEMO")
                continue
            masked = self._mask_correction(parsed)
            candidate_product = extract_product_identity(
                product_id=item.get("source_product_id"),
                product_name=item.get("source_product_name"),
                model_code=None,
                option=item.get("source_option_name"),
            )
            question_masked = self.privacy.mask(item.get("question_masked") or "")
            compatibility = self.compatibility.evaluate(
                current_question=question,
                current_product=current_product,
                candidate_question=question_masked,
                candidate_answer=masked.guidance_text or masked.source_memo,
                candidate_product=candidate_product,
                candidate_metadata={},
                authority="VERIFIED_SIGNAL",
            )
            if not compatibility.eligible:
                reject(str(compatibility.reject_reason or "COMPATIBILITY_REJECTED"))
                continue
            # Section 11's rule, applied strictly. The generic gate lets a
            # candidate through on UNCERTAIN when the *query* carries no
            # explicit topic, which is right for evidence (a policy answer
            # should still be reachable) and wrong here: measured against the
            # 30 legacy memos on the server, "포토리뷰 네이버페이 2만원은 언제
            # 받나요?" pulled in two 설치예정일 memos that way. A correction has
            # to share a topic with the question it constrains.
            candidate_topics = {
                topic for topic in compatibility.candidate_topics
                if topic not in GENERIC_TOPICS
            }
            if candidate_topics and not (
                candidate_topics & set(compatibility.query_topics)
            ):
                reject("TOPIC_SCOPE_MISMATCH")
                continue
            # A memo that says "ask for the order number", or one about a
            # 설치예정일 that has not been booked yet, is right about the
            # inquiry it was written for and wrong about a customer who has
            # not ordered. The same scope rule the Positive path applies.
            if order_evidence_required is False and any(
                order_identifier_request_reason(text) is not None
                or current_schedule_scope_reason(text) is not None
                for text in (
                    *masked.corrections, *masked.good_patterns,
                    *masked.bad_patterns,
                )
            ):
                reject("ORDER_SCOPE_MISMATCH")
                continue
            relevance = max(
                SimilarAnswerService._similarity(
                    variant, normalize_learning_question(question_masked)
                )
                for variant in query_variants
            )
            relevance += compatibility.score_adjustment
            support = max(
                apply_answer_support(
                    relevance, variant, masked.guidance_text
                )[1]
                for variant in query_variants
            )
            if relevance < minimum_relevance:
                reject("BELOW_SIMILARITY_THRESHOLD")
                continue
            ranked.append((relevance, {
                **masked.to_dict(),
                "relevance": round(relevance, 4),
                "answer_support": round(support, 4),
                "source": "LEGACY_NEGATIVE_MEMO",
                "learning_feedback_id": int(item["id"]),
                "product_scope": compatibility.product_match,
                "topics": list(compatibility.candidate_topics),
                "compatibility": compatibility.to_dict(),
            }))
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        selected = [item for _, item in ranked[:max(0, limit)]]
        return {
            "selected": selected,
            "trace": {
                "candidate_count": len(candidates),
                "above_threshold_count": len(ranked),
                "selected_count": len(selected),
                "selected_feedback_ids": [
                    int(item["feedback_id"]) for item in selected
                ],
                "not_selected_feedback_ids": [
                    int(item["feedback_id"]) for _, item in ranked[len(selected):]
                ],
                "rejection_counts": rejection_counts,
            },
        }

    def _mask_correction(self, parsed: NegativeCorrection) -> NegativeCorrection:
        """The same correction with every part run through the privacy mask.

        Operator memos are internal text and were never masked on the way in.
        The stored row keeps the operator's original wording; only this
        runtime copy, the one that can reach a prompt, is masked.
        """

        return NegativeCorrection(
            feedback_id=parsed.feedback_id,
            source_memo=self.privacy.mask(parsed.source_memo),
            reason_code=parsed.reason_code,
            bad_patterns=tuple(
                self.privacy.mask(text) for text in parsed.bad_patterns
            ),
            corrections=tuple(
                self.privacy.mask(text) for text in parsed.corrections
            ),
            good_patterns=tuple(
                self.privacy.mask(text) for text in parsed.good_patterns
            ),
        )

    def _query_variants(
        self, question: str, semantic_goal: dict[str, Any],
    ) -> list[str]:
        """The same query representations the Positive path ranks against.

        Retrieval for Negative knowledge has to key on the same understanding
        as retrieval for Positive knowledge, or the two answer different
        questions about the same inquiry.
        """

        return list(dict.fromkeys(
            value for value in (
                normalize_learning_question(self.privacy.mask(question)),
                normalize_learning_question(self.privacy.mask(
                    str(semantic_goal.get("requested_information") or "")
                )),
                normalize_learning_question(self.privacy.mask(
                    str(semantic_goal.get("atomic_question") or "")
                )),
            ) if value
        ))

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
        semantic_goal: dict[str, Any] | None = None,
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
        # The same query representations the Positive path uses. A signal
        # written in different words for the same atomic question was
        # previously reachable only through the raw sub-question text, so
        # Positive and Negative retrieval could disagree about what the
        # customer asked. ``query`` stays first, so a caller that passes no
        # semantic goal ranks exactly as it did before.
        query_variants = self._query_variants(question, semantic_goal or {})
        current_product = extract_product_identity(
            product_id=product_id, product_name=product_name,
            model_code=model_code, option=option_name,
        )
        settings = StructuredSignalAutoLearningSettings.from_environment()
        candidates = [
            item
            for item in self.repository.candidates(
                store_code=store_code,
                signal_kinds=tuple(
                    kind.value
                    for kind in (*FACTUAL_SIGNAL_KINDS, *GUIDANCE_SIGNAL_KINDS)
                ),
            )
            if self._is_eligible(item, settings=settings)
        ]
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
            candidate_question = normalize_learning_question(
                item.get("question_masked")
            )
            relevance = max(
                SimilarAnswerService._similarity(variant, candidate_question)
                for variant in query_variants
            )
            relevance += compatibility.score_adjustment
            relevance, support = max(
                (
                    apply_answer_support(
                        relevance, variant, item.get("content_text")
                    )
                    for variant in query_variants
                ),
                key=lambda pair: pair[0],
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
                "query_variants": query_variants,
                "candidate_count": len(candidates),
                "above_threshold_count": len(ranked),
                "rejection_counts": rejection_counts,
                "conflict_count": len(conflicts),
            },
        }

    def auto_extract_and_capture(
        self,
        *,
        origin_kind: OriginKind,
        inquiry: dict[str, Any] | None,
        question: str,
        source_authority: str,
        program_answer: str = "",
        final_answer: str = "",
        operator_note: str = "",
        learning_example_id: int | None = None,
        learning_feedback_id: int | None = None,
        approval_history_id: int | None = None,
        product_name: str | None = None,
        model_code: str | None = None,
        product_id: str | None = None,
        option_name: str | None = None,
        actor: str = "SYSTEM_AUTO_EXTRACTION",
    ) -> list[dict[str, Any]]:
        """Extract, classify, and safely persist signals from a staff edit.

        Never called for AI-only content with no human/Naver-confirmed
        provenance -- callers only reach this from capture paths that
        already represent a genuine staff edit and/or confirmed Naver post
        (see 4th-phase report, self-loop prevention).  A CORRECTION/
        VERIFIED_FACT candidate is *never* immediately usable as evidence
        from a single occurrence: it only becomes eligible once repeated,
        independent, conflict-free confirmation crosses the configured
        threshold with promotion enabled, or an operator manually confirms
        it via Dashboard.  GOOD_PATTERN/BAD_PATTERN (non-factual guidance)
        go live immediately since misclassifying a style pattern carries
        far less risk than fabricating a fact.
        """

        settings = StructuredSignalAutoLearningSettings.from_environment()
        if not settings.enabled:
            return []
        inquiry = inquiry or {}
        has_order_id = bool(str(inquiry.get("order_id") or "").strip())
        candidates = list(
            classify_answer_diff(
                question=question,
                program_answer=program_answer,
                final_answer=final_answer,
                has_order_id=has_order_id,
            )
        )
        note_candidate = classify_operator_note(
            question=question, note_text=operator_note, has_order_id=has_order_id,
        )
        if note_candidate is not None:
            candidates.append(note_candidate)
        if not candidates:
            return []

        inquiry_id = inquiry.get("id") or inquiry.get("inquiry_id")
        identity = extract_product_identity(
            product_id=product_id or inquiry.get("product_id"),
            product_name=product_name or inquiry.get("product_name"),
            model_code=model_code,
            option=option_name or inquiry.get("option_name"),
        )
        masked_question = self.privacy.mask(question) if question else None
        saved: list[dict[str, Any]] = []
        for candidate in candidates:
            masked_text = self.privacy.mask(candidate.content_text)
            if not masked_text:
                continue
            profile = profile_knowledge(
                question=masked_question or "", answer=masked_text, identity=identity,
            )
            normalized_key = self._normalized_identity_key(
                store_code=inquiry.get("store_code"),
                signal_kind=candidate.signal_kind,
                scope=profile.scope,
                identity=identity,
                topics=profile.topics,
                content_text=masked_text,
            )
            existing = self.repository.find_by_normalized_identity(normalized_key)
            target = existing[0] if existing else None
            if target is not None and target.get("confirmation_status") == "REJECTED":
                # An operator already rejected this exact claim -- repeated
                # auto-extraction must never resurrect it.
                continue
            if target is None:
                source_key = self._source_key(
                    "AUTO_EXTRACTED", normalized_key,
                    inquiry_id, learning_example_id, learning_feedback_id,
                )
                target = self.repository.upsert({
                    "source_key": source_key,
                    "signal_kind": candidate.signal_kind.value,
                    "origin_kind": origin_kind.value,
                    "learning_feedback_id": learning_feedback_id,
                    "learning_example_id": learning_example_id,
                    "historical_case_id": None,
                    "inquiry_id": int(inquiry_id) if inquiry_id is not None else None,
                    "store_code": inquiry.get("store_code"),
                    "question_masked": masked_question,
                    "content_text": masked_text,
                    "product_scope": profile.scope,
                    "topics_json": list(profile.topics),
                    "product_identity_json": identity.to_dict(),
                    "metadata_json": {
                        "actor": str(actor or "SYSTEM_AUTO_EXTRACTION"),
                        "diff_categories": [
                            item.value for item in candidate.diff_categories
                        ],
                        "rationale": candidate.rationale,
                    },
                    "active": True,
                    "actor": str(actor or "SYSTEM_AUTO_EXTRACTION"),
                    "generation_mode": GenerationMode.AUTO_EXTRACTED.value,
                    "confirmation_status": ConfirmationStatus.ACTIVE.value,
                    "normalized_identity_key": normalized_key,
                    "diff_category": (
                        candidate.diff_categories[0].value
                        if candidate.diff_categories else None
                    ),
                })
            self.repository.record_confirmation(
                learning_signal_id=int(target["id"]),
                inquiry_id=int(inquiry_id) if inquiry_id is not None else None,
                learning_example_id=learning_example_id,
                learning_feedback_id=learning_feedback_id,
                approval_history_id=approval_history_id,
                source_authority=source_authority,
            )
            saved.append(target)
        return saved
