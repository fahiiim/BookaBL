# BOOKABL Architecture

## Boundaries

BOOKABL uses a hexagonal-ish architecture. The domain speaks in clinics, patients, slots,
appointments, jobs, and notifications; HTTP and provider payload details stay at the edges.

| Area | Responsibility |
| --- | --- |
| `app/api` | Thin request validation, delegation, and consistent error responses |
| `app/flows` | Deterministic conversation state transitions and booking orchestration |
| `app/services` | Slot calculation, trial gating, formatting, normalization, and owner commands |
| `app/db` | Domain-level `Database` protocol plus Supabase and in-memory adapters |
| `app/adapters` | WhatsApp, Telegram, Calendar, intent, and transcription ports/implementations |
| `app/workers` | Persisted-event, outbox, and automation-job processing loops |
| `app/core` | Settings, clock, structured logging, security, and typed exceptions |

`app.bootstrap.build_runtime` is the composition root. It constructor-injects every port and uses
real adapters when their settings exist. In `dev`, absent integrations become deterministic fakes;
in `prod`, required Supabase, WhatsApp, Telegram, and OpenAI settings fail fast.

## Inbound booking flow

```text
Meta POST
  -> verify raw-body HMAC
  -> resolve clinic by metadata.phone_number_id
  -> INSERT webhook_events (dedupe on message_id)
  -> HTTP 200

event_processor
  -> pop_unprocessed_events RPC (lease + SKIP LOCKED)
  -> normalize text / button / template-button / audio
  -> download + transcribe audio when needed
  -> trial gate and once-daily throttle
  -> deterministic booking state machine
  -> direct session prompts through WhatsApp
```

Conversation state is durable in `conversation_states`:

```text
idle -> await_service -> await_slot -> await_ma_name
                                      | self-pay -> finalize
                                      v
                              await_ma_number
                                      v
                              await_ma_dependent -> finalize -> idle
```

The intent model only proposes the idle-state intent. Any OpenAI failure uses keyword mapping, and
`ConversationTransitions` validates every state change. Button IDs and clinic/service ownership
are revalidated; model output never selects a service, timestamp, tenant, or status directly.

## Availability and finalization

`SlotEngine` builds candidates in the clinic timezone from `work_days`, `work_start`, `work_end`,
and service duration. It subtracts both `CalendarProvider.free_busy` periods and overlapping open
appointments. A selected `slot:<UTC ISO timestamp>` must have been offered and is rechecked before
finalization.

`finalize_booking` is a `SECURITY DEFINER` PostgreSQL function. Under one clinic-row lock it:

1. Validates that the service belongs to the tenant and duration matches.
2. Rejects overlaps against booked/confirmed appointments.
3. Inserts the booked appointment with the service price snapshot and medical-aid values.
4. Inserts configured reminder jobs and the `starts_at + 15 minutes` no-show job.
5. Inserts patient confirmation and owner Telegram outbox rows.

Only after that transaction commits does the flow create the calendar event. Failure inserts an
idempotent `calendar_retry` job, so calendar downtime cannot destroy a booking.

## Delivery and automation

`pop_pending_outbox` leases rows with `FOR UPDATE SKIP LOCKED` and advances a five-minute safety
lease. `OutboxWorker` sends provider-specific payloads, records successful outbound messages, and
applies the required 30s/2m/10m/1h backoff. Attempt five marks the row failed and triggers a direct
Telegram DLQ alert.

`pop_due_jobs` atomically changes due jobs from `pending` to `processing`:

- `reminder`: enqueue a configured WhatsApp template, or interactive Confirm/Reschedule/Cancel.
- `no_show_check`: the `mark_no_show` RPC changes only `booked` appointments, increments the
  patient's count atomically, and causes an owner alert.
- `calendar_retry`: recreate the missing event and persist its provider ID.

Confirm conditionally moves `booked -> confirmed`. Cancel conditionally moves open appointments to
`cancelled` and alerts the owner. Reschedule cancels the old appointment and re-enters
`await_slot` with the same service.

## Tenant and security model

Every business table contains or derives a `clinic_id`. External identifiers resolve to a clinic
before domain work begins, and service/patient/appointment lookups validate ownership. New dentists
are rows plus services, with no code branch or deployment.

- Meta signatures use constant-time SHA-256 HMAC comparison over untouched request bytes.
- Webhook message IDs and job dedupe keys enforce idempotency.
- Supabase tables have RLS enabled and worker RPC execution is granted only to `service_role`.
- Secrets are loaded from environment settings and are not logged or checked into Git.
- Logs are JSON and bind `clinic_id` plus WhatsApp message ID through context variables.
- Business timestamps come from the injected `Clock`; persisted timestamps are UTC, while display
  and scheduling convert through `clinic.timezone`.

## Process model and testing

The API process handles ingress and owner commands. `python -m app.workers.runner` supervises the
event processor, outbox worker, and scheduler as three asyncio tasks. Queue leases and database
constraints allow additional worker processes without duplicate concurrent claims.

`InMemoryDatabase` mirrors the atomic boundaries behind an asyncio lock. Fakes capture provider
calls and support media, busy periods, and injected failures. Unit tests cover security, slots,
trial boundaries, transitions, atomic finalization, calendar failure, no-show behavior, and audio.
Integration tests send signed ASGI webhooks through the real API and execute the actual workers.

