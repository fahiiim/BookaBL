from datetime import UTC, datetime, timedelta

from app.core.clock import FrozenClock, SystemClock


def test_frozen_clock_advances_in_utc() -> None:
    clock = FrozenClock(datetime(2026, 8, 17, 8, tzinfo=UTC))
    clock.advance(timedelta(minutes=15))
    assert clock.now() == datetime(2026, 8, 17, 8, 15, tzinfo=UTC)


def test_system_clock_applies_offset() -> None:
    before = SystemClock().now()
    shifted = SystemClock(offset_seconds=60).now()
    assert timedelta(seconds=59) < shifted - before < timedelta(seconds=61)

