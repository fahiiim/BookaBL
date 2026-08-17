from datetime import UTC, datetime, time
from uuid import UUID

import pytest
from app.adapters.transcriber import FakeTranscriber
from app.adapters.whatsapp import FakeWhatsApp
from app.core.clock import FrozenClock
from app.db.memory import InMemoryDatabase
from app.domain.messages import IncomingMessage
from app.domain.models import Clinic
from app.workers.event_processor import EventProcessor

NOW = datetime(2026, 8, 17, 8, tzinfo=UTC)
CLINIC_ID = UUID("00000000-0000-4000-8000-000000000001")


class CaptureHandler:
    def __init__(self) -> None:
        self.messages: list[IncomingMessage] = []

    async def handle(self, clinic: Clinic, message: IncomingMessage) -> None:
        assert clinic.id == CLINIC_ID
        self.messages.append(message)


@pytest.mark.asyncio
async def test_audio_event_downloads_and_transcribes_before_dispatch() -> None:
    database = InMemoryDatabase(FrozenClock(NOW))
    clinic = Clinic(
        id=CLINIC_ID,
        name="Test Dental",
        trial_started_at=NOW,
        wa_phone_id="phone-1",
        work_start=time(8),
        work_end=time(17),
        created_at=NOW,
    )
    database.add_clinic(clinic)
    await database.persist_webhook_event(
        "wamid.audio",
        CLINIC_ID,
        {
            "contacts": [{"profile": {"name": "John"}}],
            "message": {
                "id": "wamid.audio",
                "from": "27820000000",
                "type": "audio",
                "audio": {"id": "media-1"},
            },
        },
    )
    whatsapp = FakeWhatsApp()
    whatsapp.media["media-1"] = (b"ogg-data", "audio/ogg")
    transcriber = FakeTranscriber("book appointment")
    handler = CaptureHandler()
    processor = EventProcessor(database, whatsapp, transcriber, handler)

    assert await processor.run_once() == 1

    message = handler.messages[0]
    assert message.text == "book appointment"
    assert transcriber.calls == [(b"ogg-data", "media-1.ogg", "audio/ogg")]
    assert database.events["wamid.audio"].processed_at == NOW
