from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any


SENTENCE = re.compile(r"(?<=[.!?。])\s+|\n+")
POLITE = re.compile(r"(?:습니다|드립니다|세요|바랍니다|감사합니다)[.!]?$")
APOLOGY = re.compile(r"(죄송|양해|불편을 드려)")
GUIDANCE = re.compile(r"(확인|안내|참고|부탁|가능합니다)")


@dataclass(frozen=True)
class LearningQuality:
    rating: int
    edit_ratio: float
    quality_score: float


class LearningQualityService:
    def score(self, source: str, draft: str, final: str) -> LearningQuality:
        source = str(source or "").upper()
        before, after = str(draft or "").strip(), str(final or "").strip()
        edit_ratio = 0.0 if before == after else round(
            1.0 - SequenceMatcher(None, before, after).ratio(), 4
        )
        if source == "AUTO_POST_CORRECTED":
            rating = 5
        elif source == "APPROVED_EDITED":
            rating = 5 if edit_ratio <= 0.1 else 4 if edit_ratio <= 0.3 else 3 if edit_ratio <= 0.6 else 2
        elif source == "AUTO_POST_REVIEWED_NO_CHANGE":
            rating = 4
        elif source == "APPROVED_UNEDITED":
            rating = 4
        else:
            rating = 3
        return LearningQuality(rating, edit_ratio, round(rating / 5, 4))

    def style_features(self, answer: str) -> dict[str, Any]:
        text = str(answer or "").strip()
        sentences = [part.strip() for part in SENTENCE.split(text) if part.strip()]
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return {
            "greeting": lines[0][:80] if lines else "",
            "closing": lines[-1][:80] if lines else "",
            "average_sentence_length": round(sum(map(len, sentences)) / max(len(sentences), 1), 1),
            "polite_ending_ratio": round(sum(bool(POLITE.search(item)) for item in sentences) / max(len(sentences), 1), 3),
            "uses_apology": bool(APOLOGY.search(text)),
            "uses_guidance": bool(GUIDANCE.search(text)),
        }
