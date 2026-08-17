"""Runtime construction from environment settings."""

from dataclasses import dataclass

from app.api.dependencies import ApiContext
from app.core.clock import SystemClock
from app.core.config import Settings
from app.core.exceptions import ConfigurationError
from app.db.memory import InMemoryDatabase
from app.db.protocol import Database
from app.db.supabase import SupabaseDatabase
from app.services.whatsapp_ingress import WhatsAppIngress


@dataclass(slots=True)
class Runtime:
    """Long-lived process services and the subset exposed to HTTP routes."""

    api_context: ApiContext
    database: Database


async def build_runtime(settings: Settings) -> Runtime:
    """Build a production-backed runtime or an empty in-memory development runtime."""

    clock = SystemClock(settings.time_offset_seconds)
    if settings.supabase_url and settings.supabase_service_role_key:
        database: Database = await SupabaseDatabase.create(
            settings.supabase_url,
            settings.supabase_service_role_key.get_secret_value(),
            clock,
        )
    elif settings.app_env == "dev":
        database = InMemoryDatabase(clock)
    else:
        raise ConfigurationError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required in production"
        )
    ingress = WhatsAppIngress(database)
    return Runtime(
        api_context=ApiContext(settings=settings, whatsapp_ingress=ingress),
        database=database,
    )

