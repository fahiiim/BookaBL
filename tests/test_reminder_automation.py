from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import cast
from uuid import UUID

import pytest
from app.adapters.calendar import FakeCalendar
from app.core.clock import FrozenClock
from app.db.memory import InMemoryDatabase
from app.domain.models import (
    AppointmentStatus,
    Clinic,
    FinalizeBookingCommand,
    JobStatus,
    Service,
)
from app.services.notifications import NotificationFormatter
from app.workers.scheduler import Scheduler

NOW = datetime(2026, 9, 24, 8, tzinfo=UTC)
CLINIC_ID = UUID("00000000-0000-4000-8000-000000000001")
SERVICE_ID = UUID("00000000-0000-4000-8000-000000000101")


@pytest.mark.asyncio
async def test_stale_reminder_is_skipped_and_future_reminder_is_labelled() -> None:
    clock = FrozenClock(NOW)
    database = InMemoryDatabase(clock)
    clinic = Clinic(
        id=CLINIC_ID,
        name="Test Dental",
        trial_started_at=NOW,
        wa_phone_id="phone-1",
        timezone="UTC",
        work_start=time(8),
        work_end=time(17),
        reminder_offsets_h=[24, 2],
        created_at=NOW,
    )
    service = Service(
        id=SERVICE_ID,
        clinic_id=CLINIC_ID,
        name="Consultation",
        duration_min=30,
        price=Decimal("650"),
    )
    database.add_clinic(clinic)
    database.add_service(service)
    patient = await database.get_or_create_patient(
        CLINIC_ID, "27820000000", "Thandi Nkosi"
    )
    starts_at = NOW + timedelta(hours=4)
    appointment = await database.finalize_booking(
        FinalizeBookingCommand(
            clinic_id=CLINIC_ID,
            patient_id=patient.id,
            service_id=SERVICE_ID,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(minutes=30),
            whatsapp_to=patient.wa_number,
            whatsapp_payload={"kind": "text", "text": "Booked"},
        )
    )
    database.outbox.clear()
    scheduler = Scheduler(
        database, FakeCalendar(), NotificationFormatter(clock), clock
    )

    await scheduler.run_once()

    stale = next(
        job
        for job in database.jobs.values()
        if job.job_type == "reminder" and job.due_at < NOW
    )
    assert stale.status is JobStatus.COMPLETED
    assert database.outbox == {}

    await database.transition_appointment_status(
        appointment.id,
        patient.id,
        [AppointmentStatus.BOOKED],
        AppointmentStatus.CONFIRMED,
    )
    future = next(
        job
        for job in database.jobs.values()
        if job.job_type == "reminder" and job.due_at > NOW
    )
    clock.instant = future.due_at
    await scheduler.run_once()

    reminder = next(iter(database.outbox.values()))
    assert reminder.payload["body"] == (
        "2-hour reminder: your Consultation appointment is Thu 24 Sep at 12:00."
    )
    buttons = cast(list[dict[str, str]], reminder.payload["buttons"])
    assert [button["title"] for button in buttons] == ["Reschedule", "Cancel"]
