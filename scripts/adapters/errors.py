import re


class AdapterError(RuntimeError):
    """Base class for external adapter failures."""


class AdapterConfigurationError(AdapterError):
    """Raised when required local configuration or credentials are missing."""


class AdapterDataError(AdapterError):
    """Raised when an upstream response cannot produce trustworthy data."""


class AdapterSafetyError(AdapterError):
    """Raised when an integration would cross the paper/read-only boundary."""


_OAUTH_SECRET_RE = re.compile(
    r'(?i)((?:["\']?(?:access_token|refresh_token|id_token|client_secret)["\']?)'
    r'\s*[:=]\s*["\']?)([^"\'\s,;&}]+)'
)
_BEARER_SECRET_RE = re.compile(
    r"(?i)((?:authorization\s*:\s*)?bearer\s+)([A-Za-z0-9._~+/=-]+)"
)
_OAUTH_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:code|access_token|refresh_token|id_token|client_secret)=)([^&#\s]+)"
)


def redact_external_error_text(text: str) -> str:
    redacted = _OAUTH_SECRET_RE.sub(r"\1[REDACTED]", text)
    redacted = _BEARER_SECRET_RE.sub(r"\1[REDACTED]", redacted)
    return _OAUTH_QUERY_SECRET_RE.sub(r"\1[REDACTED]", redacted)


def summarize_external_error(exc: BaseException) -> str:
    """Return the most useful bounded leaf error from async exception groups."""
    children = getattr(exc, "exceptions", None)
    if children:
        for child in children:
            summary = summarize_external_error(child)
            if summary:
                return summary
    message = redact_external_error_text(str(exc).strip())
    return f"{type(exc).__name__}: {message}"[:500]
