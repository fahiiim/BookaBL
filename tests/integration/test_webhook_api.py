import hashlib
import hmac
from datetime import UTC, datetime, time
from uuid import UUID

import httpx
import pytest
from app.api.dependencies import ApiContext
from app.core.clock import FrozenClock
from app.core.config import Settings
from app.db.memory import InMemoryDatabase
from app.domain.models import Clinic
from app.main import create_app
from app.services.whatsapp_ingress import WhatsAppIngress
from pydantic import SecretStr

NOW = datetime(2026, 8, 17, 8, tzinfo=UTC)
CLINIC_ID = UUID("00000000-0000-4000-8000-000000000001")


def payload() -> bytes:
    return (
        b'{"object":"whatsapp_business_account","entry":[{"changes":[{"value":'
        b'{"metadata":{"phone_number_id":"phone-1"},"contacts":[{"profile":'
        b'{"name":"John"},"wa_id":"27820000000"}],"messages":[{"from":'
        b'"27820000000","id":"wamid.1","type":"text","text":{"body":'
        b'"book appointment"}}]}}]}]}'
    )


def build_api() -> tuple[object, InMemoryDatabase]:
    database = InMemoryDatabase(FrozenClock(NOW))
    database.add_clinic(
        Clinic(
            id=CLINIC_ID,
            name="Test Dental",
            trial_started_at=NOW,
            wa_phone_id="phone-1",
            work_start=time(8),
            work_end=time(17),
            created_at=NOW,
        )
    )
    settings = Settings(
        _env_file=None,
        wa_app_secret=SecretStr("app-secret"),
        wa_verify_token=SecretStr("verify-me"),
    )
    context = ApiContext(settings=settings, whatsapp_ingress=WhatsAppIngress(database))
    return create_app(context), database


@pytest.mark.asyncio
async def test_webhook_verification_and_persist_first_deduplication() -> None:
    app, database = build_api()
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        verification = await client.get(
            "/webhooks/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "verify-me",
                "hub.challenge": "1234",
            },
        )
        body = payload()
        digest = hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
        first = await client.post(
            "/webhooks/whatsapp",
            content=body,
            headers={"X-Hub-Signature-256": f"sha256={digest}"},
        )
        duplicate = await client.post(
            "/webhooks/whatsapp",
            content=body,
            headers={"X-Hub-Signature-256": f"sha256={digest}"},
        )

    assert verification.status_code == 200
    assert verification.text == "1234"
    assert first.json()["persisted"] == 1
    assert duplicate.json()["duplicates"] == 1
    assert list(database.events) == ["wamid.1"]


@pytest.mark.asyncio
async def test_webhook_rejects_bad_signature_without_persisting() -> None:
    app, database = build_api()
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/webhooks/whatsapp",
            content=payload(),
            headers={"X-Hub-Signature-256": "sha256=wrong"},
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_signature"
    assert database.events == {}

