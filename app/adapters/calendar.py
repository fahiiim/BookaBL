"""Calendar provider port with Google, deterministic stub, and fake adapters."""

import hashlib
from datetime import datetime, timedelta
from typing import Any, Protocol
from urllib.parse import quote
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from pydantic import SecretStr

from app.core.clock import Clock
from app.core.encryption import decrypt_token
from app.core.exceptions import CalendarProviderError, ExternalServiceError
from app.db.protocol import Database
from app.domain.models import Appointment, BusyPeriod, Clinic, Patient
from app.services.privacy import minimal_patient_name, short_date


class CalendarProvider(Protocol):
    """Read clinic availability and mirror durable bookings to a calendar."""

    async def free_busy(
        self, clinic: Clinic, starts_at: datetime, ends_at: datetime
    ) -> list[BusyPeriod]:
        """Return busy half-open periods within the requested UTC interval."""

    async def create_event(
        self,
        clinic: Clinic,
        patient: Patient,
        appointment: Appointment,
    ) -> str | None:
        """Create a calendar event, or return ``None`` when the clinic is disconnected."""

    async def update_event(
        self, clinic: Clinic, patient: Patient, appointment: Appointment
    ) -> str | None:
        """Update an existing event, creating it when no event identifier exists."""

    async def delete_event(self, clinic: Clinic, appointment: Appointment) -> None:
        """Delete an appointment's calendar event when one exists."""


class GoogleCalendar:
    """Google Calendar adapter resolving encrypted OAuth credentials per clinic."""

    def __init__(
        self,
        database: Database,
        client_id: str,
        client_secret: str,
        encryption_key: SecretStr | str,
        clock: Clock,
        client: httpx.AsyncClient | None = None,
        *,
        api_base_url: str = "https://bookabl.co.za",
    ) -> None:
        self._database = database
        self._client_id = client_id
        self._client_secret = client_secret
        self._encryption_key = encryption_key
        self._clock = clock
        self._client = client or httpx.AsyncClient(timeout=20)
        self._api_base_url = api_base_url.rstrip("/")
        self._token_cache: dict[UUID, tuple[str, datetime]] = {}

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
        headers = await self._headers(clinic)
        if headers is None:
            return []
        try:
            response = await self._client.post(
                "https://www.googleapis.com/calendar/v3/freeBusy",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            periods = response.json()["calendars"][calendar_id].get("busy", [])
            return [
                BusyPeriod(starts_at=period["start"], ends_at=period["end"])
                for period in periods
            ]
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise CalendarProviderError("google_calendar", f"free/busy failed: {exc}") from exc

    async def create_event(
        self,
        clinic: Clinic,
        patient: Patient,
        appointment: Appointment,
    ) -> str | None:
        calendar_id = quote(clinic.google_calendar_id or "primary", safe="")
        payload = self._event_payload(clinic, patient, appointment)
        headers = await self._headers(clinic)
        if headers is None:
            return None
        try:
            response = await self._client.post(
                f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            return str(response.json()["id"])
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise CalendarProviderError("google_calendar", f"event creation failed: {exc}") from exc

    async def update_event(
        self, clinic: Clinic, patient: Patient, appointment: Appointment
    ) -> str | None:
        if not appointment.google_event_id:
            return await self.create_event(clinic, patient, appointment)
        headers = await self._headers(clinic)
        if headers is None:
            return None
        calendar_id = quote(clinic.google_calendar_id or "primary", safe="")
        event_id = quote(appointment.google_event_id, safe="")
        try:
            response = await self._client.patch(
                f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events/{event_id}",
                headers=headers,
                json=self._event_payload(clinic, patient, appointment),
            )
            if response.status_code == 404:
                return await self.create_event(clinic, patient, appointment)
            response.raise_for_status()
            return str(response.json().get("id", appointment.google_event_id))
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise CalendarProviderError("google_calendar", f"event update failed: {exc}") from exc

    async def delete_event(self, clinic: Clinic, appointment: Appointment) -> None:
        if not appointment.google_event_id:
            return
        headers = await self._headers(clinic)
        if headers is None:
            return
        calendar_id = quote(clinic.google_calendar_id or "primary", safe="")
        event_id = quote(appointment.google_event_id, safe="")
        try:
            response = await self._client.delete(
                f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events/{event_id}",
                headers=headers,
            )
            if response.status_code == 404:
                return
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise CalendarProviderError("google_calendar", f"event delete failed: {exc}") from exc

    def _event_payload(
        self, clinic: Clinic, patient: Patient, appointment: Appointment
    ) -> dict[str, Any]:
        patient_name = minimal_patient_name(patient.name)
        starts_at = appointment.starts_at.astimezone(ZoneInfo(clinic.timezone))
        created_at = appointment.created_at.astimezone(ZoneInfo(clinic.timezone))
        time_label = starts_at.strftime("%I:%M %p").lower()
        return {
            "summary": f"{patient_name} - Confirmed",
            "description": (
                "Status: Confirmed via WhatsApp\n"
                f"Booked: {created_at:%H:%M} {short_date(created_at)}\n"
                f"Patient: {patient_name}\n"
                f"Time: {time_label}\n"
                f"For more info: {self._api_base_url}\n\n"
                "Patient details remain in BookaBL's secure admin portal."
            ),
            "start": {
                "dateTime": appointment.starts_at.isoformat(),
                "timeZone": clinic.timezone,
            },
            "end": {
                "dateTime": appointment.ends_at.isoformat(),
                "timeZone": clinic.timezone,
            },
        }

    async def _headers(self, clinic: Clinic) -> dict[str, str] | None:
        token = await self._access_token(clinic)
        if token is None:
            return None
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async def _access_token(self, clinic: Clinic) -> str | None:
        now = self._clock.now()
        stored = await self._database.get_oauth_token(clinic.id, "google")
        if stored is None:
            self._token_cache.pop(clinic.id, None)
            return None
        cached = self._token_cache.get(clinic.id)
        if cached is not None and cached[1] > now:
            return cached[0]
        safe_expiry = (
            stored.token_expires_at - timedelta(seconds=60)
            if stored.token_expires_at is not None
            else None
        )
        if stored.access_token and safe_expiry is not None and safe_expiry > now:
            self._token_cache[clinic.id] = (stored.access_token, safe_expiry)
            return stored.access_token
        refresh_token = decrypt_token(stored.refresh_token_encrypted, self._encryption_key)
        try:
            response = await self._client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            if response.status_code == 400:
                try:
                    oauth_error = str(response.json().get("error", ""))
                except (TypeError, ValueError):
                    oauth_error = ""
                if oauth_error == "invalid_grant":
                    await self._database.delete_oauth_token(clinic.id, "google")
                    await self._database.set_google_oauth_connected(clinic.id, False)
                    self._token_cache.pop(clinic.id, None)
                    return None
            response.raise_for_status()
            token = str(response.json()["access_token"])
            expires_in = max(int(response.json().get("expires_in", 3600)), 60)
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise CalendarProviderError("google_calendar", f"OAuth refresh failed: {exc}") from exc
        expires_at = now + timedelta(seconds=expires_in)
        await self._database.update_oauth_access_token(
            clinic.id, "google", token, expires_at
        )
        self._token_cache[clinic.id] = (token, expires_at - timedelta(seconds=60))
        return token


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
        patient: Patient,
        appointment: Appointment,
    ) -> str:
        material = "|".join(
            [
                str(clinic.id),
                str(appointment.id),
                minimal_patient_name(patient.name),
                appointment.starts_at.isoformat(),
                appointment.ends_at.isoformat(),
            ]
        )
        return f"stub-{hashlib.sha256(material.encode()).hexdigest()[:24]}"

    async def update_event(
        self, clinic: Clinic, patient: Patient, appointment: Appointment
    ) -> str:
        return appointment.google_event_id or await self.create_event(
            clinic, patient, appointment
        )

    async def delete_event(self, clinic: Clinic, appointment: Appointment) -> None:
        del clinic, appointment


class FakeCalendar(StubCalendar):
    """Configurable calendar double with captured event creation calls."""

    def __init__(self, busy: list[BusyPeriod] | None = None) -> None:
        self.busy = busy or []
        self.created: list[dict[str, Any]] = []
        self.fail_free_busy = False
        self.fail_create = False
        self.updated: list[UUID] = []
        self.deleted: list[UUID] = []

    async def free_busy(
        self, clinic: Clinic, starts_at: datetime, ends_at: datetime
    ) -> list[BusyPeriod]:
        del clinic
        if self.fail_free_busy:
            raise CalendarProviderError("fake_calendar", "free/busy unavailable")
        return [
            period
            for period in self.busy
            if period.starts_at < ends_at and period.ends_at > starts_at
        ]

    async def create_event(
        self,
        clinic: Clinic,
        patient: Patient,
        appointment: Appointment,
    ) -> str:
        if self.fail_create:
            raise ExternalServiceError("fake_calendar", "create unavailable")
        event_id = await super().create_event(clinic, patient, appointment)
        self.created.append(
            {
                "event_id": event_id,
                "clinic_id": clinic.id,
                "patient_name": minimal_patient_name(patient.name),
                "appointment_id": appointment.id,
                "starts_at": appointment.starts_at,
                "ends_at": appointment.ends_at,
            }
        )
        return event_id

    async def update_event(
        self, clinic: Clinic, patient: Patient, appointment: Appointment
    ) -> str:
        if self.fail_create:
            raise ExternalServiceError("fake_calendar", "update unavailable")
        self.updated.append(appointment.id)
        return await super().update_event(clinic, patient, appointment)

    async def delete_event(self, clinic: Clinic, appointment: Appointment) -> None:
        if self.fail_create:
            raise ExternalServiceError("fake_calendar", "delete unavailable")
        self.deleted.append(appointment.id)
        await super().delete_event(clinic, appointment)
