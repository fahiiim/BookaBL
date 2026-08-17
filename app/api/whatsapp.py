"""Thin WhatsApp webhook routes."""

import hmac
import json
from typing import Annotated, Any, cast

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import PlainTextResponse

from app.api.dependencies import get_api_context
from app.core.exceptions import (
    ConfigurationError,
    InvalidPayloadError,
    WebhookVerificationError,
)
from app.core.security import verify_meta_signature

router = APIRouter(prefix="/webhooks/whatsapp", tags=["webhooks"])


@router.get("")
async def verify_webhook(
    request: Request,
    hub_mode: Annotated[str, Query(alias="hub.mode")],
    hub_verify_token: Annotated[str, Query(alias="hub.verify_token")],
    hub_challenge: Annotated[str, Query(alias="hub.challenge")],
) -> PlainTextResponse:
    """Complete Meta's webhook subscription challenge."""

    settings = get_api_context(request).settings
    configured = settings.wa_verify_token
    if configured is None:
        raise ConfigurationError("WA_VERIFY_TOKEN is not configured")
    if hub_mode != "subscribe" or not hmac.compare_digest(
        hub_verify_token, configured.get_secret_value()
    ):
        raise WebhookVerificationError("Webhook verification token mismatch")
    return PlainTextResponse(hub_challenge)


@router.post("")
async def receive_webhook(
    request: Request,
    signature: Annotated[str | None, Header(alias="X-Hub-Signature-256")] = None,
) -> dict[str, int | str]:
    """Verify and durably persist inbound messages before acknowledging Meta."""

    context = get_api_context(request)
    secret = context.settings.wa_app_secret
    if secret is None:
        raise ConfigurationError("WA_APP_SECRET is not configured")
    body = await request.body()
    verify_meta_signature(body, signature, secret.get_secret_value())
    try:
        decoded = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InvalidPayloadError("Webhook body is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise InvalidPayloadError("Webhook body must be a JSON object")
    result = await context.whatsapp_ingress.persist(cast(dict[str, Any], decoded))
    return {
        "status": "accepted",
        "persisted": result.persisted,
        "duplicates": result.duplicates,
        "unknown_tenants": result.unknown_tenants,
    }

