"""Provider errors classified for request-scoped failover."""
from __future__ import annotations


class ProviderError(RuntimeError):
    """An upstream model provider failed."""

    def __init__(self, message: str, *, recoverable: bool, status_code: int | None = None) -> None:
        super().__init__(message)
        self.recoverable = recoverable
        self.status_code = status_code


class ProviderResponseError(ProviderError):
    """The provider returned an unusable structured response."""

    def __init__(self, message: str) -> None:
        super().__init__(message, recoverable=False)
