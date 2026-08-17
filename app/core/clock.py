"""UTC clock abstractions used by business logic."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    """Provide the current timezone-aware UTC timestamp."""

    def now(self) -> datetime:
        """Return the current instant in UTC."""


@dataclass(frozen=True, slots=True)
class SystemClock:
    """System clock with an optional deterministic demo offset."""

    offset_seconds: int = 0

    def now(self) -> datetime:
        """Return system UTC time plus the configured offset."""

        return datetime.now(tz=UTC) + timedelta(seconds=self.offset_seconds)


@dataclass(slots=True)
class FrozenClock:
    """Mutable test clock whose value advances only when explicitly requested."""

    instant: datetime

    def __post_init__(self) -> None:
        if self.instant.tzinfo is None:
            raise ValueError("FrozenClock requires a timezone-aware datetime")
        self.instant = self.instant.astimezone(UTC)

    def now(self) -> datetime:
        """Return the configured instant in UTC."""

        return self.instant

    def advance(self, delta: timedelta) -> None:
        """Advance the clock by ``delta``."""

        self.instant += delta

