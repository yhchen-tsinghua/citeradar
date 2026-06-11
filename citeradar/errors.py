"""Shared exceptions for resumable pipeline failures."""


class RateLimitError(RuntimeError):
    """Raised when an API rate limit remains after the configured retry."""
