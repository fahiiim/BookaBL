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
from app.adapters.whatsapp import FakeWhatsApp, ListRow, ReplyButton
from app.bootstrap import Runtime, build_runtime
from app.core.clock import FrozenClock
from app.core.config import Settings
from app.db.memory import InMemoryDatabase
from app.domain.models import (
    AppointmentStatus,
    Clinic,
    ClinicStatus,
    ConversationStep,
    Service,
)
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
        welcome_buttons = cast(list[ReplyButton], runtime.whatsapp.sent[-1]["buttons"])
        await send_whatsapp(client, runtime, 2, welcome_buttons[0].id, button=True)
        service_message = runtime.whatsapp.sent[-1]
        service_buttons = cast(list[ReplyButton], service_message["buttons"])
        await send_whatsapp(
            client, runtime, 3, service_buttons[0].id, button=True
        )
        date_rows = cast(list[ListRow], runtime.whatsapp.sent[-1]["rows"])
        await send_whatsapp(client, runtime, 4, date_rows[0].id, button=True)
        time_rows = cast(list[ListRow], runtime.whatsapp.sent[-1]["rows"])
        await send_whatsapp(client, runtime, 5, time_rows[0].id, button=True)
        payment_message = runtime.whatsapp.sent[-1]
        payment_buttons = cast(list[ReplyButton], payment_message["buttons"])
        medical_aid = next(
            button for button in payment_buttons if button.title == "Medical Aid"
        )
        await send_whatsapp(client, runtime, 6, medical_aid.id, button=True)
        await send_whatsapp(client, runtime, 7, "yEs")
        await send_whatsapp(
            client, runtime, 8, "John Smith\nDiscovery Health\n1234567\n01"
        )

        assert len(runtime.database.appointments) == 1
        appointment = next(iter(runtime.database.appointments.values()))
        assert appointment.price == Decimal("850")
        assert appointment.medical_aid_name == "Discovery Health"
        assert appointment.medical_aid_number == "1234567"
        assert appointment.dependent_code == "01"
        assert appointment.google_event_id is not None
        patient = runtime.database.patients[appointment.patient_id]
        assert patient.name == "John Smith"
        consents = list(runtime.database.consents.values())
        assert len(consents) == 1
        assert consents[0].consent_type == "medical_aid"
        assert consents[0].consent_version == "v2"
        assert len(runtime.database.jobs) == 4
        assert len(runtime.database.outbox) == 2

        assert await runtime.outbox_worker.run_once() == 2
        owner_alert = str(runtime.telegram.sent[-1]["text"])
        assert owner_alert.startswith("NEW BOOKING - Test Dental\n")
        assert "John S - Confirmed" in owner_alert
        assert "Cleaning" not in owner_alert
        assert "1234567" not in owner_alert
        assert "John Smith" not in owner_alert

        telegram_response = await client.post(
            "/webhooks/telegram",
            json={"message": {"chat": {"id": 123456789}, "text": "/bookings"}},
        )
        assert telegram_response.json() == {"handled": True}
        today_reply = str(runtime.telegram.sent[-1]["text"])
        assert today_reply.startswith("TODAY ")
        assert "John S" in today_reply
        assert "John Smith" not in today_reply
        assert "Cleaning" not in today_reply

        weekly_response = await client.post(
            "/webhooks/telegram",
            json={"message": {"chat": {"id": 123456789}, "text": "weekly bookings"}},
        )
        assert weekly_response.json() == {"handled": True}
        assert str(runtime.telegram.sent[-1]["text"]).startswith("LAST 7 DAYS")

        monthly_response = await client.post(
            "/webhooks/telegram",
            json={"message": {"chat": {"id": 123456789}, "text": "monthly bookings"}},
        )
        assert monthly_response.json() == {"handled": True}
        assert str(runtime.telegram.sent[-1]["text"]).startswith("LAST 30 DAYS")

        reminder_response = await client.post("/dev/trigger-due-jobs")
        assert reminder_response.status_code == 200
        await runtime.outbox_worker.run_once()
        reminder_messages = [
            item for item in runtime.whatsapp.sent if item["kind"] == "buttons"
        ]
        assert reminder_messages
        reminder_buttons = cast(list[ReplyButton], reminder_messages[-1]["buttons"])
        confirm = next(button for button in reminder_buttons if button.title == "Confirm")
        await send_whatsapp(client, runtime, 9, confirm.id, button=True)

    assert runtime.database.appointments[appointment.id].status is AppointmentStatus.CONFIRMED


async def begin_booking(
    client: httpx.AsyncClient, runtime: Runtime
) -> tuple[ReplyButton, UUID]:
    """Advance a fresh test runtime through slot selection."""

    assert isinstance(runtime.database, InMemoryDatabase)
    assert isinstance(runtime.whatsapp, FakeWhatsApp)
    await send_whatsapp(client, runtime, 1, "book appointment")
    welcome_buttons = cast(list[ReplyButton], runtime.whatsapp.sent[-1]["buttons"])
    await send_whatsapp(client, runtime, 2, welcome_buttons[0].id, button=True)
    service_buttons = cast(list[ReplyButton], runtime.whatsapp.sent[-1]["buttons"])
    await send_whatsapp(client, runtime, 3, service_buttons[0].id, button=True)
    date_rows = cast(list[ListRow], runtime.whatsapp.sent[-1]["rows"])
    await send_whatsapp(client, runtime, 4, date_rows[0].id, button=True)
    time_rows = cast(list[ListRow], runtime.whatsapp.sent[-1]["rows"])
    await send_whatsapp(client, runtime, 5, time_rows[0].id, button=True)
    payment_buttons = cast(list[ReplyButton], runtime.whatsapp.sent[-1]["buttons"])
    patient_id = next(iter(runtime.database.patients))
    return payment_buttons[0], patient_id


@pytest.mark.asyncio
async def test_medical_aid_no_aborts_booking_and_resets_state() -> None:
    runtime, _clinic = await build_test_runtime()
    assert isinstance(runtime.database, InMemoryDatabase)
    assert isinstance(runtime.whatsapp, FakeWhatsApp)
    app = create_app(runtime.api_context)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        medical_aid, patient_id = await begin_booking(client, runtime)
        assert medical_aid.id == "payment:medical_aid"
        await send_whatsapp(client, runtime, 6, medical_aid.id, button=True)
        await send_whatsapp(client, runtime, 7, "NO")

    state = runtime.database.states[(CLINIC_ID, patient_id)]
    assert state.state is ConversationStep.IDLE
    assert state.slot == {}
    assert runtime.database.appointments == {}
    assert runtime.database.consents == {}
    assert runtime.whatsapp.sent[-1]["text"] == (
        "No problem. We cannot complete the WhatsApp booking without your consent. "
        "Please contact the clinic directly to book."
    )


@pytest.mark.asyncio
async def test_cash_path_saves_consent_and_leaves_medical_aid_null() -> None:
    runtime, _clinic = await build_test_runtime()
    assert isinstance(runtime.database, InMemoryDatabase)
    assert isinstance(runtime.whatsapp, FakeWhatsApp)
    app = create_app(runtime.api_context)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        _medical_aid, patient_id = await begin_booking(client, runtime)
        await send_whatsapp(client, runtime, 6, "payment:cash", button=True)
        await send_whatsapp(client, runtime, 7, "Thandi Nkosi")
        assert runtime.database.appointments == {}
        assert runtime.database.consents == {}
        await send_whatsapp(client, runtime, 8, "name:confirm", button=True)

    appointment = next(iter(runtime.database.appointments.values()))
    assert appointment.medical_aid_name is None
    assert appointment.medical_aid_number is None
    assert appointment.dependent_code is None
    assert runtime.database.patients[patient_id].name == "Thandi Nkosi"
    consent = next(iter(runtime.database.consents.values()))
    assert consent.consent_type == "cash"
    assert consent.consent_version == "v2"
    assert runtime.database.states[(CLINIC_ID, patient_id)].state is ConversationStep.IDLE


@pytest.mark.asyncio
async def test_cash_name_can_be_edited_before_booking_is_finalized() -> None:
    runtime, _clinic = await build_test_runtime()
    assert isinstance(runtime.database, InMemoryDatabase)
    assert isinstance(runtime.whatsapp, FakeWhatsApp)
    app = create_app(runtime.api_context)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        _medical_aid, patient_id = await begin_booking(client, runtime)
        await send_whatsapp(client, runtime, 6, "payment:cash", button=True)
        await send_whatsapp(client, runtime, 7, "Barmuda Triangle")

        state = runtime.database.states[(CLINIC_ID, patient_id)]
        assert state.state is ConversationStep.AWAIT_CASH_NAME_CONFIRMATION
        assert state.slot["patient_full_name"] == "Barmuda Triangle"
        assert runtime.database.appointments == {}

        await send_whatsapp(client, runtime, 8, "name:edit", button=True)
        await send_whatsapp(client, runtime, 9, "Fahim Sarker")
        await send_whatsapp(client, runtime, 10, "name:confirm", button=True)

    appointment = next(iter(runtime.database.appointments.values()))
    assert appointment.patient_id == patient_id
    assert runtime.database.patients[patient_id].name == "Fahim Sarker"
    assert len(runtime.database.consents) == 1


@pytest.mark.asyncio
async def test_cash_name_rejects_non_name_content() -> None:
    runtime, _clinic = await build_test_runtime()
    assert isinstance(runtime.database, InMemoryDatabase)
    assert isinstance(runtime.whatsapp, FakeWhatsApp)
    app = create_app(runtime.api_context)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        _medical_aid, patient_id = await begin_booking(client, runtime)
        await send_whatsapp(client, runtime, 6, "payment:cash", button=True)
        await send_whatsapp(client, runtime, 7, "call me on 0821234567")

    state = runtime.database.states[(CLINIC_ID, patient_id)]
    assert state.state is ConversationStep.AWAIT_CASH_NAME
    assert runtime.database.appointments == {}
    assert "name and surname" in str(runtime.whatsapp.sent[-1]["text"])


@pytest.mark.asyncio
async def test_malformed_medical_aid_details_are_retried_in_same_state() -> None:
    runtime, _clinic = await build_test_runtime()
    assert isinstance(runtime.database, InMemoryDatabase)
    assert isinstance(runtime.whatsapp, FakeWhatsApp)
    app = create_app(runtime.api_context)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        medical_aid, patient_id = await begin_booking(client, runtime)
        await send_whatsapp(client, runtime, 6, medical_aid.id, button=True)
        await send_whatsapp(client, runtime, 7, "YES")
        await send_whatsapp(client, runtime, 8, "Thandi Nkosi\nDiscovery\n1234567")

    state = runtime.database.states[(CLINIC_ID, patient_id)]
    assert state.state is ConversationStep.AWAIT_MA_DETAILS_SINGLE_MSG
    assert runtime.database.appointments == {}
    assert len(runtime.database.consents) == 1
    assert runtime.whatsapp.sent[-1]["text"] == (
        "I couldn't read those details clearly. Please send them exactly as: \n"
        "1. Name + Surname \n2. Medical Aid Scheme \n"
        "3. MA Number \n4. Dependant Code"
    )


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
