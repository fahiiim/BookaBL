"""WhatsApp Cloud API port, production adapter, and deterministic fake."""

from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.core.exceptions import ExternalServiceError
from app.domain.models import Clinic

GRAPH_API_BASE = "https://graph.facebook.com/v21.0"


@dataclass(frozen=True, slots=True)
class ReplyButton:
    """A WhatsApp interactive reply button."""

    id: str
    title: str


class WhatsAppSender(Protocol):
    """Send and retrieve messages for a clinic's WhatsApp number."""

    async def send_text(self, clinic: Clinic, to: str, text: str) -> None:
        """Send a session text message."""

    async def send_buttons(
        self, clinic: Clinic, to: str, body: str, buttons: list[ReplyButton]
    ) -> None:
        """Send a session message containing up to three reply buttons."""

    async def send_template(
        self,
        clinic: Clinic,
        to: str,
        template_name: str,
        button_payloads: list[str],
        *,
        language_code: str = "en",
    ) -> None:
        """Send a business-initiated template with quick-reply payloads."""

    async def download_media(self, clinic: Clinic, media_id: str) -> tuple[bytes, str]:
        """Download media bytes and return them with their content type."""


class MetaWhatsApp:
    """HTTP adapter for Meta's WhatsApp Graph API v21.0."""

    def __init__(self, access_token: str, client: httpx.AsyncClient | None = None) -> None:
        self._default_access_token = access_token
        self._client = client or httpx.AsyncClient(timeout=20)

    async def send_text(self, clinic: Clinic, to: str, text: str) -> None:
        await self._send(
            clinic,
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "text",
                "text": {"preview_url": False, "body": text},
            },
        )

    async def send_buttons(
        self, clinic: Clinic, to: str, body: str, buttons: list[ReplyButton]
    ) -> None:
        if not 1 <= len(buttons) <= 3:
            raise ValueError("WhatsApp interactive messages require one to three buttons")
        await self._send(
            clinic,
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": body},
                    "action": {
                        "buttons": [
                            {
                                "type": "reply",
                                "reply": {"id": button.id, "title": button.title[:20]},
                            }
                            for button in buttons
                        ]
                    },
                },
            },
        )

    async def send_template(
        self,
        clinic: Clinic,
        to: str,
        template_name: str,
        button_payloads: list[str],
        *,
        language_code: str = "en",
    ) -> None:
        components = [
            {
                "type": "button",
                "sub_type": "quick_reply",
                "index": str(index),
                "parameters": [{"type": "payload", "payload": payload}],
            }
            for index, payload in enumerate(button_payloads)
        ]
        await self._send(
            clinic,
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "template",
                "template": {
                    "name": template_name,
                    "language": {"code": language_code},
                    "components": components,
                },
            },
        )

    async def download_media(self, clinic: Clinic, media_id: str) -> tuple[bytes, str]:
        headers = self._headers(clinic)
        try:
            metadata_response = await self._client.get(
                f"{GRAPH_API_BASE}/{media_id}", headers=headers
            )
            metadata_response.raise_for_status()
            media_url = str(metadata_response.json()["url"])
            media_response = await self._client.get(media_url, headers=headers)
            media_response.raise_for_status()
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise ExternalServiceError("whatsapp", f"media download failed: {exc}") from exc
        return media_response.content, media_response.headers.get(
            "content-type", "application/octet-stream"
        )

    async def _send(self, clinic: Clinic, payload: dict[str, Any]) -> None:
        try:
            response = await self._client.post(
                f"{GRAPH_API_BASE}/{clinic.wa_phone_id}/messages",
                headers=self._headers(clinic),
                json=payload,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ExternalServiceError("whatsapp", f"send failed: {exc}") from exc

    def _headers(self, clinic: Clinic) -> dict[str, str]:
        token = clinic.wa_token_enc or self._default_access_token
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


class FakeWhatsApp:
    """Capture WhatsApp operations without making network calls."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.media: dict[str, tuple[bytes, str]] = {}

    async def send_text(self, clinic: Clinic, to: str, text: str) -> None:
        self.sent.append(
            {"kind": "text", "clinic_id": clinic.id, "to": to, "text": text}
        )

    async def send_buttons(
        self, clinic: Clinic, to: str, body: str, buttons: list[ReplyButton]
    ) -> None:
        self.sent.append(
            {
                "kind": "buttons",
                "clinic_id": clinic.id,
                "to": to,
                "body": body,
                "buttons": buttons,
            }
        )

    async def send_template(
        self,
        clinic: Clinic,
        to: str,
        template_name: str,
        button_payloads: list[str],
        *,
        language_code: str = "en",
    ) -> None:
        self.sent.append(
            {
                "kind": "template",
                "clinic_id": clinic.id,
                "to": to,
                "template_name": template_name,
                "button_payloads": button_payloads,
                "language_code": language_code,
            }
        )

    async def download_media(self, clinic: Clinic, media_id: str) -> tuple[bytes, str]:
        del clinic
        try:
            return self.media[media_id]
        except KeyError as exc:
            raise ExternalServiceError("whatsapp", "fake media not found") from exc

