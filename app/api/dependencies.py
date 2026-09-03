"""Small typed API context shared by thin route modules."""

from dataclasses import dataclass
from typing import Any, Protocol, cast

from fastapi import Request

from app.core.config import Settings
from app.db.protocol import Database
from app.services.whatsapp_ingress import WhatsAppIngress


class TelegramWebhookHandler(Protocol):
    """Handle a Telegram webhook update."""

    async def handle(self, payload: dict[str, Any]) -> bool:
        """Process an authorized owner command and report whether it was handled."""


class DueJobRunner(Protocol):
    """Run one scheduler pass."""

    async def run_once(self) -> int:
        """Process one batch and return its job count."""


@dataclass(slots=True)
class ApiContext:
    """Services used by HTTP routes."""

    settings: Settings
    whatsapp_ingress: WhatsAppIngress
    database: Database | None = None
    telegram_webhook: TelegramWebhookHandler | None = None
    scheduler: DueJobRunner | None = None


def get_api_context(request: Request) -> ApiContext:
    """Return the application-scoped API service context."""

    return cast(ApiContext, request.app.state.api_context)
