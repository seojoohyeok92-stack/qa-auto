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
        return self.parse(raw)

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
        return DraftResult(
            answer=str(raw.get("answer") or ""),
            confidence=confidence,
            used_facts=list_fields["used_facts"],
            missing_information=list_fields["missing_information"],
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
