from typing import cast

import httpx
import pytest
from app.adapters.telegram import FakeTelegram
from app.adapters.whatsapp import FakeWhatsApp, ListRow, ReplyButton
from app.bootstrap import Runtime
from app.db.memory import InMemoryDatabase
from app.domain.models import AppointmentStatus, ConversationStep
from app.main import create_app

from tests.integration.test_booking_flow import (
    CLINIC_ID,
    build_test_runtime,
    send_whatsapp,
)


@pytest.mark.asyncio
async def test_reception_handoff_relays_telegram_replies_and_can_resume() -> None:
    runtime, _clinic = await build_test_runtime()
    assert isinstance(runtime.database, InMemoryDatabase)
    assert isinstance(runtime.whatsapp, FakeWhatsApp)
    assert isinstance(runtime.telegram, FakeTelegram)
    app = create_app(runtime.api_context)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await send_whatsapp(client, runtime, 1, "Hello")
        welcome = runtime.whatsapp.sent[-1]
        assert welcome["body"] == "Hi! Welcome to Test Dental. How can I help you today?"
        buttons = cast(list[ReplyButton], welcome["buttons"])
        reception = next(button for button in buttons if button.id == "start:human")
        await send_whatsapp(client, runtime, 2, reception.id, button=True)

        patient_id = next(iter(runtime.database.patients))
        state = runtime.database.states[(CLINIC_ID, patient_id)]
        assert state.state is ConversationStep.HUMAN_HANDOFF
        reference = str(state.slot["handoff_ref"])

        await runtime.outbox_worker.run_once()
        assert f"PATIENT HANDOFF - {reference}" in str(runtime.telegram.sent[-1]["text"])

        await send_whatsapp(client, runtime, 3, "Please ask the dentist to call me")
        assert runtime.whatsapp.sent[-1]["text"].startswith("I've notified")
        await runtime.outbox_worker.run_once()
        assert "Please ask the dentist to call me" in str(runtime.telegram.sent[-1]["text"])

        response = await client.post(
            "/webhooks/telegram",
            json={
                "message": {
                    "chat": {"id": 123456789},
                    "text": f"/reply {reference} We will call you shortly.",
                }
            },
        )
        assert response.json() == {"handled": True}
        await runtime.outbox_worker.run_once()
        assert runtime.whatsapp.sent[-1]["text"] == "We will call you shortly."
        assert runtime.database.states[(CLINIC_ID, patient_id)].state is (
            ConversationStep.HUMAN_HANDOFF
        )

        await client.post(
            "/webhooks/telegram",
            json={
                "message": {
                    "chat": {"id": 123456789},
                    "text": f"/resume {reference}",
                }
            },
        )
        assert runtime.database.states[(CLINIC_ID, patient_id)].state is ConversationStep.IDLE


async def _book_cash(
    client: httpx.AsyncClient, runtime: Runtime
) -> None:
    await send_whatsapp(client, runtime, 1, "book appointment")
    whatsapp = cast(FakeWhatsApp, runtime.whatsapp)
    welcome = cast(list[ReplyButton], whatsapp.sent[-1]["buttons"])[0]
    await send_whatsapp(client, runtime, 2, welcome.id, button=True)
    service = cast(list[ReplyButton], whatsapp.sent[-1]["buttons"])[0]
    await send_whatsapp(client, runtime, 3, service.id, button=True)
    selected_date = cast(list[ListRow], whatsapp.sent[-1]["rows"])[0]
    await send_whatsapp(client, runtime, 4, selected_date.id, button=True)
    selected_time = cast(list[ListRow], whatsapp.sent[-1]["rows"])[0]
    await send_whatsapp(client, runtime, 5, selected_time.id, button=True)
    await send_whatsapp(client, runtime, 6, "payment:cash", button=True)
    await send_whatsapp(client, runtime, 7, "Thandi Nkosi")


@pytest.mark.asyncio
async def test_reschedule_preserves_booking_and_cancel_requires_confirmation() -> None:
    runtime, _clinic = await build_test_runtime()
    assert isinstance(runtime.database, InMemoryDatabase)
    assert isinstance(runtime.whatsapp, FakeWhatsApp)
    app = create_app(runtime.api_context)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _book_cash(client, runtime)
        appointment = next(iter(runtime.database.appointments.values()))
        original_start = appointment.starts_at
        original_event = appointment.google_event_id

        await send_whatsapp(client, runtime, 8, f"reschedule:{appointment.id}", button=True)
        selected_date = cast(list[ListRow], runtime.whatsapp.sent[-1]["rows"])[0]
        await send_whatsapp(client, runtime, 9, selected_date.id, button=True)
        selected_time = cast(list[ListRow], runtime.whatsapp.sent[-1]["rows"])[0]
        await send_whatsapp(client, runtime, 10, selected_time.id, button=True)

        moved = runtime.database.appointments[appointment.id]
        assert len(runtime.database.appointments) == 1
        assert moved.starts_at != original_start
        assert moved.google_event_id == original_event
        assert moved.medical_aid_number is None
        assert len(
            [job for job in runtime.database.jobs.values() if job.appointment_id == moved.id]
        ) == 4

        await send_whatsapp(client, runtime, 11, f"cancel:{appointment.id}", button=True)
        assert runtime.database.appointments[appointment.id].status is AppointmentStatus.BOOKED
        cancel_buttons = cast(list[ReplyButton], runtime.whatsapp.sent[-1]["buttons"])
        confirm = next(button for button in cancel_buttons if button.title == "Yes, cancel")
        await send_whatsapp(client, runtime, 12, confirm.id, button=True)

    cancelled = runtime.database.appointments[appointment.id]
    assert cancelled.status is AppointmentStatus.CANCELLED
    assert cancelled.google_event_id is None


@pytest.mark.asyncio
async def test_only_bound_telegram_clinic_can_mark_no_show() -> None:
    runtime, _clinic = await build_test_runtime()
    assert isinstance(runtime.database, InMemoryDatabase)
    app = create_app(runtime.api_context)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _book_cash(client, runtime)
        appointment = next(iter(runtime.database.appointments.values()))
        await runtime.database.transition_appointment_status(
            appointment.id,
            appointment.patient_id,
            [AppointmentStatus.BOOKED],
            AppointmentStatus.CONFIRMED,
        )
        unauthorized = await client.post(
            "/webhooks/telegram",
            json={
                "message": {
                    "chat": {"id": 999999},
                    "text": f"/noshow {appointment.id}",
                }
            },
        )
        assert unauthorized.json() == {"handled": False}
        assert runtime.database.appointments[appointment.id].status is (
            AppointmentStatus.CONFIRMED
        )

        authorized = await client.post(
            "/webhooks/telegram",
            json={
                "message": {
                    "chat": {"id": 123456789},
                    "text": f"/noshow {appointment.id}",
                }
            },
        )
        assert authorized.json() == {"handled": True}

    assert runtime.database.appointments[appointment.id].status is AppointmentStatus.NO_SHOW
    patient = runtime.database.patients[appointment.patient_id]
    assert patient.no_show_count == 1
