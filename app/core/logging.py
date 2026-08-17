"""Structured JSON logging with request and tenant context."""

import json
import logging
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Any

clinic_id_context: ContextVar[str | None] = ContextVar("clinic_id", default=None)
message_id_context: ContextVar[str | None] = ContextVar("message_id", default=None)


class JsonFormatter(logging.Formatter):
    """Render log records as one-line JSON documents."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        clinic_id = clinic_id_context.get()
        message_id = message_id_context.get()
        if clinic_id is not None:
            payload["clinic_id"] = clinic_id
        if message_id is not None:
            payload["whatsapp_message_id"] = message_id
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging(level: str) -> None:
    """Configure the root logger for structured application output."""

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())


def bind_log_context(
    *, clinic_id: str | None = None, message_id: str | None = None
) -> tuple[Token[str | None], Token[str | None]]:
    """Bind tenant and message identifiers, returning reset tokens."""

    return clinic_id_context.set(clinic_id), message_id_context.set(message_id)


def reset_log_context(tokens: tuple[Token[str | None], Token[str | None]]) -> None:
    """Reset context variables using tokens returned by :func:`bind_log_context`."""

    clinic_id_context.reset(tokens[0])
    message_id_context.reset(tokens[1])

