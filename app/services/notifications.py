"""Tenant-local human-readable notification formatting."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.clock import Clock
from app.domain.models import Appointment, Clinic, Patient, Service

EN_DASH = "\N{EN DASH}"


class NotificationFormatter:
    """Format owner and patient messages without provider concerns."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def owner_new_booking(
        self, clinic: Clinic, patient: Patient, service: Service, appointment: Appointment
    ) -> str:
        """Return the exact two-line owner booking notification style."""

        return self.owner_new_booking_details(
            clinic,
            patient,
            service,
            appointment.starts_at,
            appointment.medical_aid_name,
            appointment.medical_aid_number,
            appointment.dependent_code,
        )

    def owner_new_booking_details(
        self,
        clinic: Clinic,
        patient: Patient,
        service: Service,
        starts_at: datetime,
        medical_aid_name: str | None,
        medical_aid_number: str | None,
        dependent_code: str | None,
    ) -> str:
        """Format owner booking text before the atomic appointment insert."""

        when = self._friendly_datetime(clinic, starts_at)
        if medical_aid_name:
            medical_aid = (
                f"Medical Aid: {medical_aid_name} | "
                f"No: {medical_aid_number or '-'} | "
                f"Dep: {dependent_code or '-'}"
            )
        else:
            medical_aid = "Medical Aid: Self-pay"
        return (
            f"🦷 New booking: {patient.name} {EN_DASH} {service.name} "
            f"{EN_DASH} {when}\n{medical_aid}"
        )

    def patient_confirmation(
        self, clinic: Clinic, service: Service, appointment: Appointment
    ) -> str:
        """Return a concise patient booking confirmation."""

        return self.patient_confirmation_details(clinic, service, appointment.starts_at)

    def patient_confirmation_details(
        self, clinic: Clinic, service: Service, starts_at: datetime
    ) -> str:
        """Format patient confirmation text before the atomic appointment insert."""

        when = self._friendly_datetime(clinic, starts_at)
        return f"Your {service.name} appointment is booked for {when}. See you then!"

    def owner_status_change(
        self,
        prefix: str,
        clinic: Clinic,
        patient: Patient,
        service: Service,
        appointment: Appointment,
    ) -> str:
        """Format a cancellation or no-show owner alert."""

        return (
            f"{prefix}: {patient.name} {EN_DASH} {service.name} {EN_DASH} "
            f"{self._friendly_when(clinic, appointment)}"
        )

    def _friendly_when(self, clinic: Clinic, appointment: Appointment) -> str:
        return self._friendly_datetime(clinic, appointment.starts_at)

    def _friendly_datetime(self, clinic: Clinic, starts_at: datetime) -> str:
        local = starts_at.astimezone(ZoneInfo(clinic.timezone))
        today = self._clock.now().astimezone(ZoneInfo(clinic.timezone)).date()
        label = self._date_label(local.date(), today)
        return f"{label} {local:%H:%M}"

    @staticmethod
    def _date_label(value: date, today: date) -> str:
        if value == today:
            return "Today"
        if value == today + timedelta(days=1):
            return "Tomorrow"
        return value.strftime("%a %d %b")
