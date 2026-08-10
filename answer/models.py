from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class AnswerStatus(str, Enum):
    GENERATED = "GENERATED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    FAILED = "FAILED"

    def __str__(self) -> str:
        return self.value


@dataclass
class AnswerRequest:
    inquiry_id: int | None = None
    question_id: str = ""
    store_code: str = ""
    inquiry_type: str = ""
    question: str = ""
    product_name: str = ""
    option_name: str = ""
    customer_display: str = ""
    order_id: str = ""
    product_order_id: str = ""
    existing_answer: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "question_id",
            "store_code",
            "inquiry_type",
            "question",
            "product_name",
            "option_name",
            "customer_display",
            "order_id",
            "product_order_id",
            "existing_answer",
        ):
            value = getattr(self, name)
            setattr(self, name, "" if value is None else str(value))
        if not isinstance(self.metadata, dict):
            self.metadata = {}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AnswerResult:
    status: AnswerStatus | str
    category: str
    reason: str
    answer: str
    provider: str
    auto_answerable: bool
    needs_review: bool
    matched_rule: str = ""
    warnings: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.status, AnswerStatus):
            try:
                self.status = AnswerStatus(str(self.status))
            except ValueError as error:
                allowed = ", ".join(status.value for status in AnswerStatus)
                raise ValueError(
                    f"Invalid AnswerStatus: {self.status!r}. Allowed: {allowed}"
                ) from error
        self.category = str(self.category or "")
        self.reason = str(self.reason or "")
        self.answer = str(self.answer or "")
        self.provider = str(self.provider or "")
        self.matched_rule = str(self.matched_rule or "")
        self.warnings = tuple(str(item) for item in (self.warnings or ()))
        if not isinstance(self.metadata, dict):
            self.metadata = {}

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["status"] = self.status.value
        result["warnings"] = list(self.warnings)
        return result
