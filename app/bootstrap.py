"""Runtime construction from environment settings."""

from dataclasses import dataclass

from app.adapters.calendar import CalendarProvider, GoogleCalendar, StubCalendar
from app.adapters.intent import IntentModel, KeywordIntent, OpenAIIntent
from app.adapters.telegram import FakeTelegram, TelegramBot, TelegramSender
from app.adapters.transcriber import FakeTranscriber, OpenAIWhisper, Transcriber
from app.adapters.whatsapp import FakeWhatsApp, MetaWhatsApp, WhatsAppSender
from app.api.dependencies import ApiContext
from app.core.clock import Clock, SystemClock
from app.core.config import Settings
from app.core.exceptions import ConfigurationError
from app.db.memory import InMemoryDatabase
from app.db.protocol import Database
from app.db.supabase import SupabaseDatabase
from app.flows.booking import BookingFlow
from app.services.notifications import NotificationFormatter
from app.services.slot_engine import SlotEngine
from app.services.telegram_commands import TelegramCommandService
from app.services.trial_gate import TrialGate
from app.services.whatsapp_ingress import WhatsAppIngress
from app.workers.event_processor import EventProcessor
from app.workers.outbox_worker import OutboxWorker
from app.workers.scheduler import Scheduler


@dataclass(slots=True)
class Runtime:
    """Long-lived process services and the subset exposed to HTTP routes."""

    api_context: ApiContext
    clock: Clock
    database: Database
    whatsapp: WhatsAppSender
    telegram: TelegramSender
    calendar: CalendarProvider
    transcriber: Transcriber
    intent: IntentModel
    booking_flow: BookingFlow
    event_processor: EventProcessor
    outbox_worker: OutboxWorker
    scheduler: Scheduler


async def build_runtime(settings: Settings, *, injected_clock: Clock | None = None) -> Runtime:
    """Build a production-backed runtime or an empty in-memory development runtime."""

    clock = injected_clock or SystemClock(settings.time_offset_seconds)
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
    if settings.wa_access_token:
        whatsapp: WhatsAppSender = MetaWhatsApp(
            settings.wa_access_token.get_secret_value(), settings.wa_graph_api_version
        )
    elif settings.app_env == "dev":
        whatsapp = FakeWhatsApp()
    else:
        raise ConfigurationError("WA_ACCESS_TOKEN is required in production")

    if settings.telegram_bot_token:
        telegram: TelegramSender = TelegramBot(
            settings.telegram_bot_token.get_secret_value()
        )
    elif settings.app_env == "dev":
        telegram = FakeTelegram()
    else:
        raise ConfigurationError("TELEGRAM_BOT_TOKEN is required in production")

    if (
        settings.google_client_id
        and settings.google_client_secret
        and settings.google_refresh_token
    ):
        calendar: CalendarProvider = GoogleCalendar(
            settings.google_client_id,
            settings.google_client_secret.get_secret_value(),
            settings.google_refresh_token.get_secret_value(),
        )
    else:
        calendar = StubCalendar()

    if settings.openai_api_key:
        api_key = settings.openai_api_key.get_secret_value()
        transcriber: Transcriber = OpenAIWhisper(api_key)
        intent: IntentModel = OpenAIIntent(
            api_key, settings.openai_intent_model, fallback=KeywordIntent()
        )
    elif settings.app_env == "dev":
        transcriber = FakeTranscriber()
        intent = KeywordIntent()
    else:
        raise ConfigurationError("OPENAI_API_KEY is required in production")

    notifications = NotificationFormatter(clock)
    slot_engine = SlotEngine(database, calendar, clock)
    booking_flow = BookingFlow(
        database,
        whatsapp,
        calendar,
        intent,
        slot_engine,
        TrialGate(clock),
        notifications,
        clock,
    )
    event_processor = EventProcessor(
        database,
        whatsapp,
        transcriber,
        booking_flow,
        batch_size=settings.worker_batch_size,
        poll_seconds=settings.worker_poll_seconds,
    )
    outbox_worker = OutboxWorker(
        database,
        whatsapp,
        telegram,
        clock,
        batch_size=settings.worker_batch_size,
        poll_seconds=settings.worker_poll_seconds,
    )
    scheduler = Scheduler(
        database,
        calendar,
        notifications,
        clock,
        batch_size=settings.worker_batch_size,
        poll_seconds=settings.worker_poll_seconds,
    )
    telegram_commands = TelegramCommandService(database, telegram, clock)
    ingress = WhatsAppIngress(database)
    return Runtime(
        api_context=ApiContext(
            settings=settings,
            whatsapp_ingress=ingress,
            telegram_webhook=telegram_commands,
            scheduler=scheduler,
        ),
        clock=clock,
        database=database,
        whatsapp=whatsapp,
        telegram=telegram,
        calendar=calendar,
        transcriber=transcriber,
        intent=intent,
        booking_flow=booking_flow,
        event_processor=event_processor,
        outbox_worker=outbox_worker,
        scheduler=scheduler,
    )
