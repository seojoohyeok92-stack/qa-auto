from __future__ import annotations

import re
from typing import Any, Callable

from answer.facts import AnswerFacts
from answer.fact_selection import SelectedFacts
from answer.hybrid_models import DraftResult, IntentResult
from answer.inquiry_analysis import InquiryAnalysis
from answer.prompt_builder import PromptBuilder
from answer.providers.interfaces import JsonGptProvider
from services.learning_context_service import (
    DRAFT_PROMPT_BUDGET_CHARS,
    apply_prompt_budget,
    prompt_context,
)


_SUMMARY_REQUEST = re.compile(r"간단|간략|요약|대략|기본(?:적인|으로)?")
_OPTIONAL_DETAIL_QUALIFIER = re.compile(
    r"세부|상세|구체|단계별|부품별|나사별|체결별"
)
_OPTIONAL_MANUAL_DETAIL = re.compile(
    r"조립|설치|체결|순서|절차|방법|매뉴얼|설명서"
)
_SAFE_DETAIL_DEFERRAL = re.compile(
    r"설명서|매뉴얼|제품 안내|제조사 안내|설치 기사|전문 기사|기사 안내|"
    r"확인해\s*(?:주세요|주시기|보시기)|참고해\s*(?:주세요|주시기|보시기)"
)
_REQUIRED_FACT_DETAIL = re.compile(
    r"호환|브라켓|모델|옵션|배송|도착|설치일|예정일|날짜|기간|A/S|AS|"
    r"할인|혜택|주문|환불|반품|취소|파손|분쟁|책임|개인정보"
)
# The operational subset of the above. Deferring one of these to the installer
# is not an answer: they are commitments about somebody's order, a date, or
# money, and a person decides them however safely the sentence is worded. The
# product-advisory terms (호환/브라켓/모델/옵션) are deliberately absent -- those
# are exactly what an answer may safely defer once it has answered the
# question, which is what inquiry 686504818 did.
_OPERATIONAL_COMMITMENT_DETAIL = re.compile(
    r"배송|도착|설치일|예정일|날짜|기간|주문|환불|반품|교환|취소|"
    r"파손|분쟁|책임|보상|개인정보|결제|금액|가격|비용|요금|"
    r"할인|혜택|A/S|AS|재고|입고"
)


def _int_ids(value: Any) -> tuple[int, ...]:
    """Identifiers the provider reported, keeping only the ones that are ids."""

    if not isinstance(value, list):
        return ()
    found: list[int] = []
    for item in value:
        try:
            found.append(int(item))
        except (TypeError, ValueError):
            continue
    return tuple(dict.fromkeys(found))


def _all_subquestions_answered(value: Any) -> bool:
    """Whether the draft reported answering every sub-question it was given.

    The provider records this per sub-question alongside the retrieval status
    it used, and it reports ``answered: false`` honestly -- inquiries 2655,
    2692 and 2702 all did. An empty or malformed record is not a yes.
    """

    if not isinstance(value, list) or not value:
        return False
    for item in value:
        if not isinstance(item, dict):
            return False
        if not bool(item.get("answered")):
            return False
        if str(item.get("status") or "").upper() != "ANSWERABLE":
            return False
    return True


def _atomic_question_payload(
    analysis: InquiryAnalysis | None,
    learning_context: dict[str, Any],
) -> list[dict[str, Any]]:
    """Each question the customer asked, with its own verdict and evidence.

    The classifier already decides these one at a time and the aggregate then
    ORs them together, which is the right input to the *safety* gates and the
    wrong input to drafting: one part needing a person told the model nothing
    about the other three. Handing the parts over individually is what lets a
    draft answer what it can and defer only what it must.

    Evidence is attached per question rather than pooled, so a Learning found
    for the bracket question cannot present itself as grounds for the smart-TV
    one. Retrieval already decided which sub-question each source belongs to;
    this only keeps that pairing intact instead of flattening it.
    """

    records = getattr(analysis, "subquestion_analyses", ()) or ()
    if len(records) < 2:
        # A single question needs no breakdown -- the whole prompt is about it.
        return []
    evidence_by_question: dict[str, dict[str, Any]] = {}
    for item in (learning_context.get("subquestion_evidence") or []):
        if isinstance(item, dict):
            key = str(item.get("subquestion") or "").strip()
            if key:
                evidence_by_question[key] = item
    # The property each question asked about, from the semantic pass that
    # produced these questions in the first place. Joined by the question text
    # the two sides already share.
    attribute_by_question: dict[str, dict[str, Any]] = {}
    for item in (learning_context.get("semantic_atoms") or []):
        if isinstance(item, dict):
            key = str(item.get("text") or "").strip()
            if key:
                attribute_by_question[key] = item

    payload: list[dict[str, Any]] = []
    for index, record in enumerate(records, start=1):
        question = str(record.get("question") or "").strip()
        evidence = evidence_by_question.get(question) or {}
        review_required = bool(record.get("manual_review_required"))
        atom = attribute_by_question.get(question) or {}
        payload.append({
            "index": index,
            "question": question,
            # The fact the customer is missing, and which property of it they
            # asked for. Answering a different property of the same subject is
            # how "설치해 주시나요" turns into an answer about the cost.
            "requested_information": atom.get("requested_information") or None,
            "requested_attribute": atom.get("requested_attribute") or None,
            "inquiry_subtype": record.get("inquiry_subtype"),
            "detected_intent": record.get("detected_intent"),
            "answerable": not review_required,
            "review_required": review_required,
            "evidence_status": evidence.get("status"),
            "evidence_coverage": evidence.get("evidence_coverage"),
            "evidence_source": evidence.get("source"),
            "learning_ids": list(evidence.get("learning_ids") or []),
            "product_fact_fields": list(
                evidence.get("product_fact_fields") or []
            ),
            "unresolved_reason": (
                str(record.get("inquiry_subtype") or "UNRESOLVED")
                if review_required
                else None
            ),
        })
    return payload


ATOMIC_QUESTION_INSTRUCTIONS = (
    "문의에 포함된 각 질문을 하나도 빠뜨리지 말고 순서대로 다룬다.",
    "evidence가 있는 질문만 사실로 답한다.",
    "review_required 또는 근거가 없는 질문은 추측하지 않고 확인이 필요하다고만 안내한다.",
    "확인이 필요한 질문 때문에 답변 가능한 다른 질문까지 회피하지 않는다.",
    "각 질문의 근거는 그 질문에 연결된 evidence만 사용한다.",
    # The contract that was missing. Without it the model may answer a
    # neighbouring property of the same subject -- cost instead of whether the
    # service is performed -- and the answer reads as responsive while telling
    # the customer about something they did not raise.
    "각 질문은 requested_attribute 가 가리키는 속성만 답한다.",
    "고객이 묻지 않은 속성(비용, 시점, 부담 주체 등)을 새로 만들어 답하거나"
    " 확인이 필요하다고 언급하지 않는다.",
)


class DraftGenerationService:
    def __init__(
        self,
        provider: JsonGptProvider,
        *,
        prompt_builder: PromptBuilder | None = None,
        learning_context_provider: Callable[[AnswerFacts, IntentResult], dict[str, Any]] | None = None,
    ) -> None:
        self.provider = provider
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.learning_context_provider = learning_context_provider

    def generate(
        self,
        facts: AnswerFacts,
        intent: IntentResult,
        *,
        analysis: InquiryAnalysis | None = None,
        selected_facts: SelectedFacts | None = None,
        learning_context: dict[str, Any] | None = None,
        retry_feedback: dict[str, Any] | None = None,
        gpt_judged_evidence: bool = False,
    ) -> DraftResult:
        if selected_facts is None:
            context = {
                "rule": dict(facts.rule),
                "dps": {
                    "delivery_status": facts.delivery.get("status"),
                    "installation_status": facts.installation.get("status"),
                    "installation_date": facts.installation.get("date"),
                    "required_delivery_date": facts.installation.get(
                        "required_delivery_date"
                    ),
                    "installation_date_source": facts.installation.get(
                        "source"
                    ),
                    "date_parse_status": facts.installation.get(
                        "date_parse_status"
                    ),
                    "installation_date_confirmed": facts.installation.get(
                        "installation_date_confirmed"
                    ),
                },
                "intent": intent.to_dict(),
            }
        else:
            context = {
                "allowed_facts": dict(selected_facts.values),
                "answer_strategy": (
                    analysis.answer_strategy.value if analysis else ""
                ),
                "intent": intent.to_dict(),
            }
        if learning_context is None:
            try:
                learning_context = (
                    self.learning_context_provider(facts, intent)
                    if self.learning_context_provider is not None
                    else {}
                )
            except Exception:
                # Learning is an optional enrichment and can never block GPT.
                learning_context = {}
        evidence, budget_report = apply_prompt_budget(
            prompt_context(learning_context)
        )
        self.last_prompt_budget = budget_report
        # Only the evidence reaches the model. The retrieval traces stay in
        # `context` below for provenance: they describe how candidates were
        # found, not what the answer may assert, and they scale with the size
        # of the learning database rather than with the inquiry.
        prompt_input = {
            "intent": intent.to_dict(),
            # What each block *is*, not which one wins.
            #
            # This used to be a ranking, and the ranking decided answers. With
            # FIXED_TEMPLATE above SIMILAR_APPROVED_ANSWERS, 688159337's model
            # read a "확인이 필요합니다" rule as outranking the approved answer
            # that settled the question, and wrote its refusal as "현재 적용되는
            # 우선 답변에서 정확한 확인이 필요하다고 명시하고 있어". Calling the
            # historical block REFERENCE_ONLY did the same job more quietly:
            # every historical candidate came back rejected as "과거 사례" while
            # two other blocks of the same prompt describe those rows as
            # verified, reusable knowledge.
            #
            # Which evidence applies to this question is a judgement about
            # meaning. The prompt now says what each source is and leaves the
            # judgement where it belongs.
            "context_sources": {
                "CURRENT_INQUIRY": "고객이 지금 물은 내용. 답변의 대상.",
                "CURRENT_ORDER_AND_DPS": (
                    "이 고객의 현재 주문/배송 사실. 시간에 의존하는 사실은"
                    " 여기에서만 온다."
                ),
                # Named for the block it describes. "PRODUCT_FACTS" would
                # not match any block in the prompt, and a test that
                # asserts no product facts reached the model checks for
                # exactly that string.
                "PRODUCT_CATALOG_JSON": "이 상품에 대해 검증된 사양.",
                "APPROVED_LEARNING": (
                    "사람이 승인한 과거 답변. 사실 근거로 사용 가능."
                    " 각 후보의 evidence_origin.identity가 출처를 알려준다:"
                    " SAME_PRODUCT는 현재 상품에서 나온 자료,"
                    " OTHER_PRODUCT_OR_MODEL은 다른 상품/모델에서 나온 자료,"
                    " POLICY_OR_GENERAL은 상품과 무관한 정책·운영 안내,"
                    " UNKNOWN_IDENTITY는 출처 상품을 특정하지 못한 자료다."
                    " evidence_origin.knowledge가 PRODUCT_SPECIFIC이면 그 내용은"
                    " 특정 모델의 사양·구성이므로 다른 상품에 자동 적용하지 마라."
                    " 다른 상품에서 나온 자료라고 해서 무조건 버리지도 마라:"
                    " 질문의 성격, 정책 공통성, PRODUCT_CATALOG_JSON,"
                    " 다른 근거와 함께 현재 문의에 적용 가능한지 직접 판단하고,"
                    " 적용했다면 그 근거를 답변 근거로 보고하라."
                    # Two more provenance fields, for the same reason the
                    # identity label exists: the row is offered, so the model
                    # has to be able to tell what kind of thing it is holding.
                    " evidence_authority는 이 답변의 출처 권위다:"
                    " APPROVED는 담당자가 검수·승인한 답변이고,"
                    " SELLER_POSTED_NOT_VERIFIED는 과거에 판매자가 실제로 고객에게"
                    " 보낸 답변이지만 사실 근거로는 검증되지 않은 것이며,"
                    " AUTO는 파이프라인이 생성한 기록이다."
                    " 확정 사실로 인용할 수 있는 것은 APPROVED 뿐이다."
                    " hedge_reason이 비어 있지 않으면 그 답변은 스스로 추정임을"
                    " 밝힌 문장이다."
                    " SENTENCE 단위로 읽어라: 추정 표현이 있다고 해서 그 답변의"
                    " 다른 정책·사실 내용까지 버리지 말고, 반대로 추정인 부분을"
                    " 확정 사실처럼 단정하지도 마라."
                    " 검수되지 않은 출처나 추정 문장만으로 확정 표현을 쓰면"
                    " 근거 없는 주장으로 처리된다. 그런 근거로 답할 때는"
                    " 확인이 필요하다고 안내하거나 unresolved로 남겨라."
                ),
                "HISTORICAL_CASES": (
                    "과거 상담 기록. 안정적인 운영 지식이면 사실 근거로 사용 가능하며,"
                    " 특정 주문의 사실로는 사용할 수 없다."
                ),
                "TEMPLATE_CANDIDATE": (
                    "결정적 규칙이 낸 후보 문안. 다른 후보와 같은 자격의 후보이며"
                    " 우선 적용되는 답변이 아니다."
                ),
                "SELLER_STYLE_EXAMPLES": "문체 참고용. 사실 근거가 아니다.",
                "OJE_STYLE_RULES": "표현 규칙.",
            },
            **evidence,
        }
        atomic_questions = _atomic_question_payload(analysis, learning_context)
        if atomic_questions:
            prompt_input["atomic_questions"] = atomic_questions
            prompt_input["atomic_question_instructions"] = list(
                ATOMIC_QUESTION_INSTRUCTIONS
            )
        if retry_feedback:
            prompt_input["prior_attempt_feedback"] = retry_feedback
        context.update(learning_context)

        def _built(extra: dict[str, Any]) -> str:
            return self.prompt_builder.build(
                task="DRAFT",
                facts=facts,
                extra=extra,
                analysis=analysis,
                selected_facts=selected_facts,
            )

        # The budget now measures the prompt the provider is actually handed.
        #
        # It used to measure the learning evidence alone and compare that to
        # the whole-prompt limit, so every other block -- the product record,
        # the sub-question evidence map, the instruction text -- was spent
        # without being counted. Nothing enforced the limit it was named for: a
        # prompt of 67,805 characters passed a 60,000 character budget whose
        # own report said it fitted.
        #
        # Authority is still never trimmed. Only the groups in
        # ``_PROMPT_TRIM_ORDER`` shrink, least relevant entry last-first, and
        # facts, the product record and the customer's own question are not
        # among them.
        prompt_text = _built(prompt_input)
        if len(prompt_text) > DRAFT_PROMPT_BUDGET_CHARS:
            prompt_input, assembled_report = apply_prompt_budget(
                prompt_input, measure=lambda value: len(_built(value)),
            )
            prompt_text = _built(prompt_input)
            self.last_prompt_budget = {
                **(budget_report or {}),
                "assembled": assembled_report,
            }
        raw = self.provider.generate_json(
            task="DRAFT",
            prompt=prompt_text,
            context=self.prompt_builder.safe_payload(context),
        )
        raw = self._apply_learning_grounded_recovery(
            raw, learning_context
        )
        raw = self._validate_historical_usage(raw, learning_context)
        raw = self._validate_feedback_signal_usage(raw, learning_context)
        # ``missing_information`` is the model listing what it does not know.
        # Sorting those entries into "blocking" and "safe to defer" used to be
        # done here by regex, which is a judgement about meaning made from
        # wording. Where GPT ② reports ``unresolved`` directly it has already
        # answered the same question about the same text, so the regex pass is
        # skipped and its verdict comes from the model.
        raw = (
            self._apply_reported_resolution(raw)
            if gpt_judged_evidence
            else self._classify_missing_information(raw, intent)
        )
        return self.parse(raw)

    @staticmethod
    def _apply_reported_resolution(raw: dict[str, Any]) -> dict[str, Any]:
        """Read the model's own unresolved list instead of classifying prose.

        Same output shape as ``_classify_missing_information`` so every
        downstream reader (``has_required_missing_information``, the publishing
        gate, the dashboard) is unchanged -- only the source of the verdict
        moved. An item the model named as unresolved is required; anything else
        it merely could not confirm is optional, which is what "I answered this
        and here is what I could not know" has always meant.
        """

        if not isinstance(raw, dict):
            return raw
        values = raw.get("missing_information")
        missing = [
            str(item).strip()
            for item in (values if isinstance(values, list) else [])
            if str(item).strip()
        ]
        unresolved = {
            str(item).strip()
            for item in (raw.get("unresolved") or [])
            if str(item).strip()
        }
        # ``requires_review`` used to mean two incompatible things in the
        # provider contract: an evidence finding, or merely a suggestion that
        # a person might feel safer checking the answer.  Only the former is a
        # publish decision.  The answer model has already read every atom and
        # every candidate, so retain its atom-level evidence verdict rather
        # than promoting a vague publishing preference into a CODE veto.
        #
        # A malformed/older provider can omit ``unresolved`` while still
        # recording an unanswered atom.  That is still the model's evidence
        # result, not a second semantic classifier, so canonicalise it here.
        for item in raw.get("subquestion_results") or []:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "").upper()
            answered = item.get("answered")
            if answered is False or status in {
                "NO_RELIABLE_SOURCE", "CONFLICT", "NEEDS_DPS",
                "DELIVERY_SCHEDULE_REVIEW",
            }:
                text = str(item.get("subquestion") or "").strip()
                if text:
                    unresolved.add(text)
        required = [item for item in missing if item in unresolved]
        optional = [item for item in missing if item not in unresolved]
        copied = dict(raw)
        copied["provider_requires_review"] = bool(raw.get("requires_review"))
        copied["provider_can_auto_post"] = raw.get("can_auto_post")
        copied["unresolved"] = list(unresolved)
        copied["missing_information_details"] = [
            {
                "text": item,
                "severity": (
                    "REQUIRED_FOR_SAFE_ANSWER"
                    if item in unresolved
                    else "OPTIONAL_DETAIL"
                ),
            }
            for item in missing
        ]
        copied["required_missing_information"] = required
        copied["optional_missing_information"] = optional
        # GPT②'s publish-facing verdict is evidence sufficiency only.  Keep
        # the raw provider preference above for diagnosis, but do not let
        # "a staff check would be nice" turn an otherwise resolved answer into
        # a review.  Hard safety is deliberately evaluated later by CODE.
        copied["requires_review"] = bool(unresolved)
        copied["can_auto_post"] = not bool(unresolved)
        return copied

    @staticmethod
    def _classify_missing_information(
        raw: dict[str, Any], intent: IntentResult
    ) -> dict[str, Any]:
        """Classify a narrowly-defined manual detail as optional.

        Everything is required by default.  Two independent paths can lower a
        single item to optional; neither can lower one the other rejects, and
        Validator authority remains downstream either way -- a rejected answer
        is never made safe here.

        The first path is the summary request: the customer explicitly asked
        for a summary, the missing item is a finer-grained manual/assembly
        detail rather than a customer-impacting fact, and the answer defers it
        to an authoritative manual or installer.

        The second path asks the question that actually separates a safe
        answer from an unsafe one: **did the draft answer what was asked?**
        ``missing_information`` is the model listing what it does not know,
        which is not the same finding as the answer being unsupported.
        Inquiry 686504818 asked whether a separately bought bracket could wall
        mount a 50인치 TV; the draft answered it from verified product facts
        and two APPROVED learnings, then named the two things it could not
        know -- the bracket the customer has yet to buy, and the wall in their
        parents' home -- and told them to confirm both with the installer. No
        catalog of ours can ever hold either, so review could only ever
        produce the same sentence. Meanwhile 2655 ("USB-C 65W 충전 가능한가요"),
        2692 ("HDMI 단자가 몇 개") and 2702 all reported ``answered: false``
        with NO_RELIABLE_SOURCE and replied "확인이 어렵습니다" -- questions
        about our own product that we failed to answer, which is a real
        finding and still goes to a person.

        So this path requires every sub-question to be answered, the answer to
        visibly hand the unknown off, the provider not to have asked for
        review itself, and the item not to be an operational commitment about
        an order, a date, or money.
        """

        if not isinstance(raw, dict):
            return raw
        values = raw.get("missing_information")
        if not isinstance(values, list):
            return raw
        missing = [str(item).strip() for item in values if str(item).strip()]
        questions = " ".join(str(item) for item in intent.questions)
        answer_context = " ".join(
            [
                str(raw.get("answer") or ""),
                *(str(item) for item in (raw.get("warnings") or [])),
            ]
        )
        summary_requested = bool(_SUMMARY_REQUEST.search(questions))
        safe_deferral = bool(_SAFE_DETAIL_DEFERRAL.search(answer_context))
        provider_review = bool(raw.get("requires_review"))
        # Fails closed: no sub-question record at all is not evidence that the
        # question was answered.
        answered_everything = _all_subquestions_answered(
            raw.get("subquestion_results")
        )
        deferrable = bool(
            answered_everything and safe_deferral and not provider_review
        )
        required: list[str] = []
        optional: list[str] = []
        details: list[dict[str, str]] = []
        for item in missing:
            optional_detail = bool(
                summary_requested
                and safe_deferral
                and _OPTIONAL_DETAIL_QUALIFIER.search(item)
                and _OPTIONAL_MANUAL_DETAIL.search(item)
                and not _REQUIRED_FACT_DETAIL.search(item)
            ) or bool(
                deferrable and not _OPERATIONAL_COMMITMENT_DETAIL.search(item)
            )
            severity = (
                "OPTIONAL_DETAIL"
                if optional_detail
                else "REQUIRED_FOR_SAFE_ANSWER"
            )
            (optional if optional_detail else required).append(item)
            details.append({"text": item, "severity": severity})

        copied = dict(raw)
        copied["provider_requires_review"] = bool(raw.get("requires_review"))
        copied["missing_information_details"] = details
        copied["required_missing_information"] = required
        copied["optional_missing_information"] = optional
        if missing and optional and not required:
            copied["requires_review"] = False
            # Which path cleared it is kept on the draft, so a hold that is
            # lifted here is still explainable from the persisted record.
            copied["warnings"] = list(raw.get("warnings") or []) + [
                "OPTIONAL_DETAIL_ANSWERED_WITH_SAFE_DEFERRAL"
                if deferrable
                else "OPTIONAL_DETAIL_DEFERRED_TO_MANUAL_OR_INSTALLER"
            ]
        elif required:
            copied["requires_review"] = True
        return copied

    @staticmethod
    def _validate_historical_usage(
        raw: dict[str, Any], learning_context: dict[str, Any]
    ) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return raw
        attached = {
            int(item["historical_case_id"]): item
            for item in learning_context.get("historical_cases", [])
            if item.get("historical_case_id") is not None
        }
        valid: list[dict[str, Any]] = []
        reported = raw.get("historical_usage")
        if isinstance(reported, list):
            for item in reported:
                if not isinstance(item, dict):
                    continue
                try:
                    case_id = int(item.get("historical_case_id"))
                except (TypeError, ValueError):
                    continue
                selected = attached.get(case_id)
                if selected is None:
                    continue
                matched = str(item.get("matched_subquestion") or "")
                if matched != str(selected.get("matched_subquestion") or ""):
                    continue
                valid.append({
                    "historical_case_id": case_id,
                    "matched_subquestion": matched,
                    "answer_supported": bool(item.get("answer_supported")),
                    "reason": str(item.get("reason") or "")[:300],
                    "authority": "APPROVED",
                    "compatibility": dict(
                        selected.get("compatibility") or {}
                    ),
                })
        if not valid:
            answer = str(raw.get("answer") or "")
            for case_id, selected in attached.items():
                reference = str(
                    selected.get("answer_reference") or ""
                ).strip()
                if reference and reference in answer:
                    valid.append({
                        "historical_case_id": case_id,
                        "matched_subquestion": str(
                            selected.get("matched_subquestion") or ""
                        ),
                        "answer_supported": True,
                        "reason": "ANSWER_TEXT_MATCHED_ATTACHED_HISTORICAL",
                        "authority": "APPROVED",
                        "compatibility": dict(
                            selected.get("compatibility") or {}
                        ),
                    })
        copied = dict(raw)
        copied["historical_usage"] = valid
        return copied

    @staticmethod
    def _validate_feedback_signal_usage(
        raw: dict[str, Any], learning_context: dict[str, Any]
    ) -> dict[str, Any]:
        """Keep only self-reported FACTUAL signal usage that was actually attached.

        Mirrors ``_validate_historical_usage``: a signal that was rejected
        before prompt attachment (conflicting, out of scope, below the
        relevance threshold) cannot become "actually used" merely because
        the provider echoed its id.
        """

        if not isinstance(raw, dict):
            return raw
        feedback_signals = learning_context.get("feedback_signals")
        feedback_signals = feedback_signals if isinstance(feedback_signals, dict) else {}
        attached = {
            int(item["signal_id"]): item
            for item in (
                *feedback_signals.get("verified_facts", []),
                *feedback_signals.get("corrections", []),
            )
            if item.get("signal_id") is not None
        }
        valid: list[dict[str, Any]] = []
        reported = raw.get("feedback_signal_usage")
        if isinstance(reported, list):
            for item in reported:
                if not isinstance(item, dict):
                    continue
                try:
                    signal_id = int(item.get("signal_id"))
                except (TypeError, ValueError):
                    continue
                selected = attached.get(signal_id)
                if selected is None:
                    continue
                matched = str(item.get("matched_subquestion") or "")
                if matched != str(selected.get("matched_subquestion") or ""):
                    continue
                valid.append({
                    "signal_id": signal_id,
                    "matched_subquestion": matched,
                    "answer_supported": bool(item.get("answer_supported")),
                    "reason": str(item.get("reason") or "")[:300],
                })
        copied = dict(raw)
        copied["feedback_signal_usage"] = valid
        return copied

    @staticmethod
    def _apply_learning_grounded_recovery(
        raw: dict[str, Any],
        learning_context: dict[str, Any],
    ) -> dict[str, Any]:
        """Recover only policy/product subquestions grounded by Active Learning.

        This deliberately cannot supply a current order status or date.  It is
        used when the provider received mapped, verified Learning but still
        returned one blanket uncertainty answer for every sub-question.
        """

        if not isinstance(raw, dict):
            return raw
        approved = {
            int(item["learning_example_id"]): item
            for item in learning_context.get(
                "similar_approved_answers", []
            )
            if item.get("learning_example_id") is not None
        }
        historical = {
            int(item["historical_case_id"]): item
            for item in learning_context.get("historical_cases", [])
            if item.get("historical_case_id") is not None
            and (item.get("eligibility") or {}).get("context_eligible")
        }
        evidence = list(
            learning_context.get("subquestion_evidence") or []
        )
        # Sub-questions the model may answer from attached candidates. CANDIDATE
        # is the ordinary retrieval outcome now; ANSWERABLE remains for the
        # deterministic sources (verified signals, confirmed DPS).
        answerable = [
            item for item in evidence
            if item.get("status") in {"ANSWERABLE", "CANDIDATE"}
            and (item.get("learning_ids") or item.get("historical_case_ids"))
        ]
        if not approved:
            # An id the model emitted for a row that was never attached to the
            # prompt cannot be "used": there was nothing there to read. This is
            # an attachment check, not a judgement about relevance -- the
            # validation loop below keeps every reported id that matches a row
            # the prompt actually carried. Historical is unaffected and is
            # validated by ``_validate_historical_usage``.
            raw = {**raw, "learning_usage": []}
        if (not approved and not historical) or not answerable:
            return {**raw, "learning_usage": []}

        reported_usage = raw.get("learning_usage")
        valid_usage = []
        if isinstance(reported_usage, list):
            for item in reported_usage:
                if not isinstance(item, dict):
                    continue
                try:
                    learning_id = int(item.get("learning_id"))
                except (TypeError, ValueError):
                    continue
                selected = approved.get(learning_id)
                if selected is None:
                    continue
                matched = str(item.get("matched_subquestion") or "")
                if matched != str(
                    selected.get("matched_subquestion") or ""
                ):
                    continue
                valid_usage.append(
                    {
                        "learning_id": learning_id,
                        "matched_subquestion": matched,
                        "answer_supported": bool(
                            item.get("answer_supported")
                        ),
                        "reason": str(item.get("reason") or "")[:300],
                        "authority": selected.get("authority"),
                        "compatibility": dict(
                            selected.get("compatibility") or {}
                        ),
                    }
                )
        answer = str(raw.get("answer") or "")
        avoidance_markers = (
            "현재 확인된 정보만으로", "안내하기 어렵", "확인할 수 없",
            "판매처에", "담당자 확인", "직원 검토", "추가 확인",
        )
        blanket_avoidance = sum(
            marker in answer for marker in avoidance_markers
        ) >= 2
        if not valid_usage and answer and not blanket_avoidance:
            # Providers predating the learning_usage contract may still use a
            # selected answer verbatim. Record that as observed use instead of
            # confusing retrieval/attachment with actual answer support.
            for item in answerable:
                question = str(item.get("subquestion") or "")
                for learning_id in item.get("learning_ids") or []:
                    selected = approved.get(int(learning_id))
                    learned_answer = str(
                        (selected or {}).get("answer") or ""
                    ).strip()
                    if learned_answer and learned_answer in answer:
                        valid_usage.append(
                            {
                                "learning_id": int(learning_id),
                                "matched_subquestion": question,
                                "answer_supported": True,
                                "reason": "ANSWER_TEXT_MATCHED_ATTACHED_LEARNING",
                                "authority": selected.get("authority"),
                                "compatibility": dict(
                                    selected.get("compatibility") or {}
                                ),
                            }
                        )
                        break
        if valid_usage and any(
            item["answer_supported"] for item in valid_usage
        ) and not blanket_avoidance:
            copied = dict(raw)
            copied["learning_usage"] = valid_usage
            return copied
        if not blanket_avoidance:
            return raw

        # Rewriting the model's answer is a last resort, and it stays scoped to
        # the deterministic statuses it was written for. CANDIDATE means
        # retrieval found something and GPT ② was asked to judge it; if the
        # model read those candidates and concluded none of them applies -- to
        # this model, this order, this date -- that conclusion is the whole
        # point of moving the judgement, and a marker list must not overturn it
        # by pasting the first candidate in as the reply.
        recoverable = [
            item for item in answerable
            if item.get("status") == "ANSWERABLE"
        ]
        if not recoverable:
            return raw

        answer_parts: list[str] = []
        usage: list[dict[str, Any]] = []
        historical_usage: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        answered_questions: set[str] = set()
        for item in recoverable:
            question = str(item.get("subquestion") or "").strip()
            selected = next(
                (
                    approved.get(int(learning_id))
                    for learning_id in item.get("learning_ids") or []
                    if int(learning_id) in approved
                ),
                None,
            )
            selected_historical = next(
                (
                    historical.get(int(case_id))
                    for case_id in item.get("historical_case_ids") or []
                    if int(case_id) in historical
                ),
                None,
            )
            learned_answer = str(
                (selected or {}).get("answer")
                or (selected_historical or {}).get("answer_reference")
                or ""
            ).strip()
            # Never recover time-dependent order facts from Learning.
            if not learned_answer or re.search(
                r"(?<!\d)20\d{2}[년./-]\s*\d{1,2}(?:[월./-]\s*\d{1,2}일?)?",
                learned_answer,
            ):
                continue
            answer_parts.append(learned_answer)
            answered_questions.add(question)
            if selected is not None:
                learning_id = int(selected["learning_example_id"])
                usage.append(
                    {
                        "learning_id": learning_id,
                        "matched_subquestion": question,
                        "answer_supported": True,
                        "reason": "ACTIVE_POSITIVE_LEARNING_GROUNDED_RECOVERY",
                        "authority": selected.get("authority"),
                        "compatibility": dict(
                            selected.get("compatibility") or {}
                        ),
                    }
                )
            elif selected_historical is not None:
                historical_usage.append(
                    {
                        "historical_case_id": int(
                            selected_historical["historical_case_id"]
                        ),
                        "matched_subquestion": question,
                        "answer_supported": True,
                        "reason": "SAFE_HISTORICAL_GROUNDED_RECOVERY",
                        "authority": "APPROVED",
                        "compatibility": dict(
                            selected_historical.get("compatibility") or {}
                        ),
                    }
                )

        if not answer_parts:
            return raw
        for item in evidence:
            question = str(item.get("subquestion") or "").strip()
            results.append(
                {
                    "subquestion": question,
                    "status": item.get("status"),
                    "learning_ids": list(item.get("learning_ids") or []),
                    "answered": question in answered_questions,
                }
            )
        unresolved = [
            str(item.get("subquestion") or "").strip()
            for item in evidence
            if str(item.get("subquestion") or "").strip()
            not in answered_questions
        ]
        if unresolved:
            answer_parts.append(
                "그 외 현재 주문의 일정이나 확인 가능한 근거가 없는 항목은 "
                "추가 확인이 필요합니다."
            )
        copied = dict(raw)
        copied.update(
            {
                "answer": "\n\n".join(dict.fromkeys(answer_parts)),
                "confidence": max(0.75, float(raw.get("confidence") or 0)),
                "learning_usage": usage,
                "historical_usage": historical_usage,
                "subquestion_results": results,
                "missing_information": unresolved,
                "requires_review": bool(unresolved),
                "warnings": list(raw.get("warnings") or [])
                + ["LEARNING_GROUNDED_PARTIAL_RECOVERY"],
                "learning_recovery_used": True,
            }
        )
        return copied

    @staticmethod
    def parse(raw: dict[str, Any]) -> DraftResult:
        if not isinstance(raw, dict):
            raise ValueError("GPT draft output must be a JSON object.")
        confidence = float(raw.get("confidence", 0))
        if not 0 <= confidence <= 1:
            raise ValueError("GPT draft confidence must be 0..1.")
        list_fields: dict[str, tuple[str, ...]] = {}
        for field in ("used_facts", "missing_information", "warnings"):
            value = raw.get(field, [])
            if not isinstance(value, list):
                raise ValueError(f"GPT draft {field} must be a list.")
            list_fields[field] = tuple(str(item) for item in value)
        detail_values = raw.get("missing_information_details")
        details = tuple(
            dict(item)
            for item in (
                detail_values if isinstance(detail_values, list) else []
            )
            if isinstance(item, dict)
        )
        required_value = raw.get("required_missing_information")
        optional_value = raw.get("optional_missing_information")
        required = tuple(
            str(item)
            for item in (
                required_value
                if isinstance(required_value, list)
                else list_fields["missing_information"]
            )
        )
        optional = tuple(
            str(item)
            for item in (
                optional_value if isinstance(optional_value, list) else []
            )
        )
        return DraftResult(
            answer=str(raw.get("answer") or ""),
            confidence=confidence,
            used_facts=list_fields["used_facts"],
            missing_information=list_fields["missing_information"],
            required_missing_information=required,
            optional_missing_information=optional,
            missing_information_details=details,
            provider_requires_review=bool(
                raw.get("provider_requires_review", raw.get("requires_review"))
            ),
            requires_review=bool(raw.get("requires_review")),
            warnings=list_fields["warnings"],
            learning_usage=tuple(
                dict(item)
                for item in raw.get("learning_usage", [])
                if isinstance(item, dict)
            ),
            historical_usage=tuple(
                dict(item)
                for item in raw.get("historical_usage", [])
                if isinstance(item, dict)
            ),
            feedback_signal_usage=tuple(
                dict(item)
                for item in raw.get("feedback_signal_usage", [])
                if isinstance(item, dict)
            ),
            subquestion_results=tuple(
                dict(item)
                for item in raw.get("subquestion_results", [])
                if isinstance(item, dict)
            ),
            learning_recovery_used=bool(
                raw.get("learning_recovery_used")
            ),
            evidence_decisions=tuple(
                dict(item)
                for item in (raw.get("evidence_decisions") or [])
                if isinstance(item, dict)
            ),
            used_template_ids=tuple(
                str(item)
                for item in (raw.get("used_template_ids") or [])
                if str(item or "").strip()
            ),
            used_product_facts=tuple(
                str(item)
                for item in (raw.get("used_product_facts") or [])
                if str(item or "").strip()
            ),
            used_learning_ids=_int_ids(raw.get("used_learning_ids")),
            used_historical_ids=_int_ids(raw.get("used_historical_ids")),
            ignored_evidence=tuple(
                dict(item)
                for item in (raw.get("ignored_evidence") or [])
                if isinstance(item, dict)
            ),
            unresolved=tuple(
                str(item)
                for item in (raw.get("unresolved") or [])
                if str(item or "").strip()
            ),
            # Absent stays absent. ``bool(None)`` would silently read a legacy
            # provider's silence as "must not publish" and hold every answer it
            # writes.
            can_auto_post=(
                None
                if raw.get("can_auto_post") is None
                else bool(raw.get("can_auto_post"))
            ),
            reason=str(raw.get("reason") or ""),
        )
