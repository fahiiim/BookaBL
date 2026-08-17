"""Durable WhatsApp and Telegram notification delivery."""

import asyncio
import logging
from contextlib import suppress
from datetime import timedelta
from typing import Any

from app.adapters.telegram import TelegramSender
from app.adapters.whatsapp import ReplyButton, WhatsAppSender
from app.core.clock import Clock
from app.db.protocol import Database
from app.domain.models import Clinic, NotificationOutbox

logger = logging.getLogger(__name__)

BACKOFF_SECONDS = (30, 120, 600, 3600)


class OutboxWorker:
    """Deliver outbox items with bounded exponential retry and DLQ alerts."""

    def __init__(
        self,
        database: Database,
        whatsapp: WhatsAppSender,
        telegram: TelegramSender,
        clock: Clock,
        *,
        batch_size: int = 20,
        poll_seconds: float = 1,
    ) -> None:
        self._database = database
        self._whatsapp = whatsapp
        self._telegram = telegram
        self._clock = clock
        self._batch_size = batch_size
        self._poll_seconds = poll_seconds

    async def run_once(self) -> int:
        """Attempt one outbox batch and return the number of claimed items."""

        items = await self._database.pop_pending_outbox(self._batch_size)
        for item in items:
            clinic = await self._database.get_clinic(item.clinic_id)
            if clinic is None:
                await self._database.retry_outbox(
                    item.id,
                    self._clock.now(),
                    "Clinic no longer exists",
                    failed=True,
                )
                continue
            try:
                await self._deliver(clinic, item)
                await self._database.mark_outbox_sent(item.id)
                await self._database.log_message(
                    clinic.id,
                    None,
                    item.channel,
                    "outbound",
                    self._body(item),
                    item.payload,
                )
            except Exception as exc:
                logger.exception("outbox_delivery_failed")
                permanently_failed = item.attempts > 4
                delay = (
                    timedelta(0)
                    if permanently_failed
                    else timedelta(seconds=BACKOFF_SECONDS[item.attempts - 1])
                )
                await self._database.retry_outbox(
                    item.id,
                    self._clock.now() + delay,
                    str(exc),
                    failed=permanently_failed,
                )
                if permanently_failed:
                    await self._alert_dlq(clinic, item, exc)
        return len(items)

    async def _deliver(self, clinic: Clinic, item: NotificationOutbox) -> None:
        payload = item.payload
        if item.channel == "telegram":
            await self._telegram.send_message(item.to_id, str(payload["text"]))
            return
        if item.channel != "whatsapp":
            raise ValueError(f"Unsupported outbox channel: {item.channel}")
        kind = str(payload.get("kind", "text"))
        if kind == "text":
            await self._whatsapp.send_text(clinic, item.to_id, str(payload["text"]))
        elif kind == "buttons":
            buttons = self._buttons(payload.get("buttons"))
            await self._whatsapp.send_buttons(
                clinic, item.to_id, str(payload["body"]), buttons
            )
        elif kind == "template":
            raw_payloads = payload.get("button_payloads", [])
            button_payloads = (
                [str(value) for value in raw_payloads]
                if isinstance(raw_payloads, list)
                else []
            )
            await self._whatsapp.send_template(
                clinic,
                item.to_id,
                str(payload["template_name"]),
                button_payloads,
                language_code=str(payload.get("language_code", "en")),
            )
        else:
            raise ValueError(f"Unsupported WhatsApp outbox kind: {kind}")

    async def _alert_dlq(
        self, clinic: Clinic, item: NotificationOutbox, error: Exception
    ) -> None:
        if not clinic.telegram_chat_id:
            return
        with suppress(Exception):
            await self._telegram.send_message(
                clinic.telegram_chat_id,
                (
                    f"BOOKABL DLQ alert: {item.channel} message {item.id} failed "
                    f"after {item.attempts} attempts. Error: {error}"
                ),
            )

    @staticmethod
    def _buttons(value: Any) -> list[ReplyButton]:
        if not isinstance(value, list):
            raise ValueError("Outbox buttons must be a list")
        buttons: list[ReplyButton] = []
        for raw in value:
            if not isinstance(raw, dict):
                raise ValueError("Outbox button must be an object")
            buttons.append(ReplyButton(id=str(raw["id"]), title=str(raw["title"])))
        return buttons

    @staticmethod
    def _body(item: NotificationOutbox) -> str:
        for key in ("text", "body", "template_name"):
            if key in item.payload:
                return str(item.payload[key])
        return item.channel

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Poll until ``stop`` is set."""

        while not stop.is_set():
            processed = await self.run_once()
            if processed == 0:
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self._poll_seconds)
