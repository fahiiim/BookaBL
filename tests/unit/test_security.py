import hashlib
import hmac

import pytest
from app.core.exceptions import InvalidSignatureError
from app.core.security import verify_meta_signature


def test_verify_meta_signature_accepts_valid_digest() -> None:
    body = b'{"object":"whatsapp_business_account"}'
    digest = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    verify_meta_signature(body, f"sha256={digest}", "secret")


def test_verify_meta_signature_rejects_invalid_digest() -> None:
    with pytest.raises(InvalidSignatureError):
        verify_meta_signature(b"payload", "sha256=invalid", "secret")

