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
