"""Production database adapter backed by the asynchronous Supabase client."""

from collections.abc import Sequence
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Any, cast
from uuid import UUID

from postgrest.exceptions import APIError
from supabase import AsyncClient, acreate_client

from app.core.clock import Clock
from app.core.exceptions import BookingConflictError
from app.domain.models import (
    Appointment,
    AppointmentStatus,
    AutomationJob,
    BookingSummary,
    Clinic,
    ConversationState,
    DomainModel,
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


class SupabaseDatabase:
    """Map domain-level persistence operations to Supabase REST and RPC calls."""

    def __init__(self, client: AsyncClient, clock: Clock) -> None:
        self._client = client
        self._clock = clock

    @classmethod
    async def create(cls, url: str, service_role_key: str, clock: Clock) -> "SupabaseDatabase":
        """Construct a database adapter using Supabase service-role credentials."""

        return cls(await acreate_client(url, service_role_key), clock)

    async def get_clinic(self, clinic_id: UUID) -> Clinic | None:
        result = await self._client.table("clinics").select("*").eq("id", str(clinic_id)).execute()
        return self._model_or_none(Clinic, result.data)

    async def get_clinic_by_wa_phone_id(self, phone_id: str) -> Clinic | None:
        result = (
            await self._client.table("clinics")
            .select("*")
            .eq("wa_phone_id", phone_id)
            .execute()
        )
        return self._model_or_none(Clinic, result.data)

    async def get_clinic_by_telegram_chat_id(self, chat_id: str) -> Clinic | None:
        result = (
            await self._client.table("clinics")
            .select("*")
            .eq("telegram_chat_id", chat_id)
            .execute()
        )
        return self._model_or_none(Clinic, result.data)

    async def persist_webhook_event(
        self, message_id: str, clinic_id: UUID, payload: dict[str, Any]
    ) -> bool:
        try:
            await self._client.table("webhook_events").insert(
                {"message_id": message_id, "clinic_id": str(clinic_id), "payload": payload}
            ).execute()
        except APIError as exc:
            if exc.code == "23505":
                return False
            raise
        return True

    async def pop_unprocessed_events(self, limit: int) -> list[WebhookEvent]:
        result = await self._client.rpc("pop_unprocessed_events", {"n": limit}).execute()
        return [WebhookEvent.model_validate(row) for row in self._rows(result.data)]

    async def mark_event_processed(self, message_id: str) -> None:
        await self._client.table("webhook_events").update(
            {
                "processed_at": self._clock.now().isoformat(),
                "claimed_at": None,
                "last_error": None,
            }
        ).eq("message_id", message_id).execute()

    async def release_event(self, message_id: str, error: str) -> None:
        await self._client.table("webhook_events").update(
            {"claimed_at": None, "last_error": error[:2000]}
        ).eq("message_id", message_id).execute()

    async def get_or_create_patient(
        self, clinic_id: UUID, wa_number: str, name: str
    ) -> Patient:
        existing = (
            await self._client.table("patients")
            .select("*")
            .eq("clinic_id", str(clinic_id))
            .eq("wa_number", wa_number)
            .execute()
        )
        patient = self._model_or_none(Patient, existing.data)
        if patient is not None:
            return patient
        result = await self._client.table("patients").insert(
            {"clinic_id": str(clinic_id), "wa_number": wa_number, "name": name}
        ).execute()
        patient = self._model_or_none(Patient, result.data)
        if patient is None:
            raise RuntimeError("Supabase did not return the created patient")
        return patient

    async def get_patient(self, patient_id: UUID) -> Patient | None:
        result = (
            await self._client.table("patients")
            .select("*")
            .eq("id", str(patient_id))
            .execute()
        )
        return self._model_or_none(Patient, result.data)

    async def update_patient_name(self, patient_id: UUID, name: str) -> Patient:
        result = (
            await self._client.table("patients")
            .update({"name": name})
            .eq("id", str(patient_id))
            .execute()
        )
        patient = self._model_or_none(Patient, result.data)
        if patient is None:
            raise RuntimeError("Patient name update did not return a row")
        return patient

    async def list_services(self, clinic_id: UUID) -> list[Service]:
        result = (
            await self._client.table("services")
            .select("*")
            .eq("clinic_id", str(clinic_id))
            .order("name")
            .execute()
        )
        return [Service.model_validate(row) for row in self._rows(result.data)]

    async def get_service(self, service_id: UUID) -> Service | None:
        result = (
            await self._client.table("services")
            .select("*")
            .eq("id", str(service_id))
            .execute()
        )
        return self._model_or_none(Service, result.data)

    async def get_conversation_state(
        self, clinic_id: UUID, patient_id: UUID
    ) -> ConversationState | None:
        result = (
            await self._client.table("conversation_states")
            .select("*")
            .eq("clinic_id", str(clinic_id))
            .eq("patient_id", str(patient_id))
            .execute()
        )
        return self._model_or_none(ConversationState, result.data)

    async def save_conversation_state(self, state: ConversationState) -> None:
        await self._client.table("conversation_states").upsert(
            state.model_dump(mode="json"), on_conflict="clinic_id,patient_id"
        ).execute()

    async def log_message(
        self,
        clinic_id: UUID,
        patient_id: UUID | None,
        channel: str,
        direction: str,
        body: str,
        raw: dict[str, Any],
    ) -> None:
        await self._client.table("message_log").insert(
            {
                "clinic_id": str(clinic_id),
                "patient_id": str(patient_id) if patient_id else None,
                "channel": channel,
                "direction": direction,
                "body": body,
                "raw": raw,
            }
        ).execute()

    async def list_open_appointments(
        self, clinic_id: UUID, starts_before: datetime, ends_after: datetime
    ) -> list[Appointment]:
        result = (
            await self._client.table("appointments")
            .select("*")
            .eq("clinic_id", str(clinic_id))
            .in_("status", [AppointmentStatus.BOOKED, AppointmentStatus.CONFIRMED])
            .lt("starts_at", starts_before.isoformat())
            .gt("ends_at", ends_after.isoformat())
            .execute()
        )
        return [Appointment.model_validate(row) for row in self._rows(result.data)]

    async def finalize_booking(self, command: FinalizeBookingCommand) -> Appointment:
        parameters = {
            "p_clinic_id": str(command.clinic_id),
            "p_patient_id": str(command.patient_id),
            "p_service_id": str(command.service_id),
            "p_starts_at": command.starts_at.isoformat(),
            "p_ends_at": command.ends_at.isoformat(),
            "p_medical_aid_name": command.medical_aid_name,
            "p_medical_aid_number": command.medical_aid_number,
            "p_dependent_code": command.dependent_code,
            "p_whatsapp_to": command.whatsapp_to,
            "p_whatsapp_payload": command.whatsapp_payload,
            "p_telegram_to": command.telegram_to,
            "p_telegram_payload": command.telegram_payload,
        }
        try:
            result = await self._client.rpc("finalize_booking", parameters).execute()
        except APIError as exc:
            if exc.code == "23P01" or "no longer available" in str(exc):
                raise BookingConflictError("Booking slot is no longer available") from exc
            raise
        appointment = self._model_or_none(Appointment, result.data)
        if appointment is None:
            raise RuntimeError("finalize_booking RPC returned no appointment")
        return appointment

    async def set_google_event_id(self, appointment_id: UUID, event_id: str) -> None:
        await self._client.table("appointments").update({"google_event_id": event_id}).eq(
            "id", str(appointment_id)
        ).execute()

    async def get_appointment(self, appointment_id: UUID) -> Appointment | None:
        result = (
            await self._client.table("appointments")
            .select("*")
            .eq("id", str(appointment_id))
            .execute()
        )
        return self._model_or_none(Appointment, result.data)

    async def get_booking_summary(self, appointment_id: UUID) -> BookingSummary | None:
        result = (
            await self._client.table("appointments")
            .select("*,patients(*),services(*)")
            .eq("id", str(appointment_id))
            .execute()
        )
        rows = self._rows(result.data)
        return self._summary(rows[0]) if rows else None

    async def transition_appointment_status(
        self,
        appointment_id: UUID,
        patient_id: UUID,
        from_statuses: Sequence[AppointmentStatus],
        to_status: AppointmentStatus,
    ) -> Appointment | None:
        result = (
            await self._client.table("appointments")
            .update({"status": to_status.value})
            .eq("id", str(appointment_id))
            .eq("patient_id", str(patient_id))
            .in_("status", [status.value for status in from_statuses])
            .execute()
        )
        return self._model_or_none(Appointment, result.data)

    async def mark_no_show(self, appointment_id: UUID) -> Appointment | None:
        result = await self._client.rpc(
            "mark_no_show", {"p_appointment_id": str(appointment_id)}
        ).execute()
        return self._model_or_none(Appointment, result.data)

    async def list_booking_summaries(
        self, clinic_id: UUID, starts_at: datetime, ends_at: datetime
    ) -> list[BookingSummary]:
        result = (
            await self._client.table("appointments")
            .select("*,patients(*),services(*)")
            .eq("clinic_id", str(clinic_id))
            .gte("starts_at", starts_at.isoformat())
            .lt("starts_at", ends_at.isoformat())
            .order("starts_at")
            .execute()
        )
        return [self._summary(row) for row in self._rows(result.data)]

    async def enqueue_outbox(
        self, clinic_id: UUID, channel: str, to_id: str, payload: dict[str, Any]
    ) -> None:
        await self._client.table("notification_outbox").insert(
            {
                "clinic_id": str(clinic_id),
                "channel": channel,
                "to_id": to_id,
                "payload": payload,
            }
        ).execute()

    async def pop_pending_outbox(self, limit: int) -> list[NotificationOutbox]:
        result = await self._client.rpc("pop_pending_outbox", {"n": limit}).execute()
        return [NotificationOutbox.model_validate(row) for row in self._rows(result.data)]

    async def mark_outbox_sent(self, outbox_id: UUID) -> None:
        await self._client.table("notification_outbox").update(
            {"status": "sent", "last_error": None}
        ).eq("id", str(outbox_id)).execute()

    async def retry_outbox(
        self, outbox_id: UUID, next_try_at: datetime, error: str, *, failed: bool
    ) -> None:
        await self._client.table("notification_outbox").update(
            {
                "status": "failed" if failed else "pending",
                "next_try_at": next_try_at.isoformat(),
                "last_error": error[:2000],
            }
        ).eq("id", str(outbox_id)).execute()

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
        await self._client.table("automation_jobs").upsert(
            {
                "clinic_id": str(clinic_id),
                "appointment_id": str(appointment_id) if appointment_id else None,
                "patient_id": str(patient_id) if patient_id else None,
                "job_type": job_type,
                "due_at": due_at.isoformat(),
                "dedupe_key": dedupe_key,
                "payload": payload or {},
            },
            on_conflict="dedupe_key",
            ignore_duplicates=True,
        ).execute()

    async def pop_due_jobs(self, limit: int) -> list[AutomationJob]:
        result = await self._client.rpc("pop_due_jobs", {"n": limit}).execute()
        return [AutomationJob.model_validate(row) for row in self._rows(result.data)]

    async def complete_job(self, job_id: UUID) -> None:
        await self._client.table("automation_jobs").update(
            {"status": "completed", "claimed_at": None, "last_error": None}
        ).eq("id", str(job_id)).execute()

    async def retry_job(
        self, job_id: UUID, due_at: datetime, error: str, *, failed: bool = False
    ) -> None:
        await self._client.table("automation_jobs").update(
            {
                "status": "failed" if failed else "pending",
                "due_at": due_at.isoformat(),
                "claimed_at": None,
                "last_error": error[:2000],
            }
        ).eq("id", str(job_id)).execute()

    async def claim_daily_throttle(
        self, clinic_id: UUID, patient_id: UUID, throttle_key: str, local_date: date
    ) -> bool:
        try:
            await self._client.table("daily_throttles").insert(
                {
                    "clinic_id": str(clinic_id),
                    "patient_id": str(patient_id),
                    "throttle_key": throttle_key,
                    "local_date": local_date.isoformat(),
                }
            ).execute()
        except APIError as exc:
            if exc.code == "23505":
                return False
            raise
        return True

    async def list_clinics(self) -> list[Clinic]:
        result = await self._client.table("clinics").select("*").order("name").execute()
        return [Clinic.model_validate(row) for row in self._rows(result.data)]

    async def create_clinic(self, values: dict[str, Any]) -> Clinic:
        result = await self._client.table("clinics").insert(self._json_values(values)).execute()
        clinic = self._model_or_none(Clinic, result.data)
        if clinic is None:
            raise RuntimeError("Supabase did not return the created clinic")
        return clinic

    async def update_clinic(self, clinic_id: UUID, values: dict[str, Any]) -> Clinic:
        result = (
            await self._client.table("clinics")
            .update(self._json_values(values))
            .eq("id", str(clinic_id))
            .execute()
        )
        clinic = self._model_or_none(Clinic, result.data)
        if clinic is None:
            raise RuntimeError("Clinic update did not return a row")
        return clinic

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
        query = (
            self._client.table("appointments")
            .select("*,patients(*),services(*)")
            .eq("clinic_id", str(clinic_id))
        )
        if starts_at is not None:
            query = query.gte("starts_at", starts_at.isoformat())
        if ends_at is not None:
            query = query.lt("starts_at", ends_at.isoformat())
        if status is not None:
            query = query.eq("status", status.value)
        if service_id is not None:
            query = query.eq("service_id", str(service_id))
        if patient_id is not None:
            query = query.eq("patient_id", str(patient_id))
        if search:
            matching_patients = await self.list_patients(clinic_id, search)
            if not matching_patients:
                return []
            query = query.in_("patient_id", [str(patient.id) for patient in matching_patients])
        result = await query.order("starts_at", desc=True).limit(max(limit, 0)).execute()
        return [self._summary(row) for row in self._rows(result.data)]

    async def list_patients(self, clinic_id: UUID, search: str | None = None) -> list[Patient]:
        result = await (
            self._client.table("patients")
            .select("*")
            .eq("clinic_id", str(clinic_id))
            .order("name")
            .execute()
        )
        patients = [Patient.model_validate(row) for row in self._rows(result.data)]
        needle = (search or "").casefold().strip()
        if not needle:
            return patients
        return [
            patient
            for patient in patients
            if needle in patient.name.casefold() or needle in patient.wa_number.casefold()
        ]

    async def create_service(self, values: dict[str, Any]) -> Service:
        result = await self._client.table("services").insert(self._json_values(values)).execute()
        service = self._model_or_none(Service, result.data)
        if service is None:
            raise RuntimeError("Supabase did not return the created service")
        return service

    async def update_service(self, service_id: UUID, values: dict[str, Any]) -> Service:
        result = (
            await self._client.table("services")
            .update(self._json_values(values))
            .eq("id", str(service_id))
            .execute()
        )
        service = self._model_or_none(Service, result.data)
        if service is None:
            raise RuntimeError("Service update did not return a row")
        return service

    async def delete_service(self, service_id: UUID) -> None:
        await self._client.table("services").delete().eq("id", str(service_id)).execute()

    async def service_has_future_bookings(self, service_id: UUID, now: datetime) -> bool:
        result = (
            await self._client.table("appointments")
            .select("id")
            .eq("service_id", str(service_id))
            .in_("status", [AppointmentStatus.BOOKED.value, AppointmentStatus.CONFIRMED.value])
            .gt("starts_at", now.isoformat())
            .limit(1)
            .execute()
        )
        return bool(self._rows(result.data))

    async def list_message_log(
        self, clinic_id: UUID, *, patient_id: UUID | None = None, limit: int = 100
    ) -> list[MessageLogEntry]:
        query = self._client.table("message_log").select("*").eq("clinic_id", str(clinic_id))
        if patient_id is not None:
            query = query.eq("patient_id", str(patient_id))
        result = await query.order("created_at", desc=True).limit(max(limit, 0)).execute()
        return [MessageLogEntry.model_validate(row) for row in self._rows(result.data)]

    async def list_outbox(
        self, clinic_id: UUID, *, status: OutboxStatus | None = None, limit: int = 200
    ) -> list[NotificationOutbox]:
        query = (
            self._client.table("notification_outbox").select("*").eq("clinic_id", str(clinic_id))
        )
        if status is not None:
            query = query.eq("status", status.value)
        result = await query.order("created_at", desc=True).limit(max(limit, 0)).execute()
        return [NotificationOutbox.model_validate(row) for row in self._rows(result.data)]

    async def get_outbox(self, outbox_id: UUID) -> NotificationOutbox | None:
        result = (
            await self._client.table("notification_outbox")
            .select("*")
            .eq("id", str(outbox_id))
            .execute()
        )
        return self._model_or_none(NotificationOutbox, result.data)

    async def list_jobs(
        self, clinic_id: UUID, *, status: JobStatus | None = None, limit: int = 200
    ) -> list[AutomationJob]:
        query = self._client.table("automation_jobs").select("*").eq("clinic_id", str(clinic_id))
        if status is not None:
            query = query.eq("status", status.value)
        result = await query.order("due_at", desc=True).limit(max(limit, 0)).execute()
        return [AutomationJob.model_validate(row) for row in self._rows(result.data)]

    async def get_job(self, job_id: UUID) -> AutomationJob | None:
        result = (
            await self._client.table("automation_jobs").select("*").eq("id", str(job_id)).execute()
        )
        return self._model_or_none(AutomationJob, result.data)

    async def list_webhook_events(self, clinic_id: UUID, *, limit: int = 200) -> list[WebhookEvent]:
        result = (
            await self._client.table("webhook_events")
            .select("*")
            .eq("clinic_id", str(clinic_id))
            .order("created_at", desc=True)
            .limit(max(limit, 0))
            .execute()
        )
        return [WebhookEvent.model_validate(row) for row in self._rows(result.data)]

    async def list_patient_consents(
        self,
        clinic_id: UUID,
        *,
        patient_id: UUID | None = None,
        appointment_id: UUID | None = None,
    ) -> list[PatientConsent]:
        query = self._client.table("patient_consents").select("*").eq("clinic_id", str(clinic_id))
        if patient_id is not None:
            query = query.eq("patient_id", str(patient_id))
        if appointment_id is not None:
            query = query.eq("appointment_id", str(appointment_id))
        result = await query.order("consented_at", desc=True).execute()
        return [PatientConsent.model_validate(row) for row in self._rows(result.data)]

    async def save_patient_consent(
        self,
        clinic_id: UUID,
        patient_id: UUID,
        consent_type: str,
        consent_text: str,
        consent_version: str,
    ) -> PatientConsent:
        result = await self._client.table("patient_consents").insert(
            {
                "clinic_id": str(clinic_id),
                "patient_id": str(patient_id),
                "consent_type": consent_type,
                "consent_text": consent_text,
                "consent_version": consent_version,
            }
        ).execute()
        consent = self._model_or_none(PatientConsent, result.data)
        if consent is None:
            raise RuntimeError("Supabase did not return the saved patient consent")
        return consent


    @staticmethod
    def _rows(data: Any) -> list[dict[str, Any]]:
        if data is None:
            return []
        if isinstance(data, list):
            return cast(list[dict[str, Any]], data)
        if isinstance(data, dict):
            return [cast(dict[str, Any], data)]
        raise TypeError(f"Unexpected Supabase response type: {type(data)!r}")

    @classmethod
    def _json_values(cls, values: dict[str, Any]) -> dict[str, Any]:
        return {key: cls._json_value(value) for key, value in values.items()}

    @classmethod
    def _json_value(cls, value: Any) -> Any:
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, (datetime, date, time)):
            return value.isoformat()
        if isinstance(value, list):
            return [cls._json_value(item) for item in value]
        if isinstance(value, dict):
            return {str(key): cls._json_value(item) for key, item in value.items()}
        return value

    @classmethod
    def _model_or_none[ModelT: DomainModel](
        cls, model: type[ModelT], data: Any
    ) -> ModelT | None:
        rows = cls._rows(data)
        return model.model_validate(rows[0]) if rows else None

    @staticmethod
    def _summary(row: dict[str, Any]) -> BookingSummary:
        appointment_data = {
            key: value for key, value in row.items() if key not in {"patients", "services"}
        }
        return BookingSummary(
            appointment=Appointment.model_validate(appointment_data),
            patient=Patient.model_validate(row["patients"]),
            service=Service.model_validate(row["services"]),
        )
