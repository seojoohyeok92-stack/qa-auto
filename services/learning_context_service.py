from __future__ import annotations

import json
import os

import re
import uuid
from typing import Any

from answer.evidence_support import coverage_label
from answer.facts import AnswerFacts
from answer.hybrid_models import IntentResult
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_repository import LearningRepository
from repositories.log_repository import LogRepository
from services.similar_answer_service import SimilarAnswerService
from services.historical_case_service import HistoricalCaseService
from services.historical_learning_quality_service import is_data_unsafe
from repositories.learning_provenance_repository import LearningProvenanceRepository
from repositories.feedback_signal_provenance_repository import (
    FeedbackSignalProvenanceRepository,
)
from services.learning_evidence_policy import order_identifier_request_reason
from services.learning_signal_service import LearningSignalService
from services.learning_compatibility_service import (
    GENERIC_TOPICS,
    classify_topics,
)
from services.product_fact_guard import classify_product_fact
from services.semantic_analysis import (
    CURRENT_ORDER_DELIVERY_ACTIONS,
    PRE_PURCHASE_DELIVERY_ACTIONS,
    SemanticAnalysis,
    delivery_schedule_needs_review,
    purchase_confirmed,
)


# Retrieval traces: which candidates were considered, which were filtered and
# why, per sub-question. They are operational provenance, not something the
# model answers from -- and they grow with the size of the learning database,
# not with the inquiry. On the server they reached 620,203 of a 655,129
# character DRAFT prompt (94.7%) while the actual evidence was 15,045.
# They stay in the context for telemetry; they are kept out of the prompt.
PROMPT_EXCLUDED_CONTEXT_KEYS: frozenset[str] = frozenset(
    {
        "learning_retrieval",
        "historical_retrieval",
        # A verdict about the candidates, sitting in the prompt beside them.
        #
        # ``learning_evidence_policy`` requires an exact product match before
        # an approved answer "qualifies", so every product-independent policy
        # answer -- collection, installation method, after-service -- comes
        # back {"usable": false, "reason": "NO_QUALIFYING_APPROVED_LEARNING"}.
        # On 688159337 that reached the model next to the very rows that
        # answered the question. Whether a candidate applies is the judgement
        # GPT ② is being asked to make; telling it in advance that the answer
        # is no is not evidence. The verdict is still computed and still
        # persisted for the dashboard -- it is only out of the prompt.
        "approved_learning_evidence",
    }
)

# The seller answer is carried twice per historical case, as
# answer_style_reference and answer_reference. One copy is enough for the
# model; the other stays in the context for the provenance that reads it.
_HISTORICAL_CASE_PROMPT_DROP: frozenset[str] = frozenset(
    {"answer_style_reference"}
)

# What GPT ① went looking for is not something GPT ② may answer from.
#
# A retrieval query names a fact in the affirmative -- "기존 폐가전을 무상으로
# 수거해 주는지에 대한 안내" -- because that is the shape that matches a stored
# answer. Put in front of a model reading for evidence, a sentence like that is
# one careless step from being read as the answer, and it is not evidence at
# all: it is a description of what we hoped to find. Whether anything was found
# is already said by the candidates themselves and by the sub-question's
# status. So it stays in the context, where the dashboard and the operator can
# see what was searched, and out of the prompt.
_SEMANTIC_ATOM_PROMPT_DROP: frozenset[str] = frozenset({"retrieval_queries"})


def prompt_context(context: dict[str, Any]) -> dict[str, Any]:
    """The part of the learning context the model actually answers from.

    Evidence in, retrieval diagnostics out. Nothing is deleted -- the caller
    keeps the full context for logging and telemetry; this is only what gets
    serialised into the prompt.
    """

    projected: dict[str, Any] = {}
    for key, value in context.items():
        if key in PROMPT_EXCLUDED_CONTEXT_KEYS:
            continue
        if key == "historical_cases" and isinstance(value, list):
            projected[key] = [
                {
                    field: item[field]
                    for field in item
                    if field not in _HISTORICAL_CASE_PROMPT_DROP
                }
                if isinstance(item, dict)
                else item
                for item in value
            ]
            continue
        if key == "semantic_atoms" and isinstance(value, list):
            projected[key] = [
                {
                    field: item[field]
                    for field in item
                    if field not in _SEMANTIC_ATOM_PROMPT_DROP
                }
                if isinstance(item, dict)
                else item
                for item in value
            ]
            continue
        projected[key] = value
    return projected


# Evidence the model may lose last if the prompt still has to shrink, most
# expendable first. Facts, DPS, the customer's own question and the analysis
# are never in this list: they are the answer's authority and are never
# dropped. Verified feedback signals and the per-sub-question evidence map
# outrank retrieved examples, which outrank style references.
_PROMPT_TRIM_ORDER: tuple[str, ...] = (
    "seller_style_examples",
    "historical_cases",
    "similar_approved_answers",
)
# A full-evidence prompt for the six-question production inquiry measures
# about 12,000 characters once the retrieval traces are out, and about 15,000
# with no learning data at all. 60,000 leaves roughly four times that headroom
# -- generous for a genuinely rich inquiry, and far below anything that could
# repeat the 655,129 character prompt.
DRAFT_PROMPT_BUDGET_CHARS = 60_000


def apply_prompt_budget(
    context: dict[str, Any],
    *,
    budget: int = DRAFT_PROMPT_BUDGET_CHARS,
    measure: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Keep the prompt within budget without silently losing authority.

    Drops whole evidence groups in a fixed order, least authoritative first,
    and reports every drop. Facts, DPS, the analysis and the customer's own
    question are not candidates -- if the prompt is still too large after the
    optional evidence is gone, it is left too large rather than quietly cut,
    because truncating those would change what the answer is allowed to say.
    """

    size = measure or (
        lambda value: len(json.dumps(value, ensure_ascii=False, default=str))
    )
    trimmed = dict(context)
    report: dict[str, Any] = {
        "budget_chars": budget,
        "original_chars": size(trimmed),
        "dropped": [],
    }
    for key in _PROMPT_TRIM_ORDER:
        if size(trimmed) <= budget:
            break
        value = trimmed.get(key)
        if not value:
            continue
        report["dropped"].append(
            {"component": key, "chars": size(value),
             "records": len(value) if isinstance(value, list) else None}
        )
        trimmed[key] = [] if isinstance(value, list) else {}
    report["final_chars"] = size(trimmed)
    report["within_budget"] = report["final_chars"] <= budget
    return trimmed, report


# Atomic actions that genuinely turn on the customer's own order. Everything
# else -- a delivery *policy* question, a benefit's payout timing, how to apply
# for something -- is answerable before an order exists, and reusing an answer
# written for an existing order is how a customer who had said "아직 주문 안
# 했는데" was asked for their order number.
_VALID_ORDER_ID = re.compile(r"\d{16}")

# Actions whose answer is a property of one identified product. A stored
# answer written for a different model is wrong for these however well it
# matches otherwise, so identity is enforced strictly when GPT ① labels an
# atom with one of them.
PRODUCT_FACT_ACTIONS: frozenset[str] = frozenset({
    "PRODUCT_SPEC", "PRODUCT_CONCEPT", "PACKAGE_CONTENTS",
})

_ORDER_SCOPED_ACTIONS: frozenset[str] = frozenset({
    "ORDER_IDENTIFICATION",
    "DELIVERY_STATUS",
    "INSTALLATION_SCHEDULE",
    "SCHEDULE_CHANGE",
    "DELIVERY_DEADLINE_CONFIRMATION",
})


def _atomic_delivery_schedule_review(
    semantic_analysis: SemanticAnalysis | None,
    atomic: Any,
    *,
    order_id_validated: bool = False,
) -> bool:
    """Whether *this* sub-question asks when, with no confirmed order behind it.

    Per sub-question, not per inquiry: "오늘 주문하면 언제 도착? / A/S 되나요?"
    holds only the delivery half and leaves the A/S half answerable.

    The inquiry-level judgement decides whether an order exists -- that is a
    fact about the customer, not about one sentence -- and the sub-question's
    own action decides whether it is a schedule question at all.
    """

    if not delivery_schedule_needs_review(
        semantic_analysis, order_id_validated=order_id_validated
    ):
        return False
    if atomic is None:
        return True
    action = str(atomic.action).upper()
    return bool(
        action in PRE_PURCHASE_DELIVERY_ACTIONS
        or action in CURRENT_ORDER_DELIVERY_ACTIONS
    )


def _schedule_scoped(
    semantic_analysis: SemanticAnalysis | None, atomic: Any,
) -> bool | None:
    """Whether this sub-question is about a date, either side of the purchase."""

    if semantic_analysis is None or not semantic_analysis.usable:
        return None
    required = _order_evidence_required(semantic_analysis, atomic)
    if required:
        return True
    if _atomic_delivery_schedule_review(semantic_analysis, atomic):
        return True
    if atomic is not None:
        action = str(atomic.action).upper()
        return bool(
            action in PRE_PURCHASE_DELIVERY_ACTIONS
            or action in CURRENT_ORDER_DELIVERY_ACTIONS
        )
    return bool(semantic_analysis.asks_delivery_schedule)


def _order_evidence_required(
    semantic_analysis: SemanticAnalysis | None, atomic: Any,
) -> bool | None:
    """Whether this sub-question needs the customer's own order, or unknown.

    Tri-state. ``None`` means no usable understanding was available, and every
    downstream gate that reads this must then behave exactly as it did before
    the field existed -- a missing understanding is not evidence that the
    question is order-free.
    """

    if semantic_analysis is None or not semantic_analysis.usable:
        return None
    if atomic is not None:
        # This sub-question's own action, and nothing else.
        #
        # ``requires_order_context`` is a property of the *inquiry*, so ORing
        # it here made one order-shaped clause pull every sibling into the
        # customer's order. Measured on 688159421 -- "오늘 설치까지 끝났는데
        # 박스에 있어야 할 부속품 하나가 안 보이네요. 원래 따로 배송되는 건지
        # 제가 못 받은 건지" -- both atoms came back CURRENT_DPS_REQUIRED with
        # their evidence lists emptied, although ``need_dps`` was false and one
        # of them is a packaging-policy question that no order can answer.
        #
        # The inquiry-level reading is kept below for the no-atom case, where
        # there is no sub-question to ask about and the whole inquiry is the
        # only thing there is to judge.
        return bool(str(atomic.action).upper() in _ORDER_SCOPED_ACTIONS)
    return bool(
        semantic_analysis.actions & _ORDER_SCOPED_ACTIONS
        or semantic_analysis.requires_order_context
    )


_UNSET = object()

# How many meaning-based neighbours a sub-question may bring in. Recall is the
# point, and everything after this stage filters; the benchmark that chose the
# hybrid union measured its gain at rank 20.
# Raised from 20 with the rank bonus in ``similar_answer_service``: the two
# only work together. At 20, a row outside the index's top 20 fell back to
# lexical overlap, which is where the collection answer that belonged at rank 1
# was scoring 0.104. A wider neighbourhood costs nothing at query time -- the
# vectors are already loaded and the lookup is one pass -- and the bonus decays
# with rank, so a distant neighbour is admitted without being promoted.
SEMANTIC_NEIGHBOUR_LIMIT = 60


class LearningContextService:
    """GPT에 제공할 비사실성 참고 문맥만 구성한다."""

    def __init__(
        self, database: Database, *, hard_conflicts_only: bool = False,
    ) -> None:
        self.inquiries = InquiryRepository(database)
        self.search = SimilarAnswerService(LearningRepository(database))
        # Production GPT Answer receives recall-oriented candidates.  This
        # flag preserves the old precision-oriented context for direct legacy
        # callers while limiting production pre-GPT removal to hard conflicts.
        self.hard_conflicts_only = hard_conflicts_only
        # One index and one embedding client per service, so a compound
        # inquiry does not reload 1,000 vectors per sub-question.
        self._index_cache: Any = _UNSET
        self._embed_cache: Any = None
        self.logs = LogRepository(database)
        self.historical = HistoricalCaseService(database)
        self.provenance = LearningProvenanceRepository(database)
        self.feedback_signals = LearningSignalService(database)
        self.feedback_signal_provenance = FeedbackSignalProvenanceRepository(database)

    def _semantic_ranks(self, question: str, *queries: str) -> dict[int, int]:
        """Meaning-based neighbours of this sub-question, by rank.

        Returns nothing at all when the derived index is missing or the
        embedding call fails, and retrieval is then the lexical search that
        shipped before this existed. A retrieval aid may widen the candidate
        pool; it may never be the reason an inquiry produces no context.

        Extra queries are the search directions GPT ① named for this atom.
        Each is embedded and looked up the same way, and a row found by more
        than one keeps its best rank -- the bonus this feeds is a floor, so
        merging on the minimum is merging on "how sure was the closest
        match". All of them travel in one request: the endpoint takes a batch,
        so three directions cost three vectors and one round trip, not three.
        """

        texts = list(dict.fromkeys(
            str(item or "")[:400]
            for item in (question, *queries)
            if str(item or "").strip()
        ))
        if not texts:
            return {}
        try:
            # The key is checked before the index is touched: without it the
            # embedding call cannot happen, and loading tens of megabytes to
            # then fail would slow every offline run for nothing.
            if not os.environ.get("QNA_GPT_API_KEY"):
                return {}
            index = self._semantic_index()
            if index is None or not index.available:
                return {}
            merged: dict[int, int] = {}
            for vector in self._embedding_client().embed(texts):
                for rank, (identifier, _score) in enumerate(
                    index.similar(vector, limit=SEMANTIC_NEIGHBOUR_LIMIT), start=1
                ):
                    if rank < merged.get(identifier, rank + 1):
                        merged[identifier] = rank
            return merged
        except Exception:  # noqa: BLE001 - never blocks retrieval
            return {}

    def _semantic_index(self):
        if self._index_cache is _UNSET:
            from services.learning_semantic_index import LearningSemanticIndex

            loaded = LearningSemanticIndex.load_cached()
            self._index_cache = loaded if loaded.available else None
        return self._index_cache

    def _embedding_client(self):
        if self._embed_cache is None:
            from services.learning_semantic_index import EmbeddingClient

            self._embed_cache = EmbeddingClient()
        return self._embed_cache

    def build(
        self,
        facts: AnswerFacts,
        intent: IntentResult,
        *,
        semantic_analysis: SemanticAnalysis | None = None,
    ) -> dict[str, Any]:
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
        semantic_atomic = (
            list(semantic_analysis.atomic_questions)
            if semantic_analysis is not None and semantic_analysis.usable
            else []
        )
        # Semantic atomic questions are authoritative for retrieval scope.
        # The deterministic split remains as a fallback and preserves legacy
        # callers that have no semantic provider.
        questions = list(dict.fromkeys(
            str(item.text).strip() for item in semantic_atomic if str(item.text).strip()
        )) or list(dict.fromkeys(
            str(item).strip() for item in intent.questions if str(item).strip()
        ))
        # The whole message is the query only when nothing better exists.
        #
        # A semantic atom is the customer's question with the situation stripped
        # off, and that is what retrieval can match: measured on inquiry
        # 325584049 -- "혼자계신 엄마댁이라 tv설지하고 페가전 수거해주시는거죠?"
        # -- the full message scores 0.14 against every approved collection
        # answer in the store and returns nothing, while the atom alone returns
        # candidates at 0.82. Scoring is lexical, so the surrounding clause and
        # the customer's typos ("설지", "페가전") are noise the atom does not
        # carry. Falling back to the message here threw away the one query that
        # works, and did it whenever the semantic pass found a single question.
        #
        # The fallback stays for callers with no semantic pass at all, which is
        # what it was written for.
        if not semantic_atomic and len(questions) <= 1:
            questions = [original_question]
        elif not questions:
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
                metadata.get("human_verified") is True
                and source_origin != "HISTORICAL_PROMOTED"
            )
            # Only a fact about the row itself may remove it here.
            #
            # ``assess`` returns two very different kinds of finding through
            # one flag. DATA_UNSAFE statuses are properties of the stored row --
            # it is inactive, its promotion was revoked, its validity has
            # expired, it carries a policy risk, or it states one past
            # customer's order fact. Those are ours to enforce and they stay.
            #
            # The rest -- QUESTION_ANSWER_MISMATCH, LOW_RELEVANCE,
            # LOW_INFORMATION_ANSWER -- are judgements about meaning, made from
            # concept-set overlap and four hand-tuned thresholds, and they were
            # removing 321 of 899 candidates on a single inquiry before GPT ②
            # saw any of them. Whether a stored answer is on point is exactly
            # what GPT ② is given the candidates to decide, so the finding now
            # travels with the candidate as a hint instead of deleting it.
            approval_overrides_soft_quality = bool(
                explicitly_approved
                and eligibility.status in {
                    "QUESTION_ANSWER_MISMATCH", "LOW_RELEVANCE",
                    "REVIEW_REQUIRED",
                }
            )
            semantic_finding_only = (
                not eligibility.context_eligible
                and not is_data_unsafe(eligibility)
            )
            if semantic_finding_only:
                learning_quality_rejections[
                    f"DEMOTED_{eligibility.status}"
                ] = learning_quality_rejections.get(
                    f"DEMOTED_{eligibility.status}", 0
                ) + 1
            if (
                not eligibility.context_eligible
                and not approval_overrides_soft_quality
                and not semantic_finding_only
            ):
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
        signal_contexts: list[dict[str, Any]] = []
        negative_correction_contexts: list[dict[str, Any]] = []
        order_scope_by_question: dict[str, bool | None] = {}
        pre_purchase_by_question: dict[str, bool] = {}
        schedule_by_question: dict[str, bool | None] = {}
        # A validated order id on the inquiry proves the order exists whatever
        # the wording says, so it settles the same question the understanding
        # answers -- see semantic_analysis.purchase_confirmed.
        current_order_id_validated = bool(
            _VALID_ORDER_ID.fullmatch(str(inquiry.get("order_id") or "").strip())
        )
        purchase_is_confirmed = purchase_confirmed(
            semantic_analysis, order_id_validated=current_order_id_validated,
        )
        for question in questions:
            atomic = next(
                (item for item in semantic_atomic if item.text.strip() == question),
                None,
            )
            # The atom's own search directions, or nothing. Empty is the
            # ordinary case for a model that omitted the field and for every
            # deterministic caller, and it leaves the search running on the
            # customer's own words -- which is what it ran on before.
            atom_queries = tuple(atomic.retrieval_queries) if atomic is not None else ()
            semantic_goal = {
                "retrieval_queries": list(atom_queries),
                "customer_goal": (
                    atomic.action if atomic is not None
                    else (semantic_analysis.primary_action if semantic_analysis else None)
                ),
                "requested_information": question,
                "atomic_question": question,
                "all_atomic_questions": [item.to_dict() for item in semantic_atomic],
                # Whether *this* sub-question depends on the customer's own
                # order. Derived from the understanding, never from the words:
                # "주문 전인데 며칠 걸려요?" and "제 주문 언제 와요?" are the
                # same words and opposite scopes. ``None`` when there is no
                # usable understanding, which leaves retrieval unchanged.
                "order_evidence_required": _order_evidence_required(
                    semantic_analysis, atomic,
                ),
                # True when this sub-question turns on a date at all -- the
                # customer's own, or one that does not exist yet. Retrieval's
                # question-match support floor is off for these: a past
                # question worded almost identically carries another
                # customer's schedule.
                "schedule_scoped": _schedule_scoped(semantic_analysis, atomic),
            }
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
                semantic_ranks=self._semantic_ranks(question, *atom_queries),
                store_code=store_code,
                intent=intent_data.get("category") or intent_data.get("primary_intent"),
                product_name=product_name,
                model_code=question_guard.model_code,
                inquiry_type=inquiry_type,
                product_id=question_guard.product_id,
                option_name=(
                    inquiry.get("option_name") or facts.product.get("option_name")
                ),
                # Whether the customer asked for a specification, which is
                # what makes another model's answer unusable rather than merely
                # off-topic. The keyword guard misses plain phrasings -- "이
                # 제품 해상도가 4K UHD 맞나요?" carries no marker it recognises
                # -- and GPT ① has already read the question and labelled the
                # atom PRODUCT_SPEC. Either saying yes is enough; the two
                # disagree only in the direction of more caution.
                product_fact_sensitive=(
                    question_guard.sensitive
                    or (atomic is not None and atomic.action in PRODUCT_FACT_ACTIONS)
                ),
                # How many candidates one sub-question may show GPT ②.
                #
                # Two, on a compound inquiry, out of 669 that had already
                # cleared validity, product identity and the relevance floor --
                # and which two was settled by a lexical score. Choosing the
                # evidence is the judgement being moved to GPT ②; the number of
                # candidates is a budget question -- and the binding budget
                # turned out to be the provider's 45s read timeout, not the
                # 60,000-char prompt cap. At 4/6 the largest compound
                # inquiry reached 39,350 chars and timed out; 35,619 had
                # completed. These values keep the widening (2/3 -> 3/5)
                # inside the envelope that is known to answer.
                limit=3 if len(questions) > 1 else 5,
                candidate_pool=candidate_pool,
                candidate_diagnostics=candidate_diagnostics,
                semantic_goal=semantic_goal,
                hard_conflicts_only=self.hard_conflicts_only,
            )
            for key in ("similar_approved_answers", "seller_style_examples"):
                for item in item_context[key]:
                    item["matched_subquestion"] = question
                    item["attached_to_prompt"] = True
                    item["why_selected"] = (
                        "ACTIVE_VALIDITY_AND_RELEVANCE_THRESHOLD"
                        "_AND_PRODUCT_TOPIC_COMPATIBILITY"
                    )
            trace = dict(item_context.get("learning_retrieval") or {})
            trace["product_fact_sensitive"] = question_guard.sensitive
            subquestion_traces.append(trace)
            contexts.append(item_context)
            signal_result = self.feedback_signals.retrieve(
                question,
                store_code=store_code,
                product_name=product_name,
                model_code=question_guard.model_code,
                product_id=question_guard.product_id,
                option_name=(
                    inquiry.get("option_name") or facts.product.get("option_name")
                ),
                inquiry_type=inquiry_type,
                semantic_goal=semantic_goal,
                limit=2 if len(questions) > 1 else 3,
            )
            for key in (
                "verified_facts", "corrections", "good_patterns", "bad_patterns",
            ):
                for item in signal_result[key]:
                    item["matched_subquestion"] = question
            signal_contexts.append(signal_result)
            legacy = self.feedback_signals.negative_corrections(
                question,
                store_code=store_code,
                product_name=product_name,
                model_code=question_guard.model_code,
                product_id=question_guard.product_id,
                option_name=(
                    inquiry.get("option_name") or facts.product.get("option_name")
                ),
                semantic_goal=semantic_goal,
                limit=1 if len(questions) > 1 else 2,
            )
            for item in legacy["selected"]:
                item["matched_subquestion"] = question
            negative_correction_contexts.append(legacy)
            order_scope_by_question[question] = semantic_goal[
                "order_evidence_required"
            ]
            # Per sub-question, not per inquiry: a compound message can ask a
            # pre-purchase delivery question beside an answerable one, and only
            # the delivery part is held.
            pre_purchase_by_question[question] = _atomic_delivery_schedule_review(
                semantic_analysis, atomic,
                order_id_validated=current_order_id_validated,
            )
            schedule_by_question[question] = _schedule_scoped(
                semantic_analysis, atomic,
            )

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

        # The union across sub-questions. Raised with the per-question
        # limit above so a three-part inquiry is not squeezed back to two
        # candidates each by the merge.
        approved = merged("similar_approved_answers", limit=8)
        seller = merged(
            "seller_style_examples", limit=max(0, 4)
        )
        context = {
            "similar_approved_answers": approved,
            "seller_style_examples": seller,
            "oje_style_rules": contexts[0]["oje_style_rules"] if contexts else {},
        }

        def merged_signals(key: str, limit: int = 3) -> list[dict[str, Any]]:
            by_id: dict[int, dict[str, Any]] = {}
            for signal_context in signal_contexts:
                for item in signal_context[key]:
                    signal_id = int(item["id"])
                    if signal_id not in by_id or float(
                        item.get("relevance") or 0
                    ) > float(by_id[signal_id].get("relevance") or 0):
                        by_id[signal_id] = item
            return sorted(
                by_id.values(), key=lambda item: float(item.get("relevance") or 0),
                reverse=True,
            )[:limit]

        verified_facts = merged_signals("verified_facts")
        corrections = merged_signals("corrections")
        good_patterns = merged_signals("good_patterns")
        bad_patterns = merged_signals("bad_patterns")
        conflicting_signal_ids = {
            int(item["id"])
            for signal_context in signal_contexts
            for item in signal_context["conflicting_signals"]
        }
        context["feedback_signals"] = {
            "verified_facts": [
                {
                    "signal_id": int(item["id"]),
                    "content": item.get("content_text"),
                    "matched_subquestion": item.get("matched_subquestion"),
                    "relevance": item.get("relevance"),
                    "answer_support": item.get("answer_support"),
                    "product_scope": item.get("product_scope"),
                }
                for item in verified_facts
            ],
            "corrections": [
                {
                    "signal_id": int(item["id"]),
                    "content": item.get("content_text"),
                    "matched_subquestion": item.get("matched_subquestion"),
                    "relevance": item.get("relevance"),
                    "answer_support": item.get("answer_support"),
                    "product_scope": item.get("product_scope"),
                }
                for item in corrections
            ],
            "good_patterns": [
                {
                    "signal_id": int(item["id"]),
                    "guidance": item.get("content_text"),
                    "matched_subquestion": item.get("matched_subquestion"),
                }
                for item in good_patterns
            ],
            "bad_patterns": [
                {
                    "signal_id": int(item["id"]),
                    "guidance": item.get("content_text"),
                    "matched_subquestion": item.get("matched_subquestion"),
                }
                for item in bad_patterns
            ],
            "unresolved_conflicts": len(conflicting_signal_ids) > 0,
        }

        # Correction knowledge the operator wrote into a Negative memo before
        # the signal table existed. Constraints, never evidence: the model is
        # told what not to repeat and what to say instead, and none of this
        # can make a sub-question answerable -- that decision is made below
        # from Positive/verified evidence exactly as it was.
        negative_by_feedback: dict[int, dict[str, Any]] = {}
        # A negative memo records a mistake made on the message the customer
        # actually wrote, and is registered against that wording. Retrieval now
        # queries with the semantic atoms instead, which is right for finding
        # approved answers and wrong for finding this: the atom
        # "얼마 전 신청한 상품권 건이 처리되었는지 확인" no longer matches a memo
        # filed under "상품권 신청했는데 확인해주세요".
        #
        # So the original wording is looked up as well, and only ever adds --
        # a correction the atoms already found is not fetched twice, and none
        # can be removed here.
        if original_question and original_question not in questions:
            supplementary = self.feedback_signals.negative_corrections(
                original_question,
                store_code=store_code,
                product_name=product_name,
                model_code=guard.model_code,
                product_id=guard.product_id,
                option_name=(
                    inquiry.get("option_name") or facts.product.get("option_name")
                ),
                semantic_goal={},
                limit=2,
            )
            for item in supplementary["selected"]:
                item["matched_subquestion"] = original_question
            negative_correction_contexts.append(supplementary)

        for legacy_context in negative_correction_contexts:
            for item in legacy_context["selected"]:
                feedback_id = int(item["feedback_id"])
                current = negative_by_feedback.get(feedback_id)
                if current is None or float(item.get("relevance") or 0) > float(
                    current.get("relevance") or 0
                ):
                    negative_by_feedback[feedback_id] = item
        negative_corrections = sorted(
            negative_by_feedback.values(),
            key=lambda item: float(item.get("relevance") or 0),
            reverse=True,
        )[:3]
        context["negative_corrections"] = [
            {
                "correction_id": int(item["feedback_id"]),
                "matched_subquestion": item.get("matched_subquestion"),
                "reason": item.get("reason"),
                "bad_patterns": list(item.get("bad_patterns") or []),
                "corrections": list(item.get("corrections") or []),
                "good_patterns": list(item.get("good_patterns") or []),
                "relevance": item.get("relevance"),
                "answer_support": item.get("answer_support"),
                "source": item.get("source"),
            }
            for item in negative_corrections
        ]
        if context["negative_corrections"]:
            context["negative_correction_policy"] = {
                "operator_written_constraints": True,
                "never_repeat_bad_pattern_content": True,
                "apply_correction_direction_when_relevant": True,
                # Section 13. A Negative was saved because one claim in a past
                # answer was wrong; the rest of that answer, and every other
                # approved answer on the topic, stays true. Widening a
                # correction into "this whole subject is unsafe" would delete
                # correct knowledge to punish one sentence.
                "correction_scope_is_the_named_claim_only": True,
                "a_correction_never_invalidates_an_unrelated_correct_claim": True,
                "never_invent_a_correction_that_is_not_written": True,
                "corrections_are_not_current_order_or_schedule_facts": True,
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
        historical_order_scope_rejections = 0
        historical_topic_scope_rejections = 0
        for question in questions:
            detailed = self.historical.search_detailed(
                question,
                store_code=store_code,
                product_name=product_name,
                product_id=guard.product_id,
                model_code=guard.model_code,
                option_name=(
                    inquiry.get("option_name") or facts.product.get("option_name")
                ),
                inquiry_type=inquiry_type,
                limit=2 if len(questions) > 1 else 3,
                # The same contract the Learning search runs under: hard
                # validity removes, lexical concept overlap only ranks.
                hard_conflicts_only=self.hard_conflicts_only,
            )
            historical_traces.append({
                key: value for key, value in detailed.items()
                if key != "selected"
            })
            for item in detailed["selected"]:
                # Same order scope the Positive path applies. A past seller
                # answer that exists to collect an order number is the single
                # most reusable-looking, most wrong reference for a customer
                # who has just said they have not ordered yet.
                if order_scope_by_question.get(question) is False and (
                    order_identifier_request_reason(item.get("seller_answer"))
                    is not None
                ):
                    historical_order_scope_rejections += 1
                    continue
                # A historical case has no semantic metadata of its own, and
                # the general topic gate lets a candidate through on UNCERTAIN
                # when the *query* carries no explicit topic. That is how
                # "재입고 가능한가요?" -- topic OTHER -- was answered from a
                # case about ceiling-mount VESA installation, promoted to
                # ANSWERABLE with nothing holding it. Old metadata is a reason
                # to check meaning differently, never a reason to skip it: if
                # the case is about something specific and this question is
                # not about that thing, it is not evidence here.
                case_topics = {
                    topic
                    for topic in (
                        set(classify_topics(item.get("question")))
                        | set(classify_topics(item.get("seller_answer")))
                    )
                    if topic not in GENERIC_TOPICS
                }
                if case_topics and not (
                    case_topics & set(classify_topics(question))
                ):
                    historical_topic_scope_rejections += 1
                    continue
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

        signals_by_question = dict(zip(questions, signal_contexts))
        evidence_map: list[dict[str, Any]] = []
        for question in questions:
            historical_ids: list[int] = []
            feedback_signal_ids: list[int] = []
            approved_for_question = [
                item for item in approved
                if item.get("matched_subquestion") == question
            ]
            historical_for_question = [
                item for item in historical
                if item.get("matched_subquestion") == question
            ]
            question_signals = signals_by_question.get(question, {})
            verified_for_question = question_signals.get("verified_facts", [])
            corrections_for_question = question_signals.get("corrections", [])
            conflicts_for_question = question_signals.get("conflicts", [])
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
            # "주문 전인데 배송일 지정 되나요?" carries 배송일, so the regex
            # above read it as a question about a schedule that exists. The
            # plan had already decided otherwise -- requires_dps_lookup False,
            # DPS SKIPPED on the dashboard -- and the evidence map disagreeing
            # with it is the inconsistency, not a second opinion. Where the
            # understanding says this sub-question needs no customer-specific
            # order evidence, a keyword cannot make it need a current date.
            #
            # Only ``False`` suppresses. With no usable understanding the
            # keyword rule stands exactly as it did.
            if order_scope_by_question.get(question) is False:
                schedule_specific = False
            # And the understanding can add what the keywords miss. "배송이
            # 이번주 수요일로 잡혀있어 구매했는데 ... 도착 날짜좀 알려주세요"
            # is a confirmed order asking for its own arrival date, but the
            # regex above looks for 도착일 and finds 도착 날짜, so this
            # deferred to nothing and was answered from a historical case
            # carrying another customer's installation day. When the customer
            # has an order and is asking when, the current fact is DPS's to
            # give -- that is the same rule the plan already applied.
            if (
                schedule_by_question.get(question) is True
                and purchase_is_confirmed
            ):
                schedule_specific = True
            pre_purchase_delivery = pre_purchase_by_question.get(question, False)
            if pre_purchase_delivery:
                # Checked before every evidence branch, so nothing can settle
                # it. A CORRECTION signal an operator wrote -- "아직 구매하지
                # 않은 고객의 배송문의이다. 배송기간을 유추할수 없으므로
                # 답변이 생성 되더라도 직원이 검토하는게 맞다" -- was being
                # read as a verified fact and marking these ANSWERABLE. The
                # instruction to hold was being spent as grounds to answer.
                #
                # Approved Positive Learning cannot settle it either: a past
                # answer records how long one order took, which says nothing
                # about an order that does not exist yet.
                status = "DELIVERY_SCHEDULE_REVIEW"
                evidence_ids = []
                historical_ids = []
                source = "DELIVERY_SCHEDULE_UNCONFIRMED_PURCHASE"
                evidence_coverage = "UNSUPPORTED"
            elif schedule_specific and not confirmed_schedule:
                status = "NEEDS_DPS"
                evidence_ids: list[int] = []
                source = "CURRENT_DPS_REQUIRED"
                evidence_coverage = "DEFERRED_TO_CURRENT_FACT"
            elif confirmed_schedule and schedule_specific:
                status = "ANSWERABLE"
                evidence_ids = []
                source = "CURRENT_DPS"
                evidence_coverage = "SUPPORTED"
            elif conflicts_for_question:
                # Two ACTIVE VERIFIED_FACT/CORRECTION signals in the same
                # product/topic scope flatly disagree.  Never let GPT pick a
                # side -- surface this as a conflict requiring human review
                # instead (Acceptance Case F).
                status = "CONFLICT"
                evidence_ids = []
                source = "CONFLICTING_VERIFIED_FEEDBACK_SIGNALS"
                evidence_coverage = "UNSUPPORTED"
                feedback_signal_ids = sorted({
                    *(int(item["left_signal_id"]) for item in conflicts_for_question),
                    *(int(item["right_signal_id"]) for item in conflicts_for_question),
                })
            elif verified_for_question or corrections_for_question:
                # A human-verified fact/correction outranks a plain Positive
                # Learning example for the same sub-question (Acceptance
                # Case B): checked before falling back to approved Learning.
                status = "ANSWERABLE"
                evidence_ids = []
                source = "VERIFIED_FEEDBACK_SIGNAL"
                feedback_signal_ids = [
                    int(item["id"])
                    for item in (*verified_for_question, *corrections_for_question)
                ]
                evidence_coverage = coverage_label(max(
                    (
                        float(item.get("answer_support") or 0)
                        for item in (*verified_for_question, *corrections_for_question)
                    ),
                    default=0.0,
                ))
            elif approved_for_question or historical_for_question:
                # Retrieval found candidates for this sub-question. That is all
                # this ladder is entitled to say about them.
                #
                # It used to say more: unless ``answer_support`` -- the share of
                # the question's word stems reappearing in the candidate's
                # answer -- reached 0.5, the candidate was demoted to
                # NO_RELIABLE_SOURCE and its id was stripped out, while its text
                # stayed in the prompt. The measure is lexical, so "삼성기사분이
                # 설치하러 오시나요" scored 0.000 against the approved answer
                # "해당 상품은 삼성 기사님이 방문하여 설치하는 상품입니다.": no
                # shared stem, same fact. The pipeline showed the model the
                # answer and forbade it in the same breath, and the customer was
                # told their question needed a person.
                #
                # Whether a candidate answers this question is a judgement about
                # meaning, and it is GPT ② that makes it now. The scores travel
                # with each candidate as hints; ``evidence_coverage`` still
                # records what the lexical measure saw, because an operator
                # reading the trace afterwards wants to know.
                #
                # Only where a semantic understanding exists, because that is
                # the run where GPT ② is given the candidates to judge. With no
                # understanding there is no reader downstream, so the legacy
                # ladder keeps its own verdict exactly as it was.
                status = "CANDIDATE" if semantic_atomic else "ANSWERABLE"
                evidence_ids = [
                    int(item["learning_example_id"])
                    for item in approved_for_question
                ]
                historical_ids = [
                    int(item["id"]) for item in historical_for_question
                ]
                source = (
                    "ACTIVE_POSITIVE_LEARNING"
                    if approved_for_question
                    else "SAFE_HISTORICAL_LEARNING"
                )
                evidence_coverage = coverage_label(max(
                    (
                        float(item.get("answer_support") or 0)
                        for item in (
                            *approved_for_question, *historical_for_question
                        )
                    ),
                    default=0.0,
                ))
            else:
                # Retrieval genuinely found nothing for this sub-question. This
                # is the one remaining NO_RELIABLE_SOURCE, and it states an
                # absence rather than a judgement about meaning.
                status = "NO_RELIABLE_SOURCE"
                evidence_ids = []
                source = None
                historical_ids = []
                evidence_coverage = "UNSUPPORTED"
            evidence_map.append(
                {
                    "subquestion": question,
                    "status": status,
                    "source": source,
                    "learning_ids": evidence_ids,
                    "historical_case_ids": historical_ids,
                    "feedback_signal_ids": feedback_signal_ids,
                    "answer_required": status in {"ANSWERABLE", "CANDIDATE"},
                    # Question -> Evidence Coverage: retrieval finding a
                    # candidate is not the same as that candidate's answer
                    # actually supporting this sub-question (see
                    # answer/evidence_support.py).
                    "evidence_coverage": evidence_coverage,
                }
            )
        # What the semantic pass understood, kept where generation can read it.
        #
        # ``requested_attribute`` -- which property of the subject was asked --
        # was produced for every atom, recorded for the coverage gate and read
        # by the verifier, and then reached the drafting prompt not once. The
        # model was told which questions to answer and never which property of
        # each one, so nothing in the contract held it to the property asked.
        # It is surfaced here rather than rebuilt: the same objects retrieval
        # already used, at the top level instead of nested inside a per-question
        # diagnostic.
        context["semantic_atoms"] = [item.to_dict() for item in semantic_atomic]
        context["subquestion_evidence"] = evidence_map
        # What each status means to the model. CANDIDATE is the ordinary case
        # and says nothing about whether the candidates apply -- that judgement
        # moved to GPT ②. The remaining statuses are the deterministic ones: an
        # absence, a pending external lookup, a policy hold, and a contradiction
        # nobody may resolve by picking a side.
        context["subquestion_answer_policy"] = {
            "CANDIDATE": (
                "Retrieved candidates are attached for this item. Read them, "
                "decide which ones actually answer it for this product, and "
                "answer from those. If none of them does, say so for this item "
                "only."
            ),
            "ANSWERABLE": "Answer directly from the mapped evidence.",
            "NEEDS_DPS": "Do not use Learning as the current order date.",
            "NO_RELIABLE_SOURCE": (
                "Retrieval found no candidate at all for this item."
            ),
            "CONFLICT": "Do not choose between conflicting sources.",
            "DELIVERY_SCHEDULE_REVIEW": (
                "No order is confirmed to exist. Do not state a delivery "
                "period, a dispatch cutoff or an arrival date, and do not ask "
                "for an order number. Staff will answer this item."
            ),
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
                    "answer_support": item.get("answer_support"),
                    "answer_support_reason": item.get("answer_support_reason"),
                    "why_selected": item.get("why_selected"),
                    "attached_to_prompt": True,
                    "answer_supported": None,
                    "compatibility": item.get("compatibility") or {},
                }
                for item in context["similar_approved_answers"]
            ],
            "subquestion_count": len(questions),
            "semantic_goal": {
                "primary_action": (
                    semantic_analysis.primary_action
                    if semantic_analysis is not None and semantic_analysis.usable
                    else None
                ),
                "atomic_questions": [item.to_dict() for item in semantic_atomic],
            },
            # Why a Negative memo was or was not applied, per sub-question --
            # the operational question "왜 이 Negative가 적용됐지?" answered
            # without opening the database.
            "negative_corrections": {
                "selected": [
                    {
                        "correction_id": int(item["feedback_id"]),
                        "matched_subquestion": item.get("matched_subquestion"),
                        "relevance": item.get("relevance"),
                        "answer_support": item.get("answer_support"),
                        "structured_memo": bool(item.get("structured")),
                        "has_bad_pattern": bool(item.get("bad_patterns")),
                        "has_correction": bool(item.get("corrections")),
                        "product_scope": item.get("product_scope"),
                        "topics": item.get("topics"),
                        "usage_status": "ATTACHED_AS_CONSTRAINT",
                    }
                    for item in negative_corrections
                ],
                "subquestions": [
                    legacy_context["trace"]
                    for legacy_context in negative_correction_contexts
                ],
            },
            "subquestions": subquestion_traces,
            "rejection_counts": {
                "FILTERED_BY_VALIDITY": candidate_diagnostics.get("filtered_by_validity", 0),
                "REVOKED": candidate_diagnostics.get("revoked", 0),
                "NEGATIVE_EXCLUDED": candidate_diagnostics.get("negative_excluded", 0),
                "HISTORICAL_ORDER_SCOPE_MISMATCH": (
                    historical_order_scope_rejections
                ),
                "HISTORICAL_TOPIC_SCOPE_MISMATCH": (
                    historical_topic_scope_rejections
                ),
                "FILTERED_BY_RUNTIME_QUALITY": sum(
                    learning_quality_rejections.values()
                ),
                **learning_quality_rejections,
                **{
                    reason: sum(
                        int((trace.get("rejection_counts") or {}).get(reason, 0))
                        for trace in subquestion_traces
                    )
                    for reason in {
                        key
                        for trace in subquestion_traces
                        for key in (trace.get("rejection_counts") or {})
                        if key not in {
                            "FILTERED_BY_VALIDITY", "REVOKED", "NEGATIVE_EXCLUDED"
                        }
                    }
                },
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
                "answer_support": item.get("answer_support", 0.0),
                "matched_subquestion": item.get("matched_subquestion"),
                "eligibility": item.get("runtime_eligibility"),
                "compatibility": item.get("compatibility") or {},
                "attached_to_prompt": True,
                "source": item.get("reference_strength")
                or "HISTORICAL_VERIFIED_LEARNING",
                # What this past reply was written for. The model reads it to
                # tell a one-off order fact ("9월 3일 배송 예정입니다") from
                # standing product knowledge ("삼성 기사님이 방문하여 설치하는
                # 상품입니다") -- the distinction the lexical support score
                # could never make.
                "source_question": item.get("question"),
                "source_product_name": item.get("product_name"),
                "source_date": item.get("inquiry_created_at")
                or item.get("source_created_at"),
            }
            for item in historical
        ]
        context["historical_case_policy"] = {
            "safe_reusable_knowledge_allowed": True,
            "runtime_eligibility_required": True,
            "never_use_as_current_fact": True,
            # ``current_authority_order`` used to rank the sources, with
            # RULE_AND_SAFETY first and HISTORICAL_VERIFIED_LEARNING last. A
            # standing order like that answers "which source wins" before the
            # model has read either one, and it disagreed with the block names
            # in the same prompt. What remains is the one precedence that is a
            # safety rule rather than a judgement: a claim that depends on
            # *when* can only come from this customer's current order.
            "time_dependent_claims_require_current_facts": True,
            "current_order_facts_only_from": ["CURRENT_ORDER", "CURRENT_DPS"],
            "otherwise_judge_each_candidate_on_its_own_provenance": True,
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
            generated_run_id = str(uuid.uuid4())
            context_run_id = self.provenance.record_context(
                inquiry_id=int(inquiry_id),
                learning=learning_references,
                historical=context["historical_cases"],
                context_run_id=generated_run_id,
            ) or generated_run_id
            attached_signals = [
                {
                    "signal_id": int(item["signal_id"]),
                    "signal_kind": "VERIFIED_FACT",
                    "source_label": "VERIFIED_FACT",
                    "matched_subquestion": item.get("matched_subquestion"),
                    "relevance": item.get("relevance"),
                    "answer_support": item.get("answer_support"),
                }
                for item in context["feedback_signals"]["verified_facts"]
            ] + [
                {
                    "signal_id": int(item["signal_id"]),
                    "signal_kind": "CORRECTION",
                    "source_label": "CORRECTION",
                    "matched_subquestion": item.get("matched_subquestion"),
                    "relevance": item.get("relevance"),
                    "answer_support": item.get("answer_support"),
                }
                for item in context["feedback_signals"]["corrections"]
            ] + [
                {
                    "signal_id": int(item["signal_id"]),
                    "signal_kind": "GOOD_PATTERN",
                    "source_label": "GOOD_PATTERN",
                    "matched_subquestion": item.get("matched_subquestion"),
                    "relevance": None,
                    "answer_support": None,
                }
                for item in context["feedback_signals"]["good_patterns"]
            ] + [
                {
                    "signal_id": int(item["signal_id"]),
                    "signal_kind": "BAD_PATTERN",
                    "source_label": "BAD_PATTERN",
                    "matched_subquestion": item.get("matched_subquestion"),
                    "relevance": None,
                    "answer_support": None,
                }
                for item in context["feedback_signals"]["bad_patterns"]
            ] + [
                {
                    "signal_id": int(signal_id),
                    "signal_kind": "VERIFIED_FACT",
                    "source_label": "CONFLICTING_VERIFIED_FEEDBACK_SIGNAL",
                    "matched_subquestion": None,
                    "relevance": None,
                    "answer_support": None,
                    "conflict": True,
                }
                for signal_id in sorted({
                    int(item["id"])
                    for signal_context in signal_contexts
                    for item in signal_context["conflicting_signals"]
                })
            ]
            self.feedback_signal_provenance.record_context(
                inquiry_id=int(inquiry_id),
                context_run_id=generated_run_id,
                signals=attached_signals,
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
