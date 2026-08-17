begin;

create extension if not exists pgcrypto;

create table public.clinics (
    id uuid primary key default gen_random_uuid(),
    name text not null,
    industry text not null default 'dental',
    package text not null default 'starter',
    status text not null default 'trial'
        check (status in ('trial', 'active', 'paused', 'churned')),
    trial_started_at timestamptz not null default now(),
    trial_days integer not null default 7 check (trial_days >= 0),
    expiry date,
    monthly_fee numeric(12, 2) not null default 0 check (monthly_fee >= 0),
    wa_phone_id text not null unique,
    wa_token_enc text,
    telegram_chat_id text,
    google_calendar_id text,
    timezone text not null default 'Africa/Johannesburg',
    work_start time not null default '08:00',
    work_end time not null default '17:00',
    work_days integer[] not null default '{1,2,3,4,5}',
    reminder_offsets_h integer[] not null default '{24,3}',
    wa_templates jsonb not null default '{}',
    brand_voice text,
    created_at timestamptz not null default now(),
    constraint valid_work_window check (work_end > work_start)
);

create table public.services (
    id uuid primary key default gen_random_uuid(),
    clinic_id uuid not null references public.clinics(id) on delete cascade,
    name text not null,
    duration_min integer not null default 30 check (duration_min > 0),
    price numeric(12, 2) not null check (price >= 0),
    unique (clinic_id, name)
);

create table public.patients (
    id uuid primary key default gen_random_uuid(),
    clinic_id uuid not null references public.clinics(id) on delete cascade,
    wa_number text not null,
    name text not null,
    no_show_count integer not null default 0 check (no_show_count >= 0),
    unique (clinic_id, wa_number)
);

create table public.appointments (
    id uuid primary key default gen_random_uuid(),
    clinic_id uuid not null references public.clinics(id) on delete cascade,
    patient_id uuid not null references public.patients(id) on delete restrict,
    service_id uuid not null references public.services(id) on delete restrict,
    starts_at timestamptz not null,
    ends_at timestamptz not null,
    status text not null default 'booked'
        check (status in ('booked', 'confirmed', 'completed', 'cancelled', 'no_show')),
    price numeric(12, 2) not null check (price >= 0),
    medical_aid_name text,
    medical_aid_number text,
    dependent_code text,
    google_event_id text,
    created_at timestamptz not null default now(),
    constraint valid_appointment_window check (ends_at > starts_at)
);

create table public.conversation_states (
    clinic_id uuid not null references public.clinics(id) on delete cascade,
    patient_id uuid not null references public.patients(id) on delete cascade,
    state text not null,
    slot jsonb not null default '{}',
    updated_at timestamptz not null default now(),
    primary key (clinic_id, patient_id)
);

create table public.message_log (
    id uuid primary key default gen_random_uuid(),
    clinic_id uuid not null references public.clinics(id) on delete cascade,
    patient_id uuid references public.patients(id) on delete set null,
    channel text not null check (channel in ('whatsapp', 'telegram')),
    direction text not null check (direction in ('inbound', 'outbound')),
    body text not null,
    raw jsonb not null default '{}',
    created_at timestamptz not null default now()
);

create table public.webhook_events (
    message_id text primary key,
    clinic_id uuid not null references public.clinics(id) on delete cascade,
    payload jsonb not null,
    processed_at timestamptz,
    claimed_at timestamptz,
    attempts integer not null default 0,
    last_error text,
    created_at timestamptz not null default now()
);

create table public.notification_outbox (
    id uuid primary key default gen_random_uuid(),
    clinic_id uuid not null references public.clinics(id) on delete cascade,
    channel text not null check (channel in ('whatsapp', 'telegram')),
    to_id text not null,
    payload jsonb not null,
    status text not null default 'pending'
        check (status in ('pending', 'sent', 'failed')),
    attempts integer not null default 0,
    next_try_at timestamptz not null default now(),
    last_error text,
    created_at timestamptz not null default now()
);

create table public.automation_jobs (
    id uuid primary key default gen_random_uuid(),
    clinic_id uuid not null references public.clinics(id) on delete cascade,
    appointment_id uuid references public.appointments(id) on delete cascade,
    patient_id uuid references public.patients(id) on delete cascade,
    job_type text not null,
    due_at timestamptz not null,
    status text not null default 'pending'
        check (status in ('pending', 'processing', 'completed', 'failed')),
    dedupe_key text not null unique,
    payload jsonb not null default '{}',
    attempts integer not null default 0,
    claimed_at timestamptz,
    last_error text,
    created_at timestamptz not null default now()
);

create table public.daily_throttles (
    clinic_id uuid not null references public.clinics(id) on delete cascade,
    patient_id uuid not null references public.patients(id) on delete cascade,
    throttle_key text not null,
    local_date date not null,
    created_at timestamptz not null default now(),
    primary key (clinic_id, patient_id, throttle_key, local_date)
);

create index appointments_clinic_time_idx
    on public.appointments (clinic_id, starts_at, ends_at)
    where status in ('booked', 'confirmed');
create index appointments_patient_idx on public.appointments (patient_id, starts_at desc);
create index webhook_events_pending_idx on public.webhook_events (created_at)
    where processed_at is null;
create index notification_outbox_pending_idx on public.notification_outbox (next_try_at)
    where status = 'pending';
create index automation_jobs_due_idx on public.automation_jobs (due_at)
    where status = 'pending';

alter table public.clinics enable row level security;
alter table public.services enable row level security;
alter table public.patients enable row level security;
alter table public.appointments enable row level security;
alter table public.conversation_states enable row level security;
alter table public.message_log enable row level security;
alter table public.webhook_events enable row level security;
alter table public.notification_outbox enable row level security;
alter table public.automation_jobs enable row level security;
alter table public.daily_throttles enable row level security;

commit;
