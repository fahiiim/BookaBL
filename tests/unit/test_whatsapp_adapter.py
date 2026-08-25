"""Tests for the versioned WhatsApp Cloud API adapter."""

from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from app.adapters.whatsapp import MetaWhatsApp
from app.domain.models import Clinic


async def test_meta_whatsapp_uses_configured_graph_api_version() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://graph.facebook.com/v23.0/phone-1/messages"
        return httpx.Response(200, json={"messages": [{"id": "wamid.test"}]})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        sender = MetaWhatsApp("test-token", "v23.0", client)
        now = datetime(2026, 8, 25, tzinfo=UTC)
        clinic = Clinic(
            id=UUID("00000000-0000-4000-8000-000000000001"),
            name="Test Dental",
            trial_started_at=now,
            wa_phone_id="phone-1",
            created_at=now,
        )
        await sender.send_text(clinic, "27820000000", "Hello")


def test_meta_whatsapp_rejects_invalid_graph_api_version() -> None:
    with pytest.raises(ValueError, match="must look like"):
        MetaWhatsApp("test-token", "23")
