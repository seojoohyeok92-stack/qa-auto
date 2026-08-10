class GptProviderError(RuntimeError):
    pass


class GptProviderTimeoutError(GptProviderError):
    pass


class GptProviderRetryableError(GptProviderError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GptProviderAuthenticationError(GptProviderError):
    pass


class GptProviderRateLimitError(GptProviderError):
    pass


class GptProviderCostLimitError(GptProviderError):
    pass


class GptPrivacyBlockedError(GptProviderError):
    pass
