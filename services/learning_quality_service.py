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
        elif source in {"APPROVED_EDITED", "APPROVED_UNEDITED"}:
            # Both are the same event: a person read the final answer and
            # approved it. The rating used to slide from 5 down to 2 with the
            # edit ratio for APPROVED_EDITED while APPROVED_UNEDITED sat flat
            # at 4, so a heavily-reworked answer -- the one a member of staff
            # took the most care over -- ended up the least trusted of all,
            # and an unedited approval could never reach the top. How much of
            # the draft survived describes the draft, not the approved answer.
            #
            # ``edit_ratio`` is still measured and still stored: it is how
            # operations sees how often drafts need rewriting. It just no
            # longer sets the trust this row carries into retrieval.
            rating = 5
        elif source == "AUTO_POST_REVIEWED_NO_CHANGE":
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
