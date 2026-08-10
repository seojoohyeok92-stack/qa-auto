class AnswerEngineError(RuntimeError):
    """Base exception for the automatic-answer subsystem."""


class AnswerConfigError(AnswerEngineError):
    """Required answer configuration is missing or invalid."""


class UnsupportedInquiryError(AnswerEngineError):
    """The inquiry cannot be handled by the selected provider."""


class AnswerGenerationError(AnswerEngineError):
    """A provider failed to produce a valid answer result."""


class AnswerProviderUnavailableError(AnswerGenerationError):
    """The requested answer provider is intentionally unavailable."""


class AnswerAlreadyPostedError(AnswerEngineError):
    """A posted answer cannot be regenerated or overwritten."""


class AnswerGenerationInProgressError(AnswerEngineError):
    """Another generation attempt is already running for the inquiry."""
