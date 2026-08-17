"""Configuration-driven appointment candidate computation and validation."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.adapters.calendar import CalendarProvider
from app.core.clock import Clock
from app.db.protocol import Database
from app.domain.models import BusyPeriod, Clinic, Service


class SlotEngine:
    """Combine clinic hours, calendar availability, and open DB appointments."""

    def __init__(self, database: Database, calendar: CalendarProvider, clock: Clock) -> None:
        self._database = database
        self._calendar = calendar
        self._clock = clock

    async def offer(self, clinic: Clinic, service: Service, limit: int = 3) -> list[datetime]:
        """Return the next available UTC starts, normally the first three."""

        now = self._clock.now()
        horizon = now + timedelta(days=31)
        calendar_busy = await self._calendar.free_busy(clinic, now, horizon)
        appointments = await self._database.list_open_appointments(clinic.id, horizon, now)
        busy = calendar_busy + [
            BusyPeriod(starts_at=item.starts_at, ends_at=item.ends_at) for item in appointments
        ]
        return self._candidates(clinic, service, now, busy, limit)

    async def is_available(self, clinic: Clinic, service: Service, starts_at: datetime) -> bool:
        """Revalidate one UTC slot against hours, calendar, and persisted appointments."""

        if starts_at.tzinfo is None:
            return False
        starts_at = starts_at.astimezone(UTC)
        ends_at = starts_at + timedelta(minutes=service.duration_min)
        if not self._within_work_hours(clinic, starts_at, ends_at):
            return False
        calendar_busy = await self._calendar.free_busy(clinic, starts_at, ends_at)
        appointments = await self._database.list_open_appointments(
            clinic.id, ends_at, starts_at
        )
        busy = calendar_busy + [
            BusyPeriod(starts_at=item.starts_at, ends_at=item.ends_at) for item in appointments
        ]
        return not any(self._overlaps(starts_at, ends_at, period) for period in busy)

    def _candidates(
        self,
        clinic: Clinic,
        service: Service,
        now: datetime,
        busy: list[BusyPeriod],
        limit: int,
    ) -> list[datetime]:
        timezone = ZoneInfo(clinic.timezone)
        local_now = now.astimezone(timezone)
        duration = timedelta(minutes=service.duration_min)
        interval = timedelta(minutes=30)
        offered: list[datetime] = []

        for day_offset in range(31):
            local_day = local_now.date() + timedelta(days=day_offset)
            if local_day.isoweekday() not in clinic.work_days:
                continue
            day_start = datetime.combine(local_day, clinic.work_start, timezone)
            day_end = datetime.combine(local_day, clinic.work_end, timezone)
            candidate = day_start
            if candidate <= local_now:
                elapsed = local_now - day_start
                steps = int(elapsed // interval) + 1
                candidate = day_start + steps * interval

            while candidate + duration <= day_end:
                starts_at = candidate.astimezone(UTC)
                ends_at = starts_at + duration
                if not any(self._overlaps(starts_at, ends_at, period) for period in busy):
                    offered.append(starts_at)
                    if len(offered) >= limit:
                        return offered
                candidate += interval
        return offered

    @staticmethod
    def _within_work_hours(clinic: Clinic, starts_at: datetime, ends_at: datetime) -> bool:
        timezone = ZoneInfo(clinic.timezone)
        local_start = starts_at.astimezone(timezone)
        local_end = ends_at.astimezone(timezone)
        return (
            local_start.date() == local_end.date()
            and local_start.date().isoweekday() in clinic.work_days
            and local_start.time().replace(tzinfo=None) >= clinic.work_start
            and local_end.time().replace(tzinfo=None) <= clinic.work_end
        )

    @staticmethod
    def _overlaps(starts_at: datetime, ends_at: datetime, period: BusyPeriod) -> bool:
        return starts_at < period.ends_at and ends_at > period.starts_at


def local_date_bounds(local_day: date, timezone_name: str) -> tuple[datetime, datetime]:
    """Convert one tenant-local date to a half-open UTC interval."""

    timezone = ZoneInfo(timezone_name)
    local_start = datetime.combine(local_day, datetime.min.time(), timezone)
    return local_start.astimezone(UTC), (local_start + timedelta(days=1)).astimezone(UTC)

