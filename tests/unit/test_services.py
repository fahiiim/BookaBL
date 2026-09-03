from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from app.adapters.calendar import FakeCalendar
from app.core.clock import FrozenClock
from app.core.exceptions import InvalidTransitionError
from app.db.memory import InMemoryDatabase
from app.domain.models import (
    BusyPeriod,
    Clinic,
    ClinicStatus,
    ConversationStep,
    Service,
)
from app.flows.state_machine import ConversationTransitions
from app.services.slot_engine import SlotEngine
from app.services.trial_gate import TrialGate

CLINIC_ID = UUID("00000000-0000-4000-8000-000000000001")
SERVICE_ID = UUID("00000000-0000-4000-8000-000000000101")
MONDAY = datetime(2026, 8, 17, 6, tzinfo=UTC)  # 08:00 Africa/Johannesburg


def build_clinic(**updates: object) -> Clinic:
    values: dict[str, object] = {
        "id": CLINIC_ID,
        "name": "Test Dental",
        "status": ClinicStatus.TRIAL,
        "trial_started_at": MONDAY,
        "trial_days": 7,
        "wa_phone_id": "phone-1",
        "timezone": "Africa/Johannesburg",
        "work_start": time(8),
        "work_end": time(17),
        "work_days": [1, 2, 3, 4, 5],
        "created_at": MONDAY,
    }
    values.update(updates)
    return Clinic.model_validate(values)


@pytest.mark.asyncio
async def test_slot_engine_skips_calendar_busy_periods() -> None:
    clock = FrozenClock(MONDAY)
    database = InMemoryDatabase(clock)
    clinic = build_clinic()
    service = Service(
        id=SERVICE_ID,
        clinic_id=CLINIC_ID,
        name="Cleaning",
        duration_min=30,
        price=Decimal("850"),
    )
    database.add_clinic(clinic)
    database.add_service(service)
    calendar = FakeCalendar(
        [
            BusyPeriod(
                starts_at=MONDAY + timedelta(minutes=30),
                ends_at=MONDAY + timedelta(hours=1),
            )
        ]
    )

    slots = await SlotEngine(database, calendar, clock).offer(clinic, service)

    assert slots == [
        MONDAY + timedelta(hours=1),
        MONDAY + timedelta(hours=1, minutes=30),
        MONDAY + timedelta(hours=2),
    ]


def test_trial_gate_blocks_only_after_configured_duration() -> None:
    clinic = build_clinic()
    at_boundary = TrialGate(FrozenClock(MONDAY + timedelta(days=7)))
    expired = TrialGate(FrozenClock(MONDAY + timedelta(days=7, seconds=1)))

    assert not at_boundary.evaluate(clinic).blocked
    assert expired.evaluate(clinic).reason == "trial_expired"


def test_state_machine_enforces_popia_consent_gate() -> None:
    assert (
        ConversationTransitions.validate(
            ConversationStep.AWAIT_SLOT, ConversationStep.AWAIT_PAYMENT_TYPE
        )
        is ConversationStep.AWAIT_PAYMENT_TYPE
    )
    assert (
        ConversationTransitions.validate(
            ConversationStep.AWAIT_PAYMENT_TYPE,
            ConversationStep.AWAIT_POPIA_MA_CONSENT,
        )
        is ConversationStep.AWAIT_POPIA_MA_CONSENT
    )
    assert (
        ConversationTransitions.validate(
            ConversationStep.AWAIT_POPIA_MA_CONSENT,
            ConversationStep.AWAIT_MA_DETAILS_SINGLE_MSG,
        )
        is ConversationStep.AWAIT_MA_DETAILS_SINGLE_MSG
    )
    with pytest.raises(InvalidTransitionError):
        ConversationTransitions.validate(
            ConversationStep.AWAIT_PAYMENT_TYPE,
            ConversationStep.AWAIT_MA_DETAILS_SINGLE_MSG,
        )
