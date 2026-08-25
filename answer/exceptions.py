class AnswerEngineError(RuntimeError):
    """Base exception for the automatic-answer subsystem."""


class AnswerConfigError(AnswerEngineError):
    """Required answer configuration is missing or invalid."""


class UnsupportedInquiryError(AnswerEngineError):
    """The inquiry cannot be handled by the selected provider."""


class AnswerGenerationError(AnswerEngineError):
    """A provider failed to produce a valid answer result.

    ``reason_code`` carries the machine-readable cause -- ``RATE_LIMITED``,
    ``COST_LIMITED``, ``PRIVACY_BLOCKED`` and so on. Without it every cause
    reached the dashboard as the same opaque sentence, so an operator could
    not tell "wait and retry" apart from "this answer needs review".
    """

    def __init__(self, *args: object, reason_code: str | None = None) -> None:
        super().__init__(*args)
        normalized = str(reason_code or "").strip().upper()
        self.reason_code: str | None = normalized or None


class AnswerProviderUnavailableError(AnswerGenerationError):
    """The requested answer provider is intentionally unavailable."""


class AutoAnswerProhibitedError(AnswerGenerationError):
    """Policy forbids drafting an answer for this inquiry at all.

    A high-risk or dispute inquiry (physical damage, refund, legal exposure)
    must reach a person with no machine wording attached. That decision is a
    working safety gate, not a malfunction, but it travelled as a bare
    ``AnswerGenerationError`` and every caller logged it as a system fault --
    so a correctly blocked inquiry looked like an outage on the dashboard.
    Raising a distinct type lets callers report the block as what it is while
    keeping the existing ``AnswerGenerationError`` handling intact.
    """

    def __init__(
        self,
        *args: object,
        reason_code: str | None = "AUTO_ANSWER_PROHIBITED",
        policy_reason: str | None = None,
    ) -> None:
        super().__init__(*args, reason_code=reason_code)
        self.policy_reason: str | None = (
            str(policy_reason).strip().upper() if policy_reason else None
        )


class AnswerAlreadyPostedError(AnswerEngineError):
    """A posted answer cannot be regenerated or overwritten."""


class AnswerGenerationInProgressError(AnswerEngineError):
    """Another generation attempt is already running for the inquiry."""


class StaleAnswerStateError(AnswerEngineError):
    """A write was based on an older persisted answer state."""

    def __init__(
        self,
        message: str = (
            "다른 사용자가 이 답변을 이미 변경했습니다. "
            "최신 상태를 다시 불러온 후 확인해주세요."
        ),
    ) -> None:
        super().__init__(message)


class GenerationSkippedError(AnswerGenerationError):
    """Composing an answer was abandoned because it could not be published.

    Not a failure. The publishing gate was already certain to hold this
    inquiry for staff, so calling a provider would have spent a request to
    arrive at a verdict that was already known. Raised instead of returned so
    it unwinds the generation attempt cleanly, and carries the gate's own
    reason codes so the hold is reported with the same words a late hold gets.
    """

    def __init__(
        self,
        message: str = "이미 확정된 직원 검토 사유가 있어 답변 생성을 생략했습니다.",
        *,
        reasons: tuple[str, ...] = (),
        stage: str = "",
    ) -> None:
        super().__init__(message, reason_code="GENERATION_SKIPPED")
        self.reasons: tuple[str, ...] = tuple(reasons)
        self.stage: str = str(stage or "")
