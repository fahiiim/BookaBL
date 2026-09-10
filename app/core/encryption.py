"""Fernet encryption helpers for long-lived provider credentials."""

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr

from app.core.config import get_settings
from app.core.exceptions import ConfigurationError


def encrypt_token(plaintext: str, key: SecretStr | str | None = None) -> str:
    """Encrypt a provider token with the configured Fernet key."""

    return _fernet(key).encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str, key: SecretStr | str | None = None) -> str:
    """Decrypt a provider token without logging either representation."""

    try:
        return _fernet(key).decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise ConfigurationError("Google token decryption failed") from exc


def _fernet(key: SecretStr | str | None) -> Fernet:
    configured = key if key is not None else get_settings().google_token_encryption_key
    value = configured.get_secret_value() if isinstance(configured, SecretStr) else configured
    if not value:
        raise ConfigurationError("GOOGLE_TOKEN_ENCRYPTION_KEY is required")
    try:
        return Fernet(value.encode())
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("GOOGLE_TOKEN_ENCRYPTION_KEY must be a valid Fernet key") from exc
