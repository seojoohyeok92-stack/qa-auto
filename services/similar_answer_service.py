from __future__ import annotations

import re
from collections import Counter
from difflib import SequenceMatcher
from typing import Any

from answer.evidence_support import apply_answer_support
from repositories.learning_repository import LearningRepository
from services.learning_compatibility_service import (
    LearningCompatibilityService,
    extract_product_identity,
)
from services.learning_evidence_policy import (
    LEARNING_AUTHORITY,
    classify_provenance,
    contamination_reason,
    estimation_reason,
)
from services.learning_privacy_service import LearningPrivacyService


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


# Authority may only settle what relevance cannot. Adjacent candidates in the
# live corpus are separated by a median of 0.043 and a lower quartile of 0.018,
# so 0.01 sits below the point where a gap means anything -- inside it the two
# answers are equally on-point and which one to trust is a question about who
# wrote them. Banding rather than a pairwise epsilon comparison because sorting
# needs a total order; the cost is that a near-tie straddling a band edge falls
# back to pure relevance, which errs toward leaving the existing order alone.
AUTHORITY_TIE_BAND = 0.01


def _band(value: float) -> int:
    return int(value / AUTHORITY_TIE_BAND)


def _ranking_key(relevance: float, item: dict[str, Any], priority: int):
    """Relevance, then whether the answer is on point, then who wrote it.

    Answer support sits above authority deliberately. ``relevance`` already
    folds support into a single number, so two candidates can land in the same
    band with completely different composition -- one whose question looks
    similar but whose answer addresses nothing the customer asked, and one that
    actually covers part of it. Ranking the band by authority alone promoted
    the first kind: measured over 275 live questions it moved ten answers into
    the factual slot whose support was 0.058 on average, six of them exactly
    zero. Provenance is a reason to prefer an answer that already answers the
    question, never a reason to prefer one that does not.
    """

    support = _band(float(item.get("answer_support") or 0.0))
    return (
        _band(relevance), support,
        # Inside the band, an answer that covers nothing the customer asked has
        # no claim on a slot for being well sourced -- without this a verified
        # answer with zero support displaced a tone reference and took an
        # evidence slot it could not fill.
        priority if support else 0,
        # Exactly equal relevance is a different situation: nothing else is
        # left to separate the candidates, and authority has always decided it.
        # So it is applied again below the raw score, where it can only order
        # a true tie and can no longer overturn a real difference.
        relevance, priority, item["rating"], item["created_at"],
    )


def _normalized_answer(value: object) -> str:
    """Identity of an answer's *content*, for spotting repeat copies.

    Whitespace only, deliberately: two rows are the same evidence when the
    sentences are the same, and normalising further would start merging
    answers that differ in a number.
    """

    return re.sub(r"\s+", "", str(value or ""))


class SimilarAnswerService:
    def __init__(self, repository: LearningRepository) -> None:
        self.repository = repository
        self.privacy = LearningPrivacyService()
        self.compatibility = LearningCompatibilityService()

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
        # Provenance first. ``human_verified`` used to short-circuit to 10 here,
        # which put every bulk-verified seller answer above the handful a
        # member of staff actually edited. It is now read as a signal *within*
        # a class, not as a class of its own.
        authority = LEARNING_AUTHORITY.get(classify_provenance(item))
        if authority is not None:
            return authority
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
        option_name: str | None = None,
        product_fact_sensitive: bool = False,
        limit: int = 3, minimum_relevance: float = 0.24,
        candidate_pool: list[dict[str, Any]] | None = None,
        candidate_diagnostics: dict[str, int] | None = None,
        semantic_goal: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        query = normalize_learning_question(self.privacy.mask(question))
        semantic_goal = semantic_goal or {}
        requested_information = str(
            semantic_goal.get("requested_information") or ""
        ).strip()
        atomic_question = str(semantic_goal.get("atomic_question") or "").strip()
        # These are alternative representations of the *same* customer goal,
        # not independent keyword expansions.  Ranking uses the best match,
        # allowing a verified older Learning written in different words to be
        # found without letting unrelated topic neighbours accumulate score.
        query_variants = list(dict.fromkeys(
            value for value in (
                query,
                normalize_learning_question(self.privacy.mask(requested_information)),
                normalize_learning_question(self.privacy.mask(atomic_question)),
            ) if value
        ))
        required_action = str(semantic_goal.get("customer_goal") or "").upper()
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
            "BELOW_SIMILARITY_THRESHOLD": 0,
            "CONTEXT_POLICY_REJECTED": 0,
            "REDACTION_TOKEN_CONTAMINATED": 0,
            "SEMANTIC_GOAL_MISMATCH": 0,
        }
        compatibility_diagnostics: list[dict[str, Any]] = []
        type_mismatch_count = 0
        query_concepts = self._semantic_concepts(query)
        required_context = query_concepts & CONTEXT_ANCHOR_CONCEPTS
        current_product = extract_product_identity(
            product_id=product_id,
            product_name=product_name,
            model_code=model_code,
            option=option_name,
        )
        for item in candidates:
            # A stored answer containing "<masked-phone>" is a record that
            # something was removed, not a sentence anyone should read or
            # copy. Showing it to the model is how the token reached a
            # customer once already (see INTERNAL_REDACTION_TOKENS), so it is
            # dropped here rather than merely demoted -- the row itself stays
            # in the database, untouched and auditable.
            if contamination_reason(item.get("final_answer")) is not None:
                rejection_counts["REDACTION_TOKEN_CONTAMINATED"] += 1
                continue
            if inquiry_type and item.get("inquiry_type") != inquiry_type:
                # Inquiry type is intentionally a relevance signal, not a hard
                # equality filter. Taxonomies differ between old and new data.
                type_mismatch_count += 1
            metadata = item.get("metadata_json")
            metadata = metadata if isinstance(metadata, dict) else {}
            candidate_semantic = metadata.get("semantic_analysis") or metadata.get("semantic") or {}
            if not isinstance(candidate_semantic, dict):
                candidate_semantic = {}
            candidate_action = str(
                candidate_semantic.get("primary_action")
                or metadata.get("semantic_action")
                or metadata.get("customer_goal")
                or ""
            ).upper()
            # A Learning explicitly understood as a different action is never
            # evidence for this one.  Missing legacy metadata remains
            # retrievable, but must still pass the answer-support sufficiency
            # gate below; this avoids throwing away valid old Positive Learning.
            if required_action and candidate_action and candidate_action != required_action:
                rejection_counts["SEMANTIC_GOAL_MISMATCH"] += 1
                continue
            human_verified = metadata.get("human_verified") is True
            candidate_product = extract_product_identity(
                product_id=item.get("source_product_id"),
                product_name=(
                    item.get("source_product_name") or item.get("product_name")
                ),
                model_code=item.get("model_code"),
                option=item.get("source_option_name"),
                metadata=metadata,
            )
            compatibility = self.compatibility.evaluate(
                current_question=question,
                current_product=current_product,
                candidate_question=item.get("question_original_masked")
                or item.get("question_normalized"),
                candidate_answer=item.get("final_answer"),
                candidate_product=candidate_product,
                candidate_metadata=metadata,
                authority="APPROVED" if human_verified else "AUTO",
            )
            diagnostic = {
                "learning_id": int(item["id"]),
                "lifecycle": "APPROVED" if human_verified else "AUTO",
                "human_verified": human_verified,
                **compatibility.to_dict(),
            }
            if not compatibility.eligible:
                reason = str(compatibility.reject_reason or "COMPATIBILITY_REJECTED")
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
                diagnostic.update({
                    "eligible": False,
                    "similarity": None,
                    "reject_reason": reason,
                })
                if len(compatibility_diagnostics) < 40:
                    compatibility_diagnostics.append(diagnostic)
                continue
            candidate_concepts = self._semantic_concepts(
                str(item["question_normalized"])
            )
            if required_context and not required_context.issubset(candidate_concepts):
                rejection_counts["CONTEXT_POLICY_REJECTED"] += 1
                continue
            candidate_question = str(item["question_normalized"])
            relevance = max(
                self._similarity(variant, candidate_question)
                for variant in query_variants
            )
            relevance += 0.10 if intent and item.get("intent") == intent else 0
            relevance += 0.08 if product_name and item.get("product_name") == product_name else 0
            relevance += 0.08 if model_code and item.get("model_code") == model_code else 0
            relevance += 0.04 if inquiry_type and item.get("inquiry_type") == inquiry_type else 0
            relevance += compatibility.score_adjustment
            # Answer-Support Re-ranking: a candidate's *question* similarity
            # is a retrieval signal, not proof its *answer* supports this
            # query.  Boost (never replace) the score by how much of the
            # query's content the candidate's own answer actually covers.
            answer_support = max(
                apply_answer_support(relevance, variant, item.get("final_answer"))[1]
                for variant in query_variants
            )
            relevance += 0.6 * answer_support
            if required_action and candidate_action == required_action:
                relevance += 0.12
            if relevance >= minimum_relevance:
                safe = dict(item)
                safe["relevance"] = round(relevance, 4)
                safe["answer_support"] = round(answer_support, 4)
                safe["compatibility"] = compatibility.to_dict()
                safe["semantic_goal"] = {
                    "required_action": required_action or None,
                    "candidate_action": candidate_action or None,
                    "compatible": not candidate_action or candidate_action == required_action,
                }
                ranked.append((relevance, safe))
                diagnostic.update({
                    "eligible": True,
                    "similarity": round(relevance, 4),
                    "reject_reason": None,
                })
            else:
                rejection_counts["BELOW_SIMILARITY_THRESHOLD"] += 1
                diagnostic.update({
                    "eligible": False,
                    "similarity": round(relevance, 4),
                    "reject_reason": "BELOW_SIMILARITY_THRESHOLD",
                })
            if len(compatibility_diagnostics) < 40:
                compatibility_diagnostics.append(diagnostic)
        ranked.sort(
            key=lambda pair: _ranking_key(
                pair[0], pair[1], self._source_priority(pair[1])
            ),
            reverse=True,
        )
        selected = [item for _, item in ranked[: max(0, min(limit, 3))]]
        self.last_trace = {
            "query": query,
            "query_variants": query_variants,
            "semantic_goal": {
                "customer_goal": required_action or None,
                "requested_information": requested_information or None,
                "atomic_question": atomic_question or None,
            },
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
            "candidate_diagnostics": compatibility_diagnostics,
        }
        return selected

    def context(self, question: str, **filters: Any) -> dict[str, Any]:
        results = self.search(question, **filters)
        approved, seller = [], []
        factual_seen: set[str] = set()
        demotion_counts: dict[str, int] = {
            "HEDGED_FACTUAL_DEMOTED": 0,
            "DUPLICATE_EVIDENCE": 0,
        }
        for item in results:
            payload = {
                "learning_example_id": int(item["id"]),
                "learning_source": item["learning_source"],
                "question": item["question_original_masked"],
                "answer": item["final_answer"],
                "rating": item["rating"],
                "relevance": item["relevance"],
                "answer_support": item.get("answer_support", 0.0),
                "source_origin": (
                    (item.get("metadata_json") or {}).get("source_origin")
                    if isinstance(item.get("metadata_json"), dict) else None
                ),
                "historical_case_id": (
                    (item.get("metadata_json") or {}).get("historical_case_id")
                    if isinstance(item.get("metadata_json"), dict) else None
                ),
                "source_product_id": item.get("source_product_id"),
                "authority": (
                    "APPROVED"
                    if (item.get("metadata_json") or {}).get("human_verified") is True
                    else "AUTO"
                ),
                "compatibility": item.get("compatibility") or {},
                "semantic_goal": item.get("semantic_goal") or {},
            }
            # An answer that says outright it is unsure -- "8월 둘째 주 이후로
            # 예상됩니다", "전날 연락드릴 것으로 보입니다" -- was reaching the
            # model labelled APPROVED, and 10 such rows in the live store carry
            # rating 5 and HUMAN_VERIFIED_NAVER_POSTED. Whoever approved them
            # approved a guess; the authority is real and the certainty is not,
            # so the sentence must not be offered as grounds for a definite
            # claim.
            #
            # ``estimation_reason`` and not the broader hedge or
            # ``usable_as_factual_evidence`` checks. The latter asks the
            # validator's question -- does this commit to a polarity or a
            # quantity it could weigh against another claim -- and answers no
            # for plain declaratives like "이 제품은 LED 패널을 사용합니다".
            # ``hedge_reason`` is wider still and counts a deferral, which
            # would have rejected the store's own new-order delivery policy
            # over the words "결제 확인 후". What must not be offered as proof
            # is a guess, and that is what this asks.
            #
            # Demoted rather than dropped. The sentence is still written in the
            # seller's voice, so it keeps its value as a tone reference; what
            # it loses is the claim to prove something.
            factual = not item["style_only"] and (
                estimation_reason(item["final_answer"]) is None
            )
            if factual:
                normalized = _normalized_answer(item["final_answer"])
                if normalized in factual_seen:
                    # The same sentence stored many times is one fact, not
                    # several independent corroborations of it. The live store
                    # holds 122 repeat copies; letting them fill separate slots
                    # both crowds out other evidence and makes a single claim
                    # look independently confirmed.
                    demotion_counts["DUPLICATE_EVIDENCE"] += 1
                    continue
                factual_seen.add(normalized)
                approved.append(payload)
                continue
            if not item["style_only"]:
                demotion_counts["HEDGED_FACTUAL_DEMOTED"] += 1
            seller.append(payload)
        features = Counter()
        lengths = []
        style_pool = filters.get("candidate_pool")
        if not isinstance(style_pool, list):
            style_pool = self.repository.candidates(
                store_code=filters.get("store_code"), limit=100
            )
        for item in style_pool[:100]:
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
            "learning_retrieval": {
                **dict(getattr(self, "last_trace", {})),
                **demotion_counts,
            },
        }
