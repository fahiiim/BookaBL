"""Typed domain records shared across ports, services, and flows."""

from datetime import date, datetime, time
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class DomainModel(BaseModel):
    """Immutable base model for persisted domain records."""

    model_config = ConfigDict(frozen=True)


class ClinicStatus(StrEnum):
    """Commercial state controlling whether patient flows may run."""

    TRIAL = "trial"
    ACTIVE = "active"
    PAUSED = "paused"
    CHURNED = "churned"


class AppointmentStatus(StrEnum):
    """Lifecycle status of an appointment."""

    BOOKED = "booked"
    CONFIRMED = "confirmed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"


class ConversationStep(StrEnum):
    """Persisted booking conversation states."""

    IDLE = "idle"
    AWAIT_SERVICE = "await_service"
    AWAIT_SLOT = "await_slot"
    AWAIT_PAYMENT_TYPE = "await_payment_type"
    AWAIT_POPIA_MA_CONSENT = "await_popia_ma_consent"
    AWAIT_MA_DETAILS_SINGLE_MSG = "await_ma_details_single_msg"
    AWAIT_CASH_NAME = "await_cash_name"


class OutboxStatus(StrEnum):
    """Delivery status for a notification outbox item."""

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class JobStatus(StrEnum):
    """Execution status for an automation job."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class Clinic(DomainModel):
    """A single dentist tenant and all configuration-driven behavior."""

    id: UUID
    name: str
    industry: str = "dental"
    package: str = "starter"
    status: ClinicStatus = ClinicStatus.TRIAL
    trial_started_at: datetime
    trial_days: int = 7
    expiry: date | None = None
    monthly_fee: Decimal = Decimal("0")
    wa_phone_id: str
    wa_token_enc: str | None = None
    telegram_chat_id: str | None = None
    google_calendar_id: str | None = None
    google_review_url: str | None = None
    timezone: str = "Africa/Johannesburg"
    work_start: time = time(8)
    work_end: time = time(17)
    work_days: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5])
    reminder_offsets_h: list[int] = Field(default_factory=lambda: [24, 3])
    wa_templates: dict[str, Any] = Field(default_factory=dict)
    brand_voice: str | None = None
    created_at: datetime


class Service(DomainModel):
    """A bookable tenant service."""

    id: UUID
    clinic_id: UUID
    name: str
    duration_min: int = 30
    price: Decimal


class Patient(DomainModel):
    """A patient scoped to exactly one clinic."""

    id: UUID
    clinic_id: UUID
    wa_number: str
    name: str
    no_show_count: int = 0


class Appointment(DomainModel):
    """A booked appointment with immutable commercial snapshot data."""

    id: UUID
    clinic_id: UUID
    patient_id: UUID
    service_id: UUID
    starts_at: datetime
    ends_at: datetime
    status: AppointmentStatus
    price: Decimal
    medical_aid_name: str | None = None
    medical_aid_number: str | None = None
    dependent_code: str | None = None
    google_event_id: str | None = None
    created_at: datetime


class ConversationState(DomainModel):
    """Durable state-machine cursor and its small JSON context."""

    clinic_id: UUID
    patient_id: UUID
    state: ConversationStep = ConversationStep.IDLE
    slot: dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime


class WebhookEvent(DomainModel):
    """Persisted inbound WhatsApp event claimed by the event worker."""

    message_id: str
    clinic_id: UUID
    payload: dict[str, Any]
    processed_at: datetime | None = None
    claimed_at: datetime | None = None
    attempts: int = 0
    last_error: str | None = None
    created_at: datetime


class NotificationOutbox(DomainModel):
    """A durable outbound message."""

    id: UUID
    clinic_id: UUID
    channel: str
    to_id: str
    payload: dict[str, Any]
    status: OutboxStatus = OutboxStatus.PENDING
    attempts: int = 0
    next_try_at: datetime
    last_error: str | None = None
    created_at: datetime


class AutomationJob(DomainModel):
    """A durable reminder, no-show, or calendar-retry task."""

    id: UUID
    clinic_id: UUID
    appointment_id: UUID | None = None
    patient_id: UUID | None = None
    job_type: str
    due_at: datetime
    status: JobStatus = JobStatus.PENDING
    dedupe_key: str
    payload: dict[str, Any] = Field(default_factory=dict)
    attempts: int = 0
    claimed_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime


class BusyPeriod(DomainModel):
    """Half-open interval during which a calendar or database is unavailable."""

    starts_at: datetime
    ends_at: datetime


class FinalizeBookingCommand(DomainModel):
    """Atomic booking input passed to the database transaction boundary."""

    clinic_id: UUID
    patient_id: UUID
    service_id: UUID
    starts_at: datetime
    ends_at: datetime
    medical_aid_name: str | None = None
    medical_aid_number: str | None = None
    dependent_code: str | None = None
    whatsapp_to: str
    whatsapp_payload: dict[str, Any]
    telegram_to: str | None = None
    telegram_payload: dict[str, Any] = Field(default_factory=dict)


class BookingSummary(DomainModel):
    """Joined appointment data for owner views and notifications."""

    appointment: Appointment
    patient: Patient
    service: Service


class MessageLogEntry(DomainModel):
    """One persisted inbound or outbound conversation message."""

    id: UUID
    clinic_id: UUID
    patient_id: UUID | None = None
    channel: str
    direction: str
    body: str
    raw: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class PatientConsent(DomainModel):
    """A versioned consent record associated with a patient and appointment."""

    id: UUID
    clinic_id: UUID
    patient_id: UUID
    appointment_id: UUID | None = None
    consent_type: str
    consent_text: str
    consent_version: str
    consented_at: datetime
