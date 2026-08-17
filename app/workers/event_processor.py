"""Asynchronous processing of persist-first WhatsApp webhook events."""

import asyncio
import logging
from contextlib import suppress
from typing import Protocol

from app.adapters.transcriber import Transcriber
from app.adapters.whatsapp import WhatsAppSender
from app.core.exceptions import ClinicNotFoundError
from app.core.logging import bind_log_context, reset_log_context
from app.db.protocol import Database
from app.domain.messages import IncomingMessage, MessageKind
from app.domain.models import Clinic, WebhookEvent
from app.services.message_normalizer import normalize_whatsapp_event

logger = logging.getLogger(__name__)


class IncomingMessageHandler(Protocol):
    """Application flow capable of handling one normalized patient message."""

    async def handle(self, clinic: Clinic, message: IncomingMessage) -> None:
        """Apply the deterministic patient conversation flow."""


class EventProcessor:
    """Claim persisted webhook events and dispatch them to the booking flow."""

    def __init__(
        self,
        database: Database,
        whatsapp: WhatsAppSender,
        transcriber: Transcriber,
        handler: IncomingMessageHandler,
        *,
        batch_size: int = 20,
        poll_seconds: float = 1,
    ) -> None:
        self._database = database
        self._whatsapp = whatsapp
        self._transcriber = transcriber
        self._handler = handler
        self._batch_size = batch_size
        self._poll_seconds = poll_seconds

    async def run_once(self) -> int:
        """Process one claimed batch and return the number of successful events."""

        events = await self._database.pop_unprocessed_events(self._batch_size)
        succeeded = 0
        for event in events:
            tokens = bind_log_context(
                clinic_id=str(event.clinic_id), message_id=event.message_id
            )
            try:
                await self.process_event(event)
                await self._database.mark_event_processed(event.message_id)
                succeeded += 1
            except Exception as exc:
                logger.exception("webhook_event_processing_failed")
                await self._database.release_event(event.message_id, str(exc))
            finally:
                reset_log_context(tokens)
        return succeeded

    async def process_event(self, event: WebhookEvent) -> None:
        """Normalize and dispatch one claimed event."""

        clinic = await self._database.get_clinic(event.clinic_id)
        if clinic is None:
            raise ClinicNotFoundError(str(event.clinic_id))
        message = normalize_whatsapp_event(event.payload)
        if message.kind is MessageKind.AUDIO:
            if message.audio_media_id is None:
                raise ValueError("Normalized audio message has no media id")
            audio, content_type = await self._whatsapp.download_media(
                clinic, message.audio_media_id
            )
            extension = content_type.partition("/")[2].partition(";")[0] or "ogg"
            transcript = await self._transcriber.transcribe(
                audio, f"{message.audio_media_id}.{extension}", content_type
            )
            message = message.model_copy(
                update={"text": transcript, "display_text": transcript}
            )
        await self._handler.handle(clinic, message)

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Poll until ``stop`` is set."""

        while not stop.is_set():
            processed = await self.run_once()
            if processed == 0:
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self._poll_seconds)
