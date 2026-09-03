"""Environment-backed application configuration."""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables and an optional .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Literal["dev", "prod"] = "dev"
    log_level: str = "INFO"
    time_offset_seconds: int = 0
    run_workers_in_api: bool = False

    openai_api_key: SecretStr | None = None
    openai_intent_model: str = "gpt-4o-mini"

    supabase_url: str | None = None
    supabase_service_role_key: SecretStr | None = None

    wa_app_id: str | None = None
    wa_app_secret: SecretStr | None = None
    wa_verify_token: SecretStr | None = None
    wa_phone_number_id: str | None = None
    wa_access_token: SecretStr | None = None
    wa_graph_api_version: str = "v23.0"

    telegram_bot_token: SecretStr | None = None
    telegram_bot_username: str | None = None

    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    google_refresh_token: SecretStr | None = None

    api_base_url: str = "http://localhost:8000"

    admin_username: SecretStr | None = None
    admin_password: SecretStr | None = None

    worker_poll_seconds: float = 1.0
    worker_batch_size: int = 20


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide immutable settings instance."""

    return Settings()
