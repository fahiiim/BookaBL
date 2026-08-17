"""Calendar provider port with Google, deterministic stub, and fake adapters."""

import hashlib
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from app.core.exceptions import ExternalServiceError
from app.domain.models import BusyPeriod, Clinic


class CalendarProvider(Protocol):
    """Read clinic availability and mirror durable bookings to a calendar."""

    async def free_busy(
        self, clinic: Clinic, starts_at: datetime, ends_at: datetime
    ) -> list[BusyPeriod]:
        """Return busy half-open periods within the requested UTC interval."""

    async def create_event(
        self,
        clinic: Clinic,
        summary: str,
        patient_name: str,
        starts_at: datetime,
        ends_at: datetime,
    ) -> str:
        """Create a calendar event and return its provider identifier."""


class GoogleCalendar:
    """Minimal Google Calendar adapter using an OAuth refresh token."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._client = client or httpx.AsyncClient(timeout=20)

    async def free_busy(
        self, clinic: Clinic, starts_at: datetime, ends_at: datetime
    ) -> list[BusyPeriod]:
        calendar_id = clinic.google_calendar_id or "primary"
        payload = {
            "timeMin": starts_at.isoformat(),
            "timeMax": ends_at.isoformat(),
            "timeZone": clinic.timezone,
            "items": [{"id": calendar_id}],
        }
        try:
            response = await self._client.post(
                "https://www.googleapis.com/calendar/v3/freeBusy",
                headers=await self._headers(),
                json=payload,
            )
            response.raise_for_status()
            periods = response.json()["calendars"][calendar_id].get("busy", [])
            return [
                BusyPeriod(starts_at=period["start"], ends_at=period["end"])
                for period in periods
            ]
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise ExternalServiceError("google_calendar", f"free/busy failed: {exc}") from exc

    async def create_event(
        self,
        clinic: Clinic,
        summary: str,
        patient_name: str,
        starts_at: datetime,
        ends_at: datetime,
    ) -> str:
        calendar_id = quote(clinic.google_calendar_id or "primary", safe="")
        payload = {
            "summary": summary,
            "description": f"BOOKABL appointment for {patient_name}",
            "start": {"dateTime": starts_at.isoformat(), "timeZone": clinic.timezone},
            "end": {"dateTime": ends_at.isoformat(), "timeZone": clinic.timezone},
        }
        try:
            response = await self._client.post(
                f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
                headers=await self._headers(),
                json=payload,
            )
            response.raise_for_status()
            return str(response.json()["id"])
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise ExternalServiceError("google_calendar", f"event creation failed: {exc}") from exc

    async def _headers(self) -> dict[str, str]:
        try:
            response = await self._client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": self._refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            response.raise_for_status()
            token = str(response.json()["access_token"])
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise ExternalServiceError("google_calendar", f"OAuth refresh failed: {exc}") from exc
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


class StubCalendar:
    """Always-free calendar with deterministic event identifiers."""

    async def free_busy(
        self, clinic: Clinic, starts_at: datetime, ends_at: datetime
    ) -> list[BusyPeriod]:
        del clinic, starts_at, ends_at
        return []

    async def create_event(
        self,
        clinic: Clinic,
        summary: str,
        patient_name: str,
        starts_at: datetime,
        ends_at: datetime,
    ) -> str:
        material = "|".join(
            [str(clinic.id), summary, patient_name, starts_at.isoformat(), ends_at.isoformat()]
        )
        return f"stub-{hashlib.sha256(material.encode()).hexdigest()[:24]}"


class FakeCalendar(StubCalendar):
    """Configurable calendar double with captured event creation calls."""

    def __init__(self, busy: list[BusyPeriod] | None = None) -> None:
        self.busy = busy or []
        self.created: list[dict[str, Any]] = []
        self.fail_free_busy = False
        self.fail_create = False

    async def free_busy(
        self, clinic: Clinic, starts_at: datetime, ends_at: datetime
    ) -> list[BusyPeriod]:
        del clinic
        if self.fail_free_busy:
            raise ExternalServiceError("fake_calendar", "free/busy unavailable")
        return [
            period
            for period in self.busy
            if period.starts_at < ends_at and period.ends_at > starts_at
        ]

    async def create_event(
        self,
        clinic: Clinic,
        summary: str,
        patient_name: str,
        starts_at: datetime,
        ends_at: datetime,
    ) -> str:
        if self.fail_create:
            raise ExternalServiceError("fake_calendar", "create unavailable")
        event_id = await super().create_event(
            clinic, summary, patient_name, starts_at, ends_at
        )
        self.created.append(
            {
                "event_id": event_id,
                "clinic_id": clinic.id,
                "summary": summary,
                "patient_name": patient_name,
                "starts_at": starts_at,
                "ends_at": ends_at,
            }
        )
        return event_id
