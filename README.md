# BOOKABL

BOOKABL is a multi-tenant WhatsApp receptionist backend for South African dental clinics.
This repository implements Milestone 1 only.

## Assumptions

- Clinic `work_days` use ISO weekdays (`1` = Monday, `7` = Sunday).
- Appointment candidates start on 30-minute boundaries and are searched up to 30 days ahead.
- A patient name comes from the WhatsApp contact profile; when absent, `Patient` is used.
- Self-pay bookings store all medical-aid columns as `NULL`.
- Google Calendar is optional in M1. Without all three Google OAuth values, the deterministic
  stub provider is used.
- The Meta webhook delivers one clinic phone-number ID per `value` object. Unknown phone IDs are
  acknowledged without persistence because `webhook_events.clinic_id` is intentionally required.
- `OPENAI_INTENT_MODEL` may override the default `gpt-4o-mini`; any model failure falls back to
  deterministic keyword classification.

