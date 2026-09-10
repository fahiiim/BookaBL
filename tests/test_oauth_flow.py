import re
from datetime import UTC, datetime, time, timedelta
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import httpx
import pytest
from app.adapters.calendar import GoogleCalendar
from app.api.dependencies import ApiContext
from app.api.oauth import create_google_oauth_state, validate_google_oauth_state
from app.core.clock import FrozenClock
from app.core.config import Settings
from app.core.encryption import decrypt_token, encrypt_token
from app.db.memory import InMemoryDatabase
from app.domain.models import Clinic, ClinicStatus
from app.main import create_app
from app.services.whatsapp_ingress import WhatsAppIngress
from cryptography.fernet import Fernet
from fastapi import FastAPI
from pydantic import SecretStr

NOW = datetime(2026, 8, 17, 8, tzinfo=UTC)
CLINIC_A = UUID("00000000-0000-4000-8000-000000000001")
CLINIC_B = UUID("00000000-0000-4000-8000-000000000002")


def _settings(key: str) -> Settings:
    return Settings(
        _env_file=None,
        app_env="dev",
        admin_username=SecretStr("admin"),
        admin_password=SecretStr("admin-secret"),
        google_oauth_client_id="google-client-id",
        google_oauth_client_secret=SecretStr("google-client-secret"),
        google_oauth_redirect_uri="https://bookabl.co.za/oauth/google/callback",
        google_token_encryption_key=SecretStr(key),
    )


def _clinic(clinic_id: UUID, *, connected: bool = False) -> Clinic:
    return Clinic(
        id=clinic_id,
        name=f"Clinic {clinic_id.int}",
        status=ClinicStatus.ACTIVE,
        trial_started_at=NOW,
        wa_phone_id=f"phone-{clinic_id.int}",
        google_calendar_id="primary",
        google_oauth_connected=connected,
        work_start=time(8),
        work_end=time(17),
        created_at=NOW,
    )


def _oauth_app(
    key: str, handler: httpx.MockTransport
) -> tuple[FastAPI, InMemoryDatabase, FrozenClock, httpx.AsyncClient, Settings]:
    clock = FrozenClock(NOW)
    database = InMemoryDatabase(clock)
    database.add_clinic(_clinic(CLINIC_A))
    settings = _settings(key)
    oauth_client = httpx.AsyncClient(transport=handler)
    context = ApiContext(
        settings=settings,
        whatsapp_ingress=WhatsAppIngress(database),
        database=database,
        clock=clock,
        oauth_http_client=oauth_client,
    )
    return create_app(context), database, clock, oauth_client, settings


async def _login(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/admin/login",
        data={"username": "admin", "password": "admin-secret"},
    )
    assert response.status_code == 303


@pytest.mark.asyncio
async def test_initiate_generates_valid_google_url_and_signed_state() -> None:
    key = Fernet.generate_key().decode()
    app, _database, _clock, oauth_client, settings = _oauth_app(
        key, httpx.MockTransport(lambda _request: httpx.Response(500))
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            await _login(client)
            response = await client.get(f"/oauth/google/initiate?clinic_id={CLINIC_A}")

        assert response.status_code == 302
        parsed = urlparse(response.headers["location"])
        query = parse_qs(parsed.query)
        assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == (
            "https://accounts.google.com/o/oauth2/v2/auth"
        )
        assert query["client_id"] == ["google-client-id"]
        assert query["redirect_uri"] == [
            "https://bookabl.co.za/oauth/google/callback"
        ]
        assert query["scope"] == ["https://www.googleapis.com/auth/calendar"]
        assert query["access_type"] == ["offline"]
        assert query["prompt"] == ["consent"]
        assert validate_google_oauth_state(settings, query["state"][0], NOW) == CLINIC_A
    finally:
        await oauth_client.aclose()


@pytest.mark.asyncio
async def test_callback_rejects_invalid_and_expired_state() -> None:
    key = Fernet.generate_key().decode()
    app, _database, clock, oauth_client, settings = _oauth_app(
        key, httpx.MockTransport(lambda _request: httpx.Response(500))
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            invalid = await client.get("/oauth/google/callback?code=x&state=tampered")
            state = create_google_oauth_state(settings, CLINIC_A, NOW)
            clock.advance(timedelta(minutes=11))
            expired = await client.get(
                "/oauth/google/callback", params={"code": "x", "state": state}
            )

        assert invalid.status_code == 400
        assert invalid.json()["error"]["code"] == "invalid_oauth_state"
        assert expired.status_code == 400
        assert expired.json()["error"]["code"] == "invalid_oauth_state"
    finally:
        await oauth_client.aclose()


@pytest.mark.asyncio
async def test_callback_stores_encrypted_refresh_token_and_sets_clinic_flag() -> None:
    key = Fernet.generate_key().decode()

    def exchange(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://oauth2.googleapis.com/token"
        form = parse_qs(request.content.decode())
        assert form["code"] == ["authorization-code"]
        return httpx.Response(
            200,
            json={
                "refresh_token": "clinic-a-refresh",
                "access_token": "initial-access",
                "expires_in": 3600,
                "scope": "https://www.googleapis.com/auth/calendar",
            },
        )

    app, database, _clock, oauth_client, settings = _oauth_app(
        key, httpx.MockTransport(exchange)
    )
    state = create_google_oauth_state(settings, CLINIC_A, NOW)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            response = await client.get(
                "/oauth/google/callback",
                params={"code": "authorization-code", "state": state},
            )

        assert response.status_code == 303
        token = await database.get_oauth_token(CLINIC_A)
        assert token is not None
        assert token.refresh_token_encrypted != "clinic-a-refresh"
        assert decrypt_token(token.refresh_token_encrypted, key) == "clinic-a-refresh"
        assert token.access_token == "initial-access"
        clinic = await database.get_clinic(CLINIC_A)
        assert clinic is not None and clinic.google_oauth_connected
    finally:
        await oauth_client.aclose()


@pytest.mark.asyncio
async def test_calendar_refreshes_per_clinic_token_and_isolates_tenants() -> None:
    key = Fernet.generate_key().decode()
    clock = FrozenClock(NOW)
    database = InMemoryDatabase(clock)
    clinic_a = _clinic(CLINIC_A, connected=True)
    clinic_b = _clinic(CLINIC_B, connected=True)
    database.add_clinic(clinic_a)
    database.add_clinic(clinic_b)
    await database.upsert_oauth_token(
        CLINIC_A,
        "google",
        encrypt_token("clinic-a-refresh", key),
        access_token="expired-access",
        token_expires_at=NOW - timedelta(seconds=1),
    )
    requests: list[httpx.Request] = []

    def google(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if str(request.url) == "https://oauth2.googleapis.com/token":
            form = parse_qs(request.content.decode())
            assert form["refresh_token"] == ["clinic-a-refresh"]
            return httpx.Response(
                200, json={"access_token": "fresh-access", "expires_in": 3600}
            )
        assert request.headers["authorization"] == "Bearer fresh-access"
        return httpx.Response(200, json={"id": "event-a"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(google))
    calendar = GoogleCalendar(
        database,
        "google-client-id",
        "google-client-secret",
        SecretStr(key),
        clock,
        client,
    )
    try:
        event_id = await calendar.create_event(
            clinic_a,
            "Cleaning",
            "Thandi Nkosi",
            NOW,
            NOW + timedelta(minutes=30),
        )
        refreshed = await database.get_oauth_token(CLINIC_A)
        isolated = await calendar.create_event(
            clinic_b,
            "Cleaning",
            "Other Patient",
            NOW,
            NOW + timedelta(minutes=30),
        )
        await database.delete_oauth_token(CLINIC_A)
        disconnected = await calendar.create_event(
            clinic_a,
            "Cleaning",
            "Thandi Nkosi",
            NOW,
            NOW + timedelta(minutes=30),
        )

        assert event_id == "event-a"
        assert refreshed is not None
        assert refreshed.access_token == "fresh-access"
        assert refreshed.token_expires_at == NOW + timedelta(hours=1)
        assert isolated is None
        assert disconnected is None
        assert len(requests) == 2
        assert await database.get_oauth_token(CLINIC_A) is None
        assert await database.get_oauth_token(CLINIC_B) is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_disconnect_removes_only_clinic_token_and_clears_flag() -> None:
    key = Fernet.generate_key().decode()
    app, database, _clock, oauth_client, _settings_value = _oauth_app(
        key, httpx.MockTransport(lambda _request: httpx.Response(500))
    )
    await database.upsert_oauth_token(
        CLINIC_A, "google", encrypt_token("clinic-a-refresh", key)
    )
    await database.set_google_oauth_connected(CLINIC_A, True)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            await _login(client)
            page = await client.get(
                f"/admin/clinics/{CLINIC_A}?clinic_id={CLINIC_A}"
            )
            match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
            assert match is not None
            response = await client.post(
                f"/admin/clinics/{CLINIC_A}/disconnect-calendar",
                data={"csrf_token": match.group(1)},
            )

        assert response.status_code == 303
        assert await database.get_oauth_token(CLINIC_A) is None
        clinic = await database.get_clinic(CLINIC_A)
        assert clinic is not None and not clinic.google_oauth_connected
    finally:
        await oauth_client.aclose()
