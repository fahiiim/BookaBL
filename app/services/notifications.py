"""Tenant-local human-readable notification formatting."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.clock import Clock
from app.domain.models import Appointment, Clinic, Patient, Service
from app.services.privacy import minimal_patient_name, short_date


class NotificationFormatter:
    """Format owner and patient messages without provider concerns."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def owner_new_booking(
        self, clinic: Clinic, patient: Patient, service: Service, appointment: Appointment
    ) -> str:
        """Return a privacy-minimal owner booking notification."""

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
        """Format an alert without treatment, contact, or medical-aid data."""

        del service, medical_aid_name, medical_aid_number, dependent_code
        local = starts_at.astimezone(ZoneInfo(clinic.timezone))
        return (
            f"NEW BOOKING - {clinic.name}\n"
            f"{short_date(local)} {local:%H:%M}\n"
            f"{minimal_patient_name(patient.name)} - Confirmed"
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

        when = self.friendly_datetime(clinic, starts_at)
        offsets = sorted(
            {
                offset
                for offset in clinic.reminder_offsets_h
                if starts_at - timedelta(hours=offset) > self._clock.now()
            },
            reverse=True,
        )
        reminder_sentence = self._reminder_sentence(offsets)
        return (
            f"Your {service.name} appointment is all set for {when}. "
            f"{reminder_sentence}See you then!"
        )

    def owner_status_change(
        self,
        prefix: str,
        clinic: Clinic,
        patient: Patient,
        service: Service,
        appointment: Appointment,
    ) -> str:
        """Format a privacy-minimal cancellation or no-show owner alert."""

        del service
        local = appointment.starts_at.astimezone(ZoneInfo(clinic.timezone))
        return (
            f"{prefix} - {minimal_patient_name(patient.name)} - "
            f"{short_date(local)} {local:%H:%M}"
        )

    def friendly_datetime(self, clinic: Clinic, starts_at: datetime) -> str:
        """Return a clinic-local date/time phrase for patient messages."""

        local = starts_at.astimezone(ZoneInfo(clinic.timezone))
        today = self._clock.now().astimezone(ZoneInfo(clinic.timezone)).date()
        label = self._date_label(local.date(), today)
        return f"{label} {local:%H:%M}"

    @staticmethod
    def _reminder_offsets(offsets: list[int]) -> str:
        labels = [f"{value} hour{'s' if value != 1 else ''}" for value in offsets]
        if not labels:
            return "shortly"
        if len(labels) == 1:
            return labels[0]
        return f"{', '.join(labels[:-1])} and {labels[-1]}"

    @classmethod
    def _reminder_sentence(cls, offsets: list[int]) -> str:
        if not offsets:
            return ""
        noun = "reminder" if len(offsets) == 1 else "reminders"
        return f"We'll send you {noun} {cls._reminder_offsets(offsets)} beforehand. "

    @staticmethod
    def _date_label(value: date, today: date) -> str:
        if value == today:
            return "Today"
        if value == today + timedelta(days=1):
            return "Tomorrow"
        return value.strftime("%a %d %b")
