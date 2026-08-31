from __future__ import annotations

from datetime import datetime
from typing import Any

from config import PositiveLearningSettings
from repositories.database import Database


class PositiveLearningService:
    """Compatibility facade for the retired automatic promotion job.

    Observation/history rows are preserved by their owning repositories, but
    no elapsed period or unchanged Naver answer may create Positive Learning.
    New ``learning_examples`` require the existing explicit approval path.
    """

    def __init__(
        self,
        database: Database,
        *,
        settings: PositiveLearningSettings | None = None,
    ) -> None:
        self.database = database
        self.settings = settings or PositiveLearningSettings.from_environment()

    def observe(
        self,
        *,
        inquiry_id: int,
        seller_answer: str,
        observed_at: datetime | None = None,
    ) -> dict[str, Any]:
        del inquiry_id, seller_answer, observed_at
        return {
            "saved": False,
            "reason": "MANUAL_APPROVAL_REQUIRED",
            "learning": None,
        }
