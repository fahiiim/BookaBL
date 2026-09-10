from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from app.adapters.calendar import FakeCalendar
from app.core.clock import FrozenClock
from app.db.memory import InMemoryDatabase
from app.domain.models import (
    AppointmentStatus,
    AutomationJob,
    Clinic,
    FinalizeBookingCommand,
    JobStatus,
    Service,
)
from app.services.notifications import NotificationFormatter
from app.workers.scheduler import Scheduler

NOW = datetime(2026, 8, 17, 8, tzinfo=UTC)
CLINIC_ID = UUID("00000000-0000-4000-8000-000000000001")
SERVICE_ID = UUID("00000000-0000-4000-8000-000000000101")


async def _book(
    review_url: str | None = "https://g.page/r/test/review"
) -> tuple[InMemoryDatabase, FrozenClock, UUID, UUID]:
    clock = FrozenClock(NOW)
    database = InMemoryDatabase(clock)
    clinic = Clinic(
        id=CLINIC_ID,
        name="Test Dental",
        trial_started_at=NOW,
        wa_phone_id="phone-1",
        google_review_url=review_url,
        work_start=time(8),
        work_end=time(17),
        created_at=NOW,
    )
    database.add_clinic(clinic)
    database.add_service(
        Service(
            id=SERVICE_ID,
            clinic_id=CLINIC_ID,
            name="Cleaning",
            duration_min=30,
            price=Decimal("850"),
        )
    )
    patient = await database.get_or_create_patient(CLINIC_ID, "27820000000", "Thandi Nkosi")
    starts_at = NOW + timedelta(hours=1)
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
    return database, clock, patient.id, appointment.id


def _review_job(database: InMemoryDatabase) -> AutomationJob:
    return next(job for job in database.jobs.values() if job.job_type == "review_request")


@pytest.mark.asyncio
async def test_finalize_booking_creates_review_job_two_hours_after_end() -> None:
    database, _clock, _patient_id, appointment_id = await _book()
    appointment = database.appointments[appointment_id]
    job = _review_job(database)

    assert job.appointment_id == appointment.id
    assert job.due_at == appointment.ends_at + timedelta(hours=2)
    assert job.dedupe_key == f"review:{appointment.id}"


@pytest.mark.asyncio
async def test_review_worker_skips_cancelled_appointment() -> None:
    database, clock, patient_id, appointment_id = await _book()
    await database.transition_appointment_status(
        appointment_id,
        patient_id,
        [AppointmentStatus.BOOKED],
        AppointmentStatus.CANCELLED,
    )
    database.outbox.clear()
    clock.instant = _review_job(database).due_at

    await Scheduler(
        database, FakeCalendar(), NotificationFormatter(clock), clock
    ).run_once()

    assert database.outbox == {}
    assert _review_job(database).status is JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_review_worker_skips_clinic_without_review_url() -> None:
    database, clock, patient_id, appointment_id = await _book(None)
    await database.transition_appointment_status(
        appointment_id,
        patient_id,
        [AppointmentStatus.BOOKED],
        AppointmentStatus.CONFIRMED,
    )
    database.outbox.clear()
    clock.instant = _review_job(database).due_at

    await Scheduler(
        database, FakeCalendar(), NotificationFormatter(clock), clock
    ).run_once()

    assert database.outbox == {}
    assert _review_job(database).status is JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_review_worker_enqueues_exact_whatsapp_message() -> None:
    database, clock, patient_id, appointment_id = await _book()
    await database.transition_appointment_status(
        appointment_id,
        patient_id,
        [AppointmentStatus.BOOKED],
        AppointmentStatus.CONFIRMED,
    )
    database.outbox.clear()
    clock.instant = _review_job(database).due_at

    await Scheduler(
        database, FakeCalendar(), NotificationFormatter(clock), clock
    ).run_once()

    messages = list(database.outbox.values())
    assert len(messages) == 1
    assert messages[0].channel == "whatsapp"
    assert messages[0].to_id == "27820000000"
    assert messages[0].payload == {
        "kind": "text",
        "text": (
            "Thanks for visiting Test Dental, Thandi Nkosi! We'd love your feedback. "
            "Please leave us a Google review here: https://g.page/r/test/review"
        ),
    }
    assert _review_job(database).status is JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_review_job_dedupe_key_is_idempotent() -> None:
    database, _clock, _patient_id, appointment_id = await _book()
    job = _review_job(database)

    await database.enqueue_job(
        CLINIC_ID,
        "review_request",
        job.due_at,
        job.dedupe_key,
        appointment_id=appointment_id,
    )

    assert sum(item.dedupe_key == job.dedupe_key for item in database.jobs.values()) == 1
