from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from answer.facts import AnswerFacts
from answer.inquiry_analysis import InquiryAnalysis


# The ``analysis.*`` namespace is virtual: these values live on the
# InquiryAnalysis, not on AnswerFacts, so ``AnswerFacts.get_fact`` cannot see
# them.  Every component that resolves a fact path -- selection, the prompt's
# allowed_fact_paths, and the validator's fact-existence check -- must go
# through ``resolve_fact`` below.  When the validator resolved paths with
# ``facts.get_fact`` alone it reported the keys the pipeline had itself put in
# allowed_fact_paths as "존재하지 않는 Fact", rejecting drafts for citing
# exactly what they were told to cite.  Keep this mapping the single place
# where an ``analysis.*`` key is defined.
ANALYSIS_FACT_RESOLVERS: dict[str, Callable[[InquiryAnalysis], Any]] = {
    "analysis.requires_order_id": lambda analysis: analysis.requires_order_id,
    "analysis.order_id_status": lambda analysis: analysis.order_id_status.value,
    "analysis.private_post_required": lambda analysis: True,
}
ANALYSIS_FACT_KEYS: frozenset[str] = frozenset(ANALYSIS_FACT_RESOLVERS)


def resolve_fact(
    facts: AnswerFacts,
    path: str,
    *,
    analysis: InquiryAnalysis | None = None,
) -> Any:
    """Resolve one fact path against the facts and the inquiry analysis.

    Returns ``None`` for an ``analysis.*`` path when no analysis is available,
    because such a path could not legitimately have been offered to the
    provider in that case either.
    """

    resolver = ANALYSIS_FACT_RESOLVERS.get(str(path))
    if resolver is not None:
        return None if analysis is None else resolver(analysis)
    return facts.get_fact(path)


@dataclass(frozen=True)
class SelectedFacts:
    values: dict[str, Any]
    keys: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"values": dict(self.values), "keys": list(self.keys)}


class FactSelectionService:
    def select(
        self,
        facts: AnswerFacts,
        analysis: InquiryAnalysis,
    ) -> SelectedFacts:
        values: dict[str, Any] = {}
        keys: list[str] = []
        for path in analysis.selected_fact_keys:
            value = self._value(facts, analysis, path)
            if value not in (None, "", [], {}, ()):
                values[path] = value
                keys.append(path)
        if (
            "installation.date" in analysis.selected_fact_keys
            and "installation.date" not in values
            and facts.rule.get("answer")
        ):
            values["rule.answer"] = facts.rule["answer"]
            keys.append("rule.answer")
        return SelectedFacts(values=values, keys=tuple(keys))

    @staticmethod
    def _value(
        facts: AnswerFacts,
        analysis: InquiryAnalysis,
        path: str,
    ) -> Any:
        return resolve_fact(facts, path, analysis=analysis)
