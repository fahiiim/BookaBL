"""Authorized Telegram owner command handling."""

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.adapters.telegram import TelegramSender
from app.core.clock import Clock
from app.db.protocol import Database
from app.domain.models import AppointmentStatus
from app.services.privacy import minimal_patient_name, short_date
from app.services.slot_engine import local_date_bounds

TODAY_COMMANDS = {"today's bookings", "todays bookings", "today bookings", "/bookings", "/today"}
WEEKLY_COMMANDS = {"weekly bookings", "/weekly", "/weekly_bookings"}
MONTHLY_COMMANDS = {"monthly bookings", "/monthly", "/monthly_bookings"}
TELEGRAM_MESSAGE_LIMIT = 4000


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
        text = str(message.get("text", "")).casefold().strip()
        command = text.split("@", maxsplit=1)[0]
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
