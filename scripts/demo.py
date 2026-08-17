"""Run a deterministic, network-free BOOKABL booking demonstration."""

import asyncio
from datetime import UTC, datetime, time
from decimal import Decimal
from typing import cast
from uuid import UUID

from app.adapters.telegram import FakeTelegram
from app.adapters.whatsapp import FakeWhatsApp, ReplyButton
from app.bootstrap import build_runtime
from app.core.clock import FrozenClock
from app.core.config import Settings
from app.db.memory import InMemoryDatabase
from app.domain.messages import IncomingMessage, MessageKind
from app.domain.models import Clinic, ClinicStatus, Service

CLINIC_ID = UUID("00000000-0000-4000-8000-000000000001")
SERVICE_ID = UUID("00000000-0000-4000-8000-000000000101")
PATIENT_NUMBER = "27820000000"


async def run_demo() -> None:
    """Simulate a complete medical-aid booking and deliver its outbox."""

    clock = FrozenClock(datetime(2026, 8, 17, 6, tzinfo=UTC))
    runtime = await build_runtime(
        Settings(_env_file=None, app_env="dev"), injected_clock=clock
    )
    assert isinstance(runtime.database, InMemoryDatabase)
    assert isinstance(runtime.whatsapp, FakeWhatsApp)
    assert isinstance(runtime.telegram, FakeTelegram)
    clinic = Clinic(
        id=CLINIC_ID,
        name="BOOKABL Demo Dental",
        status=ClinicStatus.ACTIVE,
        trial_started_at=clock.now(),
        wa_phone_id="demo-phone",
        telegram_chat_id="demo-owner",
        timezone="Africa/Johannesburg",
        work_start=time(8),
        work_end=time(17),
        created_at=clock.now(),
    )
    runtime.database.add_clinic(clinic)
    runtime.database.add_service(
        Service(
            id=SERVICE_ID,
            clinic_id=CLINIC_ID,
            name="Cleaning",
            duration_min=30,
            price=Decimal("850"),
        )
    )

    sequence = 0

    async def say(text: str, *, button: bool = False) -> None:
        nonlocal sequence
        sequence += 1
        await runtime.booking_flow.handle(
            clinic,
            IncomingMessage(
                message_id=f"demo-{sequence}",
                from_number=PATIENT_NUMBER,
                profile_name="John",
                kind=MessageKind.BUTTON if button else MessageKind.TEXT,
                text=text,
                display_text=text,
                raw={"demo": True},
            ),
        )

    await say("book appointment")
    service_buttons = cast(list[ReplyButton], runtime.whatsapp.sent[-1]["buttons"])
    await say(service_buttons[0].id, button=True)
    slot_buttons = cast(list[ReplyButton], runtime.whatsapp.sent[-1]["buttons"])
    await say(slot_buttons[0].id, button=True)
    await say("Discovery Health")
    await say("1234567")
    await say("01")
    await runtime.outbox_worker.run_once()

    appointment = next(iter(runtime.database.appointments.values()))
    print(f"Booked appointment: {appointment.id} at {appointment.starts_at.isoformat()}")
    print(f"Price snapshot: R{appointment.price}")
    print(f"Automation jobs: {len(runtime.database.jobs)}")
    notification = str(runtime.telegram.sent[-1]["text"])
    print(f"Telegram: {notification.removeprefix('🦷 ')}")


if __name__ == "__main__":
    asyncio.run(run_demo())
