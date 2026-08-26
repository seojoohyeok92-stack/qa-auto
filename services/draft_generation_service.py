from __future__ import annotations

import re
from typing import Any, Callable

from answer.facts import AnswerFacts
from answer.fact_selection import SelectedFacts
from answer.hybrid_models import DraftResult, IntentResult
from answer.inquiry_analysis import InquiryAnalysis
from answer.prompt_builder import PromptBuilder
from answer.providers.interfaces import JsonGptProvider
from services.learning_context_service import apply_prompt_budget, prompt_context


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
            "context_priority": [
                "CURRENT_INQUIRY", "PRODUCT_DB", "POLICY", "FIXED_TEMPLATE",
                "SIMILAR_APPROVED_ANSWERS", "SELLER_STYLE_EXAMPLES",
                "HISTORICAL_CASES_REFERENCE_ONLY", "OJE_STYLE_RULES",
            ],
            **evidence,
        }
        if retry_feedback:
            prompt_input["prior_attempt_feedback"] = retry_feedback
        context.update(learning_context)
        raw = self.provider.generate_json(
            task="DRAFT",
            prompt=self.prompt_builder.build(
                task="DRAFT",
                facts=facts,
                extra=prompt_input,
                analysis=analysis,
                selected_facts=selected_facts,
            ),
            context=self.prompt_builder.safe_payload(context),
        )
        raw = self._apply_learning_grounded_recovery(
            raw, learning_context
        )
        raw = self._validate_historical_usage(raw, learning_context)
        raw = self._validate_feedback_signal_usage(raw, learning_context)
        raw = self._classify_missing_information(raw, intent)
        return self.parse(raw)

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
        answerable = [
            item for item in evidence
            if item.get("status") == "ANSWERABLE"
            and (item.get("learning_ids") or item.get("historical_case_ids"))
        ]
        if not approved:
            # Provider-reported IDs are never authoritative.  A Learning that
            # was rejected before prompt attachment cannot be resurrected as
            # "actually used" merely because a model emitted its ID.
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

        answer_parts: list[str] = []
        usage: list[dict[str, Any]] = []
        historical_usage: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        answered_questions: set[str] = set()
        for item in answerable:
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
        )
