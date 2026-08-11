from __future__ import annotations

from enum import StrEnum


class AnswerProvenance(StrEnum):
    """Origin of an answer without implying that every answer was posted."""

    PROGRAM_GENERATED = "PROGRAM_GENERATED"
    STAFF_EDITED = "STAFF_EDITED"
    NAVER_POSTED = "NAVER_POSTED"
    FINAL_ANSWER = "FINAL_ANSWER"
    HISTORICAL_VERIFIED = "HISTORICAL_VERIFIED"

