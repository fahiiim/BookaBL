"""Small signed-cookie authentication helpers for the admin edge."""

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, cast
from urllib.parse import quote

from fastapi import HTTPException, Request, Response

from app.core.config import Settings

COOKIE_NAME = "bookabl_admin_session"
SESSION_SECONDS = 8 * 60 * 60


def credentials_configured(settings: Settings) -> bool:
    """Return whether both administrator credentials are present."""

    return bool(settings.admin_username and settings.admin_password)


def credentials_match(settings: Settings, username: str, password: str) -> bool:
    """Compare submitted credentials without early-return timing differences."""

    configured_user = settings.admin_username.get_secret_value() if settings.admin_username else ""
    configured_password = (
        settings.admin_password.get_secret_value() if settings.admin_password else ""
    )
    user_ok = hmac.compare_digest(username.encode(), configured_user.encode())
    password_ok = hmac.compare_digest(password.encode(), configured_password.encode())
    return credentials_configured(settings) and user_ok and password_ok


def set_session_cookie(response: Response, settings: Settings, username: str) -> None:
    """Attach a signed, time-limited, HttpOnly session cookie."""

    payload = {
        "username": username,
        "expires": int(time.time()) + SESSION_SECONDS,
        "csrf": secrets.token_urlsafe(24),
    }
    encoded = _encode_json(payload)
    signature = hmac.new(_signing_key(settings), encoded.encode(), hashlib.sha256).hexdigest()
    response.set_cookie(
        COOKIE_NAME,
        f"{encoded}.{signature}",
        max_age=SESSION_SECONDS,
        httponly=True,
        secure=settings.app_env == "prod",
        samesite="lax",
        path="/admin",
    )


def clear_session_cookie(response: Response) -> None:
    """Expire the dashboard session cookie."""

    response.delete_cookie(COOKIE_NAME, path="/admin")


def read_session(request: Request) -> dict[str, Any] | None:
    """Validate and decode the current signed session."""

    settings = request.app.state.api_context.settings
    raw = request.cookies.get(COOKIE_NAME, "")
    encoded, separator, supplied = raw.partition(".")
    if not separator or not credentials_configured(settings):
        return None
    expected = hmac.new(_signing_key(settings), encoded.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(supplied, expected):
        return None
    try:
        payload = _decode_json(encoded)
        expires = int(payload["expires"])
        username = str(payload["username"])
        csrf = str(payload["csrf"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    configured_user = settings.admin_username
    if configured_user is None or not hmac.compare_digest(
        username.encode(), configured_user.get_secret_value().encode()
    ):
        return None
    if expires < int(time.time()) or not csrf:
        return None
    return payload


def require_admin(request: Request) -> dict[str, Any]:
    """Require an authenticated admin or redirect to the login page."""

    session = read_session(request)
    if session is None:
        return_to = quote(str(request.url.path), safe="/")
        raise HTTPException(
            status_code=303,
            headers={"Location": f"/admin/login?next={return_to}"},
        )
    return session


def verify_csrf(session: dict[str, Any], supplied: str) -> None:
    """Reject a state-changing form without the session CSRF value."""

    expected = str(session.get("csrf", ""))
    if not expected or not hmac.compare_digest(expected.encode(), supplied.encode()):
        raise HTTPException(status_code=403, detail="Invalid form token")


def safe_return_to(value: str | None) -> str:
    """Restrict post-login navigation to this application's admin routes."""

    if value and value.startswith("/admin") and not value.startswith("//"):
        return value
    return "/admin"


def _signing_key(settings: Settings) -> bytes:
    password = settings.admin_password.get_secret_value() if settings.admin_password else ""
    return hashlib.sha256(f"bookabl-admin-session-v1:{password}".encode()).digest()


def _encode_json(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _decode_json(encoded: str) -> dict[str, Any]:
    padded = encoded + "=" * (-len(encoded) % 4)
    decoded = base64.urlsafe_b64decode(padded.encode())
    value = json.loads(decoded)
    if not isinstance(value, dict):
        raise ValueError("Session payload must be an object")
    return cast(dict[str, Any], value)
