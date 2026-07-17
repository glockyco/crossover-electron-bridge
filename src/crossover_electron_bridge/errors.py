"""Errors raised by CrossOver Electron Bridge operations."""


class CrossoverElectronBridgeError(Exception):
    """Base class for expected operational and validation failures.

    Callers can catch this error to present an actionable message to users
    without masking unexpected programming errors.
    """


class ConfigurationError(CrossoverElectronBridgeError):
    """Raised when configuration or profile data is invalid."""


class OperationalError(CrossoverElectronBridgeError):
    """Raised when a requested operation cannot be completed safely."""


__all__ = [
    "ConfigurationError",
    "CrossoverElectronBridgeError",
    "OperationalError",
]
