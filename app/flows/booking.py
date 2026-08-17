"""Deterministic WhatsApp booking conversation orchestration."""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from app.adapters.calendar import CalendarProvider
from app.adapters.intent import IntentKind, IntentModel
from app.adapters.whatsapp import ReplyButton, WhatsAppSender
from app.core.clock import Clock
from app.core.exceptions import BookingConflictError
from app.db.protocol import Database
from app.domain.messages import IncomingMessage
from app.domain.models import (
    AppointmentStatus,
    Clinic,
    ConversationState,
    ConversationStep,
    FinalizeBookingCommand,
    Patient,
    Service,
)
from app.flows.state_machine import ConversationTransitions
from app.services.notifications import NotificationFormatter
from app.services.slot_engine import SlotEngine
from app.services.trial_gate import TrialGate

logger = logging.getLogger(__name__)


class BookingFlow:
    """Own the patient state machine; models may classify but never transition it."""

    def __init__(
        self,
        database: Database,
        whatsapp: WhatsAppSender,
        calendar: CalendarProvider,
        intent: IntentModel,
        slot_engine: SlotEngine,
        trial_gate: TrialGate,
        notifications: NotificationFormatter,
        clock: Clock,
    ) -> None:
        self._database = database
        self._whatsapp = whatsapp
        self._calendar = calendar
        self._intent = intent
        self._slot_engine = slot_engine
        self._trial_gate = trial_gate
        self._notifications = notifications
        self._clock = clock

    async def handle(self, clinic: Clinic, message: IncomingMessage) -> None:
        """Handle one normalized inbound patient message."""

        patient = await self._database.get_or_create_patient(
            clinic.id, message.from_number, message.profile_name
        )
        await self._database.log_message(
            clinic.id,
            patient.id,
            "whatsapp",
            "inbound",
            message.display_text or message.text,
            message.raw,
        )
        decision = self._trial_gate.evaluate(clinic)
        if decision.blocked:
            await self._handle_blocked(clinic, patient, decision.reason or "inactive")
            return
        if await self._handle_appointment_action(clinic, patient, message.text):
            return

        state = await self._database.get_conversation_state(clinic.id, patient.id)
        if state is None:
            state = ConversationState(
                clinic_id=clinic.id,
                patient_id=patient.id,
                state=ConversationStep.IDLE,
                updated_at=self._clock.now(),
            )

        match state.state:
            case ConversationStep.IDLE:
                await self._handle_idle(clinic, patient, state, message.text)
            case ConversationStep.AWAIT_SERVICE:
                await self._handle_service(clinic, patient, state, message.text)
            case ConversationStep.AWAIT_SLOT:
                await self._handle_slot(clinic, patient, state, message.text)
            case ConversationStep.AWAIT_MA_NAME:
                await self._handle_medical_aid_name(clinic, patient, state, message.text)
            case ConversationStep.AWAIT_MA_NUMBER:
                await self._handle_medical_aid_number(clinic, patient, state, message.text)
            case ConversationStep.AWAIT_MA_DEPENDENT:
                await self._handle_medical_aid_dependent(
                    clinic, patient, state, message.text
                )

    async def _handle_idle(
        self, clinic: Clinic, patient: Patient, state: ConversationState, text: str
    ) -> None:
        intent = await self._intent.classify(text)
        if intent.intent is IntentKind.BOOK:
            await self._offer_services(clinic, patient, state)
            return
        greeting = clinic.brand_voice or (
            f"Hi {patient.name}! I can help you book a dental appointment. "
            "Reply 'book appointment' to begin."
        )
        await self._reply_text(clinic, patient, greeting)

    async def _offer_services(
        self, clinic: Clinic, patient: Patient, state: ConversationState
    ) -> None:
        services = await self._database.list_services(clinic.id)
        if not services:
            await self._reply_text(
                clinic, patient, "This clinic has no bookable services configured yet."
            )
            return
        offered = services[:3]
        await self._save_state(
            state,
            ConversationStep.AWAIT_SERVICE,
            {"offered_service_ids": [str(service.id) for service in offered]},
        )
        await self._reply_buttons(
            clinic,
            patient,
            "Which service would you like?",
            [ReplyButton(f"service:{service.id}", service.name) for service in offered],
        )

    async def _handle_service(
        self, clinic: Clinic, patient: Patient, state: ConversationState, text: str
    ) -> None:
        service = await self._resolve_service(clinic, state, text)
        if service is None:
            await self._reply_text(clinic, patient, "Please choose one of the offered services.")
            await self._offer_services(clinic, patient, state)
            return
        context = dict(state.slot)
        context["service_id"] = str(service.id)
        await self._offer_slots(clinic, patient, state, service, context)

    async def _offer_slots(
        self,
        clinic: Clinic,
        patient: Patient,
        state: ConversationState,
        service: Service,
        context: dict[str, Any],
    ) -> None:
        slots = await self._slot_engine.offer(clinic, service)
        if not slots:
            await self._reply_text(
                clinic, patient, "I couldn't find an available slot in the next 30 days."
            )
            return
        context["service_id"] = str(service.id)
        context["offered_slots"] = [self._iso_utc(slot) for slot in slots]
        await self._save_state(state, ConversationStep.AWAIT_SLOT, context)
        timezone = ZoneInfo(clinic.timezone)
        buttons = [
            ReplyButton(
                f"slot:{self._iso_utc(slot)}",
                slot.astimezone(timezone).strftime("%a %d %H:%M"),
            )
            for slot in slots
        ]
        await self._reply_buttons(clinic, patient, "Choose an appointment time:", buttons)

    async def _handle_slot(
        self, clinic: Clinic, patient: Patient, state: ConversationState, text: str
    ) -> None:
        service = await self._service_from_state(clinic, state)
        selected = text.removeprefix("slot:") if text.startswith("slot:") else ""
        offered = state.slot.get("offered_slots", [])
        if not isinstance(offered, list) or selected not in offered:
            await self._reply_text(clinic, patient, "Please choose one of the offered times.")
            await self._offer_slots(clinic, patient, state, service, dict(state.slot))
            return
        starts_at = self._parse_utc(selected)
        if not await self._slot_engine.is_available(clinic, service, starts_at):
            await self._reply_text(
                clinic, patient, "That time was just taken. Here are the next available times."
            )
            await self._offer_slots(clinic, patient, state, service, dict(state.slot))
            return
        context = dict(state.slot)
        context["starts_at"] = self._iso_utc(starts_at)
        context["ends_at"] = self._iso_utc(
            starts_at + timedelta(minutes=service.duration_min)
        )
        await self._save_state(state, ConversationStep.AWAIT_MA_NAME, context)
        await self._reply_text(
            clinic,
            patient,
            "What is your medical aid provider? Reply 'self-pay' if you are paying yourself.",
        )

    async def _handle_medical_aid_name(
        self, clinic: Clinic, patient: Patient, state: ConversationState, text: str
    ) -> None:
        if text.casefold().strip() in {"self-pay", "self pay", "cash", "none"}:
            await self._finalize(clinic, patient, state, None, None, None)
            return
        if not text.strip():
            await self._reply_text(clinic, patient, "Please enter a medical aid provider.")
            return
        context = dict(state.slot)
        context["medical_aid_name"] = text.strip()[:100]
        await self._save_state(state, ConversationStep.AWAIT_MA_NUMBER, context)
        await self._reply_text(clinic, patient, "What is your medical aid membership number?")

    async def _handle_medical_aid_number(
        self, clinic: Clinic, patient: Patient, state: ConversationState, text: str
    ) -> None:
        if not text.strip():
            await self._reply_text(clinic, patient, "Please enter your membership number.")
            return
        context = dict(state.slot)
        context["medical_aid_number"] = text.strip()[:100]
        await self._save_state(state, ConversationStep.AWAIT_MA_DEPENDENT, context)
        await self._reply_text(clinic, patient, "What is the dependent code? (For example, 01)")

    async def _handle_medical_aid_dependent(
        self, clinic: Clinic, patient: Patient, state: ConversationState, text: str
    ) -> None:
        if not text.strip():
            await self._reply_text(clinic, patient, "Please enter the dependent code.")
            return
        await self._finalize(
            clinic,
            patient,
            state,
            self._optional_text(state.slot.get("medical_aid_name")),
            self._optional_text(state.slot.get("medical_aid_number")),
            text.strip()[:20],
        )

    async def _finalize(
        self,
        clinic: Clinic,
        patient: Patient,
        state: ConversationState,
        medical_aid_name: str | None,
        medical_aid_number: str | None,
        dependent_code: str | None,
    ) -> None:
        service = await self._service_from_state(clinic, state)
        starts_at = self._parse_utc(str(state.slot["starts_at"]))
        ends_at = self._parse_utc(str(state.slot["ends_at"]))
        confirmation = self._notifications.patient_confirmation_details(
            clinic, service, starts_at
        )
        owner_text = self._notifications.owner_new_booking_details(
            clinic,
            patient,
            service,
            starts_at,
            medical_aid_name,
            medical_aid_number,
            dependent_code,
        )
        command = FinalizeBookingCommand(
            clinic_id=clinic.id,
            patient_id=patient.id,
            service_id=service.id,
            starts_at=starts_at,
            ends_at=ends_at,
            medical_aid_name=medical_aid_name,
            medical_aid_number=medical_aid_number,
            dependent_code=dependent_code,
            whatsapp_to=patient.wa_number,
            whatsapp_payload={"kind": "text", "text": confirmation},
            telegram_to=clinic.telegram_chat_id,
            telegram_payload={"text": owner_text},
        )
        try:
            appointment = await self._database.finalize_booking(command)
        except BookingConflictError:
            await self._reply_text(
                clinic, patient, "That time was just taken. Please choose another slot."
            )
            await self._offer_slots(clinic, patient, state, service, dict(state.slot))
            return

        try:
            event_id = await self._calendar.create_event(
                clinic, service.name, patient.name, appointment.starts_at, appointment.ends_at
            )
            await self._database.set_google_event_id(appointment.id, event_id)
        except Exception as exc:
            logger.warning("calendar_create_deferred", exc_info=exc)
            await self._database.enqueue_job(
                clinic.id,
                "calendar_retry",
                self._clock.now() + timedelta(minutes=5),
                f"calendar-retry:{appointment.id}",
                appointment_id=appointment.id,
                patient_id=patient.id,
            )
        await self._save_state(state, ConversationStep.IDLE, {})

    async def _handle_appointment_action(
        self, clinic: Clinic, patient: Patient, text: str
    ) -> bool:
        action, separator, raw_id = text.partition(":")
        if not separator or action not in {"confirm", "reschedule", "cancel"}:
            return False
        try:
            appointment_id = UUID(raw_id)
        except ValueError:
            await self._reply_text(clinic, patient, "That appointment action is invalid.")
            return True
        if action == "confirm":
            updated = await self._database.transition_appointment_status(
                appointment_id,
                patient.id,
                [AppointmentStatus.BOOKED],
                AppointmentStatus.CONFIRMED,
            )
            await self._reply_text(
                clinic,
                patient,
                "Thanks—your appointment is confirmed."
                if updated
                else "That appointment can no longer be confirmed.",
            )
            return True

        updated = await self._database.transition_appointment_status(
            appointment_id,
            patient.id,
            [AppointmentStatus.BOOKED, AppointmentStatus.CONFIRMED],
            AppointmentStatus.CANCELLED,
        )
        if updated is None:
            await self._reply_text(clinic, patient, "That appointment can no longer be changed.")
            return True
        summary = await self._database.get_booking_summary(appointment_id)
        if action == "cancel":
            await self._reply_text(clinic, patient, "Your appointment has been cancelled.")
            if clinic.telegram_chat_id and summary:
                await self._database.enqueue_outbox(
                    clinic.id,
                    "telegram",
                    clinic.telegram_chat_id,
                    {
                        "text": self._notifications.owner_status_change(
                            "Cancelled",
                            clinic,
                            summary.patient,
                            summary.service,
                            summary.appointment,
                        )
                    },
                )
            return True

        if summary is None:
            await self._reply_text(clinic, patient, "I couldn't reload that appointment.")
            return True
        state = ConversationState(
            clinic_id=clinic.id,
            patient_id=patient.id,
            state=ConversationStep.IDLE,
            slot={"reschedule_from": str(appointment_id)},
            updated_at=self._clock.now(),
        )
        await self._offer_slots(
            clinic,
            patient,
            state,
            summary.service,
            {"service_id": str(summary.service.id), "reschedule_from": str(appointment_id)},
        )
        return True

    async def _handle_blocked(
        self, clinic: Clinic, patient: Patient, reason: str
    ) -> None:
        claimed = await self._database.claim_daily_throttle(
            clinic.id, patient.id, "patient_flow_blocked", self._trial_gate.local_date(clinic)
        )
        if not claimed:
            return
        await self._database.enqueue_outbox(
            clinic.id,
            "whatsapp",
            patient.wa_number,
            {
                "kind": "text",
                "text": (
                    "Sorry, this clinic's booking assistant is temporarily unavailable. "
                    "Please contact the clinic directly."
                ),
            },
        )
        if clinic.telegram_chat_id:
            await self._database.enqueue_outbox(
                clinic.id,
                "telegram",
                clinic.telegram_chat_id,
                {"text": f"BOOKABL patient flow blocked for {clinic.name}: {reason}."},
            )

    async def _resolve_service(
        self, clinic: Clinic, state: ConversationState, text: str
    ) -> Service | None:
        offered_raw = state.slot.get("offered_service_ids", [])
        offered = {str(item) for item in offered_raw} if isinstance(offered_raw, list) else set()
        if text.startswith("service:"):
            raw_id = text.removeprefix("service:")
            if raw_id not in offered:
                return None
            try:
                service = await self._database.get_service(UUID(raw_id))
            except ValueError:
                return None
            return service if service and service.clinic_id == clinic.id else None
        normalized = text.casefold().strip()
        return next(
            (
                service
                for service in await self._database.list_services(clinic.id)
                if str(service.id) in offered and service.name.casefold() == normalized
            ),
            None,
        )

    async def _service_from_state(
        self, clinic: Clinic, state: ConversationState
    ) -> Service:
        raw_id = str(state.slot.get("service_id", ""))
        service = await self._database.get_service(UUID(raw_id))
        if service is None or service.clinic_id != clinic.id:
            raise ValueError("Conversation references an invalid service")
        return service

    async def _save_state(
        self,
        current: ConversationState,
        target: ConversationStep,
        context: dict[str, Any],
    ) -> ConversationState:
        ConversationTransitions.validate(current.state, target)
        updated = ConversationState(
            clinic_id=current.clinic_id,
            patient_id=current.patient_id,
            state=target,
            slot=context,
            updated_at=self._clock.now(),
        )
        await self._database.save_conversation_state(updated)
        return updated

    async def _reply_text(self, clinic: Clinic, patient: Patient, text: str) -> None:
        await self._whatsapp.send_text(clinic, patient.wa_number, text)
        await self._database.log_message(
            clinic.id, patient.id, "whatsapp", "outbound", text, {}
        )

    async def _reply_buttons(
        self,
        clinic: Clinic,
        patient: Patient,
        body: str,
        buttons: list[ReplyButton],
    ) -> None:
        await self._whatsapp.send_buttons(clinic, patient.wa_number, body, buttons)
        await self._database.log_message(
            clinic.id,
            patient.id,
            "whatsapp",
            "outbound",
            body,
            {"buttons": [{"id": button.id, "title": button.title} for button in buttons]},
        )

    @staticmethod
    def _iso_utc(value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _parse_utc(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("Slot timestamp must include a timezone")
        return parsed.astimezone(UTC)

    @staticmethod
    def _optional_text(value: object) -> str | None:
        return str(value) if isinstance(value, str) and value else None

