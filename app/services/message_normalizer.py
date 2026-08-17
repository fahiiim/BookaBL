"""Normalize supported Meta webhook message shapes."""

from typing import Any, cast

from app.domain.messages import IncomingMessage, MessageKind


def normalize_whatsapp_event(payload: dict[str, Any]) -> IncomingMessage:
    """Convert a persisted event envelope into one normalized patient message."""

    message = _mapping(payload.get("message"), "message")
    contacts_raw = payload.get("contacts", [])
    contacts = contacts_raw if isinstance(contacts_raw, list) else []
    profile_name = "Patient"
    if contacts and isinstance(contacts[0], dict):
        profile = contacts[0].get("profile", {})
        if isinstance(profile, dict):
            profile_name = str(profile.get("name") or "Patient")

    message_type = str(message.get("type", ""))
    text = ""
    display_text = ""
    audio_media_id: str | None = None
    if message_type == "text":
        text_body = _mapping(message.get("text"), "text")
        text = str(text_body.get("body", "")).strip()
        display_text = text
        kind = MessageKind.TEXT
    elif message_type == "interactive":
        interactive = _mapping(message.get("interactive"), "interactive")
        reply = _mapping(interactive.get("button_reply"), "interactive.button_reply")
        text = str(reply.get("id", "")).strip()
        display_text = str(reply.get("title", text)).strip()
        kind = MessageKind.BUTTON
    elif message_type == "button":
        button = _mapping(message.get("button"), "button")
        text = str(button.get("payload", "")).strip()
        display_text = str(button.get("text", text)).strip()
        kind = MessageKind.BUTTON
    elif message_type == "audio":
        audio = _mapping(message.get("audio"), "audio")
        audio_media_id = str(audio.get("id", "")).strip()
        if not audio_media_id:
            raise ValueError("Audio message has no media id")
        kind = MessageKind.AUDIO
    else:
        raise ValueError(f"Unsupported WhatsApp message type: {message_type!r}")

    message_id = str(message.get("id", "")).strip()
    from_number = str(message.get("from", "")).strip()
    if not message_id or not from_number:
        raise ValueError("WhatsApp message requires id and from")
    return IncomingMessage(
        message_id=message_id,
        from_number=from_number,
        profile_name=profile_name,
        kind=kind,
        text=text,
        display_text=display_text,
        audio_media_id=audio_media_id,
        raw=message,
    )


def _mapping(value: object, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Expected object at {path}")
    return cast(dict[str, Any], value)

