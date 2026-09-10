"""Google OAuth authorization-code routes for clinic calendar connections."""

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import urlencode, urlparse
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import SecretStr

from app.admin.auth import require_admin
from app.api.dependencies import ApiContext, get_api_context
from app.core.config import Settings
from app.core.encryption import encrypt_token
from app.core.exceptions import ConfigurationError, OAuthStateError
from app.db.protocol import Database

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_OAUTH_EXCHANGE_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
OAUTH_STATE_SECONDS = 10 * 60

router = APIRouter(prefix="/oauth/google", tags=["oauth"])


@router.get("/initiate", dependencies=[Depends(require_admin)])
async def initiate_google_oauth(request: Request, clinic_id: UUID) -> Response:
    """Redirect an authenticated administrator to Google's consent screen."""

    context = get_api_context(request)
    database = _database(context)
    if await database.get_clinic(clinic_id) is None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    client_id, _client_secret, redirect_uri, _key = _oauth_settings(context.settings)
    state = create_google_oauth_state(context.settings, clinic_id, _now(context))
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": GOOGLE_CALENDAR_SCOPE,
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "response_type": "code",
        }
    )
    return RedirectResponse(f"{GOOGLE_AUTH_URL}?{query}", status_code=302)


@router.get("/callback")
async def google_oauth_callback(request: Request, code: str, state: str) -> Response:
    """Exchange Google's code, encrypt credentials, and connect the owning clinic."""

    context = get_api_context(request)
    clinic_id = validate_google_oauth_state(context.settings, state, _now(context))
    database = _database(context)
    if await database.get_clinic(clinic_id) is None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    client_id, client_secret, redirect_uri, encryption_key = _oauth_settings(
        context.settings
    )
    try:
        payload = await _exchange_code(
            context,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            code=code,
        )
        refresh_token = str(payload.get("refresh_token") or "")
        if not refresh_token:
            raise ValueError("Google did not return a refresh token")
        access_token = str(payload.get("access_token") or "") or None
        expires_in = max(int(payload.get("expires_in", 3600)), 0)
        expires_at = (
            _now(context) + timedelta(seconds=expires_in) if access_token else None
        )
        await database.upsert_oauth_token(
            clinic_id,
            "google",
            encrypt_token(refresh_token, encryption_key),
            access_token=access_token,
            token_expires_at=expires_at,
            scope=str(payload.get("scope") or GOOGLE_CALENDAR_SCOPE),
        )
        await database.set_google_oauth_connected(clinic_id, True)
    except (httpx.HTTPError, KeyError, TypeError, ValueError, ConfigurationError):
        return _clinic_redirect(
            clinic_id,
            "Google Calendar connection failed. Please try again.",
            "error",
        )
    return _clinic_redirect(clinic_id, "Google Calendar connected.", "success")


def create_google_oauth_state(
    settings: Settings, clinic_id: UUID, now: datetime | None = None
) -> str:
    """Create a signed, ten-minute state token binding the callback to a clinic."""

    issued_at = now or datetime.now(UTC)
    return _serializer(settings).dumps(
        {
            "clinic_id": str(clinic_id),
            "nonce": secrets.token_urlsafe(24),
            "expires_at": int(
                (issued_at + timedelta(seconds=OAUTH_STATE_SECONDS)).timestamp()
            ),
        }
    )


def validate_google_oauth_state(
    settings: Settings, state: str, now: datetime | None = None
) -> UUID:
    """Validate a Google OAuth state signature, age, expiry, and clinic identifier."""

    try:
        raw = _serializer(settings).loads(state, max_age=OAUTH_STATE_SECONDS)
        payload = cast(dict[str, Any], raw)
        clinic_id = UUID(str(payload["clinic_id"]))
        expires_at = int(payload["expires_at"])
        if expires_at < int((now or datetime.now(UTC)).timestamp()):
            raise OAuthStateError("Google OAuth state has expired")
        if not str(payload["nonce"]):
            raise OAuthStateError("Google OAuth state is malformed")
        return clinic_id
    except SignatureExpired as exc:
        raise OAuthStateError("Google OAuth state has expired") from exc
    except (BadSignature, KeyError, TypeError, ValueError) as exc:
        raise OAuthStateError("Google OAuth state is invalid") from exc


async def _exchange_code(
    context: ApiContext,
    *,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code: str,
) -> dict[str, Any]:
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    if context.oauth_http_client is not None:
        response = await context.oauth_http_client.post(GOOGLE_OAUTH_EXCHANGE_URL, data=data)
    else:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(GOOGLE_OAUTH_EXCHANGE_URL, data=data)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Google token response must be an object")
    return cast(dict[str, Any], payload)


def _oauth_settings(settings: Settings) -> tuple[str, str, str, SecretStr]:
    if not settings.google_oauth_client_id or not settings.google_oauth_client_secret:
        raise ConfigurationError("Google OAuth client credentials are not configured")
    if not settings.google_token_encryption_key:
        raise ConfigurationError("GOOGLE_TOKEN_ENCRYPTION_KEY is required")
    redirect_uri = settings.google_oauth_redirect_uri
    if urlparse(redirect_uri).scheme != "https":
        raise ConfigurationError("GOOGLE_OAUTH_REDIRECT_URI must use HTTPS")
    return (
        settings.google_oauth_client_id,
        settings.google_oauth_client_secret.get_secret_value(),
        redirect_uri,
        settings.google_token_encryption_key,
    )


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    if settings.admin_password is None:
        raise ConfigurationError("ADMIN_PASSWORD is required to sign Google OAuth state")
    return URLSafeTimedSerializer(
        settings.admin_password.get_secret_value(),
        salt="bookabl-google-oauth-state-v1",
    )


def _database(context: ApiContext) -> Database:
    if context.database is None:
        raise ConfigurationError("Database is unavailable")
    return context.database


def _now(context: ApiContext) -> datetime:
    return context.clock.now() if context.clock is not None else datetime.now(UTC)


def _clinic_redirect(clinic_id: UUID, notice: str, kind: str) -> RedirectResponse:
    query = urlencode({"clinic_id": str(clinic_id), "notice": notice, "kind": kind})
    return RedirectResponse(f"/admin/clinics/{clinic_id}?{query}", status_code=303)
