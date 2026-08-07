class AdapterError(RuntimeError):
    """Base class for external adapter failures."""


class AdapterConfigurationError(AdapterError):
    """Raised when required local configuration or credentials are missing."""


class AdapterDataError(AdapterError):
    """Raised when an upstream response cannot produce trustworthy data."""


class AdapterSafetyError(AdapterError):
    """Raised when an integration would cross the paper/read-only boundary."""


def summarize_external_error(exc: BaseException) -> str:
    """Return the most useful bounded leaf error from async exception groups."""
    children = getattr(exc, "exceptions", None)
    if children:
        for child in children:
            summary = summarize_external_error(child)
            if summary:
                return summary
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}"[:500]
