begin;

alter table public.clinics
    add column if not exists google_review_url text;

create table if not exists public.faq_entries (
    id uuid primary key default gen_random_uuid(),
    clinic_id uuid not null references public.clinics(id) on delete cascade,
    question text not null,
    answer text not null,
    category text not null default 'General',
    active boolean not null default true,
    created_at timestamptz not null default now()
);

create table if not exists public.patient_consents (
    id uuid primary key default gen_random_uuid(),
    clinic_id uuid not null references public.clinics(id) on delete cascade,
    patient_id uuid not null references public.patients(id) on delete cascade,
    appointment_id uuid references public.appointments(id) on delete set null,
    consent_type text not null,
    consent_text text not null,
    consent_version text not null,
    consented_at timestamptz not null default now()
);

create index if not exists faq_entries_clinic_category_idx
    on public.faq_entries (clinic_id, category, created_at desc);
create index if not exists patient_consents_patient_idx
    on public.patient_consents (clinic_id, patient_id, consented_at desc);

alter table public.faq_entries enable row level security;
alter table public.patient_consents enable row level security;

revoke all on table public.faq_entries from anon, authenticated;
revoke all on table public.patient_consents from anon, authenticated;
grant select, insert, update, delete on table public.faq_entries to service_role;
grant select, insert, update, delete on table public.patient_consents to service_role;

commit;
