import hashlib
import hmac
import json
from datetime import UTC, datetime, time
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from app.adapters.telegram import FakeTelegram
from app.adapters.whatsapp import FakeWhatsApp, ReplyButton
from app.bootstrap import Runtime, build_runtime
from app.core.clock import FrozenClock
from app.core.config import Settings
from app.db.memory import InMemoryDatabase
from app.domain.models import AppointmentStatus, Clinic, ClinicStatus, Service
from app.main import create_app
from pydantic import SecretStr

NOW = datetime(2026, 8, 17, 8, tzinfo=UTC)
CLINIC_ID = UUID("00000000-0000-4000-8000-000000000001")
SERVICE_ID = UUID("00000000-0000-4000-8000-000000000101")
WA_NUMBER = "27820000000"
APP_SECRET = "integration-secret"


async def build_test_runtime(*, expired: bool = False) -> tuple[Runtime, Clinic]:
    settings = Settings(
        _env_file=None,
        app_env="dev",
        time_offset_seconds=0,
        wa_app_secret=SecretStr(APP_SECRET),
        wa_verify_token=SecretStr("verify"),
    )
    runtime = await build_runtime(settings, injected_clock=FrozenClock(NOW))
    assert isinstance(runtime.database, InMemoryDatabase)
    # Derive all time assertions from the runtime clock rather than hard-coding today's date.
    now = runtime.clock.now()
    clinic = Clinic(
        id=CLINIC_ID,
        name="Test Dental",
        status=ClinicStatus.TRIAL,
        trial_started_at=now.replace(year=now.year - 1) if expired else now,
        trial_days=7,
        wa_phone_id="phone-1",
        telegram_chat_id="123456789",
        timezone="Africa/Johannesburg",
        work_start=time(8),
        work_end=time(17),
        work_days=[1, 2, 3, 4, 5],
        created_at=now,
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
    return runtime, clinic


async def send_whatsapp(
    client: httpx.AsyncClient,
    runtime: Runtime,
    sequence: int,
    text: str,
    *,
    button: bool = False,
) -> httpx.Response:
    if button:
        message: dict[str, Any] = {
            "from": WA_NUMBER,
            "id": f"wamid.{sequence}",
            "type": "interactive",
            "interactive": {
                "type": "button_reply",
                "button_reply": {"id": text, "title": text[:20]},
            },
        }
    else:
        message = {
            "from": WA_NUMBER,
            "id": f"wamid.{sequence}",
            "type": "text",
            "text": {"body": text},
        }
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "phone-1"},
                            "contacts": [
                                {"profile": {"name": "John"}, "wa_id": WA_NUMBER}
                            ],
                            "messages": [message],
                        }
                    }
                ]
            }
        ],
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    digest = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    response = await client.post(
        "/webhooks/whatsapp",
        content=body,
        headers={"X-Hub-Signature-256": f"sha256={digest}"},
    )
    assert response.status_code == 200
    assert await runtime.event_processor.run_once() == 1
    return response


@pytest.mark.asyncio
async def test_full_booking_reminder_and_telegram_owner_paths() -> None:
    runtime, _clinic = await build_test_runtime()
    assert isinstance(runtime.database, InMemoryDatabase)
    assert isinstance(runtime.whatsapp, FakeWhatsApp)
    assert isinstance(runtime.telegram, FakeTelegram)
    app = create_app(runtime.api_context)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await send_whatsapp(client, runtime, 1, "book appointment")
        service_message = runtime.whatsapp.sent[-1]
        service_buttons = cast(list[ReplyButton], service_message["buttons"])
        await send_whatsapp(
            client, runtime, 2, service_buttons[0].id, button=True
        )
        slot_message = runtime.whatsapp.sent[-1]
        slot_buttons = cast(list[ReplyButton], slot_message["buttons"])
        await send_whatsapp(client, runtime, 3, slot_buttons[0].id, button=True)
        await send_whatsapp(client, runtime, 4, "Discovery Health")
        await send_whatsapp(client, runtime, 5, "1234567")
        await send_whatsapp(client, runtime, 6, "01")

        assert len(runtime.database.appointments) == 1
        appointment = next(iter(runtime.database.appointments.values()))
        assert appointment.price == Decimal("850")
        assert appointment.medical_aid_name == "Discovery Health"
        assert appointment.medical_aid_number == "1234567"
        assert appointment.dependent_code == "01"
        assert appointment.google_event_id is not None
        assert len(runtime.database.jobs) == 3
        assert len(runtime.database.outbox) == 2

        assert await runtime.outbox_worker.run_once() == 2
        assert "🦷 New booking: John" in runtime.telegram.sent[-1]["text"]
        assert "Medical Aid: Discovery Health | No: 1234567 | Dep: 01" in (
            runtime.telegram.sent[-1]["text"]
        )

        telegram_response = await client.post(
            "/webhooks/telegram",
            json={"message": {"chat": {"id": 123456789}, "text": "/bookings"}},
        )
        assert telegram_response.json() == {"handled": True}
        assert "John" in runtime.telegram.sent[-1]["text"]

        reminder_response = await client.post("/dev/trigger-due-jobs")
        assert reminder_response.status_code == 200
        await runtime.outbox_worker.run_once()
        reminder_messages = [
            item for item in runtime.whatsapp.sent if item["kind"] == "buttons"
        ]
        assert reminder_messages
        reminder_buttons = cast(list[ReplyButton], reminder_messages[-1]["buttons"])
        confirm = next(button for button in reminder_buttons if button.title == "Confirm")
        await send_whatsapp(client, runtime, 7, confirm.id, button=True)

    assert runtime.database.appointments[appointment.id].status is AppointmentStatus.CONFIRMED


@pytest.mark.asyncio
async def test_expired_trial_is_blocked_and_throttled_once_per_day() -> None:
    runtime, _clinic = await build_test_runtime(expired=True)
    assert isinstance(runtime.database, InMemoryDatabase)
    assert isinstance(runtime.whatsapp, FakeWhatsApp)
    assert isinstance(runtime.telegram, FakeTelegram)
    app = create_app(runtime.api_context)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await send_whatsapp(client, runtime, 1, "book appointment")
        await send_whatsapp(client, runtime, 2, "book appointment")

    assert runtime.database.appointments == {}
    assert len(runtime.database.outbox) == 2
    await runtime.outbox_worker.run_once()
    assert len(runtime.whatsapp.sent) == 1
    assert "temporarily unavailable" in runtime.whatsapp.sent[0]["text"]
    assert len(runtime.telegram.sent) == 1
