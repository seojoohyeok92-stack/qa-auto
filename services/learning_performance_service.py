from __future__ import annotations

from typing import Any

from config import PositiveLearningSettings
from repositories.database import Database
from repositories.learning_performance_repository import LearningPerformanceRepository


class LearningPerformanceService:
    def __init__(self, database: Database) -> None:
        self.repository = LearningPerformanceRepository(database)

    def snapshot(self, *, period_days: int = 30) -> dict[str, Any]:
        period_days = int(period_days)
        if period_days not in {7, 30, 90}:
            raise ValueError("QUALITY_PERIOD_NOT_SUPPORTED")
        current_7 = self.repository.outcome_period(start_days=7)
        current_30 = self.repository.outcome_period(start_days=30)
        previous_30 = self.repository.outcome_period(start_days=60, end_days=30)
        delta = None
        if current_30["unchanged_rate"] is not None and previous_30["unchanged_rate"] is not None:
            delta = round(current_30["unchanged_rate"] - previous_30["unchanged_rate"], 1)
        return {
            "learning": self.repository.learning_counts(),
            "current_7": current_7,
            "current_30": current_30,
            "previous_30": previous_30,
            "unchanged_delta_30": delta,
            "quality": {
                "period_days": period_days,
                "current": self.repository.quality_period(
                    start_days=period_days
                ),
                "previous": self.repository.quality_period(
                    start_days=period_days * 2,
                    end_days=period_days,
                ),
                "correction_trend": self.repository.correction_trend(
                    days=period_days
                ),
            },
            "provenance": self.repository.provenance_effect(),
            "sources": self.repository.source_rows(),
            "types": self.repository.type_quality(),
            "trend": self.repository.trend(),
            "positive": self.repository.positive_observation(
                PositiveLearningSettings.from_environment().observation_days
            ),
            "corrections": self.repository.correction_summary(),
        }
