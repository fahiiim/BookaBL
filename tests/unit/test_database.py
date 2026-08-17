from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from app.core.clock import FrozenClock
from app.core.exceptions import BookingConflictError
from app.db.memory import InMemoryDatabase
from app.domain.models import Clinic, FinalizeBookingCommand, Service

NOW = datetime(2026, 8, 17, 8, tzinfo=UTC)
CLINIC_ID = UUID("00000000-0000-4000-8000-000000000001")
SERVICE_ID = UUID("00000000-0000-4000-8000-000000000101")


def clinic() -> Clinic:
    return Clinic(
        id=CLINIC_ID,
        name="Test Dental",
        trial_started_at=NOW,
        wa_phone_id="phone-1",
        telegram_chat_id="chat-1",
        work_start=time(8),
        work_end=time(17),
        created_at=NOW,
    )


@pytest.mark.asyncio
async def test_finalize_booking_is_atomic_and_rejects_conflict() -> None:
    db = InMemoryDatabase(FrozenClock(NOW))
    db.add_clinic(clinic())
    db.add_service(
        Service(
            id=SERVICE_ID,
            clinic_id=CLINIC_ID,
            name="Cleaning",
            duration_min=30,
            price=Decimal("850"),
        )
    )
    patient = await db.get_or_create_patient(CLINIC_ID, "27820000000", "John")
    starts_at = NOW + timedelta(days=1)
    command = FinalizeBookingCommand(
        clinic_id=CLINIC_ID,
        patient_id=patient.id,
        service_id=SERVICE_ID,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(minutes=30),
        whatsapp_to=patient.wa_number,
        whatsapp_payload={"kind": "text", "text": "Confirmed"},
        telegram_to="chat-1",
        telegram_payload={"text": "New booking"},
    )

    appointment = await db.finalize_booking(command)

    assert appointment.price == Decimal("850")
    assert len(db.jobs) == 3
    assert len(db.outbox) == 2
    with pytest.raises(BookingConflictError):
        await db.finalize_booking(command)
    assert len(db.appointments) == 1
    assert len(db.jobs) == 3
    assert len(db.outbox) == 2


@pytest.mark.asyncio
async def test_webhook_event_deduplication_and_leasing() -> None:
    db = InMemoryDatabase(FrozenClock(NOW))

    assert await db.persist_webhook_event("wamid.1", CLINIC_ID, {"message": "one"})
    assert not await db.persist_webhook_event("wamid.1", CLINIC_ID, {"message": "duplicate"})
    assert [event.message_id for event in await db.pop_unprocessed_events(10)] == ["wamid.1"]
    assert await db.pop_unprocessed_events(10) == []

    await db.mark_event_processed("wamid.1")
    assert db.events["wamid.1"].processed_at == NOW

