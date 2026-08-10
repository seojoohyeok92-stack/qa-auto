from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from repositories.database import Database
from repositories.quality_metric_repository import QualityMetricRepository


SENTENCE_PATTERN = re.compile(r"(?<=[.!?。！？])\s+|\n+")
WORD_PATTERN = re.compile(r"[가-힣A-Za-z0-9]+")
FACT_PATTERN = re.compile(
    r"(?:\d{4}[-년/.]\s*\d{1,2}[-월/.]\s*\d{1,2}일?|"
    r"\b\d{1,3}(?:,\d{3})+\b|배송\s*(?:준비|중|완료)|설치\s*(?:예정|완료))"
)
PROHIBITED_PATTERN = re.compile(r"(확실히|무조건|반드시\s+배송|100%|추측컨대)")
TONE_PATTERN = re.compile(r"(안녕하세요|고객님|감사합니다|확인 부탁드립니다)")


@dataclass(frozen=True)
class QualityMetrics:
    character_change_ratio: float
    word_change_ratio: float
    sentences_added: int
    sentences_deleted: int
    fact_changed: bool
    prohibited_expression_changed: bool
    tone_changed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "character_change_ratio": self.character_change_ratio,
            "word_change_ratio": self.word_change_ratio,
            "sentences_added": self.sentences_added,
            "sentences_deleted": self.sentences_deleted,
            "fact_changed": self.fact_changed,
            "prohibited_expression_changed": self.prohibited_expression_changed,
            "tone_changed": self.tone_changed,
        }


def _ratio(left: list[str] | str, right: list[str] | str) -> float:
    if not left and not right:
        return 0.0
    return round(1.0 - SequenceMatcher(None, left, right).ratio(), 4)


def _sentences(value: str) -> list[str]:
    return [
        item.strip()
        for item in SENTENCE_PATTERN.split(str(value or "").strip())
        if item.strip()
    ]


class QualityMetricsService:
    def __init__(self, database: Database | None = None) -> None:
        self.repository = QualityMetricRepository(database) if database else None

    def calculate(self, program_answer: str, staff_answer: str) -> QualityMetrics:
        original = str(program_answer or "").strip()
        edited = str(staff_answer or "").strip()
        original_words = WORD_PATTERN.findall(original)
        edited_words = WORD_PATTERN.findall(edited)
        original_sentences = _sentences(original)
        edited_sentences = _sentences(edited)
        matcher = SequenceMatcher(None, original_sentences, edited_sentences)
        added = deleted = 0
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag in {"insert", "replace"}:
                added += j2 - j1
            if tag in {"delete", "replace"}:
                deleted += i2 - i1
        return QualityMetrics(
            character_change_ratio=_ratio(original, edited),
            word_change_ratio=_ratio(original_words, edited_words),
            sentences_added=added,
            sentences_deleted=deleted,
            fact_changed=FACT_PATTERN.findall(original) != FACT_PATTERN.findall(edited),
            prohibited_expression_changed=bool(
                PROHIBITED_PATTERN.search(original)
            ) != bool(PROHIBITED_PATTERN.search(edited)),
            tone_changed=bool(TONE_PATTERN.search(original))
            != bool(TONE_PATTERN.search(edited)),
        )

    def calculate_and_store(
        self,
        *,
        inquiry_id: int,
        answer_draft_id: int,
        actor: str,
        category: str | None,
        program_answer: str,
        staff_answer: str,
        edit_duration_seconds: int | None = None,
        approved: bool = False,
        regeneration_count: int = 0,
    ) -> dict[str, Any]:
        if self.repository is None:
            raise RuntimeError("품질 지표 저장에는 Database가 필요합니다.")
        metric = self.calculate(program_answer, staff_answer)
        return self.repository.create(
            {
                "inquiry_id": inquiry_id,
                "answer_draft_id": answer_draft_id,
                "actor": str(actor).strip() or "system",
                "category": category,
                **metric.to_dict(),
                "edit_duration_seconds": edit_duration_seconds,
                "approved": approved,
                "regeneration_count": regeneration_count,
                "details": {"label": "수정 지표", "not_ground_truth_score": True},
            }
        )

