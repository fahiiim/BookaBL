"""Persist-first WhatsApp webhook ingestion and tenant routing."""

import logging
from dataclasses import dataclass
from typing import Any, cast

from app.db.protocol import Database

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IngressResult:
    """Counts from ingesting a webhook payload."""

    persisted: int = 0
    duplicates: int = 0
    unknown_tenants: int = 0


class WhatsAppIngress:
    """Resolve tenant metadata and durably enqueue each inbound message."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def persist(self, payload: dict[str, Any]) -> IngressResult:
        """Persist supported inbound messages before returning to Meta."""

        persisted = 0
        duplicates = 0
        unknown_tenants = 0
        for value in self._values(payload):
            metadata = value.get("metadata", {})
            if not isinstance(metadata, dict):
                continue
            phone_id = str(metadata.get("phone_number_id", ""))
            clinic = await self._database.get_clinic_by_wa_phone_id(phone_id)
            messages = value.get("messages", [])
            if not isinstance(messages, list) or not messages:
                continue
            if clinic is None:
                unknown_tenants += len(messages)
                logger.warning("unknown_whatsapp_phone_id")
                continue
            contacts = value.get("contacts", [])
            for raw_message in messages:
                if not isinstance(raw_message, dict):
                    continue
                message_id = str(raw_message.get("id", "")).strip()
                if not message_id:
                    continue
                event_payload = {
                    "phone_number_id": phone_id,
                    "contacts": contacts if isinstance(contacts, list) else [],
                    "message": raw_message,
                }
                inserted = await self._database.persist_webhook_event(
                    message_id, clinic.id, event_payload
                )
                if inserted:
                    persisted += 1
                else:
                    duplicates += 1
        return IngressResult(persisted, duplicates, unknown_tenants)

    @staticmethod
    def _values(payload: dict[str, Any]) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        entries = payload.get("entry", [])
        if not isinstance(entries, list):
            return values
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            changes = entry.get("changes", [])
            if not isinstance(changes, list):
                continue
            for change in changes:
                if isinstance(change, dict) and isinstance(change.get("value"), dict):
                    values.append(cast(dict[str, Any], change["value"]))
        return values

