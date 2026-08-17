"""Telegram Bot API port, production adapter, and fake."""

from typing import Any, Protocol

import httpx

from app.core.exceptions import ExternalServiceError


class TelegramSender(Protocol):
    """Deliver a text message to a Telegram chat."""

    async def send_message(self, chat_id: str, text: str) -> None:
        """Send a plain-text Telegram message."""


class TelegramBot:
    """HTTP adapter for the Telegram Bot API."""

    def __init__(self, token: str, client: httpx.AsyncClient | None = None) -> None:
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._client = client or httpx.AsyncClient(timeout=20)

    async def send_message(self, chat_id: str, text: str) -> None:
        try:
            response = await self._client.post(
                f"{self._base_url}/sendMessage",
                json={"chat_id": chat_id, "text": text},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ExternalServiceError("telegram", f"send failed: {exc}") from exc


class FakeTelegram:
    """Capture Telegram messages without making network calls."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_message(self, chat_id: str, text: str) -> None:
        self.sent.append({"chat_id": chat_id, "text": text})

