"""Typed application and domain exceptions."""

from dataclasses import dataclass


class BookablError(Exception):
    """Base class for expected BOOKABL failures."""

    code = "bookabl_error"
    status_code = 500


class ConfigurationError(BookablError):
    """A required runtime integration setting is absent or invalid."""

    code = "configuration_error"
    status_code = 500


class InvalidSignatureError(BookablError):
    """A webhook signature failed verification."""

    code = "invalid_signature"
    status_code = 401


class ClinicNotFoundError(BookablError):
    """No clinic is configured for the requested external identifier."""

    code = "clinic_not_found"
    status_code = 404


class BookingConflictError(BookablError):
    """An appointment slot became unavailable before finalization."""

    code = "booking_conflict"
    status_code = 409


class InvalidTransitionError(BookablError):
    """A conversation input is invalid for the current state."""

    code = "invalid_transition"
    status_code = 422


@dataclass(frozen=True, slots=True)
class ExternalServiceError(BookablError):
    """An external service call failed after local validation."""

    service: str
    detail: str

    code = "external_service_error"
    status_code = 502

    def __str__(self) -> str:
        return f"{self.service}: {self.detail}"

