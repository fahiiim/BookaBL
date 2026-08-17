"""Normalized inbound message value objects."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict


class MessageKind(StrEnum):
    """Inbound WhatsApp message types supported by Milestone 1."""

    TEXT = "text"
    BUTTON = "button"
    AUDIO = "audio"


class IncomingMessage(BaseModel):
    """Provider-independent representation of one patient message."""

    model_config = ConfigDict(frozen=True)

    message_id: str
    from_number: str
    profile_name: str
    kind: MessageKind
    text: str = ""
    display_text: str = ""
    audio_media_id: str | None = None
    raw: dict[str, Any]

