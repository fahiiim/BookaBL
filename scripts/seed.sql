-- Development-only deterministic tenant and services. Never run against production data.
insert into public.clinics (
    id, name, industry, package, status, trial_started_at, trial_days,
    monthly_fee, wa_phone_id, telegram_chat_id, timezone
) values (
    '00000000-0000-4000-8000-000000000001', 'BOOKABL Test Dental', 'dental',
    'starter', 'trial', now(), 7, 999.00, 'test-phone-id', '123456789',
    'Africa/Johannesburg'
) on conflict (id) do update set name = excluded.name;

insert into public.services (id, clinic_id, name, duration_min, price) values
    ('00000000-0000-4000-8000-000000000101', '00000000-0000-4000-8000-000000000001',
     'Cleaning', 30, 850.00),
    ('00000000-0000-4000-8000-000000000102', '00000000-0000-4000-8000-000000000001',
     'Consultation', 45, 650.00)
on conflict (id) do update set
    name = excluded.name, duration_min = excluded.duration_min, price = excluded.price;

