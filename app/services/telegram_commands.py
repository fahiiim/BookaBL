"""Authorized Telegram owner command handling."""

from typing import Any
from zoneinfo import ZoneInfo

from app.adapters.telegram import TelegramSender
from app.core.clock import Clock
from app.db.protocol import Database
from app.services.slot_engine import local_date_bounds

EN_DASH = "\N{EN DASH}"


class TelegramCommandService:
    """Reply to clinic-owner booking-list commands in tenant-local time."""

    def __init__(self, database: Database, telegram: TelegramSender, clock: Clock) -> None:
        self._database = database
        self._telegram = telegram
        self._clock = clock

    async def handle(self, payload: dict[str, Any]) -> bool:
        """Handle an authorized `today's bookings` or `/bookings` update."""

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
        if command not in {"today's bookings", "todays bookings", "/bookings"}:
            return False
        local_day = self._clock.now().astimezone(ZoneInfo(clinic.timezone)).date()
        starts_at, ends_at = local_date_bounds(local_day, clinic.timezone)
        bookings = await self._database.list_booking_summaries(
            clinic.id, starts_at, ends_at
        )
        if bookings:
            timezone = ZoneInfo(clinic.timezone)
            lines = ["Today's bookings:"]
            lines.extend(
                (
                    f"{item.appointment.starts_at.astimezone(timezone):%H:%M} {EN_DASH} "
                    f"{item.patient.name} {EN_DASH} {item.service.name} {EN_DASH} "
                    f"{item.appointment.status.value}"
                )
                for item in bookings
            )
            reply = "\n".join(lines)
        else:
            reply = "No bookings today."
        await self._telegram.send_message(chat_id, reply)
        await self._database.log_message(
            clinic.id, None, "telegram", "outbound", reply, {}
        )
        return True
