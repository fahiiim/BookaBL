"""Deterministic in-memory database used by tests and local demonstrations."""

import asyncio
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from app.core.clock import Clock
from app.core.exceptions import BookingConflictError
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


class InMemoryDatabase:
    """Lock-protected database double preserving production atomicity boundaries."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._lock = asyncio.Lock()
        self.clinics: dict[UUID, Clinic] = {}
        self.services: dict[UUID, Service] = {}
        self.patients: dict[UUID, Patient] = {}
        self.appointments: dict[UUID, Appointment] = {}
        self.states: dict[tuple[UUID, UUID], ConversationState] = {}
        self.events: dict[str, WebhookEvent] = {}
        self.outbox: dict[UUID, NotificationOutbox] = {}
        self.jobs: dict[UUID, AutomationJob] = {}
        self.message_log: list[dict[str, Any]] = []
        self.daily_throttles: set[tuple[UUID, UUID, str, date]] = set()
        self.consents: dict[UUID, PatientConsent] = {}
        self.faq_entries: dict[UUID, FAQEntry] = {}

    def add_clinic(self, clinic: Clinic) -> None:
        """Seed a clinic for a test or local demonstration."""

        self.clinics[clinic.id] = clinic

    def add_service(self, service: Service) -> None:
        """Seed a service for a test or local demonstration."""

        self.services[service.id] = service

    async def get_clinic(self, clinic_id: UUID) -> Clinic | None:
        return self.clinics.get(clinic_id)

    async def get_clinic_by_wa_phone_id(self, phone_id: str) -> Clinic | None:
        return next(
            (clinic for clinic in self.clinics.values() if clinic.wa_phone_id == phone_id),
            None,
        )

    async def get_clinic_by_telegram_chat_id(self, chat_id: str) -> Clinic | None:
        return next(
            (clinic for clinic in self.clinics.values() if clinic.telegram_chat_id == chat_id),
            None,
        )

    async def persist_webhook_event(
        self, message_id: str, clinic_id: UUID, payload: dict[str, Any]
    ) -> bool:
        async with self._lock:
            if message_id in self.events:
                return False
            self.events[message_id] = WebhookEvent(
                message_id=message_id,
                clinic_id=clinic_id,
                payload=payload,
                created_at=self._clock.now(),
            )
            return True

    async def pop_unprocessed_events(self, limit: int) -> list[WebhookEvent]:
        async with self._lock:
            lease_expired = self._clock.now() - timedelta(minutes=5)
            candidates = sorted(self.events.values(), key=lambda event: event.created_at)
            claimed: list[WebhookEvent] = []
            for event in candidates:
                if event.processed_at is not None:
                    continue
                if event.claimed_at is not None and event.claimed_at >= lease_expired:
                    continue
                updated = event.model_copy(
                    update={"claimed_at": self._clock.now(), "attempts": event.attempts + 1}
                )
                self.events[event.message_id] = updated
                claimed.append(updated)
                if len(claimed) >= limit:
                    break
            return claimed

    async def mark_event_processed(self, message_id: str) -> None:
        async with self._lock:
            event = self.events[message_id]
            self.events[message_id] = event.model_copy(
                update={"processed_at": self._clock.now(), "claimed_at": None, "last_error": None}
            )

    async def release_event(self, message_id: str, error: str) -> None:
        async with self._lock:
            event = self.events[message_id]
            self.events[message_id] = event.model_copy(
                update={"claimed_at": None, "last_error": error[:2000]}
            )

    async def get_or_create_patient(self, clinic_id: UUID, wa_number: str, name: str) -> Patient:
        async with self._lock:
            existing = next(
                (
                    patient
                    for patient in self.patients.values()
                    if patient.clinic_id == clinic_id and patient.wa_number == wa_number
                ),
                None,
            )
            if existing is not None:
                return existing
            patient = Patient(id=uuid4(), clinic_id=clinic_id, wa_number=wa_number, name=name)
            self.patients[patient.id] = patient
            return patient

    async def get_patient(self, patient_id: UUID) -> Patient | None:
        return self.patients.get(patient_id)

    async def update_patient_name(self, patient_id: UUID, name: str) -> Patient:
        async with self._lock:
            patient = self.patients[patient_id]
            updated = patient.model_copy(update={"name": name})
            self.patients[patient_id] = updated
            return updated

    async def list_services(self, clinic_id: UUID) -> list[Service]:
        return sorted(
            (service for service in self.services.values() if service.clinic_id == clinic_id),
            key=lambda service: service.name,
        )

    async def get_service(self, service_id: UUID) -> Service | None:
        return self.services.get(service_id)

    async def get_conversation_state(
        self, clinic_id: UUID, patient_id: UUID
    ) -> ConversationState | None:
        return self.states.get((clinic_id, patient_id))

    async def save_conversation_state(self, state: ConversationState) -> None:
        async with self._lock:
            self.states[(state.clinic_id, state.patient_id)] = state

    async def log_message(
        self,
        clinic_id: UUID,
        patient_id: UUID | None,
        channel: str,
        direction: str,
        body: str,
        raw: dict[str, Any],
    ) -> None:
        async with self._lock:
            self.message_log.append(
                {
                    "id": uuid4(),
                    "clinic_id": clinic_id,
                    "patient_id": patient_id,
                    "channel": channel,
                    "direction": direction,
                    "body": body,
                    "raw": raw,
                    "created_at": self._clock.now(),
                }
            )

    async def list_open_appointments(
        self, clinic_id: UUID, starts_before: datetime, ends_after: datetime
    ) -> list[Appointment]:
        return [
            appointment
            for appointment in self.appointments.values()
            if appointment.clinic_id == clinic_id
            and appointment.status in {AppointmentStatus.BOOKED, AppointmentStatus.CONFIRMED}
            and appointment.starts_at < starts_before
            and appointment.ends_at > ends_after
        ]

    async def finalize_booking(self, command: FinalizeBookingCommand) -> Appointment:
        async with self._lock:
            service = self.services.get(command.service_id)
            clinic = self.clinics.get(command.clinic_id)
            if service is None or service.clinic_id != command.clinic_id or clinic is None:
                raise ValueError("Unknown clinic or service")
            has_conflict = any(
                appointment.clinic_id == command.clinic_id
                and appointment.status in {AppointmentStatus.BOOKED, AppointmentStatus.CONFIRMED}
                and appointment.starts_at < command.ends_at
                and appointment.ends_at > command.starts_at
                for appointment in self.appointments.values()
            )
            if has_conflict:
                raise BookingConflictError("Booking slot is no longer available")
            expected_end = command.starts_at + timedelta(minutes=service.duration_min)
            if command.ends_at != expected_end:
                raise ValueError("Appointment duration does not match service")

            appointment = Appointment(
                id=uuid4(),
                clinic_id=command.clinic_id,
                patient_id=command.patient_id,
                service_id=command.service_id,
                starts_at=command.starts_at,
                ends_at=command.ends_at,
                status=AppointmentStatus.BOOKED,
                price=service.price,
                medical_aid_name=command.medical_aid_name,
                medical_aid_number=command.medical_aid_number,
                dependent_code=command.dependent_code,
                created_at=self._clock.now(),
            )
            self.appointments[appointment.id] = appointment
            for offset in clinic.reminder_offsets_h:
                self._insert_job(
                    clinic_id=clinic.id,
                    job_type="reminder",
                    due_at=appointment.starts_at - timedelta(hours=offset),
                    dedupe_key=f"reminder:{appointment.id}:{offset}",
                    appointment_id=appointment.id,
                    patient_id=appointment.patient_id,
                )
            self._insert_job(
                clinic_id=clinic.id,
                job_type="no_show_check",
                due_at=appointment.starts_at + timedelta(minutes=15),
                dedupe_key=f"no-show:{appointment.id}",
                appointment_id=appointment.id,
                patient_id=appointment.patient_id,
            )
            self._insert_outbox(
                clinic.id, "whatsapp", command.whatsapp_to, command.whatsapp_payload
            )
            if command.telegram_to:
                self._insert_outbox(
                    clinic.id, "telegram", command.telegram_to, command.telegram_payload
                )
            return appointment

    async def set_google_event_id(self, appointment_id: UUID, event_id: str) -> None:
        async with self._lock:
            appointment = self.appointments[appointment_id]
            self.appointments[appointment_id] = appointment.model_copy(
                update={"google_event_id": event_id}
            )

    async def get_appointment(self, appointment_id: UUID) -> Appointment | None:
        return self.appointments.get(appointment_id)

    async def get_booking_summary(self, appointment_id: UUID) -> BookingSummary | None:
        appointment = self.appointments.get(appointment_id)
        return self._booking_summary(appointment) if appointment else None

    async def transition_appointment_status(
        self,
        appointment_id: UUID,
        patient_id: UUID,
        from_statuses: Sequence[AppointmentStatus],
        to_status: AppointmentStatus,
    ) -> Appointment | None:
        async with self._lock:
            appointment = self.appointments.get(appointment_id)
            if (
                appointment is None
                or appointment.patient_id != patient_id
                or appointment.status not in from_statuses
            ):
                return None
            updated = appointment.model_copy(update={"status": to_status})
            self.appointments[appointment_id] = updated
            return updated

    async def mark_no_show(self, appointment_id: UUID) -> Appointment | None:
        async with self._lock:
            appointment = self.appointments.get(appointment_id)
            if appointment is None or appointment.status is not AppointmentStatus.BOOKED:
                return None
            updated = appointment.model_copy(update={"status": AppointmentStatus.NO_SHOW})
            self.appointments[appointment_id] = updated
            patient = self.patients[appointment.patient_id]
            self.patients[patient.id] = patient.model_copy(
                update={"no_show_count": patient.no_show_count + 1}
            )
            return updated

    async def list_booking_summaries(
        self, clinic_id: UUID, starts_at: datetime, ends_at: datetime
    ) -> list[BookingSummary]:
        appointments = sorted(
            (
                appointment
                for appointment in self.appointments.values()
                if appointment.clinic_id == clinic_id
                and starts_at <= appointment.starts_at < ends_at
            ),
            key=lambda appointment: appointment.starts_at,
        )
        return [self._booking_summary(appointment) for appointment in appointments]

    async def enqueue_outbox(
        self, clinic_id: UUID, channel: str, to_id: str, payload: dict[str, Any]
    ) -> None:
        async with self._lock:
            self._insert_outbox(clinic_id, channel, to_id, payload)

    async def pop_pending_outbox(self, limit: int) -> list[NotificationOutbox]:
        async with self._lock:
            candidates = sorted(self.outbox.values(), key=lambda item: item.next_try_at)
            claimed: list[NotificationOutbox] = []
            for item in candidates:
                if item.status is not OutboxStatus.PENDING or item.next_try_at > self._clock.now():
                    continue
                updated = item.model_copy(
                    update={
                        "attempts": item.attempts + 1,
                        "next_try_at": self._clock.now() + timedelta(minutes=5),
                    }
                )
                self.outbox[item.id] = updated
                claimed.append(updated)
                if len(claimed) >= limit:
                    break
            return claimed

    async def mark_outbox_sent(self, outbox_id: UUID) -> None:
        async with self._lock:
            item = self.outbox[outbox_id]
            self.outbox[outbox_id] = item.model_copy(
                update={"status": OutboxStatus.SENT, "last_error": None}
            )

    async def retry_outbox(
        self, outbox_id: UUID, next_try_at: datetime, error: str, *, failed: bool
    ) -> None:
        async with self._lock:
            item = self.outbox[outbox_id]
            self.outbox[outbox_id] = item.model_copy(
                update={
                    "status": OutboxStatus.FAILED if failed else OutboxStatus.PENDING,
                    "next_try_at": next_try_at,
                    "last_error": error[:2000],
                }
            )

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
        async with self._lock:
            self._insert_job(
                clinic_id,
                job_type,
                due_at,
                dedupe_key,
                appointment_id=appointment_id,
                patient_id=patient_id,
                payload=payload,
            )

    async def pop_due_jobs(self, limit: int) -> list[AutomationJob]:
        async with self._lock:
            candidates = sorted(self.jobs.values(), key=lambda job: job.due_at)
            claimed: list[AutomationJob] = []
            lease_expired = self._clock.now() - timedelta(minutes=5)
            for job in candidates:
                is_due = job.status is JobStatus.PENDING and job.due_at <= self._clock.now()
                is_abandoned = (
                    job.status is JobStatus.PROCESSING
                    and job.claimed_at is not None
                    and job.claimed_at < lease_expired
                )
                if not is_due and not is_abandoned:
                    continue
                updated = job.model_copy(
                    update={
                        "status": JobStatus.PROCESSING,
                        "claimed_at": self._clock.now(),
                        "attempts": job.attempts + 1,
                    }
                )
                self.jobs[job.id] = updated
                claimed.append(updated)
                if len(claimed) >= limit:
                    break
            return claimed

    async def complete_job(self, job_id: UUID) -> None:
        async with self._lock:
            job = self.jobs[job_id]
            self.jobs[job_id] = job.model_copy(
                update={
                    "status": JobStatus.COMPLETED,
                    "claimed_at": None,
                    "last_error": None,
                }
            )

    async def retry_job(
        self, job_id: UUID, due_at: datetime, error: str, *, failed: bool = False
    ) -> None:
        async with self._lock:
            job = self.jobs[job_id]
            self.jobs[job_id] = job.model_copy(
                update={
                    "status": JobStatus.FAILED if failed else JobStatus.PENDING,
                    "due_at": due_at,
                    "claimed_at": None,
                    "last_error": error[:2000],
                }
            )

    async def claim_daily_throttle(
        self, clinic_id: UUID, patient_id: UUID, throttle_key: str, local_date: date
    ) -> bool:
        async with self._lock:
            key = (clinic_id, patient_id, throttle_key, local_date)
            if key in self.daily_throttles:
                return False
            self.daily_throttles.add(key)
            return True

    def _insert_outbox(
        self, clinic_id: UUID, channel: str, to_id: str, payload: dict[str, Any]
    ) -> NotificationOutbox:
        item = NotificationOutbox(
            id=uuid4(),
            clinic_id=clinic_id,
            channel=channel,
            to_id=to_id,
            payload=payload,
            next_try_at=self._clock.now(),
            created_at=self._clock.now(),
        )
        self.outbox[item.id] = item
        return item

    def _insert_job(
        self,
        clinic_id: UUID,
        job_type: str,
        due_at: datetime,
        dedupe_key: str,
        *,
        appointment_id: UUID | None = None,
        patient_id: UUID | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AutomationJob:
        existing = next((job for job in self.jobs.values() if job.dedupe_key == dedupe_key), None)
        if existing is not None:
            return existing
        job = AutomationJob(
            id=uuid4(),
            clinic_id=clinic_id,
            appointment_id=appointment_id,
            patient_id=patient_id,
            job_type=job_type,
            due_at=due_at,
            dedupe_key=dedupe_key,
            payload=payload or {},
            created_at=self._clock.now(),
        )
        self.jobs[job.id] = job
        return job

    def _booking_summary(self, appointment: Appointment) -> BookingSummary:
        return BookingSummary(
            appointment=appointment,
            patient=self.patients[appointment.patient_id],
            service=self.services[appointment.service_id],
        )

    async def list_clinics(self) -> list[Clinic]:
        return sorted(self.clinics.values(), key=lambda clinic: clinic.name.casefold())

    async def create_clinic(self, values: dict[str, Any]) -> Clinic:
        async with self._lock:
            clinic = Clinic.model_validate(values)
            self.clinics[clinic.id] = clinic
            return clinic

    async def update_clinic(self, clinic_id: UUID, values: dict[str, Any]) -> Clinic:
        async with self._lock:
            current = self.clinics[clinic_id]
            updated = Clinic.model_validate({**current.model_dump(), **values})
            self.clinics[clinic_id] = updated
            return updated

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
        needle = (search or "").casefold().strip()
        matches: list[Appointment] = []
        for appointment in self.appointments.values():
            patient = self.patients.get(appointment.patient_id)
            if appointment.clinic_id != clinic_id or patient is None:
                continue
            if starts_at is not None and appointment.starts_at < starts_at:
                continue
            if ends_at is not None and appointment.starts_at >= ends_at:
                continue
            if status is not None and appointment.status is not status:
                continue
            if service_id is not None and appointment.service_id != service_id:
                continue
            if patient_id is not None and appointment.patient_id != patient_id:
                continue
            if needle and needle not in patient.name.casefold() and needle not in patient.wa_number:
                continue
            matches.append(appointment)
        matches.sort(key=lambda appointment: appointment.starts_at, reverse=True)
        return [self._booking_summary(item) for item in matches[: max(limit, 0)]]

    async def list_patients(self, clinic_id: UUID, search: str | None = None) -> list[Patient]:
        needle = (search or "").casefold().strip()
        patients = [
            patient
            for patient in self.patients.values()
            if patient.clinic_id == clinic_id
            and (
                not needle
                or needle in patient.name.casefold()
                or needle in patient.wa_number.casefold()
            )
        ]
        return sorted(patients, key=lambda patient: patient.name.casefold())

    async def create_service(self, values: dict[str, Any]) -> Service:
        async with self._lock:
            service = Service.model_validate(values)
            self.services[service.id] = service
            return service

    async def update_service(self, service_id: UUID, values: dict[str, Any]) -> Service:
        async with self._lock:
            current = self.services[service_id]
            updated = Service.model_validate({**current.model_dump(), **values})
            self.services[service_id] = updated
            return updated

    async def delete_service(self, service_id: UUID) -> None:
        async with self._lock:
            self.services.pop(service_id, None)

    async def service_has_future_bookings(self, service_id: UUID, now: datetime) -> bool:
        return any(
            appointment.service_id == service_id
            and appointment.starts_at > now
            and appointment.status in {AppointmentStatus.BOOKED, AppointmentStatus.CONFIRMED}
            for appointment in self.appointments.values()
        )

    async def list_message_log(
        self, clinic_id: UUID, *, patient_id: UUID | None = None, limit: int = 100
    ) -> list[MessageLogEntry]:
        messages = [
            MessageLogEntry.model_validate(item)
            for item in self.message_log
            if item["clinic_id"] == clinic_id
            and (patient_id is None or item["patient_id"] == patient_id)
        ]
        messages.sort(key=lambda item: item.created_at, reverse=True)
        return messages[: max(limit, 0)]

    async def list_outbox(
        self, clinic_id: UUID, *, status: OutboxStatus | None = None, limit: int = 200
    ) -> list[NotificationOutbox]:
        items = [
            item
            for item in self.outbox.values()
            if item.clinic_id == clinic_id and (status is None or item.status is status)
        ]
        items.sort(key=lambda item: item.created_at, reverse=True)
        return items[: max(limit, 0)]

    async def get_outbox(self, outbox_id: UUID) -> NotificationOutbox | None:
        return self.outbox.get(outbox_id)

    async def list_jobs(
        self, clinic_id: UUID, *, status: JobStatus | None = None, limit: int = 200
    ) -> list[AutomationJob]:
        jobs = [
            job
            for job in self.jobs.values()
            if job.clinic_id == clinic_id and (status is None or job.status is status)
        ]
        jobs.sort(key=lambda job: job.due_at, reverse=True)
        return jobs[: max(limit, 0)]

    async def get_job(self, job_id: UUID) -> AutomationJob | None:
        return self.jobs.get(job_id)

    async def list_webhook_events(self, clinic_id: UUID, *, limit: int = 200) -> list[WebhookEvent]:
        events = [event for event in self.events.values() if event.clinic_id == clinic_id]
        events.sort(key=lambda event: event.created_at, reverse=True)
        return events[: max(limit, 0)]

    async def list_patient_consents(
        self,
        clinic_id: UUID,
        *,
        patient_id: UUID | None = None,
        appointment_id: UUID | None = None,
    ) -> list[PatientConsent]:
        consents = [
            consent
            for consent in self.consents.values()
            if consent.clinic_id == clinic_id
            and (patient_id is None or consent.patient_id == patient_id)
            and (appointment_id is None or consent.appointment_id == appointment_id)
        ]
        return sorted(consents, key=lambda consent: consent.consented_at, reverse=True)

    async def save_patient_consent(
        self,
        clinic_id: UUID,
        patient_id: UUID,
        consent_type: str,
        consent_text: str,
        consent_version: str,
    ) -> PatientConsent:
        async with self._lock:
            patient = self.patients.get(patient_id)
            if patient is None or patient.clinic_id != clinic_id:
                raise ValueError("Patient does not belong to clinic")
            consent = PatientConsent(
                id=uuid4(),
                clinic_id=clinic_id,
                patient_id=patient_id,
                consent_type=consent_type,
                consent_text=consent_text,
                consent_version=consent_version,
                consented_at=self._clock.now(),
            )
            self.consents[consent.id] = consent
            return consent

    async def list_faq_entries(self, clinic_id: UUID) -> list[FAQEntry]:
        entries = [entry for entry in self.faq_entries.values() if entry.clinic_id == clinic_id]
        return sorted(
            entries,
            key=lambda entry: (entry.category.casefold(), entry.question.casefold()),
        )

    async def get_faq_entry(self, entry_id: UUID) -> FAQEntry | None:
        return self.faq_entries.get(entry_id)

    async def create_faq_entry(self, values: dict[str, Any]) -> FAQEntry:
        async with self._lock:
            entry = FAQEntry.model_validate(values)
            self.faq_entries[entry.id] = entry
            return entry

    async def update_faq_entry(self, entry_id: UUID, values: dict[str, Any]) -> FAQEntry:
        async with self._lock:
            current = self.faq_entries[entry_id]
            updated = FAQEntry.model_validate({**current.model_dump(), **values})
            self.faq_entries[entry_id] = updated
            return updated

    async def delete_faq_entry(self, entry_id: UUID) -> None:
        async with self._lock:
            self.faq_entries.pop(entry_id, None)
