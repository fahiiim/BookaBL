from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from app.adapters.telegram import FakeTelegram
from app.core.clock import FrozenClock
from app.db.memory import InMemoryDatabase
from app.domain.models import (
    Appointment,
    AppointmentStatus,
    Clinic,
    ClinicStatus,
    FinalizeBookingCommand,
    Patient,
    Service,
)
from app.services.notifications import NotificationFormatter
from app.services.privacy import minimal_patient_name
from app.services.telegram_commands import TelegramCommandService

NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)
CLINIC_ID = UUID("00000000-0000-4000-8000-000000000001")
SERVICE_ID = UUID("00000000-0000-4000-8000-000000000101")


def _clinic() -> Clinic:
    return Clinic(
        id=CLINIC_ID,
        name="Dr. James",
        status=ClinicStatus.ACTIVE,
        trial_started_at=NOW,
        wa_phone_id="phone-1",
        telegram_chat_id="owner-chat",
        timezone="UTC",
        work_start=time(8),
        work_end=time(17),
        created_at=NOW,
    )


def _service() -> Service:
    return Service(
        id=SERVICE_ID,
        clinic_id=CLINIC_ID,
        name="Root Canal",
        duration_min=30,
        price=Decimal("850"),
    )


def test_owner_booking_alert_contains_only_minimal_patient_details() -> None:
    clinic = _clinic()
    service = _service()
    patient = Patient(
        id=UUID("00000000-0000-4000-8000-000000000201"),
        clinic_id=CLINIC_ID,
        wa_number="27820000000",
        name="John Smith",
    )
    appointment = Appointment(
        id=UUID("00000000-0000-4000-8000-000000000301"),
        clinic_id=CLINIC_ID,
        patient_id=patient.id,
        service_id=SERVICE_ID,
        starts_at=datetime(2026, 9, 9, 10, tzinfo=UTC),
        ends_at=datetime(2026, 9, 9, 10, 30, tzinfo=UTC),
        status=AppointmentStatus.BOOKED,
        price=Decimal("850"),
        medical_aid_name="Private Health",
        medical_aid_number="MA-123456",
        dependent_code="01",
        created_at=NOW,
    )

    alert = NotificationFormatter(FrozenClock(NOW)).owner_new_booking(
        clinic, patient, service, appointment
    )

    assert alert == "NEW BOOKING - Dr. James\n09 Sept. 10:00\nJohn S - Confirmed"
    assert "Root Canal" not in alert
    assert "27820000000" not in alert
    assert "MA-123456" not in alert


def test_minimal_patient_name_handles_compound_surnames() -> None:
    assert minimal_patient_name("Muriel Van Wyk") == "Muriel VW"
    assert minimal_patient_name("John Smith") == "John S"
    assert minimal_patient_name("Cher") == "Cher"


@pytest.mark.asyncio
async def test_telegram_daily_weekly_and_monthly_ranges_are_private() -> None:
    clock = FrozenClock(NOW)
    database = InMemoryDatabase(clock)
    clinic = _clinic()
    service = _service()
    database.add_clinic(clinic)
    database.add_service(service)
    names_by_age = {
        0: "Current Patient",
        6: "Six Days",
        7: "Seven Days",
        29: "TwentyNine Days",
        30: "Thirty Days",
    }
    for index, (days_ago, name) in enumerate(names_by_age.items(), start=1):
        patient = await database.get_or_create_patient(
            CLINIC_ID, f"2782000000{index}", name
        )
        starts_at = (NOW - timedelta(days=days_ago)).replace(hour=8)
        await database.finalize_booking(
            FinalizeBookingCommand(
                clinic_id=CLINIC_ID,
                patient_id=patient.id,
                service_id=SERVICE_ID,
                starts_at=starts_at,
                ends_at=starts_at + timedelta(minutes=30),
                medical_aid_name="Private Health",
                medical_aid_number=f"MA-{index}",
                dependent_code="01",
                whatsapp_to=patient.wa_number,
                whatsapp_payload={"kind": "text", "text": "Booked"},
            )
        )

    telegram = FakeTelegram()
    commands = TelegramCommandService(database, telegram, clock)

    assert await commands.handle(
        {"message": {"chat": {"id": "owner-chat"}, "text": "today's bookings"}}
    )
    today = str(telegram.sent[-1]["text"])
    assert today.startswith("TODAY 10 Sept. - Dr. James bookings")
    assert "08:00 - Current P" in today
    assert "Six D" not in today

    assert await commands.handle(
        {"message": {"chat": {"id": "owner-chat"}, "text": "weekly bookings"}}
    )
    weekly = str(telegram.sent[-1]["text"])
    assert "Current P" in weekly
    assert "Six D" in weekly
    assert "Seven D" not in weekly

    assert await commands.handle(
        {"message": {"chat": {"id": "owner-chat"}, "text": "monthly bookings"}}
    )
    monthly = str(telegram.sent[-1]["text"])
    assert "Seven D" in monthly
    assert "TwentyNine D" in monthly
    assert "Thirty D" not in monthly
    assert "Root Canal" not in monthly
    assert "Private Health" not in monthly
    assert "MA-" not in monthly

    assert not await commands.handle(
        {"message": {"chat": {"id": "another-clinic"}, "text": "monthly bookings"}}
    )
