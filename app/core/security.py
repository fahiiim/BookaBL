"""Webhook authenticity helpers."""

import hashlib
import hmac

from app.core.exceptions import InvalidSignatureError


def verify_meta_signature(body: bytes, signature_header: str | None, app_secret: str) -> None:
    """Validate a Meta ``X-Hub-Signature-256`` header in constant time."""

    if not signature_header or not signature_header.startswith("sha256="):
        raise InvalidSignatureError("Missing or malformed Meta signature")
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    supplied = signature_header.removeprefix("sha256=")
    if not hmac.compare_digest(expected, supplied):
        raise InvalidSignatureError("Meta signature mismatch")

