"""Deterministic WhatsApp booking conversation orchestration."""

import logging
import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from app.adapters.calendar import CalendarProvider
from app.adapters.intent import IntentModel
from app.adapters.whatsapp import ListRow, ReplyButton, WhatsAppSender
from app.core.clock import Clock
from app.core.exceptions import BookingConflictError
from app.db.protocol import Database
from app.domain.messages import IncomingMessage
from app.domain.models import (
    Appointment,
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
from app.services.privacy import minimal_patient_name
from app.services.slot_engine import SlotEngine
from app.services.trial_gate import TrialGate

logger = logging.getLogger(__name__)

_PATIENT_PLACEHOLDER = "WhatsApp patient"
_CONSENT_VERSION = "v2"
_MA_DETAILS_RETRY = (
    "I couldn't match all four details. Send them together like this:\n"
    "1. Name + Surname \n2. Medical Aid Scheme \n"
    "3. MA Number \n4. Dependant Code"
)


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
            clinic.id, message.from_number, _PATIENT_PLACEHOLDER
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
        state = await self._database.get_conversation_state(clinic.id, patient.id)
        first_contact = state is None
        if state is None:
            state = ConversationState(
                clinic_id=clinic.id,
                patient_id=patient.id,
                state=ConversationStep.IDLE,
                updated_at=self._clock.now(),
            )

        if first_contact:
            await self._show_entry_menu(clinic, patient, state)
            return

        if state.state is ConversationStep.HUMAN_HANDOFF:
            await self._relay_handoff_message(clinic, patient, state, message.display_text)
            return
        if await self._handle_appointment_action(clinic, patient, state, message.text):
            return

        match state.state:
            case ConversationStep.IDLE:
                await self._handle_idle(clinic, patient, state, message.text)
            case ConversationStep.AWAIT_ENTRY_CHOICE:
                await self._handle_entry_choice(clinic, patient, state, message.text)
            case ConversationStep.AWAIT_SERVICE:
                await self._handle_service(clinic, patient, state, message.text)
            case ConversationStep.AWAIT_DATE:
                await self._handle_date(clinic, patient, state, message.text)
            case ConversationStep.AWAIT_CUSTOM_DATE:
                await self._handle_custom_date(clinic, patient, state, message.text)
            case ConversationStep.AWAIT_TIME:
                await self._handle_time(clinic, patient, state, message.text)
            case ConversationStep.AWAIT_CUSTOM_TIME:
                await self._handle_custom_time(clinic, patient, state, message.text)
            case ConversationStep.AWAIT_PAYMENT_TYPE:
                await self._handle_payment_type(clinic, patient, state, message.text)
            case ConversationStep.AWAIT_POPIA_MA_CONSENT:
                await self._handle_medical_aid_consent(clinic, patient, state, message.text)
            case ConversationStep.AWAIT_MA_DETAILS_SINGLE_MSG:
                await self._handle_medical_aid_details(clinic, patient, state, message.text)
            case ConversationStep.AWAIT_MA_DETAILS_CONFIRMATION:
                await self._handle_medical_aid_details_confirmation(
                    clinic, patient, state, message.text
                )
            case ConversationStep.AWAIT_CASH_NAME:
                await self._handle_cash_name(clinic, patient, state, message.text)
            case ConversationStep.AWAIT_CASH_NAME_CONFIRMATION:
                await self._handle_cash_name_confirmation(
                    clinic, patient, state, message.text
                )
            case ConversationStep.AWAIT_CANCEL_CONFIRMATION:
                await self._handle_cancel_confirmation(
                    clinic, patient, state, message.text
                )

    async def _handle_idle(
        self, clinic: Clinic, patient: Patient, state: ConversationState, text: str
    ) -> None:
        del text
        await self._show_entry_menu(clinic, patient, state)

    async def _show_entry_menu(
        self, clinic: Clinic, patient: Patient, state: ConversationState
    ) -> None:
        await self._save_state(state, ConversationStep.AWAIT_ENTRY_CHOICE, {})
        await self._reply_buttons(
            clinic,
            patient,
            self._welcome_message(clinic),
            [
                ReplyButton("start:book", "Book appointment"),
                ReplyButton("start:human", "Chat to receptionist"),
            ],
            style=True,
        )

    async def _handle_entry_choice(
        self, clinic: Clinic, patient: Patient, state: ConversationState, text: str
    ) -> None:
        choice = text.casefold().strip()
        if choice == "start:book" or self._is_booking_request(choice):
            await self._offer_services(clinic, patient, state)
            return
        if choice in {
            "start:human",
            "chat to reception",
            "chat to receptionist",
            "chat with receptionist",
        }:
            await self._start_handoff(
                clinic,
                patient,
                state,
                "Patient requested reception",
                voluntary=True,
            )
            return
        await self._start_handoff(clinic, patient, state, text)

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
            "What would you like to book?",
            [ReplyButton(f"service:{service.id}", service.name) for service in offered],
        )

    async def _handle_service(
        self, clinic: Clinic, patient: Patient, state: ConversationState, text: str
    ) -> None:
        service = await self._resolve_service(clinic, state, text)
        if service is None:
            await self._start_handoff(clinic, patient, state, text)
            return
        context = dict(state.slot)
        context["service_id"] = str(service.id)
        await self._offer_dates(clinic, patient, state, service, context)

    async def _offer_dates(
        self,
        clinic: Clinic,
        patient: Patient,
        state: ConversationState,
        service: Service,
        context: dict[str, Any],
    ) -> None:
        dates = await self._slot_engine.offer_dates(clinic, service)
        if not dates:
            await self._reply_text(
                clinic,
                patient,
                "I couldn't find availability in the next 30 days. Reception can help.",
            )
            await self._start_handoff(clinic, patient, state, "No availability found")
            return
        context["service_id"] = str(service.id)
        context["offered_dates"] = [value.isoformat() for value in dates]
        await self._save_state(state, ConversationStep.AWAIT_DATE, context)
        await self._reply_list(
            clinic,
            patient,
            "Let's find a day that suits you. Here are the nearest available dates.",
            "Choose a date",
            [
                ListRow(f"date:{value.isoformat()}", value.strftime("%a %d %b"))
                for value in dates
            ]
            + [ListRow("date:other", "Other date", "Type your preferred date")],
        )

    async def _handle_date(
        self, clinic: Clinic, patient: Patient, state: ConversationState, text: str
    ) -> None:
        if text.casefold().strip() == "date:other":
            await self._save_state(
                state, ConversationStep.AWAIT_CUSTOM_DATE, dict(state.slot)
            )
            await self._reply_text(
                clinic, patient, "What date works best? You can type it as 25/09/2026."
            )
            return
        raw_date = text.removeprefix("date:") if text.startswith("date:") else ""
        offered = state.slot.get("offered_dates", [])
        if not isinstance(offered, list) or raw_date not in offered:
            await self._start_handoff(clinic, patient, state, text)
            return
        await self._offer_times(clinic, patient, state, date.fromisoformat(raw_date))

    async def _handle_custom_date(
        self, clinic: Clinic, patient: Patient, state: ConversationState, text: str
    ) -> None:
        selected_date = self._parse_local_date(text, clinic)
        if selected_date is None:
            await self._reply_text(
                clinic,
                patient,
                "I couldn't make out that date. Try DD/MM/YYYY, for example 25/09/2026.",
            )
            return
        await self._offer_times(clinic, patient, state, selected_date)

    async def _offer_times(
        self,
        clinic: Clinic,
        patient: Patient,
        state: ConversationState,
        selected_date: date,
    ) -> None:
        service = await self._service_from_state(clinic, state)
        slots = await self._slot_engine.offer_on_date(clinic, service, selected_date)
        if not slots:
            await self._reply_text(
                clinic,
                patient,
                "There are no free times on that date. Please choose another date.",
            )
            await self._offer_dates(clinic, patient, state, service, dict(state.slot))
            return
        context = dict(state.slot)
        context["selected_date"] = selected_date.isoformat()
        context["offered_times"] = [self._iso_utc(slot) for slot in slots]
        await self._save_state(state, ConversationStep.AWAIT_TIME, context)
        timezone = ZoneInfo(clinic.timezone)
        await self._reply_list(
            clinic,
            patient,
            f"Here are the available times for {selected_date:%a %d %b}:",
            "Choose a time",
            [
                ListRow(f"time:{self._iso_utc(slot)}", slot.astimezone(timezone).strftime("%H:%M"))
                for slot in slots
            ]
            + [ListRow("time:other", "Other time", "Type your preferred time")],
        )

    async def _handle_time(
        self, clinic: Clinic, patient: Patient, state: ConversationState, text: str
    ) -> None:
        if text.casefold().strip() == "time:other":
            await self._save_state(
                state, ConversationStep.AWAIT_CUSTOM_TIME, dict(state.slot)
            )
            await self._reply_text(
                clinic, patient, "What time works best? You can type it as 14:30."
            )
            return
        selected = text.removeprefix("time:") if text.startswith("time:") else ""
        offered = state.slot.get("offered_times", [])
        if not isinstance(offered, list) or selected not in offered:
            await self._start_handoff(clinic, patient, state, text)
            return
        await self._select_time(clinic, patient, state, self._parse_utc(selected))

    async def _handle_custom_time(
        self, clinic: Clinic, patient: Patient, state: ConversationState, text: str
    ) -> None:
        selected_time = self._parse_local_time(text)
        raw_date = state.slot.get("selected_date")
        if selected_time is None or not isinstance(raw_date, str):
            await self._reply_text(
                clinic, patient, "I couldn't make out that time. Try HH:MM, for example 14:30."
            )
            return
        timezone = ZoneInfo(clinic.timezone)
        starts_at = datetime.combine(date.fromisoformat(raw_date), selected_time, timezone)
        await self._select_time(clinic, patient, state, starts_at.astimezone(UTC))

    async def _select_time(
        self,
        clinic: Clinic,
        patient: Patient,
        state: ConversationState,
        starts_at: datetime,
    ) -> None:
        service = await self._service_from_state(clinic, state)
        if not await self._slot_engine.is_available(clinic, service, starts_at):
            await self._reply_text(
                clinic, patient, "That time is unavailable. Here are the available times again."
            )
            await self._offer_times(
                clinic, patient, state, date.fromisoformat(str(state.slot["selected_date"]))
            )
            return
        context = dict(state.slot)
        context["starts_at"] = self._iso_utc(starts_at)
        context["ends_at"] = self._iso_utc(
            starts_at + timedelta(minutes=service.duration_min)
        )
        if context.get("reschedule_from"):
            await self._complete_reschedule(clinic, patient, state, service, context)
            return
        await self._save_state(state, ConversationStep.AWAIT_PAYMENT_TYPE, context)
        await self._reply_buttons(
            clinic,
            patient,
            "Will you be paying by medical aid or cash?",
            [
                ReplyButton("payment:medical_aid", "Medical Aid"),
                ReplyButton("payment:cash", "Cash"),
            ],
        )

    async def _handle_payment_type(
        self, clinic: Clinic, patient: Patient, state: ConversationState, text: str
    ) -> None:
        choice = text.casefold().strip()
        context = dict(state.slot)
        if choice in {"payment:medical_aid", "medical aid"}:
            context["payment_type"] = "medical_aid"
            await self._save_state(
                state, ConversationStep.AWAIT_POPIA_MA_CONSENT, context
            )
            await self._reply_text(
                clinic, patient, self._medical_aid_consent(clinic), style=False
            )
            return
        if choice in {"payment:cash", "cash"}:
            context["payment_type"] = "cash"
            await self._save_state(state, ConversationStep.AWAIT_CASH_NAME, context)
            await self._reply_text(clinic, patient, self._cash_consent(clinic), style=False)
            return
        await self._start_handoff(clinic, patient, state, text)

    async def _handle_medical_aid_consent(
        self, clinic: Clinic, patient: Patient, state: ConversationState, text: str
    ) -> None:
        if text.casefold().strip() != "yes":
            await self._save_state(state, ConversationStep.IDLE, {})
            await self._reply_text(
                clinic,
                patient,
                "No problem. We cannot complete the WhatsApp booking without your consent. "
                "Please contact the clinic directly to book.",
                style=False,
            )
            return
        await self._database.save_patient_consent(
            clinic.id,
            patient.id,
            "medical_aid",
            self._medical_aid_consent(clinic),
            _CONSENT_VERSION,
        )
        await self._save_state(
            state, ConversationStep.AWAIT_MA_DETAILS_SINGLE_MSG, dict(state.slot)
        )
        await self._reply_text(
            clinic,
            patient,
            "Great, send these details together in one message:\n"
            "1. Name + Surname\n"
            "2. Medical aid scheme/name (e.g. Discovery, GEMS or Bonitas)\n"
            "3. Medical aid no\n"
            "4. Dependant code",
            style=False,
        )

    async def _handle_medical_aid_details(
        self, clinic: Clinic, patient: Patient, state: ConversationState, text: str
    ) -> None:
        details = self._parse_medical_aid_details(text)
        if details is None:
            await self._reply_text(clinic, patient, _MA_DETAILS_RETRY, style=False)
            return
        patient_full_name, medical_aid_name, medical_aid_number, dependent_code = details
        context = dict(state.slot)
        context.update(
            {
                "patient_full_name": patient_full_name,
                "medical_aid_name": medical_aid_name,
                "medical_aid_number": medical_aid_number,
                "dependent_code": dependent_code,
            }
        )
        await self._save_state(
            state, ConversationStep.AWAIT_MA_DETAILS_CONFIRMATION, context
        )
        await self._reply_buttons(
            clinic,
            patient,
            self._medical_aid_confirmation(context),
            [
                ReplyButton("ma:confirm", "Confirm details"),
                ReplyButton("ma:edit", "Edit details"),
            ],
        )

    async def _handle_medical_aid_details_confirmation(
        self, clinic: Clinic, patient: Patient, state: ConversationState, text: str
    ) -> None:
        choice = text.casefold().strip()
        if choice in {"ma:edit", "edit", "edit details", "no"}:
            context = self._without_medical_aid_details(state.slot)
            await self._save_state(
                state, ConversationStep.AWAIT_MA_DETAILS_SINGLE_MSG, context
            )
            await self._reply_text(
                clinic,
                patient,
                "No problem—send the four corrected details together in one message.",
            )
            return
        if choice not in {"ma:confirm", "confirm", "confirm details", "yes"}:
            await self._reply_buttons(
                clinic,
                patient,
                self._medical_aid_confirmation(state.slot),
                [
                    ReplyButton("ma:confirm", "Confirm details"),
                    ReplyButton("ma:edit", "Edit details"),
                ],
            )
            return
        details = self._medical_aid_details_from_context(state.slot)
        if details is None:
            await self._save_state(
                state,
                ConversationStep.AWAIT_MA_DETAILS_SINGLE_MSG,
                self._without_medical_aid_details(state.slot),
            )
            await self._reply_text(clinic, patient, _MA_DETAILS_RETRY)
            return
        patient_full_name, medical_aid_name, medical_aid_number, dependent_code = details
        patient = await self._database.update_patient_name(patient.id, patient_full_name)
        await self._finalize(
            clinic,
            patient,
            state,
            medical_aid_name,
            medical_aid_number,
            dependent_code,
        )

    async def _handle_cash_name(
        self, clinic: Clinic, patient: Patient, state: ConversationState, text: str
    ) -> None:
        patient_full_name = self._parse_patient_name(text)
        if patient_full_name is None:
            await self._reply_text(
                clinic,
                patient,
                "Please send the patient's name and surname using letters only, "
                "for example Thandi Nkosi.",
            )
            return
        context = dict(state.slot)
        context["patient_full_name"] = patient_full_name
        await self._save_state(
            state, ConversationStep.AWAIT_CASH_NAME_CONFIRMATION, context
        )
        await self._reply_buttons(
            clinic,
            patient,
            f"I have the booking name as {patient_full_name}. Is that correct?",
            [
                ReplyButton("name:confirm", "Confirm name"),
                ReplyButton("name:edit", "Edit name"),
            ],
        )

    async def _handle_cash_name_confirmation(
        self, clinic: Clinic, patient: Patient, state: ConversationState, text: str
    ) -> None:
        choice = text.casefold().strip()
        if choice in {"name:edit", "edit", "edit name", "no"}:
            context = dict(state.slot)
            context.pop("patient_full_name", None)
            await self._save_state(state, ConversationStep.AWAIT_CASH_NAME, context)
            await self._reply_text(
                clinic,
                patient,
                "Please send the correct name and surname, for example Thandi Nkosi.",
            )
            return
        if choice not in {"name:confirm", "confirm", "confirm name", "yes"}:
            pending_name = str(state.slot.get("patient_full_name", ""))
            await self._reply_buttons(
                clinic,
                patient,
                f"I have the booking name as {pending_name}. Is that correct?",
                [
                    ReplyButton("name:confirm", "Confirm name"),
                    ReplyButton("name:edit", "Edit name"),
                ],
            )
            return
        patient_full_name = self._parse_patient_name(
            str(state.slot.get("patient_full_name", ""))
        )
        if patient_full_name is None:
            context = dict(state.slot)
            context.pop("patient_full_name", None)
            await self._save_state(state, ConversationStep.AWAIT_CASH_NAME, context)
            await self._reply_text(
                clinic,
                patient,
                "Please send the correct name and surname, for example Thandi Nkosi.",
            )
            return
        await self._database.save_patient_consent(
            clinic.id,
            patient.id,
            "cash",
            self._cash_consent(clinic),
            _CONSENT_VERSION,
        )
        patient = await self._database.update_patient_name(patient.id, patient_full_name)
        await self._finalize(clinic, patient, state, None, None, None)

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
            await self._offer_dates(clinic, patient, state, service, dict(state.slot))
            return

        try:
            event_id = await self._calendar.create_event(
                clinic, patient, appointment
            )
            if event_id is not None:
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
        self,
        clinic: Clinic,
        patient: Patient,
        state: ConversationState,
        text: str,
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

        summary = await self._database.get_booking_summary(appointment_id)
        if (
            summary is None
            or summary.patient.id != patient.id
            or summary.appointment.status
            not in {AppointmentStatus.BOOKED, AppointmentStatus.CONFIRMED}
        ):
            await self._reply_text(clinic, patient, "That appointment can no longer be changed.")
            return True
        if action == "cancel":
            context = {"appointment_id": str(appointment_id)}
            action_state = ConversationState(
                clinic_id=clinic.id,
                patient_id=patient.id,
                state=ConversationStep.IDLE,
                updated_at=self._clock.now(),
            )
            await self._save_state(
                action_state, ConversationStep.AWAIT_CANCEL_CONFIRMATION, context
            )
            await self._reply_buttons(
                clinic,
                patient,
                "Are you sure you want to cancel this appointment?",
                [
                    ReplyButton(f"cancel_confirm:{appointment_id}", "Yes, cancel"),
                    ReplyButton(f"cancel_keep:{appointment_id}", "Keep appointment"),
                ],
            )
            return True

        action_state = ConversationState(
            clinic_id=clinic.id,
            patient_id=patient.id,
            state=ConversationStep.IDLE,
            updated_at=self._clock.now(),
        )
        await self._offer_dates(
            clinic,
            patient,
            action_state,
            summary.service,
            {"service_id": str(summary.service.id), "reschedule_from": str(appointment_id)},
        )
        return True

    async def _handle_cancel_confirmation(
        self, clinic: Clinic, patient: Patient, state: ConversationState, text: str
    ) -> None:
        raw_id = str(state.slot.get("appointment_id", ""))
        if text == f"cancel_keep:{raw_id}":
            await self._save_state(state, ConversationStep.IDLE, {})
            await self._reply_text(clinic, patient, "Your appointment is still booked.")
            return
        if text != f"cancel_confirm:{raw_id}":
            await self._start_handoff(clinic, patient, state, text)
            return
        appointment_id = UUID(raw_id)
        updated = await self._database.transition_appointment_status(
            appointment_id,
            patient.id,
            [AppointmentStatus.BOOKED, AppointmentStatus.CONFIRMED],
            AppointmentStatus.CANCELLED,
        )
        await self._save_state(state, ConversationStep.IDLE, {})
        if updated is None:
            await self._reply_text(clinic, patient, "That appointment can no longer be cancelled.")
            return
        await self._sync_cancelled_calendar(clinic, patient, updated)
        await self._reply_text(clinic, patient, "Your appointment has been cancelled.")
        if clinic.telegram_chat_id:
            summary = await self._database.get_booking_summary(appointment_id)
            if summary:
                await self._database.enqueue_outbox(
                    clinic.id,
                    "telegram",
                    clinic.telegram_chat_id,
                    {
                        "text": self._notifications.owner_status_change(
                            "Cancelled", clinic, patient, summary.service, updated
                        )
                    },
                )

    async def _complete_reschedule(
        self,
        clinic: Clinic,
        patient: Patient,
        state: ConversationState,
        service: Service,
        context: dict[str, Any],
    ) -> None:
        appointment_id = UUID(str(context["reschedule_from"]))
        starts_at = self._parse_utc(str(context["starts_at"]))
        ends_at = self._parse_utc(str(context["ends_at"]))
        try:
            appointment = await self._database.reschedule_appointment(
                appointment_id, patient.id, starts_at, ends_at
            )
        except BookingConflictError:
            await self._reply_text(
                clinic, patient, "That time was just taken. Please choose another time."
            )
            await self._offer_times(
                clinic, patient, state, date.fromisoformat(str(context["selected_date"]))
            )
            return
        if appointment is None:
            await self._save_state(state, ConversationStep.IDLE, {})
            await self._reply_text(clinic, patient, "That appointment can no longer be changed.")
            return
        await self._sync_rescheduled_calendar(clinic, patient, appointment)
        await self._save_state(state, ConversationStep.IDLE, {})
        when = self._notifications.friendly_datetime(clinic, appointment.starts_at)
        await self._reply_text(
            clinic,
            patient,
            f"Your appointment has been moved to {when}. Your reminder schedule has been updated.",
        )
        if clinic.telegram_chat_id:
            await self._database.enqueue_outbox(
                clinic.id,
                "telegram",
                clinic.telegram_chat_id,
                {
                    "text": self._notifications.owner_status_change(
                        "Rescheduled", clinic, patient, service, appointment
                    )
                },
            )

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

    async def _start_handoff(
        self,
        clinic: Clinic,
        patient: Patient,
        state: ConversationState,
        request: str,
        *,
        voluntary: bool = False,
    ) -> None:
        reference = patient.id.hex[:8].upper()
        context = {"handoff_ref": reference}
        await self._save_state(state, ConversationStep.HUMAN_HANDOFF, context)
        await self._reply_text(
            clinic,
            patient,
            (
                f"I've notified {clinic.name} reception. The automated assistant will "
                "pause here while they assist you."
                if voluntary
                else (
                    f"We cannot help you with that request. We are now handing you over to "
                    f"{clinic.name} reception, who will assist you shortly."
                )
            ),
            style=False,
        )
        if clinic.telegram_chat_id:
            await self._database.enqueue_outbox(
                clinic.id,
                "telegram",
                clinic.telegram_chat_id,
                {
                    "text": (
                        f"PATIENT HANDOFF - {reference}\n"
                        f"{minimal_patient_name(patient.name)} needs assistance.\n"
                        f"Request: {request or 'Chat with reception'}\n\n"
                        f"Reply: /reply {reference} your message\n"
                        f"Return to bot: /resume {reference}"
                    )
                },
            )

    async def _relay_handoff_message(
        self,
        clinic: Clinic,
        patient: Patient,
        state: ConversationState,
        text: str,
    ) -> None:
        if not clinic.telegram_chat_id:
            return
        reference = str(state.slot.get("handoff_ref", patient.id.hex[:8].upper()))
        await self._database.enqueue_outbox(
            clinic.id,
            "telegram",
            clinic.telegram_chat_id,
            {
                "text": (
                    f"HANDOFF {reference} - {minimal_patient_name(patient.name)}\n"
                    f"{text}\n\nReply: /reply {reference} your message"
                )
            },
        )

    async def _sync_rescheduled_calendar(
        self, clinic: Clinic, patient: Patient, appointment: Appointment
    ) -> None:
        try:
            event_id = await self._calendar.update_event(clinic, patient, appointment)
            if event_id is not None and event_id != appointment.google_event_id:
                await self._database.set_google_event_id(appointment.id, event_id)
        except Exception as exc:
            logger.warning("calendar_reschedule_deferred", exc_info=exc)
            await self._database.enqueue_job(
                clinic.id,
                "calendar_retry",
                self._clock.now() + timedelta(minutes=5),
                f"calendar-reschedule:{appointment.id}:{int(self._clock.now().timestamp())}",
                appointment_id=appointment.id,
                patient_id=patient.id,
                payload={"action": "update"},
            )

    async def _sync_cancelled_calendar(
        self, clinic: Clinic, patient: Patient, appointment: Appointment
    ) -> None:
        del patient
        if not appointment.google_event_id:
            return
        try:
            await self._calendar.delete_event(clinic, appointment)
            await self._database.set_google_event_id(appointment.id, None)
        except Exception as exc:
            logger.warning("calendar_cancel_deferred", exc_info=exc)
            await self._database.enqueue_job(
                clinic.id,
                "calendar_retry",
                self._clock.now() + timedelta(minutes=5),
                f"calendar-cancel:{appointment.id}:{int(self._clock.now().timestamp())}",
                appointment_id=appointment.id,
                patient_id=appointment.patient_id,
                payload={"action": "delete"},
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

    async def _reply_text(
        self,
        clinic: Clinic,
        patient: Patient,
        text: str,
        *,
        style: bool = False,
    ) -> None:
        if style:
            text = await self._intent.style(text, clinic.brand_voice)
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
        *,
        style: bool = False,
    ) -> None:
        if style:
            styled = await self._intent.style(body, clinic.brand_voice)
            if clinic.name.casefold() in styled.casefold():
                body = styled
        await self._whatsapp.send_buttons(clinic, patient.wa_number, body, buttons)
        await self._database.log_message(
            clinic.id,
            patient.id,
            "whatsapp",
            "outbound",
            body,
            {"buttons": [{"id": button.id, "title": button.title} for button in buttons]},
        )

    async def _reply_list(
        self,
        clinic: Clinic,
        patient: Patient,
        body: str,
        button_text: str,
        rows: list[ListRow],
        *,
        style: bool = False,
    ) -> None:
        if style:
            body = await self._intent.style(body, clinic.brand_voice)
        await self._whatsapp.send_list(
            clinic, patient.wa_number, body, button_text, rows
        )
        await self._database.log_message(
            clinic.id,
            patient.id,
            "whatsapp",
            "outbound",
            body,
            {
                "rows": [
                    {"id": row.id, "title": row.title, "description": row.description}
                    for row in rows
                ]
            },
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

    def _welcome_message(self, clinic: Clinic) -> str:
        local_hour = self._clock.now().astimezone(ZoneInfo(clinic.timezone)).hour
        if local_hour < 12:
            greeting = "Good morning"
        elif local_hour < 18:
            greeting = "Good afternoon"
        else:
            greeting = "Good evening"
        return (
            f"{greeting}! Welcome to {clinic.name}. I can help you book a visit or "
            "connect you with reception. What would you like to do?"
        )

    @staticmethod
    def _medical_aid_consent(clinic: Clinic) -> str:
        return (
            f"To confirm your booking at {clinic.name}, we need your name, surname, "
            "medical aid scheme/name, medical aid no + dependant code to secure your "
            "slot + check benefits. "
            "Info stays with our rooms only. Required to book. "
            "Privacy: bookabl.co.za/privacy\n\n"
            f"By replying YES, you consent to {clinic.name} collecting and processing "
            "this information for your booking and benefit check. You may withdraw "
            "your consent by contacting the clinic.\n\n"
            "Reply YES to continue"
        )

    @staticmethod
    def _cash_consent(clinic: Clinic) -> str:
        return (
            f"Cash booking at {clinic.name} - we just need your name + surname to hold "
            "your slot. Info stays with our rooms only.\n"
            "Privacy: bookabl.co.za/privacy\n\n"
            f"By replying with your name, you consent to {clinic.name} collecting and "
            "processing it for this booking. You may withdraw your consent by contacting "
            "the clinic.\n\n"
            "Reply with your name + surname e.g. Thandi Nkosi"
        )

    @staticmethod
    def _parse_medical_aid_details(text: str) -> tuple[str, str, str, str] | None:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) != 4:
            return None
        values: list[str] = []
        labels = (
            r"(?:name(?:\s*\+\s*surname)?|full\s+name)",
            r"(?:medical\s+aid\s+(?:scheme|name)|scheme)",
            r"(?:medical\s+aid(?:\s+(?:no|number))?|ma\s+(?:no|number))",
            r"(?:dependant|dependent)(?:\s+code)?",
        )
        for line, label in zip(lines, labels, strict=True):
            value = re.sub(r"^\s*\d+\s*[.)-]\s*", "", line)
            value = re.sub(rf"^\s*{label}\s*[:=-]\s*", "", value, flags=re.IGNORECASE)
            value = value.strip()
            if not value:
                return None
            values.append(value)
        patient_name = BookingFlow._parse_patient_name(values[0])
        if patient_name is None:
            return None
        scheme, member_number, dependent_code = values[1:]
        if not BookingFlow._valid_medical_aid_scheme(scheme):
            return None
        if not BookingFlow._valid_medical_identifier(member_number, min_length=4):
            return None
        if not BookingFlow._valid_medical_identifier(dependent_code, min_length=1):
            return None
        return patient_name, scheme, member_number, dependent_code

    @staticmethod
    def _valid_medical_aid_scheme(value: str) -> bool:
        lowered = value.casefold()
        return (
            2 <= len(value) <= 80
            and any(character.isalpha() for character in value)
            and "@" not in value
            and "http://" not in lowered
            and "https://" not in lowered
        )

    @staticmethod
    def _valid_medical_identifier(value: str, *, min_length: int) -> bool:
        return (
            min_length <= len(value) <= 40
            and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._/-]*", value))
            and any(character.isdigit() for character in value)
        )

    @classmethod
    def _medical_aid_details_from_context(
        cls, context: dict[str, Any]
    ) -> tuple[str, str, str, str] | None:
        values = [
            str(context.get("patient_full_name", "")),
            str(context.get("medical_aid_name", "")),
            str(context.get("medical_aid_number", "")),
            str(context.get("dependent_code", "")),
        ]
        return cls._parse_medical_aid_details("\n".join(values))

    @staticmethod
    def _without_medical_aid_details(context: dict[str, Any]) -> dict[str, Any]:
        cleaned = dict(context)
        for key in (
            "patient_full_name",
            "medical_aid_name",
            "medical_aid_number",
            "dependent_code",
        ):
            cleaned.pop(key, None)
        return cleaned

    @staticmethod
    def _medical_aid_confirmation(context: dict[str, Any]) -> str:
        member_number = str(context.get("medical_aid_number", ""))
        masked_number = (
            f"ending {member_number[-4:]}" if len(member_number) > 4 else "provided"
        )
        return (
            "Let's check those details before I book:\n\n"
            f"Name: {context.get('patient_full_name', '')}\n"
            f"Medical aid: {context.get('medical_aid_name', '')}\n"
            f"Medical aid number: {masked_number}\n"
            f"Dependant code: {context.get('dependent_code', '')}\n\n"
            "Is everything correct?"
        )

    def _parse_local_date(self, value: str, clinic: Clinic) -> date | None:
        normalized = value.casefold().strip()
        local_today = self._clock.now().astimezone(ZoneInfo(clinic.timezone)).date()
        relative_day = re.search(r"\b(today|tomorrow)\b", normalized)
        if relative_day and relative_day.group(1) == "today":
            parsed = local_today
        elif relative_day and relative_day.group(1) == "tomorrow":
            parsed = local_today + timedelta(days=1)
        else:
            parsed = None
            numeric_date = re.search(
                r"(?<!\d)(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|"
                r"\d{4}-\d{1,2}-\d{1,2})(?!\d)",
                normalized,
            )
            date_text = numeric_date.group(0) if numeric_date else normalized
            for pattern in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y"):
                try:
                    parsed = datetime.strptime(date_text, pattern).date()
                    break
                except ValueError:
                    continue
            if parsed is None:
                month_name = (
                    "january|february|march|april|may|june|july|august|"
                    "september|october|november|december|jan|feb|mar|apr|jun|"
                    "jul|aug|sep|sept|oct|nov|dec"
                )
                named_date = re.search(
                    rf"\b(?:\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{month_name})|"
                    rf"(?:{month_name})\s+\d{{1,2}}(?:st|nd|rd|th)?)\b",
                    normalized,
                )
                named_text = named_date.group(0) if named_date else normalized
                named_text = re.sub(r"(?<=\d)(?:st|nd|rd|th)\b", "", named_text)
                for pattern in ("%d %B", "%d %b", "%B %d", "%b %d"):
                    try:
                        parsed = datetime.strptime(named_text, pattern).date().replace(
                            year=local_today.year
                        )
                        if parsed < local_today:
                            parsed = parsed.replace(year=local_today.year + 1)
                        break
                    except ValueError:
                        continue
            weekday_names = {
                name: index
                for index, name in enumerate(
                    (
                        "monday",
                        "tuesday",
                        "wednesday",
                        "thursday",
                        "friday",
                        "saturday",
                        "sunday",
                    )
                )
            }
            weekday_match = re.search(
                r"\b(next\s+)?(monday|tuesday|wednesday|thursday|friday|"
                r"saturday|sunday)\b",
                normalized,
            )
            if parsed is None and weekday_match:
                weekday_text = weekday_match.group(2)
                delta = (weekday_names[weekday_text] - local_today.weekday()) % 7
                if weekday_match.group(1) and delta == 0:
                    delta = 7
                parsed = local_today + timedelta(days=delta)
        if parsed is None or parsed < local_today or parsed > local_today + timedelta(days=30):
            return None
        return parsed

    @staticmethod
    def _parse_patient_name(value: str) -> str | None:
        normalized = " ".join(value.strip().split())
        if not 3 <= len(normalized) <= 100:
            return None
        lowered = normalized.casefold()
        if "@" in normalized or "http://" in lowered or "https://" in lowered:
            return None
        parts = normalized.split()
        if len(parts) < 2:
            return None
        allowed_punctuation = {"'", "-", "."}
        if any(
            not all(character.isalpha() or character in allowed_punctuation for character in part)
            or not any(character.isalpha() for character in part)
            for part in parts
        ):
            return None
        return normalized

    @staticmethod
    def _is_booking_request(value: str) -> bool:
        normalized = value.casefold().strip()
        return bool(
            re.search(r"\b(?:book|booking)\b", normalized)
            and re.search(r"\b(?:appointment|visit|slot)\b", normalized)
        ) or normalized in {"book", "appointment"}

    @staticmethod
    def _parse_local_time(value: str) -> time | None:
        normalized = value.casefold().strip().replace(".", "")
        normalized = re.sub(r"(?<=\d)h(?=\d)", ":", normalized)
        for pattern in ("%H:%M", "%H%M", "%I:%M %p", "%I %p"):
            try:
                return datetime.strptime(normalized, pattern).time()
            except ValueError:
                continue
        return None
