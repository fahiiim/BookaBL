"""Thin Telegram webhook route."""

from typing import Any

from fastapi import APIRouter, Request

from app.api.dependencies import get_api_context
from app.core.exceptions import ConfigurationError, InvalidPayloadError

router = APIRouter(prefix="/webhooks/telegram", tags=["webhooks"])


@router.post("")
async def receive_telegram(request: Request, payload: dict[str, Any]) -> dict[str, bool]:
    """Delegate a Telegram update to the authorized owner-command service."""

    handler = get_api_context(request).telegram_webhook
    if handler is None:
        raise ConfigurationError("Telegram webhook handler is not configured")
    if not isinstance(payload, dict):
        raise InvalidPayloadError("Telegram webhook body must be an object")
    return {"handled": await handler.handle(payload)}

