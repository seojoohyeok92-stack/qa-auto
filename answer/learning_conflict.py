from __future__ import annotations

from typing import Any


class LearningConflictError(RuntimeError):
    """Raised when the same persisted answer is evaluated both ways."""

    def __init__(self, message: str, *, conflict: dict[str, Any] | None = None):
        super().__init__(message)
        self.conflict = conflict or {}
