from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from app.adapters.calendar import FakeCalendar
from app.adapters.intent import FakeIntent
from app.adapters.whatsapp import FakeWhatsApp
from app.core.clock import FrozenClock
from app.db.memory import InMemoryDatabase
from app.domain.messages import IncomingMessage, MessageKind
from app.domain.models import (
    AppointmentStatus,
    Clinic,
    ClinicStatus,
    ConversationState,
    ConversationStep,
    Service,
)
from app.flows.booking import BookingFlow
from app.services.notifications import NotificationFormatter
from app.services.slot_engine import SlotEngine
from app.services.trial_gate import TrialGate
from app.workers.scheduler import Scheduler

NOW = datetime(2026, 8, 17, 8, tzinfo=UTC)
CLINIC_ID = UUID("00000000-0000-4000-8000-000000000001")
SERVICE_ID = UUID("00000000-0000-4000-8000-000000000101")


@pytest.mark.asyncio
async def test_calendar_outage_does_not_rollback_booking_and_no_show_job_tags() -> None:
    clock = FrozenClock(NOW)
    database = InMemoryDatabase(clock)
    clinic = Clinic(
        id=CLINIC_ID,
        name="Test Dental",
        status=ClinicStatus.ACTIVE,
        trial_started_at=NOW,
        wa_phone_id="phone-1",
        telegram_chat_id="chat-1",
        work_start=time(8),
        work_end=time(17),
        created_at=NOW,
    )
    service = Service(
        id=SERVICE_ID,
        clinic_id=CLINIC_ID,
        name="Cleaning",
        duration_min=30,
        price=Decimal("850"),
    )
    database.add_clinic(clinic)
    database.add_service(service)
    patient = await database.get_or_create_patient(CLINIC_ID, "27820000000", "John")
    starts_at = NOW + timedelta(days=1)
    await database.save_conversation_state(
        ConversationState(
            clinic_id=CLINIC_ID,
            patient_id=patient.id,
            state=ConversationStep.AWAIT_MA_DEPENDENT,
            slot={
                "service_id": str(SERVICE_ID),
                "starts_at": starts_at.isoformat().replace("+00:00", "Z"),
                "ends_at": (starts_at + timedelta(minutes=30))
                .isoformat()
                .replace("+00:00", "Z"),
                "medical_aid_name": "Discovery Health",
                "medical_aid_number": "1234567",
            },
            updated_at=NOW,
        )
    )
    calendar = FakeCalendar()
    calendar.fail_create = True
    whatsapp = FakeWhatsApp()
    notifications = NotificationFormatter(clock)
    flow = BookingFlow(
        database,
        whatsapp,
        calendar,
        FakeIntent(),
        SlotEngine(database, calendar, clock),
        TrialGate(clock),
        notifications,
        clock,
    )

    await flow.handle(
        clinic,
        IncomingMessage(
            message_id="wamid.1",
            from_number=patient.wa_number,
            profile_name=patient.name,
            kind=MessageKind.TEXT,
            text="01",
            display_text="01",
            raw={"id": "wamid.1"},
        ),
    )

    appointment = next(iter(database.appointments.values()))
    assert appointment.status is AppointmentStatus.BOOKED
    assert appointment.google_event_id is None
    assert any(job.job_type == "calendar_retry" for job in database.jobs.values())
    assert len(database.outbox) == 2

    clock.instant = appointment.starts_at + timedelta(minutes=16)
    scheduler = Scheduler(database, calendar, notifications, clock)
    await scheduler.run_once()

    assert database.appointments[appointment.id].status is AppointmentStatus.NO_SHOW
    assert database.patients[patient.id].no_show_count == 1
    assert any(
        item.channel == "telegram" and str(item.payload.get("text", "")).startswith("No-show")
        for item in database.outbox.values()
    )

