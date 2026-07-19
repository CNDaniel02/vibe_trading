class AdapterError(RuntimeError):
    """Base class for external adapter failures."""


class AdapterConfigurationError(AdapterError):
    """Raised when required local configuration or credentials are missing."""


class AdapterDataError(AdapterError):
    """Raised when an upstream response cannot produce trustworthy data."""


class AdapterSafetyError(AdapterError):
    """Raised when an integration would cross the paper/read-only boundary."""
