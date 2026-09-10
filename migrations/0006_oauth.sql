begin;

create table if not exists public.oauth_tokens (
    id uuid primary key default gen_random_uuid(),
    clinic_id uuid not null references public.clinics(id) on delete cascade,
    provider text not null default 'google',
    refresh_token_encrypted text not null,
    access_token text,
    token_expires_at timestamptz,
    scope text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (clinic_id, provider)
);

alter table public.clinics
    add column if not exists google_oauth_connected boolean not null default false;

alter table public.oauth_tokens enable row level security;
revoke all on table public.oauth_tokens from anon, authenticated;
grant select, insert, update, delete on table public.oauth_tokens to service_role;

commit;
