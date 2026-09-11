from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from answer.answer_validator import AnswerValidator
from answer.fact_selection import FactSelectionService, SelectedFacts
from answer.facts import AnswerFacts, build_answer_facts
from answer.hybrid_models import (
    DraftResult,
    Emotion,
    IntentResult,
    SelfReviewResult,
    ValidationResult,
)
from answer.models import AnswerRequest, AnswerResult, AnswerStatus
from answer.inquiry_analysis import InquiryAnalysis
from answer.providers.interfaces import JsonGptProvider
from answer.text_utils import split_subquestions
from answer.providers.provider_factory import create_gpt_provider
from services.draft_generation_service import DraftGenerationService
from services import learning_evidence_policy
from services.learning_evidence_policy import usable_as_factual_evidence
from services.gpt_understanding_service import GptUnderstandingService
# ``required_fact_groups`` is no longer consulted here: see _product_fact_fields.
from services.product_knowledge_service import SUBJECT_SENSITIVE_FIELDS
from services.self_review_service import SelfReviewService


def _join_evidence(*parts: str) -> str:
    """Concatenate grounding corpora, skipping the empty ones."""

    return "\n".join(part for part in parts if part and part.strip())


@dataclass(frozen=True)
class HybridEvent:
    code: str
    message: str
    level: str = "INFO"
    details: dict[str, Any] | None = None


@dataclass(frozen=True)
class HybridAnswerOutcome:
    result: AnswerResult
    facts: AnswerFacts
    intent: IntentResult | None
    draft: DraftResult | None
    self_review: SelfReviewResult | None
    validation: ValidationResult | None
    fallback_used: bool
    events: tuple[HybridEvent, ...]


class HybridAnswerService:
    def __init__(
        self,
        provider: JsonGptProvider | None = None,
        *,
        validator: AnswerValidator | None = None,
        fact_selection: FactSelectionService | None = None,
        learning_context_provider: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.provider = provider or create_gpt_provider()
        self.understanding = GptUnderstandingService(self.provider)
        self.drafts = DraftGenerationService(
            self.provider,
            learning_context_provider=learning_context_provider,
        )
        self.self_review = SelfReviewService(self.provider)
        self.validator = validator or AnswerValidator()
        self.fact_selection = fact_selection or FactSelectionService()
        self._learning_context_provider = learning_context_provider
        self._stage_seconds: dict[str, float] = {}

    def _provider_telemetry(
        self, *, started: float | None = None
    ) -> dict[str, Any]:
        """How many provider round trips this generation cost, and where.

        When a generation failed the safe draft replaced the GPT draft and the
        evidence went with it, so nobody could tell afterwards whether the
        pipeline had made one call or five. Sizes and timings only -- the
        records carry no prompt text, context values or customer data.
        """

        records = list(getattr(self.provider, "call_records", []) or [])
        budget = getattr(self.drafts, "last_prompt_budget", None)
        telemetry: dict[str, Any] = {
            "prompt_budget": budget or {},
            "stage_seconds": dict(self._stage_seconds),
            "provider_call_count": len(records),
            "tasks": [str(item.get("task") or "") for item in records],
            "calls": records,
        }
        if started is not None:
            telemetry["total_elapsed_seconds"] = round(
                time.monotonic() - started, 3
            )
        return telemetry

    @staticmethod
    def _product_facts_context(request: AnswerRequest) -> dict[str, Any]:
        """The safe product facts this inquiry may quote, prompt-ready.

        Read from the lookup AnswerService already performed before any
        provider call. Only ``ProductKnowledgeService`` decides what is safe;
        nothing here re-judges a fact, and an unsafe one never appears.
        """

        knowledge = request.metadata.get("product_knowledge")
        if knowledge is None:
            return {}
        # Always say how the lookup ended, including when it found nothing.
        # "The catalogue holds no verified specification for this listing" and
        # "nobody looked" are different situations for the model: the first
        # means the listing text is the only product information there is, and
        # the second would mean something upstream failed.
        context: dict[str, Any] = {
            "product_identity": {
                "status": getattr(knowledge, "identity_status", None),
                "matched": bool(getattr(knowledge, "matched", False)),
                "listing_id": getattr(knowledge, "listing_id", None),
                "reason": getattr(knowledge, "unavailable_reason", None),
            }
        }
        block = getattr(knowledge, "prompt_block", None)
        rendered = block() if callable(block) else ""
        if rendered:
            context["product_catalog"] = {
                "instructions": rendered,
                # The model's copy, not the audit copy. ``to_dict`` still
                # backs telemetry and the review UI through
                # ``result.metadata``; what goes in the prompt is the fields
                # a reader can act on. See ``ProductFact.as_prompt_fact``.
                "facts": [
                    item.as_prompt_fact() for item in knowledge.safe_facts
                ],
                "product_id": knowledge.product_id,
                "identity_status": getattr(knowledge, "identity_status", None),
            }
        # Models the listing could have meant, when it named no single one.
        # Reported as candidates and labelled as such: two 85-inch panels can
        # differ in exactly the field being asked about, so a candidate's
        # specification is not this product's specification until something
        # says which candidate this is.
        candidates = tuple(getattr(knowledge, "candidate_models", ()) or ())
        if candidates:
            context["product_candidates"] = {
                "identity_status": getattr(knowledge, "identity_status", None),
                "models": [dict(item) for item in candidates],
                "usage": (
                    "현재 상품이 이 후보들 중 어느 모델인지 확정되지 않았습니다."
                    " 후보의 사양을 현재 상품의 확정 사실로 answer 에 쓰지"
                    " 마세요. 후보 전체가 동일한 값을 가진 경우에도 확정"
                    " 표현 대신 확인이 필요하다고 안내하거나, 판매 페이지"
                    " 표기를 근거로 삼는 편이 안전합니다."
                ),
            }
        return context

    @staticmethod
    def _gpt_judges_evidence(request: AnswerRequest) -> bool:
        """Whether GPT ② is the reader deciding what the evidence supports.

        True exactly when GPT ① produced a usable understanding, which is also
        the condition under which retrieval hands over candidates instead of
        verdicts. The two have to agree: gates that exist to catch an
        unsupervised answer must keep their authority on the legacy path, where
        no model reads the candidates at all.
        """

        return bool(
            getattr(
                request.metadata.get("_semantic_routing_value"),
                "usable",
                False,
            )
        )

    @staticmethod
    def _product_fact_fields(knowledge: Any) -> tuple[str, ...]:
        """Every verified field this product has, as candidate evidence.

        This replaced ``_product_fact_support``, which answered a different
        question: *may* a product fact settle this sub-question. It answered it
        from ``required_fact_groups``, a table keyed on the customer's wording
        -- and that table has no installation branch at all, so no phrasing of
        "누가 설치하나요" could ever be settled by ``installation_method``, and a
        wording it did model still had to match its keywords exactly.

        Naming which rows exist for this product is something code can settle;
        which of them answers the customer is not. The rows travel to GPT ②
        with their field keys and raw values, and the model reports the ones it
        used.
        """

        safe_keys = getattr(knowledge, "safe_field_keys", None)
        if not callable(safe_keys):
            return ()
        try:
            return tuple(sorted(safe_keys()))
        except Exception:  # noqa: BLE001 - evidence assembly never blocks
            return ()

    @classmethod
    def _apply_product_fact_evidence(
        cls, request: AnswerRequest, learning_context: dict[str, Any]
    ) -> dict[str, Any]:
        """Let a VERIFIED product fact answer a sub-question it covers.

        The evidence ladder in ``learning_context_service`` knew about DPS,
        verified feedback signals, approved Learning and historical cases, but
        not about ``product_facts.db`` -- so a specification question the
        Product DB answers exactly still came out ``NO_RELIABLE_SOURCE``.

        That single gap broke two things at once, which is why fixing only the
        publishing gate never worked. ``subquestion_evidence_is_binding`` is
        in the prompt contract, so the model was told not to answer a
        sub-question whose verified fact was sitting in the very same prompt
        and replied "추가 확인이 필요합니다"; and the validator's
        QUESTION_ANSWER_ALIGNMENT rule then flagged any answer that *did*
        state the fact as an unsupported claim.

        Only ``NO_RELIABLE_SOURCE`` items are promoted. NEEDS_DPS keeps
        deferring to the current order, CONFLICT keeps requiring a person, and
        an item already ANSWERABLE keeps the source it had -- so this can add
        evidence but never remove or overrule any.
        """

        knowledge = request.metadata.get("product_knowledge")
        if not getattr(knowledge, "matched", False):
            return learning_context
        if not getattr(knowledge, "has_safe_facts", False):
            return learning_context
        evidence = learning_context.get("subquestion_evidence")
        if not isinstance(evidence, list):
            return learning_context
        covering = cls._product_fact_fields(knowledge)
        if not covering:
            return learning_context
        # Whether this inquiry is about the product at all. Asked of GPT ①,
        # which read the question, rather than of the keyword topic table --
        # that table is what made "삼성기사분이 설치하러 오시나요" match nothing
        # and lose its catalogue rows. With no usable understanding the answer
        # is no, so a delivery-only inquiry never drags a screen size into its
        # prompt on either path.
        understanding = request.metadata.get("gpt_understanding")
        product_requested = bool(
            isinstance(understanding, dict)
            and understanding.get("usable") is True
            and understanding.get("offer_product_record")
        )
        if not product_requested:
            return learning_context
        # Which subject a measurement belongs to is a scope fact, and it stays.
        # A listing weighs the panel and the stand's carton separately, and
        # ``SUBJECT_SENSITIVE_FIELDS`` are exactly the rows where the field name
        # alone cannot say which one was asked about. The knowledge service
        # already decided that from the question's subject; a row it let through
        # on that basis is offered, but one of them on its own does not turn a
        # sub-question into an answerable one -- 25.3kg of packaging is not the
        # television, however confidently it is catalogued.
        promotable = bool(
            set(covering) - SUBJECT_SENSITIVE_FIELDS
            or getattr(knowledge, "component_subject", False)
        )
        for item in evidence:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "")
            # NEEDS_DPS still defers to the current order, CONFLICT still needs
            # a person, and DELIVERY_SCHEDULE_REVIEW is a policy hold. Those are
            # deterministic and a catalogue row does not lift them.
            if status not in {"NO_RELIABLE_SOURCE", "ANSWERABLE", "CANDIDATE"}:
                continue
            item["product_fact_fields"] = list(covering)
            if status == "NO_RELIABLE_SOURCE" and promotable:
                item["status"] = "CANDIDATE"
                item["source"] = "VERIFIED_PRODUCT_FACT"
                item["answer_required"] = True
        return learning_context

    @staticmethod
    def _apply_evidence_conflicts(
        request: AnswerRequest, learning_context: dict[str, Any]
    ) -> dict[str, Any]:
        """Record contradictions between the evidence the model was given.

        Retrieval already withholds verified *signals* that disagree, but two
        approved Learning answers can still contradict each other, and an
        approved answer can contradict a VERIFIED product fact. Neither was
        checked anywhere, so the model would have been handed both sides and
        left to pick -- exactly the choice this pipeline never lets it make.

        The contradiction is written into the sub-question's existing CONFLICT
        status rather than a new field, so every downstream reader (the
        prompt's answer policy, the validator, the publishing gate) treats it
        as the conflict it already knows how to refuse.
        """

        knowledge = request.metadata.get("product_knowledge")
        # The facts the customer's own wording puts in play, not the whole
        # record the model reads. See
        # ``ProductKnowledgeResult.facts_in_question_scope``: this check
        # compares polarities and quantities without knowing what either
        # sentence is about, so a wider set only manufactures conflicts.
        scoped = getattr(knowledge, "facts_in_question_scope", None)
        safe_facts = (
            scoped(request.question) if callable(scoped)
            else getattr(knowledge, "safe_facts", ()) or ()
        )
        def _scope_for(subquestion: object) -> set[str] | None:
            """Field keys one sub-question put in play, for the check above."""

            text = str(subquestion or "").strip()
            if not text or not callable(scoped):
                return None
            return {item.field_key for item in scoped(text)}

        decision = learning_evidence_policy.evaluate(
            learning_context=learning_context,
            safe_facts=safe_facts,
            scope_for=_scope_for,
        )
        learning_context["approved_learning_evidence"] = decision.to_dict()
        if not decision.conflict:
            return learning_context
        disputed = {
            str(item.get("subquestion") or "")
            for item in decision.conflicts
        }
        evidence = learning_context.get("subquestion_evidence")
        if isinstance(evidence, list):
            for item in evidence:
                if not isinstance(item, dict):
                    continue
                if str(item.get("subquestion") or "") not in disputed:
                    continue
                item["status"] = "CONFLICT"
                item["evidence_coverage"] = "UNSUPPORTED"
                item["answer_required"] = False
                item["source"] = decision.reason
        return learning_context

    @staticmethod
    def _product_facts_evidence(request: AnswerRequest) -> str:
        """Flat product-fact text for the deterministic grounding check."""

        knowledge = request.metadata.get("product_knowledge")
        evidence = getattr(knowledge, "evidence_text", None)
        return evidence() if callable(evidence) else ""

    @staticmethod
    def _evidence_texts(learning_context: dict[str, Any]) -> str:
        """The texts that may *prove* a factual claim, for grounding checks.

        The validator can see the facts but not the retrieved answers, so
        without this a claim taken straight from an approved learning example
        would look unsupported. What belongs here is therefore exactly what
        the pipeline is willing to call evidence -- and three kinds of
        retrieved text are not:

        ``seller_style_examples``
            Learning harvested from past Naver answers with no review. The
            prompt already tells the model these are not facts
            (``seller_style_examples_are_facts: false``) and
            ``learning_evidence_policy`` refuses them outright, but this
            corpus admitted them anyway -- so an unreviewed sentence could
            ground a claim the two other layers had already rejected. They
            still reach the prompt for tone; they no longer prove anything.

        ``good_patterns`` / ``bad_patterns``
            Guidance about how to write, never about the product.

        hedged and redaction-contaminated answers
            An answer that declines to commit cannot establish a definite
            claim, and one containing a ``<masked-...>`` token is a record of
            something removed, not a statement about the product.

        Narrowing this corpus can only make the validator stricter: a claim
        it can no longer find becomes an ungrounded-claim error.
        """

        parts: list[str] = []
        for key in ("similar_approved_answers", "historical_cases"):
            for item in learning_context.get(key) or []:
                if not isinstance(item, dict):
                    continue
                if not usable_as_factual_evidence(item):
                    continue
                parts.extend(
                    str(value) for value in item.values()
                    if isinstance(value, str)
                )
        signals = learning_context.get("feedback_signals")
        if isinstance(signals, dict):
            for key in ("verified_facts", "corrections"):
                for item in signals.get(key) or []:
                    if isinstance(item, dict):
                        parts.append(str(item.get("content") or ""))
        # An operator's Negative memo says what the answer should have said.
        # That sentence is human-written operational knowledge of the same
        # kind as a CORRECTION signal, so an answer that follows it is
        # grounded. ``bad_patterns`` are deliberately absent: they are the
        # wrong claim, and nothing may be grounded in them.
        for item in learning_context.get("negative_corrections") or []:
            if not isinstance(item, dict):
                continue
            parts.extend(str(text) for text in (item.get("corrections") or []))
            parts.extend(str(text) for text in (item.get("good_patterns") or []))
        return " ".join(part for part in parts if part)

    @staticmethod
    def _style_reference_texts(learning_context: dict[str, Any]) -> str:
        """Tone references. Kept separate so nothing can grade them as proof."""

        parts: list[str] = []
        for item in learning_context.get("seller_style_examples") or []:
            if isinstance(item, dict):
                parts.append(str(item.get("answer") or ""))
        return " ".join(part for part in parts if part)


    @staticmethod
    def _deterministic_intent(
        facts: AnswerFacts,
        analysis: InquiryAnalysis | None,
        rule_result: AnswerResult,
        semantic: Any = None,
    ) -> IntentResult:
        """The understanding GPT ② works from.

        No UNDERSTANDING round trip is made: GPT ① already read this inquiry
        before routing, and asking a second model to re-read it bought a second
        opinion on a decision that was already made.

        Which decomposition to carry forward is the part that mattered. Two
        existed side by side -- GPT ①'s ``atomic_questions`` and
        ``split_subquestions``, a splitter keyed on sentence enders and list
        punctuation -- and the pipeline retrieved evidence against the first
        while handing the second to drafting, coverage and topic relevance. So
        one half of the pipeline was answering questions the other half had not
        asked. A usable GPT ① is now the single source.

        ``requires_review`` moved for the same reason. It came from
        ``analysis.manual_review_required``, which the keyword classifier
        raises whenever no rule matched the wording: "삼성기사분이 설치하러
        오시나요" scores UNCLASSIFIED at confidence 0.45 and carried a review
        hold no answer could clear, whatever GPT ② found. A classifier gap is
        not a safety finding, and the finding it stands in for -- did this
        answer leave something unresolved -- is one GPT ② reports directly.
        With no usable understanding the legacy signal is kept exactly as it
        was, because then it is the only reading of the inquiry there is.
        """

        atoms = tuple(
            str(getattr(item, "text", "") or "").strip()
            for item in (getattr(semantic, "atomic_questions", ()) or ())
        ) if getattr(semantic, "usable", False) else ()
        atoms = tuple(dict.fromkeys(item for item in atoms if item))
        usable_understanding = bool(atoms)
        questions = atoms or split_subquestions(facts.inquiry.get("question"))
        return IntentResult(
            category=(
                (analysis.inquiry_subtype if analysis else "")
                or rule_result.category
                or "기타/직원확인"
            ),
            questions=questions,
            emotion=Emotion.NORMAL,
            urgency="NORMAL",
            confidence=float(analysis.confidence) if analysis else 1.0,
            requires_review=(
                False
                if usable_understanding
                else bool(
                    analysis.manual_review_required
                    if analysis is not None
                    else rule_result.needs_review
                )
            ),
            reason=(
                "GPT① 이해 결과로 문의를 분해했습니다."
                if usable_understanding
                else "결정적 분석으로 문의를 분해했습니다."
            ),
        )

    @staticmethod
    def _neutral_self_review(questions: tuple[str, ...]) -> SelfReviewResult:
        """Stand in for the provider's self review.

        Every field the self review reported is checked deterministically by
        AnswerValidator -- speculation by pattern, fact existence against the
        resolved facts, coverage against the sub-questions, dates against DPS.
        Asking the model to grade its own answer added a third provider call
        whose opinion could veto a draft the validator would have passed, and
        on inquiry 686097134 that is exactly what happened: the model reported
        a fact inconsistency it could not point at, and a correct partial
        answer was replaced by the generic safe draft. Grading now belongs to
        the validator alone; this neutral result keeps its signature intact.
        """

        return SelfReviewResult(
            passed=True,
            answered_all_questions=True,
            has_speculation=False,
            facts_consistent=True,
            requires_review=False,
            reason="Validator가 결정적으로 검증합니다.",
            warnings=(),
        )

    @staticmethod
    def _fallback(
        rule_result: AnswerResult,
        facts: AnswerFacts,
        *,
        reason: str,
        provider_name: str,
        events: list[HybridEvent],
        intent: IntentResult | None = None,
        draft: DraftResult | None = None,
        review: SelfReviewResult | None = None,
        validation: ValidationResult | None = None,
        telemetry: dict[str, Any] | None = None,
    ) -> HybridAnswerOutcome:
        metadata = dict(rule_result.metadata)
        metadata["hybrid"] = {
            "enabled": True,
            # A provider failure must never hand semantic publish authority
            # back to the deterministic rule/intent path.  The retained rule
            # answer is staff context only; the lifecycle records an explicit
            # workflow failure and keeps it out of Auto Post.
            "answer_pipeline": "GPT_PIPELINE_UNAVAILABLE",
            "provider": provider_name,
            "fallback_used": True,
            "fallback_reason": reason,
            "facts": {
                "warnings": list(facts.warnings),
                "available": _available_fact_paths(facts),
            },
            "intent": intent.to_dict() if intent else None,
            "draft": draft.to_dict() if draft else None,
            "self_review": review.to_dict() if review else None,
            "validation": validation.to_dict() if validation else None,
            "provider_telemetry": telemetry or {},
            "confirmed_facts": {
                "installation_date": facts.installation.get("date"),
                "required_delivery_date": facts.installation.get(
                    "required_delivery_date"
                ),
                "installation_date_source": facts.installation.get(
                    "source"
                ),
                "installation_date_status": (
                    "CONFIRMED"
                    if facts.installation.get(
                        "installation_date_confirmed"
                    )
                    else "UNCONFIRMED"
                ),
                "dps_lookup_id": facts.installation.get(
                    "dps_lookup_id"
                ),
            },
        }
        # No answer is carried out of a failed generation. This used to copy
        # ``rule_result.answer``/``provider``/``matched_rule`` through with
        # ``auto_answerable`` forced off -- the keyword answer preserved as
        # "staff context" while still being the draft body a person saw first
        # and could send. The deterministic text remains retrievable evidence;
        # it is not what the failure produces. AnswerService raises on
        # ``fallback_used`` and writes its own review draft from the customer's
        # own questions, which is the reply staff actually want to edit.
        fallback = AnswerResult(
            status=AnswerStatus.NEEDS_REVIEW,
            category=rule_result.category,
            reason=f"GPT_ANSWER_STEP_UNAVAILABLE:{reason}",
            answer="",
            provider="gpt_answer_step_unavailable",
            auto_answerable=False,
            needs_review=True,
            matched_rule="",
            warnings=tuple(rule_result.warnings),
            metadata=metadata,
        )
        events.append(
            HybridEvent(
                "GPT_ANSWER_STEP_UNAVAILABLE",
                "GPT 답변 생성이 실행되지 못해 직원 검토 Draft로 전환했습니다.",
                "WARNING",
                {"reason": reason},
            )
        )
        return HybridAnswerOutcome(
            fallback,
            facts,
            intent,
            draft,
            review,
            validation,
            True,
            tuple(events),
        )

    def generate(
        self,
        request: AnswerRequest,
        rule_result: AnswerResult,
    ) -> HybridAnswerOutcome:
        generation_started = time.monotonic()
        # Wall clock per stage. The event log records when a row was written,
        # not when the work happened -- hybrid events are flushed together
        # after generate() returns -- so the log alone cannot say where the
        # time went. These are measured in place. Durations only.
        stage_seconds: dict[str, float] = {}
        self._stage_seconds = stage_seconds

        def _stage(name: str, since: float) -> float:
            now = time.monotonic()
            stage_seconds[name] = round(now - since, 3)
            return now

        _mark = time.monotonic()
        facts = build_answer_facts(request, rule_result)
        analysis_value = request.metadata.get("phase9_analysis")
        analysis = (
            InquiryAnalysis.from_dict(analysis_value)
            if isinstance(analysis_value, dict) and analysis_value
            else None
        )
        selected_facts = (
            self.fact_selection.select(facts, analysis)
            if analysis is not None
            else SelectedFacts(
                values={
                    path: facts.get_fact(path)
                    for path in _available_fact_paths(facts)
                },
                keys=tuple(_available_fact_paths(facts)),
            )
        )
        # Which listing the customer is writing from is context, not evidence,
        # and the model needs it to read anything else in the prompt.
        #
        # ``FactSelectionService`` picks fact paths from the keyword
        # classifier's ``answer_strategy``, and MANUAL_REVIEW -- what that
        # classifier returns whenever no rule matched the wording -- selects
        # ``rule.answer`` alone. For "이 제품 해상도가 4K UHD 맞나요?" the rule
        # answer was empty, so the model was handed no product at all: not the
        # catalogue, not the option, not even the product's name. It could only
        # say it was unable to confirm.
        #
        # A classifier gap is not a reason to hide which product is being asked
        # about, so with a usable understanding the listing fields are restored
        # to whatever the selection produced. They are added, never substituted:
        # every path the strategy chose is still there.
        if self._gpt_judges_evidence(request):
            selected_facts = _with_listing_metadata(selected_facts, facts)
        _mark = _stage("facts_and_selection", _mark)
        phase9_metadata = (
            dict(rule_result.metadata.get("phase9"))
            if isinstance(rule_result.metadata.get("phase9"), dict)
            else {}
        )
        phase9_metadata["analysis"] = analysis.to_dict() if analysis else {}
        phase9_metadata["selected_facts"] = selected_facts.to_dict()
        rule_result.metadata["phase9"] = phase9_metadata
        installation_date = facts.installation.get("date")
        events = [
            HybridEvent(
                "PHASE9_FACTS_SELECTED",
                "문의 유형에 필요한 사실만 선택했습니다.",
                details={
                    "answer_strategy": (
                        analysis.answer_strategy.value if analysis else None
                    ),
                    "selected_fact_keys": list(selected_facts.keys),
                },
            ),
            HybridEvent(
                "GPT_FACTS_READY",
                "현재 문의의 AnswerFacts 준비를 완료했습니다.",
                details={
                    "status": (
                        "READY" if installation_date else "NO_DATE"
                    ),
                    "dps_lookup_id": facts.installation.get(
                        "dps_lookup_id"
                    ),
                },
            ),
            HybridEvent(
                "ANSWER_FACTS_INSTALLATION_DATE_INCLUDED",
                "현재 문의의 설치예정일 Facts를 준비했습니다.",
                details={
                    "status": (
                        "CONFIRMED"
                        if facts.installation.get(
                            "installation_date_confirmed"
                        )
                        else "UNCONFIRMED"
                    ),
                    "normalized_date": installation_date,
                    "dps_lookup_id": facts.installation.get(
                        "dps_lookup_id"
                    ),
                },
            ),
            HybridEvent(
                "GPT_PROMPT_FACTS_READY",
                "GPT Prompt용 확정 Facts를 준비했습니다.",
                details={
                    "status": (
                        "READY" if installation_date else "NO_DATE"
                    ),
                    "normalized_date": installation_date,
                    "dps_lookup_id": facts.installation.get(
                        "dps_lookup_id"
                    ),
                },
            ),
            HybridEvent(
                "GPT_PROMPT_READY",
                "현재 문의의 GPT Prompt 준비를 완료했습니다.",
                details={
                    "status": "READY",
                    "normalized_date": installation_date,
                },
            ),
            HybridEvent(
                "GPT_ANALYSIS_STARTED",
                "Facts 기반 GPT 문의 분석을 시작했습니다.",
                details={"provider": self.provider.name},
            )
        ]
        intent: IntentResult | None = None
        draft: DraftResult | None = None
        review: SelfReviewResult | None = None
        validation: ValidationResult | None = None
        # The plan-level pre-generation gate used to run here and could stop
        # the provider call from the keyword classifier's intent, subtype and
        # high-risk flags alone. Whether an inquiry is answerable is a reading
        # of the question, so it is the understanding stage's to make; the call
        # is removed rather than neutralised.
        try:
            events.append(
                HybridEvent(
                    "GPT_PROVIDER_STARTED",
                    "GPT Provider 호출을 시작했습니다.",
                    details={"provider": self.provider.name},
                )
            )
            intent = self._deterministic_intent(
                facts,
                analysis,
                rule_result,
                request.metadata.get("_semantic_routing_value"),
            )
            _mark = _stage("intent", _mark)
            events.append(
                HybridEvent(
                    "GPT_ANALYSIS_COMPLETED",
                    "문의 분석을 완료했습니다.",
                    details={
                        "category": intent.category,
                        "emotion": intent.emotion.value,
                        "question_count": len(intent.questions),
                        "confidence": intent.confidence,
                        "source": "DETERMINISTIC",
                    },
                )
            )
            learning_context: dict[str, Any] = {}
            # REQUEST_ORDER_ID used to short-circuit here: ``learning_context``
            # stayed empty, the provider was never called, and the deterministic
            # rule body became the draft. The strategy is a keyword-tier
            # conclusion -- "this inquiry needs the customer's order number and
            # does not have it" -- and when the understanding stage was
            # unavailable nothing recomputed that premise, so a product or
            # policy question could lose Learning, Historical, the product
            # record and the answer step together. Measured on 688393266: with
            # a usable understanding the premise was withdrawn and the inquiry
            # retrieved and answered normally, which is the only reason the
            # branch did not fire.
            #
            # Asking the customer for an order number is still a real
            # behaviour; it belongs to the execution route that owns it
            # (``AnswerService`` -> ORDER_ID_REQUEST), which is reached before
            # generation and is untouched. Inside semantic generation the
            # strategy is now context like any other, not an answer.
            try:
                if self._learning_context_provider is None:
                    learning_context = {}
                else:
                    try:
                        # The semantic pass is a real understanding already
                        # paid for before routing.  Keep it attached to the
                        # retrieval request: otherwise atomic questions were
                        # persisted for audit but retrieval still saw only
                        # the old keyword split.
                        learning_context = self._learning_context_provider(
                            facts,
                            intent,
                            semantic_analysis=request.metadata.get(
                                "_semantic_routing_value"
                            ),
                        )
                    except TypeError:
                        # Existing integrations intentionally expose the
                        # historical two-argument callable.  They are not
                        # semantic-aware but remain safe, and must not be
                        # silently converted into an empty context.
                        learning_context = self._learning_context_provider(
                            facts, intent
                        )
            except Exception:
                # Learning is an optional enrichment and can never block
                # GPT.  Computed once here (instead of inside
                # DraftGenerationService) so a bounded corrective
                # regeneration below can reuse it without a second
                # Learning/Historical DB lookup.
                learning_context = {}
            # Product facts travel alongside Learning, never merged into
            # it: Learning carries tone, policy and past answers, product
            # facts carry this product's verified specification. Both
            # reach the prompt; neither overwrites the other.
            learning_context.update(self._product_facts_context(request))
            # AnswerEngine and Phase9 candidates are rendered by existing
            # deterministic code, but a usable GPT① route does not let
            # either one terminate a compound inquiry.  Carry their
            # compact provenance into the same evidence context as
            # Product and Learning so GPT② can choose, combine or reject
            # them.  This is intentionally metadata reuse, not a second
            # template selector.
            template_candidates = request.metadata.get(
                "template_candidates"
            )
            if isinstance(template_candidates, list):
                learning_context["template_candidates"] = [
                    dict(item)
                    for item in template_candidates
                    if isinstance(item, dict)
                ]
            # ...and they are evidence, not just prompt text. Applied
            # before the conflict pass below so a product fact that
            # contradicts an approved Learning answer is still resolved
            # as a CONFLICT rather than silently winning.
            learning_context = self._apply_product_fact_evidence(
                request, learning_context
            )
            # PRE-GENERATION GATE (2/2) -- the retrieved evidence.
            # Retrieval and the product-fact lookup are local reads, so
            # both sides of a contradiction are known while the provider
            # is still untouched. If the sources for a sub-question flatly
            # disagree, no wording of an answer is publishable, and asking
            # the model to write one would only mean handing it both sides
            # of a dispute a person has to settle.
            learning_context = self._apply_evidence_conflicts(
                request, learning_context
            )
            # The evidence-level pre-generation gate used to run here: a
            # sub-question whose sources disagreed skipped composition
            # entirely, so one disputed atom erased the independently
            # grounded answers beside it. A conflict holds publication, not
            # composition -- it is marked on the evidence, the model reads
            # both sides and leaves the disputed claim unresolved, and the
            # publishing gate decides. The call is removed.
            # The draft provider is the one evidence reader in this path.
            # A selector/verifier pair used to run here, making two further
            # semantic provider calls after retrieval and before drafting
            # without changing the prompt, and deciding which retrieved
            # candidates the answer step was allowed to see. Production
            # switched it off and then nothing turned it back on; choosing the
            # evidence is GPT ②'s, so it is gone rather than disabled.
            draft = self.drafts.generate(
                facts,
                intent,
                analysis=analysis,
                selected_facts=selected_facts,
                learning_context=learning_context,
                gpt_judged_evidence=self._gpt_judges_evidence(request),
            )
            events.append(
                HybridEvent(
                    "GPT_RESPONSE_NORMALIZED",
                    "GPT 응답 본문 정규화를 완료했습니다.",
                    details={
                        "status": (
                            "NON_EMPTY"
                            if draft.answer.strip()
                            else "EMPTY"
                        ),
                        "answer_length": len(draft.answer.strip()),
                    },
                )
            )
            events.append(
                HybridEvent(
                    "GPT_RESPONSE_RECEIVED",
                    "GPT 응답 본문을 수신했습니다.",
                    details={
                        "status": (
                            "RECEIVED"
                            if draft.answer.strip()
                            else "EMPTY"
                        ),
                        "normalized_date": installation_date,
                    },
                )
            )
            events.append(
                HybridEvent(
                    "GPT_DRAFT_CREATED",
                    "Facts 기반 GPT 답변 후보를 생성했습니다.",
                    details={
                        "confidence": draft.confidence,
                        "used_facts": list(draft.used_facts),
                        "missing_information": list(
                            draft.missing_information
                        ),
                    },
                )
            )
            events.append(
                HybridEvent(
                    "LEARNING_ANSWER_USAGE_EVALUATED",
                    "선택 Learning의 실제 답변 근거 사용 여부를 확인했습니다.",
                    level=(
                        "WARNING"
                        if draft.learning_recovery_used
                        else "INFO"
                    ),
                    details={
                        "used_count": sum(
                            1
                            for item in draft.learning_usage
                            if item.get("answer_supported")
                        ),
                        "learning_usage": [
                            {
                                "learning_id": item.get("learning_id"),
                                "matched_subquestion": item.get(
                                    "matched_subquestion"
                                ),
                                "answer_supported": bool(
                                    item.get("answer_supported")
                                ),
                                "reason": item.get("reason"),
                            }
                            for item in draft.learning_usage
                        ],
                        "subquestion_results": [
                            dict(item)
                            for item in draft.subquestion_results
                        ],
                        "learning_recovery_used": (
                            draft.learning_recovery_used
                        ),
                    },
                )
            )
            # The matching self-review shortcut is gone with the draft
            # shortcut above: there is no longer a draft here that the
            # deterministic path wrote, so there is nothing to declare
            # pre-reviewed. The validator remains the decisive check.
            review = self._neutral_self_review(intent.questions)
            events.append(
                HybridEvent(
                    "GPT_PROVIDER_FINISHED",
                    "GPT Provider 호출을 완료했습니다.",
                    details={
                        "provider": self.provider.name,
                        "status": "COMPLETED",
                    },
                )
            )
            events.append(
                HybridEvent(
                    "GPT_SELF_REVIEW",
                    "GPT 답변 자체 검토를 수행했습니다.",
                    level="INFO" if review.passed else "WARNING",
                    details={
                        "passed": review.passed,
                        "requires_review": review.requires_review,
                    },
                )
            )
            events.append(
                HybridEvent(
                    "GPT_VALIDATOR_STARTED",
                    "GPT 답변 Validator 확인을 시작했습니다.",
                )
            )
            _mark = _stage("draft_provider_call", _mark)
            validation = self.validator.validate(
                facts,
                intent,
                draft,
                review,
                analysis=analysis,
                selected_facts=selected_facts,
                subquestion_evidence=learning_context.get("subquestion_evidence"),
                evidence_texts=_join_evidence(
                    self._evidence_texts(learning_context),
                    self._product_facts_evidence(request),
                ),
                gpt_judged_evidence=self._gpt_judges_evidence(request),
            )
            _mark = _stage("validation", _mark)
            events.append(
                HybridEvent(
                    "GPT_VALIDATOR_FINISHED",
                    "GPT 답변 Validator 확인을 완료했습니다.",
                    level="INFO" if validation.passed else "WARNING",
                    details={
                        "status": (
                            "PASSED" if validation.passed else "FAILED"
                        )
                    },
                )
            )
            if (
                facts.installation.get(
                    "installation_date_confirmed"
                )
                and any(
                    (
                        "누락" in error
                        or "확인할 수 없" in error
                    )
                    for error in validation.errors
                )
            ):
                events.append(
                    HybridEvent(
                        "GPT_INSTALLATION_DATE_MISSING_IN_ANSWER",
                        "GPT 답변에 확정 설치예정일이 반영되지 않았습니다.",
                        "WARNING",
                        {
                            "status": "VALIDATION_FAILED",
                            "normalized_date": installation_date,
                            "dps_lookup_id": facts.installation.get(
                                "dps_lookup_id"
                            ),
                        },
                    )
                )
            if facts.dps.get("requires_human_review"):
                events.append(
                    HybridEvent(
                        "GPT_INSTALLATION_DATE_CONFLICT",
                        "복수 설치 일정 충돌로 직원 확인이 필요합니다.",
                        "WARNING",
                        {
                            "status": "CONFLICT",
                            "dps_lookup_id": facts.installation.get(
                                "dps_lookup_id"
                            ),
                        },
                    )
                )
            # Regeneration was withheld for REQUEST_ORDER_ID because the
            # draft had not been generated. It is generated now, so a rejected
            # one gets the same single corrective attempt as any other.
            if not validation.passed:
                # Bounded, single corrective regeneration: a rejected draft is
                # often a fixable blanket-uncertainty or speculation problem,
                # not proof that no grounded answer exists.  Reuse the same
                # pre-computed learning_context (no extra Learning/Historical
                # query) and give the provider the concrete rejection reasons
                # so it can answer the supported parts and only ask for
                # confirmation on the parts that actually lack evidence.
                # Exactly one retry: no loop, no extra DPS call, no repeated
                # abuse of the provider.
                events.append(
                    HybridEvent(
                        "GPT_CORRECTIVE_REGENERATION_STARTED",
                        "검증 실패 답변에 대해 1회 보정 재생성을 시도합니다.",
                        "WARNING",
                        {"previous_errors": list(validation.errors)},
                    )
                )
                retry_feedback = {
                    "previous_attempt_rejected": True,
                    "previous_validation_errors": list(validation.errors),
                    "instruction": (
                        "이전 답변은 검증에 실패했습니다. 근거가 있는 "
                        "sub-question만 사실에 기반해 답하고, 근거가 없는 "
                        "부분은 추측하지 말고 확인이 필요하다고 안내하세요. "
                        "질문과 관련 없는 내용을 답변에 포함하지 마세요."
                    ),
                }
                retry_draft = self.drafts.generate(
                    facts,
                    intent,
                    analysis=analysis,
                    selected_facts=selected_facts,
                    learning_context=learning_context,
                    retry_feedback=retry_feedback,
                )
                retry_review = self._neutral_self_review(intent.questions)
                retry_validation = self.validator.validate(
                    facts,
                    intent,
                    retry_draft,
                    retry_review,
                    analysis=analysis,
                    selected_facts=selected_facts,
                    subquestion_evidence=learning_context.get("subquestion_evidence"),
                    evidence_texts=_join_evidence(
                    self._evidence_texts(learning_context),
                    self._product_facts_evidence(request),
                ),
                    gpt_judged_evidence=self._gpt_judges_evidence(request),
                )
                events.append(
                    HybridEvent(
                        "GPT_CORRECTIVE_REGENERATION_COMPLETED",
                        (
                            "보정 재생성 답변이 Validator를 통과했습니다."
                            if retry_validation.passed
                            else "보정 재생성 답변도 Validator를 통과하지 "
                            "못했습니다."
                        ),
                        "INFO" if retry_validation.passed else "WARNING",
                        {
                            "passed": retry_validation.passed,
                            "errors": list(retry_validation.errors),
                        },
                    )
                )
                if retry_validation.passed:
                    draft, review, validation = (
                        retry_draft,
                        retry_review,
                        retry_validation,
                    )
            if not validation.passed:
                events.append(
                    HybridEvent(
                        "GPT_VALIDATION_FAILED",
                        "GPT 답변이 Facts 검증을 통과하지 못했습니다.",
                        "WARNING",
                        {"errors": list(validation.errors)},
                    )
                )
                return self._fallback(
                    rule_result,
                    facts,
                    reason="VALIDATION_FAILED",
                    provider_name=self.provider.name,
                    events=events,
                    intent=intent,
                    draft=draft,
                    review=review,
                    validation=validation,
                    telemetry=self._provider_telemetry(
                        started=generation_started
                    ),
                )
            # The legacy validator still records semantic rule-policy signals
            # for operator diagnostics.  Once GPT① supplied usable
            # understanding and GPT② resolved its evidence, those signals are
            # not an independent publish authority.  Preserve them in
            # ``warnings``/``review_signals`` but persist a PASS technical
            # verdict; true technical failures already reached the branch
            # above with ``passed=False``.
            if (
                bool(
                    getattr(
                        request.metadata.get("_semantic_routing_value"),
                        "usable",
                        False,
                    )
                )
                and validation.status == "REVIEW_REQUIRED"
            ):
                validation = ValidationResult(
                    passed=True,
                    errors=validation.errors,
                    warnings=validation.warnings,
                    checked_facts=validation.checked_facts,
                    status="PASS",
                    rules=validation.rules,
                    review_signals=validation.review_signals,
                )
            # Whose verdict holds an answer back.
            #
            # ``rule_result`` is the keyword engine's attempt at this inquiry.
            # Once GPT ① is understanding the question, that attempt is one
            # candidate among several and its failure to match is not a finding
            # about the answer GPT ② wrote: "삼성기사분이 설치하러 오시나요"
            # renders 기타/직원확인 with ``needs_review=True`` purely because no
            # substring matched, and that flag held every answer downstream.
            # Where GPT ① is unusable the rule result is the only reading of
            # the inquiry there is, so it keeps its authority untouched.
            #
            # GPT ②'s own findings are added here instead: an item it could not
            # resolve, or an explicit refusal to publish.
            gpt_understanding_usable = bool(
                getattr(
                    request.metadata.get("_semantic_routing_value"),
                    "usable",
                    False,
                )
            )
            requires_review = bool(
                # GPT② is the sole semantic publish authority once GPT①
                # supplied usable understanding.  Rule/intent/self-review
                # flags remain diagnostic telemetry; they must not revive as
                # a second review decision after GPT② resolved every atom.
                # If GPT① was unavailable, the downstream workflow gate
                # records UNDERSTANDING_UNAVAILABLE rather than treating a
                # legacy rule classification as an answer verdict.
                draft.requires_review
                or draft.has_required_missing_information
                or draft.unresolved
                or draft.can_auto_post is False
            )
            status = (
                AnswerStatus.NEEDS_REVIEW
                if requires_review
                else AnswerStatus.GENERATED
            )
            metadata = dict(rule_result.metadata)
            metadata["phase9"] = {
                "analysis": analysis.to_dict() if analysis else {},
                "selected_facts": selected_facts.to_dict(),
            }
            product_facts_context = self._product_facts_context(request)
            metadata["hybrid"] = {
                "enabled": True,
                # One pipeline now, so this is a statement rather than a
                # choice. Historical drafts may still carry
                # ``LEGACY_SELECTOR_VERIFIER``; readers keep recognising it.
                "answer_pipeline": "GPT_UNDERSTAND_RETRIEVE_ANSWER",
                "provider": self.provider.name,
                "fallback_used": False,
                # Recorded from the context that was actually built for this
                # generation. The auto-post gate requires this to be true
                # before a product fact may settle anything, so that a fact
                # the model never read can never justify publishing.
                "product_catalog_in_prompt": bool(product_facts_context),
                # The approved-Learning verdict from this same generation.
                # Persisted rather than recomputed downstream so the gate can
                # only ever judge the evidence the model was actually given.
                "approved_learning_evidence": dict(
                    learning_context.get("approved_learning_evidence") or {}
                ),
                # One compact, persisted retrieval trace answers the operator
                # question "why was Learning not used?" without re-running a
                # selector.  Candidate selection remains the responsibility
                # of GPT ANSWER; this is provenance only.
                "retrieval": {
                    "learning": dict(
                        learning_context.get("learning_retrieval") or {}
                    ),
                    "product_catalog": {
                        "requested": bool(product_facts_context),
                        "fact_count": len(
                            product_facts_context.get("product_catalog", {})
                            .get("facts", [])
                        ),
                    },
                },
                "product_fact_fields": sorted(
                    str(item.get("field_key") or "")
                    for item in (
                        product_facts_context.get("product_catalog", {})
                        .get("facts", [])
                    )
                ),
                "provider_telemetry": self._provider_telemetry(
                    started=generation_started
                ),
                "facts": {
                    "warnings": list(facts.warnings),
                    "available": _available_fact_paths(facts),
                },
                "intent": intent.to_dict(),
                "draft": draft.to_dict(),
                "self_review": review.to_dict(),
                "validation": validation.to_dict(),
                "phase9": metadata["phase9"],
                # Persisted so the downstream auto-post eligibility gate can
                # judge on Evidence/Authority instead of a bare confidence
                # number.  Retrieval already computed this; nothing is
                # recomputed and no extra provider call is made.
                "subquestion_evidence": [
                    dict(item)
                    for item in (
                        learning_context.get("subquestion_evidence") or []
                    )
                ],
                "confirmed_facts": {
                    "installation_date": installation_date,
                    "required_delivery_date": facts.installation.get(
                        "required_delivery_date"
                    ),
                    "installation_date_source": facts.installation.get(
                        "source"
                    ),
                    "installation_date_status": (
                        "CONFIRMED"
                        if facts.installation.get(
                            "installation_date_confirmed"
                        )
                        else "UNCONFIRMED"
                    ),
                    "dps_lookup_id": facts.installation.get(
                        "dps_lookup_id"
                    ),
                },
            }
            result = AnswerResult(
                status=status,
                category=intent.category or rule_result.category,
                reason=(
                    "Facts 기반 GPT 답변이 자체 검토와 Validator를 통과했습니다."
                ),
                answer=draft.answer,
                provider=f"{self.provider.name}_hybrid",
                auto_answerable=not requires_review,
                needs_review=requires_review,
                matched_rule=rule_result.matched_rule,
                warnings=tuple(
                    dict.fromkeys(
                        [
                            *rule_result.warnings,
                            *draft.warnings,
                            *review.warnings,
                            *validation.warnings,
                        ]
                    )
                ),
                metadata=metadata,
            )
            events.append(
                HybridEvent(
                    "GPT_APPROVED",
                    "GPT 답변이 Validator를 통과해 Program Answer로 채택되었습니다.",
                    details={
                        "confidence": draft.confidence,
                        "requires_review": requires_review,
                    },
                )
            )
            return HybridAnswerOutcome(
                result,
                facts,
                intent,
                draft,
                review,
                validation,
                False,
                tuple(events),
            )
        except Exception as error:
            return self._fallback(
                rule_result,
                facts,
                reason=error.__class__.__name__.upper(),
                provider_name=self.provider.name,
                events=events,
                intent=intent,
                draft=draft,
                review=review,
                validation=validation,
                telemetry=self._provider_telemetry(
                    started=generation_started
                ),
            )


# The listing fields that say which product the inquiry came from. Kept to the
# three the customer's own page shows; nothing here is a catalogued
# specification, and the prompt labels them separately for that reason.
_LISTING_FACT_PATHS: tuple[str, ...] = (
    "product.product_id", "product.name", "product.option_name",
)


def _with_listing_metadata(
    selected: SelectedFacts, facts: AnswerFacts
) -> SelectedFacts:
    """Ensure the current listing is visible, whatever the strategy selected."""

    values = dict(selected.values)
    keys = list(selected.keys)
    for path in _LISTING_FACT_PATHS:
        if path in values:
            continue
        value = facts.get_fact(path)
        if value in (None, "", [], {}, ()):
            continue
        values[path] = value
        keys.append(path)
    return SelectedFacts(values=values, keys=tuple(keys))


def _available_fact_paths(facts: AnswerFacts) -> list[str]:
    result: list[str] = []
    for section, values in facts.to_prompt_dict().items():
        if isinstance(values, dict):
            for key, value in values.items():
                if value not in (None, "", [], {}, ()):
                    result.append(f"{section}.{key}")
        elif values not in (None, "", [], {}, ()):
            result.append(section)
    return result
