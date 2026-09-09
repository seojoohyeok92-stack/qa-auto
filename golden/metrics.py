"""Scoring one run. A metric with no label is missing, never zero.

Two rules hold everywhere in this module.

*A metric that needs a label it does not have returns ``None``.* It is then
reported as NOT_YET_LABELED. Filling it with a default would turn an unlabelled
corpus into a confident number, which is the failure this file exists to avoid.

*A metric that the current run mode cannot observe returns ``None`` too.* FAST
mode replaces GPT ② with a fixed policy, so ``wrong_evidence_usage`` and
``unsupported_claim`` -- both judgements about meaning -- are not measurable
there and say so, rather than reporting a flattering zero.

Severity follows the harm, not the tidiness: publishing a wrong answer to a
customer is worse than sending a good one to a person.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from golden.schema import CaseResult

CRITICAL = "CRITICAL"
MAJOR = "MAJOR"
MINOR = "MINOR"

# What each finding costs. Ordered by who gets hurt: a customer receiving an
# unsupported claim automatically, then a wrong answer a person still has to
# catch, then work created for staff.
SEVERITY: dict[str, str] = {
    "auto_post_false_positive": CRITICAL,
    "unsupported_claim": CRITICAL,
    "wrong_product_fact": CRITICAL,
    "missed_required_review": CRITICAL,
    "wrong_evidence_usage": MAJOR,
    "evidence_retrieval_miss": MAJOR,
    "compound_coverage_gap": MAJOR,
    "subquestion_evidence_suppression": MAJOR,
    "order_routing_error": MAJOR,
    "dps_routing_error": MAJOR,
    "product_fact_unavailable": MAJOR,
    "evidence_adoption_miss": MAJOR,
    "unnecessary_review": MINOR,
    "auto_post_false_negative": MINOR,
    "replay_fidelity_drift": MINOR,
}

# Metrics that a FAST run cannot honestly produce, because the deterministic
# GPT ② stand-in is not making the judgement they measure.
LAYER2_ONLY = frozenset({
    "wrong_evidence_usage",
    "unsupported_claim",
    "answer_quality",
})

# How to read a number that is technically computable in FAST mode but does not
# mean there what it means in FULL. Printed next to the value so nobody reads a
# structural check as a quality result.
FAST_MODE_CAVEATS: dict[str, str] = {
    "evidence_adoption": (
        "FAST adopts every attached candidate by policy, so this measures"
        " attachment to the sub-question, not judgement. Read in FULL mode."
    ),
    "compound_coverage": (
        "FAST resolves an atom iff evidence was attached to it; it is a"
        " retrieval reach measure here, not answer completeness."
    ),
}


@dataclass
class Finding:
    case_id: str
    metric: str
    severity: str
    detail: str


@dataclass
class MetricValue:
    """A ratio, plus how many cases could actually be scored."""

    numerator: int = 0
    denominator: int = 0
    not_measurable: int = 0
    not_yet_labelled: int = 0

    @property
    def value(self) -> float | None:
        if self.denominator <= 0:
            return None
        return round(self.numerator / self.denominator, 4)

    def render(self) -> str:
        if self.denominator <= 0:
            if self.not_yet_labelled:
                return f"NOT_YET_LABELED (n={self.not_yet_labelled})"
            return f"NOT_MEASURABLE (n={self.not_measurable})"
        text = f"{self.value:.1%} ({self.numerator}/{self.denominator})"
        extra = []
        if self.not_yet_labelled:
            extra.append(f"unlabelled={self.not_yet_labelled}")
        if self.not_measurable:
            extra.append(f"not_measurable={self.not_measurable}")
        return text + (f"  [{', '.join(extra)}]" if extra else "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "not_measurable": self.not_measurable,
            "not_yet_labelled": self.not_yet_labelled,
            "render": self.render(),
        }


METRIC_NAMES = (
    "semantic_correctness",
    "evidence_retrieval_recall",
    "evidence_adoption",
    "wrong_evidence_usage",
    "unsupported_claim",
    "unnecessary_review",
    "missed_required_review",
    "product_fact_availability",
    "compound_coverage",
    "subquestion_evidence_suppression",
    "order_routing_correctness",
    "dps_routing_correctness",
    "auto_post_false_positive",
    "auto_post_false_negative",
    "answer_quality",
)


def score(results: list[CaseResult], *, mode: str) -> dict[str, Any]:
    metrics = {name: MetricValue() for name in METRIC_NAMES}
    findings: list[Finding] = []

    def note(case_id: str, metric: str, detail: str) -> None:
        findings.append(
            Finding(case_id, metric, SEVERITY.get(metric, MINOR), detail)
        )

    for result in results:
        case, seen, label = result.case, result.observed, result.case.label
        cid = case.case_id
        if not seen.ran:
            continue

        # --- A. semantic_correctness -------------------------------------
        # Scored against the sub-questions a human said had to be understood.
        if label.required_subquestions:
            metrics["semantic_correctness"].denominator += 1
            understood = _covers(seen.atoms, label.required_subquestions)
            metrics["semantic_correctness"].numerator += int(understood)
        else:
            metrics["semantic_correctness"].not_yet_labelled += 1

        # --- B. evidence_retrieval_recall --------------------------------
        wanted_l = set(label.expected_evidence_learning_ids)
        wanted_h = set(label.expected_evidence_historical_ids)
        if wanted_l or wanted_h:
            reached = set(seen.prompt_learning_ids) | {
                -v for v in seen.prompt_historical_ids
            }
            want = wanted_l | {-v for v in wanted_h}
            metrics["evidence_retrieval_recall"].denominator += len(want)
            hit = want & reached
            metrics["evidence_retrieval_recall"].numerator += len(hit)
            if want - reached:
                note(cid, "evidence_retrieval_miss",
                     f"expected evidence never reached the prompt: {sorted(want - reached)}")
        else:
            metrics["evidence_retrieval_recall"].not_yet_labelled += 1

        # --- C. evidence_adoption ----------------------------------------
        # Per case, and satisfied by *any* of the expected items.
        #
        # Counting each expected id separately punished the right behaviour:
        # 688159337's label lists one approved answer and two historical cases
        # that say the same thing, and a model that grounds its reply in one of
        # them and declines the redundant two scored 33%. What matters is
        # whether the claim ended up grounded in evidence a person said was
        # correct, not how many copies of it were cited.
        if wanted_l or wanted_h:
            reached_l = wanted_l & set(seen.prompt_learning_ids)
            reached_h = wanted_h & set(seen.prompt_historical_ids)
            if reached_l or reached_h:
                used = bool(
                    (reached_l & set(seen.used_learning_ids))
                    or (reached_h & set(seen.used_historical_ids))
                )
                metrics["evidence_adoption"].denominator += 1
                metrics["evidence_adoption"].numerator += int(used)
                if not used:
                    note(cid, "evidence_adoption_miss",
                         "expected evidence reached GPT and none of it was used")
        else:
            metrics["evidence_adoption"].not_yet_labelled += 1

        # --- D. wrong_evidence_usage -------------------------------------
        # Detectable: the label names ids that would be wrong to use as fact,
        # and the run reports which ids it used.
        if mode != "full":
            metrics["wrong_evidence_usage"].not_measurable += 1
        elif not label.forbidden_evidence_learning_ids:
            metrics["wrong_evidence_usage"].not_yet_labelled += 1
        else:
            metrics["wrong_evidence_usage"].denominator += 1

        # --- E. unsupported_claim ----------------------------------------
        # Not detectable here, in either mode. Deciding whether a sentence
        # asserts something the evidence does not carry is a reading of the
        # finished text, and nothing in this file reads it. Counting it as
        # zero would report "no unsupported claims" on the strength of never
        # having looked.
        metrics["unsupported_claim"].not_measurable += 1
        if mode == "full" and label.forbidden_evidence_learning_ids:
            wrong = set(label.forbidden_evidence_learning_ids) & set(
                seen.used_learning_ids
            )
            if wrong:
                metrics["wrong_evidence_usage"].numerator += 1
                note(cid, "wrong_evidence_usage",
                     f"used evidence the label forbids: {sorted(wrong)}")

        # --- F/G. review correctness -------------------------------------
        safe = seen.eligibility_decision == "SAFE"
        if label.expected_review_required is None:
            metrics["unnecessary_review"].not_yet_labelled += 1
            metrics["missed_required_review"].not_yet_labelled += 1
            metrics["auto_post_false_positive"].not_yet_labelled += 1
            metrics["auto_post_false_negative"].not_yet_labelled += 1
        else:
            metrics["unnecessary_review"].denominator += 1
            metrics["missed_required_review"].denominator += 1
            metrics["auto_post_false_positive"].denominator += 1
            metrics["auto_post_false_negative"].denominator += 1
            if label.expected_review_required is False and not safe:
                metrics["unnecessary_review"].numerator += 1
                metrics["auto_post_false_negative"].numerator += 1
                note(cid, "unnecessary_review",
                     f"evidence was sufficient; gate held it: {seen.eligibility_reasons}")
            if label.expected_review_required is True and safe:
                metrics["missed_required_review"].numerator += 1
                metrics["auto_post_false_positive"].numerator += 1
                note(cid, "auto_post_false_positive",
                     "gate cleared a case the label says needs a person")

        # --- H. product_fact_availability --------------------------------
        if seen.need_product:
            metrics["product_fact_availability"].denominator += 1
            available = bool(seen.verified_product_facts)
            metrics["product_fact_availability"].numerator += int(available)
            if not available:
                note(cid, "product_fact_unavailable",
                     f"need_product=true, verified facts=0, identity={seen.product_identity_status}")

        # --- I. compound_coverage ----------------------------------------
        # Only over inquiries a label says were answerable. Counting a correct
        # hold as a coverage miss would punish 688159361 -- two questions about
        # the customer's own order, with no order number -- for doing exactly
        # the right thing, and would make the metric drop when safety improves.
        if (seen.atom_count or 0) >= 2:
            if label.expected_answerability == "ANSWERABLE":
                metrics["compound_coverage"].denominator += seen.atom_count or 0
                answered = (seen.atom_count or 0) - len(seen.unresolved)
                metrics["compound_coverage"].numerator += max(0, answered)
                if seen.unresolved:
                    note(cid, "compound_coverage_gap",
                         f"{len(seen.unresolved)} of {seen.atom_count} sub-questions unresolved")
            elif label.expected_answerability is None:
                metrics["compound_coverage"].not_yet_labelled += 1

        # --- J. subquestion_evidence_suppression -------------------------
        # An atom whose evidence was emptied by another atom's order/DPS need.
        suppressed = _suppressed_atoms(seen)
        metrics["subquestion_evidence_suppression"].denominator += max(
            len(seen.subquestion_evidence), 0
        )
        metrics["subquestion_evidence_suppression"].numerator += len(suppressed)
        for text in suppressed:
            note(cid, "subquestion_evidence_suppression",
                 f"order/DPS scope emptied evidence for a non-order sub-question: {text[:60]}")

        # --- K/L. routing correctness ------------------------------------
        for metric, expected, actual in (
            ("order_routing_correctness", label.expected_order_lookup, seen.need_order),
            ("dps_routing_correctness", label.expected_dps_lookup, seen.need_dps),
        ):
            if expected is None:
                metrics[metric].not_yet_labelled += 1
            else:
                metrics[metric].denominator += 1
                if bool(expected) == bool(actual):
                    metrics[metric].numerator += 1
                else:
                    key = ("order_routing_error" if metric.startswith("order")
                           else "dps_routing_error")
                    note(cid, key, f"expected={expected} observed={actual}")

        # --- O. answer_quality -------------------------------------------
        # The label says what a correct answer would be; scoring the answer
        # actually produced against it means reading Korean prose for meaning.
        # Golden does not do that, and must not imply it did -- an earlier
        # version incremented the denominator and never the numerator, and
        # reported a confident 0.0%.
        metrics["answer_quality"].not_measurable += 1

        # Only contract drift is a finding. Corpus drift means the Learning
        # store grew since the case ran, which is expected and harmless:
        # before and after are both measured against today's corpus.
        if seen.fidelity == "CONTRACT_DRIFT":
            note(cid, "replay_fidelity_drift", "; ".join(seen.fidelity_notes)[:200])

    severity_counts = Counter(item.severity for item in findings)
    return {
        "mode": mode,
        "cases": len(results),
        "cases_ran": sum(1 for r in results if r.observed.ran),
        "metrics": {name: metrics[name].to_dict() for name in METRIC_NAMES},
        "findings": [vars(item) for item in findings],
        "severity": {
            CRITICAL: severity_counts.get(CRITICAL, 0),
            MAJOR: severity_counts.get(MAJOR, 0),
            MINOR: severity_counts.get(MINOR, 0),
        },
        "side_effects": {
            "naver_post_calls": sum(r.observed.naver_post_calls for r in results),
            "dps_calls": sum(r.observed.dps_calls for r in results),
            "order_lookup_calls": sum(r.observed.order_lookup_calls for r in results),
            # Provider invocations, which are network calls only in FULL mode.
            # Reporting a FAST stub call as a "gpt call" in a stored artifact
            # would misstate what the run cost and what it touched.
            "provider_calls": sum(r.observed.gpt_calls for r in results),
            "live_gpt_calls": (
                sum(r.observed.gpt_calls for r in results) if mode == "full" else 0
            ),
        },
        "fidelity": Counter(
            str(r.observed.fidelity) for r in results
        ).most_common(),
    }


def _covers(atoms: list[str], required: list[str]) -> bool:
    """Did the understanding raise every sub-question the label requires?

    Substring containment, not token equality. Korean agglutinates: a label
    reading "기존 TV 수거 가능 여부" and the atom it describes -- "새 TV 설치
    방문 시 기존의 오래된 TV도 함께 수거해 주실 수 있나요?" -- share the stems
    기존/수거 and no whole token, so word-set matching scored this correct
    understanding as a miss. Golden never compares text exactly; this asks
    whether the subject the label names is present at all.
    """

    def normalise(value: str) -> str:
        return "".join(ch.lower() if ch.isalnum() else " " for ch in str(value))

    haystack = normalise(" ".join(str(item) for item in atoms))

    def satisfied(phrase: str) -> bool:
        want = [t for t in normalise(phrase).split() if len(t) > 1]
        if not want:
            return True
        hit = sum(1 for token in want if token in haystack)
        # Half the label's content words have to appear. A label is a short
        # phrase, so this is "the same subject", not "the same sentence".
        return hit * 2 >= len(want)

    for item in required:
        # ``a|b`` means "either of these names the same subject". Korean
        # paraphrase is lexically disjoint far more often than English --
        # "1~2일 연기 가능 여부" and "하루 이틀 뒤로 미룰 수 있는지" share no
        # stem at all -- so without alternatives a label ends up testing the
        # labeller's vocabulary rather than the pipeline's understanding.
        if not any(satisfied(part) for part in str(item).split("|")):
            return False
    return True


def _suppressed_atoms(observed: Any) -> list[str]:
    """Atoms deferred to the current order while a sibling drove that need.

    The signature of G26: more than one sub-question, at least one of them not
    order-shaped by its own action, and the whole set carrying
    ``CURRENT_DPS_REQUIRED`` with no evidence ids at all.
    """

    evidence = observed.subquestion_evidence or []
    if len(evidence) < 2:
        return []
    deferred = [
        item for item in evidence
        if str(item.get("source") or "") == "CURRENT_DPS_REQUIRED"
        and not (item.get("learning_ids") or item.get("historical_case_ids"))
    ]
    if len(deferred) != len(evidence):
        return []
    order_actions = {
        "ORDER_IDENTIFICATION", "DELIVERY_STATUS", "INSTALLATION_SCHEDULE",
        "SCHEDULE_CHANGE", "DELIVERY_DEADLINE_CONFIRMATION",
    }
    actions = list(observed.semantic_actions or [])
    if not actions or all(action in order_actions for action in actions):
        return []
    return [
        str(item.get("subquestion") or "")
        for item, action in zip(evidence, actions)
        if action not in order_actions
    ]
