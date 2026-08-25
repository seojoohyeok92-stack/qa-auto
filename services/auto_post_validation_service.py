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
# Redaction tokens this codebase writes when it masks something. They exist so
# personal data never reaches a prompt, a log or the learning store -- they are
# internal bookkeeping and must never be shown to a customer.
#
# One of them did reach customers: an approved answer containing the company's
# own switchboard number was masked on its way into the learning store, so the
# stored example literally reads "<masked-phone>로 문의 바랍니다". Retrieval put
# that text in the prompt and the model copied it into a new answer. Nothing
# stopped it: the placeholder rule above only recognises {{...}}/${...}/[[...]]
# /{name}, and the PII check passes because re-masking already-masked text
# changes nothing.
#
# Listed literally rather than as <...> so that ordinary angle brackets in an
# answer are not mistaken for a leak. Only tokens that actually exist in this
# codebase are listed; nothing new is invented here.
INTERNAL_REDACTION_TOKENS = (
    "masked-phone", "masked-email", "masked-name", "masked-address",
    "masked-order", "masked-order-id", "masked-product-order-id",
    "masked-secret", "masked-internal-url", "masked-file-path",
    "masked-parent", "masked-value",
)
INTERNAL_PLACEHOLDER = re.compile(
    r"<(?:{})>".format("|".join(re.escape(t) for t in INTERNAL_REDACTION_TOKENS)),
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
        if INTERNAL_PLACEHOLDER.search(text):
            # Deliberately not repaired: the answer says a redaction happened
            # but not what was redacted, and guessing would risk publishing a
            # real customer's number as if it were the company's. A person
            # decides what the sentence should say.
            errors.append("INTERNAL_PLACEHOLDER_EXPOSURE")
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

