"""Domain-level persistence port used by application services."""

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, Protocol
from uuid import UUID

from app.domain.models import (
    Appointment,
    AppointmentStatus,
    AutomationJob,
    BookingSummary,
    Clinic,
    ConversationState,
    FAQEntry,
    FinalizeBookingCommand,
    JobStatus,
    MessageLogEntry,
    NotificationOutbox,
    OutboxStatus,
    Patient,
    PatientConsent,
    Service,
    WebhookEvent,
)


class Database(Protocol):
    """Persistence operations expressed in domain language."""

    async def get_clinic(self, clinic_id: UUID) -> Clinic | None:
        """Return a clinic by primary key."""

    async def get_clinic_by_wa_phone_id(self, phone_id: str) -> Clinic | None:
        """Resolve a tenant from Meta's phone-number identifier."""

    async def get_clinic_by_telegram_chat_id(self, chat_id: str) -> Clinic | None:
        """Resolve a tenant from an authorized Telegram owner chat."""

    async def persist_webhook_event(
        self, message_id: str, clinic_id: UUID, payload: dict[str, Any]
    ) -> bool:
        """Persist an inbound event, returning false when it is a duplicate."""

    async def pop_unprocessed_events(self, limit: int) -> list[WebhookEvent]:
        """Lease the oldest unprocessed webhook events."""

    async def mark_event_processed(self, message_id: str) -> None:
        """Mark a webhook event successfully processed."""

    async def release_event(self, message_id: str, error: str) -> None:
        """Release a failed event lease for a future attempt."""

    async def get_or_create_patient(self, clinic_id: UUID, wa_number: str, name: str) -> Patient:
        """Resolve a clinic-scoped patient, creating one when absent."""

    async def get_patient(self, patient_id: UUID) -> Patient | None:
        """Return a patient by primary key."""

    async def update_patient_name(self, patient_id: UUID, name: str) -> Patient:
        """Replace a patient's placeholder name with their consented typed name."""

    async def list_services(self, clinic_id: UUID) -> list[Service]:
        """List services configured for a clinic."""

    async def get_service(self, service_id: UUID) -> Service | None:
        """Return a service by primary key."""

    async def get_conversation_state(
        self, clinic_id: UUID, patient_id: UUID
    ) -> ConversationState | None:
        """Return a patient's durable state-machine cursor."""

    async def save_conversation_state(self, state: ConversationState) -> None:
        """Insert or replace a patient's durable conversation state."""

    async def log_message(
        self,
        clinic_id: UUID,
        patient_id: UUID | None,
        channel: str,
        direction: str,
        body: str,
        raw: dict[str, Any],
    ) -> None:
        """Append an inbound or outbound message audit record."""

    async def list_open_appointments(
        self, clinic_id: UUID, starts_before: datetime, ends_after: datetime
    ) -> list[Appointment]:
        """List overlapping booked or confirmed appointments."""

    async def finalize_booking(self, command: FinalizeBookingCommand) -> Appointment:
        """Atomically create a conflict-free appointment, jobs, and notifications."""

    async def set_google_event_id(self, appointment_id: UUID, event_id: str) -> None:
        """Attach a created Google Calendar event identifier."""

    async def get_appointment(self, appointment_id: UUID) -> Appointment | None:
        """Return an appointment by primary key."""

    async def get_booking_summary(self, appointment_id: UUID) -> BookingSummary | None:
        """Return joined appointment, patient, and service data."""

    async def transition_appointment_status(
        self,
        appointment_id: UUID,
        patient_id: UUID,
        from_statuses: Sequence[AppointmentStatus],
        to_status: AppointmentStatus,
    ) -> Appointment | None:
        """Conditionally transition an appointment owned by a patient."""

    async def mark_no_show(self, appointment_id: UUID) -> Appointment | None:
        """Atomically mark a booked appointment no-show and increment the patient count."""

    async def list_booking_summaries(
        self, clinic_id: UUID, starts_at: datetime, ends_at: datetime
    ) -> list[BookingSummary]:
        """List joined appointments in a half-open time interval."""

    async def enqueue_outbox(
        self, clinic_id: UUID, channel: str, to_id: str, payload: dict[str, Any]
    ) -> None:
        """Persist an outbound notification."""

    async def pop_pending_outbox(self, limit: int) -> list[NotificationOutbox]:
        """Lease pending outbound notifications."""

    async def mark_outbox_sent(self, outbox_id: UUID) -> None:
        """Mark an outbox delivery successful."""

    async def retry_outbox(
        self, outbox_id: UUID, next_try_at: datetime, error: str, *, failed: bool
    ) -> None:
        """Reschedule or permanently fail an outbound notification."""

    async def enqueue_job(
        self,
        clinic_id: UUID,
        job_type: str,
        due_at: datetime,
        dedupe_key: str,
        *,
        appointment_id: UUID | None = None,
        patient_id: UUID | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Persist an idempotent automation job."""

    async def pop_due_jobs(self, limit: int) -> list[AutomationJob]:
        """Atomically claim due automation jobs."""

    async def complete_job(self, job_id: UUID) -> None:
        """Mark an automation job complete."""

    async def retry_job(
        self, job_id: UUID, due_at: datetime, error: str, *, failed: bool = False
    ) -> None:
        """Reschedule or permanently fail an automation job."""

    async def claim_daily_throttle(
        self, clinic_id: UUID, patient_id: UUID, throttle_key: str, local_date: date
    ) -> bool:
        """Claim a once-per-local-day action, returning false if already claimed."""

    async def list_clinics(self) -> list[Clinic]:
        """List every clinic for multi-tenant administration."""

    async def create_clinic(self, values: dict[str, Any]) -> Clinic:
        """Create and return a clinic from validated administrative values."""

    async def update_clinic(self, clinic_id: UUID, values: dict[str, Any]) -> Clinic:
        """Update editable clinic settings and return the resulting clinic."""

    async def admin_list_appointments(
        self,
        clinic_id: UUID,
        *,
        starts_at: datetime | None = None,
        ends_at: datetime | None = None,
        status: AppointmentStatus | None = None,
        service_id: UUID | None = None,
        patient_id: UUID | None = None,
        search: str | None = None,
        limit: int = 500,
    ) -> list[BookingSummary]:
        """List tenant appointments with the filters used by the admin dashboard."""

    async def list_patients(self, clinic_id: UUID, search: str | None = None) -> list[Patient]:
        """List tenant patients, optionally matching their name or WhatsApp number."""

    async def create_service(self, values: dict[str, Any]) -> Service:
        """Create a tenant service."""

    async def update_service(self, service_id: UUID, values: dict[str, Any]) -> Service:
        """Update an existing service."""

    async def delete_service(self, service_id: UUID) -> None:
        """Delete a service with no protected future bookings."""

    async def service_has_future_bookings(self, service_id: UUID, now: datetime) -> bool:
        """Return whether a service has future booked or confirmed appointments."""

    async def list_message_log(
        self, clinic_id: UUID, *, patient_id: UUID | None = None, limit: int = 100
    ) -> list[MessageLogEntry]:
        """List recent tenant messages, optionally for one patient."""

    async def list_outbox(
        self, clinic_id: UUID, *, status: OutboxStatus | None = None, limit: int = 200
    ) -> list[NotificationOutbox]:
        """List tenant outbox records for operations."""

    async def get_outbox(self, outbox_id: UUID) -> NotificationOutbox | None:
        """Return one outbox record."""

    async def list_jobs(
        self, clinic_id: UUID, *, status: JobStatus | None = None, limit: int = 200
    ) -> list[AutomationJob]:
        """List tenant automation jobs for operations."""

    async def get_job(self, job_id: UUID) -> AutomationJob | None:
        """Return one automation job."""

    async def list_webhook_events(self, clinic_id: UUID, *, limit: int = 200) -> list[WebhookEvent]:
        """List recent webhook events for a tenant."""

    async def list_patient_consents(
        self,
        clinic_id: UUID,
        *,
        patient_id: UUID | None = None,
        appointment_id: UUID | None = None,
    ) -> list[PatientConsent]:
        """List tenant consent records by patient or appointment."""

    async def save_patient_consent(
        self,
        clinic_id: UUID,
        patient_id: UUID,
        consent_type: str,
        consent_text: str,
        consent_version: str,
    ) -> PatientConsent:
        """Persist a versioned POPIA consent decision for a patient."""

    async def list_faq_entries(self, clinic_id: UUID) -> list[FAQEntry]:
        """List FAQ entries for a clinic."""

    async def get_faq_entry(self, entry_id: UUID) -> FAQEntry | None:
        """Return one FAQ entry."""

    async def create_faq_entry(self, values: dict[str, Any]) -> FAQEntry:
        """Create a clinic FAQ entry."""

    async def update_faq_entry(self, entry_id: UUID, values: dict[str, Any]) -> FAQEntry:
        """Update a clinic FAQ entry."""

    async def delete_faq_entry(self, entry_id: UUID) -> None:
        """Delete a clinic FAQ entry."""
