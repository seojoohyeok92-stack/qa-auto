from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from answer.facts import AnswerFacts
from answer.inquiry_analysis import InquiryAnalysis


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
        if path == "analysis.requires_order_id":
            return analysis.requires_order_id
        if path == "analysis.order_id_status":
            return analysis.order_id_status.value
        if path == "analysis.private_post_required":
            return True
        return facts.get_fact(path)
