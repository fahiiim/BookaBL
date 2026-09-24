"""Thin Telegram webhook route."""

import hmac
from typing import Any

from fastapi import APIRouter, Request

from app.api.dependencies import get_api_context
from app.core.exceptions import ConfigurationError, InvalidPayloadError, InvalidSignatureError

router = APIRouter(prefix="/webhooks/telegram", tags=["webhooks"])


@router.post("")
async def receive_telegram(request: Request, payload: dict[str, Any]) -> dict[str, bool]:
    """Delegate a Telegram update to the authorized owner-command service."""

    context = get_api_context(request)
    configured_secret = context.settings.telegram_webhook_secret
    if configured_secret is not None:
        supplied_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(
            configured_secret.get_secret_value(), supplied_secret
        ):
            raise InvalidSignatureError("Telegram webhook secret mismatch")
    handler = context.telegram_webhook
    if handler is None:
        raise ConfigurationError("Telegram webhook handler is not configured")
    if not isinstance(payload, dict):
        raise InvalidPayloadError("Telegram webhook body must be an object")
    return {"handled": await handler.handle(payload)}
