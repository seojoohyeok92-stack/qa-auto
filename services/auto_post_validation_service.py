from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from services.learning_privacy_service import LearningPrivacyService


PLACEHOLDER = re.compile(
    r"(?:\{\{[^{}]+\}\}|\$\{[^{}]+\}|\[\[[^\[\]]+\]\]|"
    r"(?<!\{)\{(?:name|customer|order|date|model|answer|placeholder)[^{}]*\}(?!\}))",
    re.IGNORECASE,
)
SECRET = re.compile(
    r"(?i)(?:authorization\s*[:=]|bearer\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"(?:access[_ -]?token|refresh[_ -]?token|client[_ -]?secret|api[_ -]?key|password)\s*[:=])"
)


@dataclass(frozen=True)
class AutoPostValidation:
    passed: bool
    errors: tuple[str, ...]


class AutoPostTechnicalValidator:
    """Only transport/integrity/privacy failures block automatic posting."""

    def __init__(self) -> None:
        self.privacy = LearningPrivacyService()

    def validate_answer(self, answer: object) -> AutoPostValidation:
        text = str(answer or "").strip()
        errors: list[str] = []
        if not text:
            errors.append("FINAL_ANSWER_REQUIRED")
        if PLACEHOLDER.search(text):
            errors.append("UNRESOLVED_PLACEHOLDER")
        if SECRET.search(text):
            errors.append("SECRET_EXPOSURE")
        if text and self.privacy.mask(text) != text:
            errors.append("PII_EXPOSURE")
        return AutoPostValidation(not errors, tuple(errors))

    def validate_payload(
        self, *, final_answer: str, payload: dict[str, Any], source_type: str,
    ) -> AutoPostValidation:
        source = str(source_type or "").upper()
        field = (
            "commentContent" if source == "PRODUCT_INQUIRY"
            else "answerComment" if source == "CUSTOMER_INQUIRY"
            else ""
        )
        errors: list[str] = []
        if not field:
            errors.append("UNSUPPORTED_SOURCE_TYPE")
        elif str(payload.get(field) or "").strip() != str(final_answer or "").strip():
            errors.append("PAYLOAD_FINAL_ANSWER_MISMATCH")
        answer_check = self.validate_answer(final_answer)
        errors.extend(answer_check.errors)
        return AutoPostValidation(not errors, tuple(dict.fromkeys(errors)))

