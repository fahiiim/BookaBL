begin;

create index if not exists automation_jobs_type_status_due_idx
    on public.automation_jobs (job_type, status, due_at);

comment on column public.automation_jobs.job_type is
    'Automation discriminator; review_request sends a Google review invitation two hours after an appointment ends.';

create or replace function public.finalize_booking(
    p_clinic_id uuid,
    p_patient_id uuid,
    p_service_id uuid,
    p_starts_at timestamptz,
    p_ends_at timestamptz,
    p_medical_aid_name text,
    p_medical_aid_number text,
    p_dependent_code text,
    p_whatsapp_to text,
    p_whatsapp_payload jsonb,
    p_telegram_to text,
    p_telegram_payload jsonb
) returns public.appointments
language plpgsql
security definer
set search_path = public
as $$
declare
    v_clinic public.clinics%rowtype;
    v_service public.services%rowtype;
    v_appointment public.appointments%rowtype;
    v_offset integer;
begin
    select * into strict v_clinic
    from public.clinics
    where id = p_clinic_id
    for update;

    select * into strict v_service
    from public.services
    where id = p_service_id and clinic_id = p_clinic_id;

    if not exists (
        select 1 from public.patients
        where id = p_patient_id and clinic_id = p_clinic_id
    ) then
        raise exception using errcode = '23503', message = 'patient does not belong to clinic';
    end if;

    if p_ends_at <= p_starts_at
       or p_ends_at <> p_starts_at + make_interval(mins => v_service.duration_min) then
        raise exception using errcode = '22023', message = 'invalid appointment duration';
    end if;

    if exists (
        select 1 from public.appointments a
        where a.clinic_id = p_clinic_id
          and a.status in ('booked', 'confirmed')
          and tstzrange(a.starts_at, a.ends_at, '[)') && tstzrange(p_starts_at, p_ends_at, '[)')
    ) then
        raise exception using errcode = '23P01', message = 'booking slot is no longer available';
    end if;

    insert into public.appointments (
        clinic_id, patient_id, service_id, starts_at, ends_at, status, price,
        medical_aid_name, medical_aid_number, dependent_code
    ) values (
        p_clinic_id, p_patient_id, p_service_id, p_starts_at, p_ends_at, 'booked',
        v_service.price, p_medical_aid_name, p_medical_aid_number, p_dependent_code
    ) returning * into v_appointment;

    foreach v_offset in array v_clinic.reminder_offsets_h loop
        insert into public.automation_jobs (
            clinic_id, appointment_id, patient_id, job_type, due_at, dedupe_key
        ) values (
            p_clinic_id, v_appointment.id, p_patient_id, 'reminder',
            p_starts_at - make_interval(hours => v_offset),
            'reminder:' || v_appointment.id::text || ':' || v_offset::text
        ) on conflict (dedupe_key) do nothing;
    end loop;

    insert into public.automation_jobs (
        clinic_id, appointment_id, patient_id, job_type, due_at, dedupe_key
    ) values (
        p_clinic_id, v_appointment.id, p_patient_id, 'no_show_check',
        p_starts_at + interval '15 minutes', 'no-show:' || v_appointment.id::text
    ) on conflict (dedupe_key) do nothing;

    insert into public.automation_jobs (
        clinic_id, appointment_id, patient_id, job_type, due_at, dedupe_key
    ) values (
        p_clinic_id, v_appointment.id, p_patient_id, 'review_request',
        p_ends_at + interval '2 hours', 'review:' || v_appointment.id::text
    ) on conflict (dedupe_key) do nothing;

    insert into public.notification_outbox (clinic_id, channel, to_id, payload)
    values (p_clinic_id, 'whatsapp', p_whatsapp_to, p_whatsapp_payload);

    if p_telegram_to is not null and p_telegram_to <> '' then
        insert into public.notification_outbox (clinic_id, channel, to_id, payload)
        values (p_clinic_id, 'telegram', p_telegram_to, p_telegram_payload);
    end if;

    return v_appointment;
end;
$$;

revoke all on function public.finalize_booking(
    uuid, uuid, uuid, timestamptz, timestamptz, text, text, text, text, jsonb, text, jsonb
) from public, anon, authenticated;
grant execute on function public.finalize_booking(
    uuid, uuid, uuid, timestamptz, timestamptz, text, text, text, text, jsonb, text, jsonb
) to service_role;

commit;
