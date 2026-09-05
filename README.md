# BOOKABL

BOOKABL Milestone 1 is a multi-tenant AI WhatsApp receptionist backend for South African
dental clinics. It accepts signed WhatsApp Cloud API webhooks, runs a deterministic booking
conversation, stores bookings in Supabase, mirrors them to Google Calendar when configured,
and notifies clinic owners through Telegram.

Only M1 is implemented. Waitlists, review and recall campaigns, payments, an admin dashboard,
and deployment automation are intentionally absent.

## What is included

- Signed, deduplicated, persist-first WhatsApp webhook ingress.
- Tenant resolution from Meta `metadata.phone_number_id`.
- Config-driven trial access, hours, services, timezone, reminders, templates, and branding.
- Service, slot, medical-aid, and self-pay booking conversation states.
- Three availability candidates combining clinic hours, Google free/busy, and open DB bookings.
- One transactional `finalize_booking` RPC for the appointment, reminder/no-show jobs, and outbox.
- Calendar failure isolation through durable `calendar_retry` jobs.
- Telegram booking alerts and authorized `today's bookings` / `/bookings` commands.
- Confirm, reschedule, cancel, reminder, no-show, and patient no-show-count handling.
- Optional WhatsApp voice-note transcription through OpenAI `whisper-1`.
- Network-free fakes, a deterministic demo, and end-to-end signed-webhook acceptance tests.

## Prerequisites

- Python 3.12 (the project deliberately excludes 3.13+).
- A Supabase project for production-style persistence.
- Meta WhatsApp Cloud API and Telegram Bot credentials.
- An OpenAI API key for structured intent output and voice-note transcription.
- Optional Google OAuth client credentials and refresh token.
- Optional Supabase CLI for `make migrate`; the SQL Editor route below needs no CLI.

Never commit `.env`. The repository ignores it, and settings use `SecretStr` so secrets are not
rendered accidentally. If a secret has been pasted into chat, logs, or a ticket, rotate it before
using this service.

## Quickstart

On PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

On macOS/Linux:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
```

Fill `.env`. `WA_VERIFY_TOKEN` is a random value you create and then enter in both Meta and this
file; it is not the Meta access token. `TELEGRAM_BOT_USERNAME` is informational in M1. A minimal
local fake-backed runtime can start with blank integration values, but real webhook processing
needs Supabase plus the provider credentials.

Apply database files in this exact order in the Supabase SQL Editor:

1. `migrations/0001_schema.sql`
2. `migrations/0002_functions.sql`
3. `migrations/0004_admin.sql`
4. `scripts/seed.sql` only for a disposable development project

Alternatively, after installing and linking the Supabase CLI, run `make migrate`.

For local development, `.env.example` enables embedded workers, so one command starts the API,
inbound event processor, outbox delivery, and scheduler:

```powershell
python -m uvicorn app.main:app --reload
```

For production or separate-process development, set `RUN_WORKERS_IN_API=false` and start the API
and worker in separate terminals:

```powershell
make run-api
make run-worker
```

Without `make`, the equivalent separate-process commands are:

```powershell
python -m uvicorn app.main:app --reload
python -m app.workers.runner
```

Configure Meta's callback as `https://<public-api>/webhooks/whatsapp`, using your
`WA_VERIFY_TOKEN`. Configure Telegram's webhook as
`https://<public-api>/webhooks/telegram`. Both providers require a public HTTPS endpoint; local
development therefore needs an HTTPS tunnel or a test server.

## Quality gates and demo

```powershell
make lint
make typecheck
make test
make demo
```

`make demo` uses only the in-memory database and fakes. It completes a medical-aid booking,
creates its jobs/outbox, and prints the owner notification without making network calls.

The integration suite signs and submits WhatsApp payloads through `httpx.ASGITransport`, then
drives the actual event processor. It covers the full booking, reminder/confirm, expired-trial,
Telegram owner-command, calendar-outage, no-show, and audio-transcription paths.

## Adding a clinic with zero code changes

One `clinics` row is one dentist. Add the clinic and its services; do not edit Python:

```sql
insert into public.clinics (
  name, industry, package, status, trial_started_at, trial_days,
  monthly_fee, wa_phone_id, telegram_chat_id, google_calendar_id,
  timezone, work_start, work_end, work_days, reminder_offsets_h,
  wa_templates, brand_voice
) values (
  'Example Dental', 'dental', 'starter', 'trial', now(), 7,
  999.00, '<META_PHONE_NUMBER_ID>', '<OWNER_CHAT_ID>', null,
  'Africa/Johannesburg', '08:00', '17:00', '{1,2,3,4,5}', '{24,3}',
  '{"reminder":{"name":"appointment_reminder","language":"en"}}',
  'Warm, concise, and professional.'
) returning id;

insert into public.services (clinic_id, name, duration_min, price) values
  ('<RETURNED_CLINIC_ID>', 'Cleaning', 30, 850.00),
  ('<RETURNED_CLINIC_ID>', 'Consultation', 45, 650.00);
```

Use the real Meta `phone_number_id`, not the display phone number. The configured system-user
`WA_ACCESS_TOKEN` must have access to every enrolled phone-number ID. `telegram_chat_id` is the
owner’s numeric chat ID. The owner must start the bot before Telegram can deliver messages.
`WA_GRAPH_API_VERSION` controls the versioned Meta endpoint and defaults to `v23.0`.

`work_days` use ISO weekday numbers. Reminder template configuration is optional; without it,
the scheduler sends an interactive session message. Google Calendar is optional; when the three
`GOOGLE_*` OAuth values are absent, a deterministic calendar stub is used.

## Operational checks

- `GET /health` returns `{"status":"ok"}`.
- Meta subscription verification uses `GET /webhooks/whatsapp` with the standard `hub.*` query
  values.
- Signed message delivery uses `POST /webhooks/whatsapp` and returns only after persistence.
- `POST /dev/trigger-due-jobs` exists only when `APP_ENV=dev` and runs one scheduler batch.
- `TIME_OFFSET_SECONDS` shifts the injected system clock for demos. Restart API and worker after
  changing it so both processes use the same effective time.
- `RUN_WORKERS_IN_API=true` supervises all three worker loops inside the API process. Do not also
  start `app.workers.runner` unless you intentionally want another queue consumer.
- Failed outbox sends retry after 30 seconds, 2 minutes, 10 minutes, and 1 hour. A fifth failed
  attempt moves the item to `failed` and sends the owner a direct Telegram DLQ alert.

## Admin dashboard

BOOKABL includes a server-rendered administration dashboard at `/admin`. It uses Jinja templates,
one responsive CSS file, signed HttpOnly sessions, and tenant-scoped database queries. The dashboard
provides the day sheet, weekly and list booking views, appointment and patient records, services
CRUD, no-code clinic onboarding, operations queues, and webhook inspection.

Configure dashboard access in `.env` before opening it:

```dotenv
ADMIN_USERNAME=admin
ADMIN_PASSWORD=replace-with-a-long-random-password
```

The password signs the short-lived admin cookie and is never sent to a template. Production cookies
are marked `Secure`; every write form also carries a session-bound CSRF token. Select a clinic from
the top bar to keep every page and mutation scoped to that tenant.

> Screenshot placeholder — The Day Sheet overview in the Heritage Teal & Brass theme.

> Screenshot placeholder — Weekly booking strip and appointment detail record.

Apply `migrations/0004_admin.sql` before using consent views against Supabase. The migration adds
patient consent records and `clinics.google_review_url`.

## Assumptions

- Clinic `work_days` use ISO weekdays (`1` = Monday, `7` = Sunday).
- Slot candidates start at 30-minute increments relative to `work_start`; the search horizon is
  31 calendar days and the first three free candidates are offered.
- WhatsApp permits three reply buttons, so M1 offers the first three services ordered by name.
- A patient's name comes from the WhatsApp contact profile; absent names become `Patient`.
- Self-pay bookings store all medical-aid columns as `NULL`.
- Reschedule cancels the prior booking before offering replacement slots; the owner receives the
  new-booking alert after the replacement is finalized.
- The global `WA_ACCESS_TOKEN` is a Meta system-user token authorized for all enrolled clinic
  numbers. `wa_token_enc` is retained in the required schema for a future provisioning service
  and is not interpreted as plaintext by M1.
- Unknown WhatsApp phone IDs are acknowledged but not stored because `webhook_events.clinic_id`
  is required. The condition is logged without patient payload data.
- `clinic.expiry` is revenue-ready data; M1 access gating follows the specified trial status and
  `trial_started_at + trial_days`. Paused and churned clinics are also blocked.
- Calendar creation is best-effort after the database transaction. A failed creation never
  rolls back a booking and is retried by the scheduler.
- Reminder jobs whose due time is already past are eligible immediately, provided the appointment
  has not started. Confirmed appointments are not marked no-show by the M1 rule.
- `OPENAI_INTENT_MODEL` defaults to `gpt-4o-mini`. Any OpenAI classification error falls back to
  the keyword classifier; the state machine remains authoritative.

OpenAI integration follows the official guidance for
[Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs) and
[file transcription](https://developers.openai.com/api/docs/guides/speech-to-text).
