"""Authorized Telegram owner command handling."""

from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from app.adapters.telegram import TelegramSender
from app.core.clock import Clock
from app.db.protocol import Database
from app.domain.models import AppointmentStatus, ConversationStep
from app.services.privacy import minimal_patient_name, short_date
from app.services.slot_engine import local_date_bounds

TODAY_COMMANDS = {"today's bookings", "todays bookings", "today bookings", "/bookings", "/today"}
WEEKLY_COMMANDS = {"weekly bookings", "/weekly", "/weekly_bookings"}
MONTHLY_COMMANDS = {"monthly bookings", "/monthly", "/monthly_bookings"}
TELEGRAM_MESSAGE_LIMIT = 4000
COMMANDS_HELP = """BOOKABL TELEGRAM COMMANDS

/commands - Show this command guide.
/today or /bookings - Show today's appointments.
/weekly - Show appointments from the last 7 days.
/monthly - Show appointments from the last 30 days.
/reply REFERENCE message - Reply to a patient during an active receptionist handoff.
/resume REFERENCE - Return an active handoff to the automated booking assistant.
/noshow APPOINTMENT_ID - Mark a booked or confirmed appointment as a no-show.

The REFERENCE is shown in PATIENT HANDOFF alerts.
The APPOINTMENT_ID is shown in ATTENDANCE CHECK alerts."""


class TelegramCommandService:
    """Reply to clinic-owner booking-list commands in tenant-local time."""

    def __init__(self, database: Database, telegram: TelegramSender, clock: Clock) -> None:
        self._database = database
        self._telegram = telegram
        self._clock = clock

    async def handle(self, payload: dict[str, Any]) -> bool:
        """Handle authorized daily, seven-day, or thirty-day booking commands."""

        message = payload.get("message")
        if not isinstance(message, dict):
            return False
        chat = message.get("chat")
        if not isinstance(chat, dict) or "id" not in chat:
            return False
        chat_id = str(chat["id"])
        clinic = await self._database.get_clinic_by_telegram_chat_id(chat_id)
        if clinic is None:
            return False
        raw_text = str(message.get("text", "")).strip()
        command = raw_text.casefold().split("@", maxsplit=1)[0]
        await self._database.log_message(
            clinic.id, None, "telegram", "inbound", str(message.get("text", "")), payload
        )
        if command == "/commands":
            await self._send(chat_id, clinic.id, COMMANDS_HELP)
            return True
        if command.startswith("/reply "):
            return await self._reply_to_patient(clinic.id, chat_id, raw_text)
        if command.startswith("/resume "):
            return await self._resume_bot(clinic.id, chat_id, command)
        if command.startswith("/noshow "):
            return await self._mark_no_show(clinic.id, chat_id, command)
        period = self._command_period(command)
        if period is None:
            return False

        local_day = self._clock.now().astimezone(ZoneInfo(clinic.timezone)).date()
        start_day = local_day - timedelta(days=period - 1)
        starts_at = local_date_bounds(start_day, clinic.timezone)[0]
        ends_at = local_date_bounds(local_day, clinic.timezone)[1]
        bookings = await self._database.list_booking_summaries(
            clinic.id, starts_at, ends_at
        )
        bookings = [
            item
            for item in bookings
            if item.appointment.status
            not in {AppointmentStatus.CANCELLED, AppointmentStatus.NO_SHOW}
        ]
        if bookings:
            timezone = ZoneInfo(clinic.timezone)
            lines = [self._header(period, local_day, clinic.name)]
            lines.extend(
                self._booking_line(
                    item.appointment.starts_at.astimezone(timezone),
                    item.patient.name,
                    include_date=period > 1,
                )
                for item in bookings
            )
            replies = self._chunks(lines)
        else:
            label = "today" if period == 1 else f"in the last {period} days"
            replies = [f"No bookings {label}."]

        for reply in replies:
            await self._telegram.send_message(chat_id, reply)
            await self._database.log_message(
                clinic.id, None, "telegram", "outbound", reply, {}
            )
        return True

    async def _reply_to_patient(
        self, clinic_id: UUID, chat_id: str, command: str
    ) -> bool:
        parts = command.split(maxsplit=2)
        if len(parts) != 3 or not parts[2].strip():
            await self._send(chat_id, clinic_id, "Use: /reply REFERENCE your message")
            return True
        state = await self._database.get_handoff_state(clinic_id, parts[1])
        if state is None:
            await self._send(chat_id, clinic_id, "That handoff is not active for this clinic.")
            return True
        patient = await self._database.get_patient(state.patient_id)
        if patient is None:
            await self._send(chat_id, clinic_id, "The patient could not be found.")
            return True
        reply = parts[2].strip()
        await self._database.enqueue_outbox(
            clinic_id,
            "whatsapp",
            patient.wa_number,
            {"kind": "text", "text": reply},
        )
        await self._database.log_message(
            clinic_id, patient.id, "telegram", "outbound", reply, {"handoff": parts[1]}
        )
        await self._send(chat_id, clinic_id, f"Reply sent for handoff {parts[1].upper()}.")
        return True

    async def _resume_bot(self, clinic_id: UUID, chat_id: str, command: str) -> bool:
        parts = command.split(maxsplit=1)
        if len(parts) != 2:
            await self._send(chat_id, clinic_id, "Use: /resume REFERENCE")
            return True
        state = await self._database.get_handoff_state(clinic_id, parts[1])
        if state is None:
            await self._send(chat_id, clinic_id, "That handoff is not active for this clinic.")
            return True
        patient = await self._database.get_patient(state.patient_id)
        if patient is None:
            await self._send(chat_id, clinic_id, "The patient could not be found.")
            return True
        await self._database.save_conversation_state(
            state.model_copy(
                update={
                    "state": ConversationStep.AWAIT_ENTRY_CHOICE,
                    "slot": {},
                    "updated_at": self._clock.now(),
                }
            )
        )
        await self._database.enqueue_outbox(
            clinic_id,
            "whatsapp",
            patient.wa_number,
            {
                "kind": "buttons",
                "body": "The automated booking assistant is available again. How can I help?",
                "buttons": [
                    {"id": "start:book", "title": "Book appointment"},
                    {"id": "start:human", "title": "Chat to receptionist"},
                ],
            },
        )
        await self._send(
            chat_id,
            clinic_id,
            f"Automation resumed for {parts[1].upper()}. "
            "The booking menu is being sent to the patient.",
        )
        return True

    async def _mark_no_show(self, clinic_id: UUID, chat_id: str, command: str) -> bool:
        parts = command.split(maxsplit=1)
        try:
            appointment_id = UUID(parts[1])
        except (IndexError, ValueError):
            await self._send(chat_id, clinic_id, "Use: /noshow APPOINTMENT_ID")
            return True
        summary = await self._database.get_booking_summary(appointment_id)
        if summary is None or summary.appointment.clinic_id != clinic_id:
            await self._send(chat_id, clinic_id, "That appointment is not part of this clinic.")
            return True
        updated = await self._database.mark_no_show(appointment_id)
        if updated is None:
            await self._send(
                chat_id,
                clinic_id,
                "That appointment can no longer be marked no-show.",
            )
            return True
        await self._send(
            chat_id,
            clinic_id,
            f"No-show recorded for {minimal_patient_name(summary.patient.name)}.",
        )
        return True

    async def _send(self, chat_id: str, clinic_id: UUID, text: str) -> None:
        await self._telegram.send_message(chat_id, text)
        await self._database.log_message(
            clinic_id, None, "telegram", "outbound", text, {}
        )

    @staticmethod
    def _command_period(command: str) -> int | None:
        if command in TODAY_COMMANDS:
            return 1
        if command in WEEKLY_COMMANDS:
            return 7
        if command in MONTHLY_COMMANDS:
            return 30
        return None

    @staticmethod
    def _header(period: int, local_day: date, clinic_name: str) -> str:
        if period == 1:
            return f"TODAY {short_date(local_day)} - {clinic_name} bookings"
        return f"LAST {period} DAYS - {clinic_name} bookings"

    @staticmethod
    def _booking_line(starts_at: datetime, patient_name: str, *, include_date: bool) -> str:
        prefix = (
            f"{short_date(starts_at)} {starts_at:%H:%M}"
            if include_date
            else starts_at.strftime("%H:%M")
        )
        return f"{prefix} - {minimal_patient_name(patient_name)}"

    @staticmethod
    def _chunks(lines: list[str]) -> list[str]:
        chunks: list[str] = []
        current = lines[0]
        for line in lines[1:]:
            candidate = f"{current}\n{line}"
            if len(candidate) <= TELEGRAM_MESSAGE_LIMIT:
                current = candidate
            else:
                chunks.append(current)
                current = line
        chunks.append(current)
        return chunks
