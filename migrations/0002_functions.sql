begin;

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
        );
    end loop;

    insert into public.automation_jobs (
        clinic_id, appointment_id, patient_id, job_type, due_at, dedupe_key
    ) values (
        p_clinic_id, v_appointment.id, p_patient_id, 'no_show_check',
        p_starts_at + interval '15 minutes', 'no-show:' || v_appointment.id::text
    );

    insert into public.notification_outbox (clinic_id, channel, to_id, payload)
    values (p_clinic_id, 'whatsapp', p_whatsapp_to, p_whatsapp_payload);

    if p_telegram_to is not null and p_telegram_to <> '' then
        insert into public.notification_outbox (clinic_id, channel, to_id, payload)
        values (p_clinic_id, 'telegram', p_telegram_to, p_telegram_payload);
    end if;

    return v_appointment;
end;
$$;

create or replace function public.pop_unprocessed_events(n integer default 20)
returns setof public.webhook_events
language sql
security definer
set search_path = public
as $$
    with selected as (
        select message_id
        from public.webhook_events
        where processed_at is null
          and (claimed_at is null or claimed_at < clock_timestamp() - interval '5 minutes')
        order by created_at
        for update skip locked
        limit greatest(n, 0)
    )
    update public.webhook_events event
    set claimed_at = clock_timestamp(), attempts = event.attempts + 1
    from selected
    where event.message_id = selected.message_id
    returning event.*;
$$;

create or replace function public.pop_pending_outbox(n integer default 20)
returns setof public.notification_outbox
language sql
security definer
set search_path = public
as $$
    with selected as (
        select id
        from public.notification_outbox
        where status = 'pending' and next_try_at <= clock_timestamp()
        order by next_try_at, created_at
        for update skip locked
        limit greatest(n, 0)
    )
    update public.notification_outbox item
    set attempts = item.attempts + 1,
        next_try_at = clock_timestamp() + interval '5 minutes'
    from selected
    where item.id = selected.id
    returning item.*;
$$;

create or replace function public.pop_due_jobs(n integer default 20)
returns setof public.automation_jobs
language sql
security definer
set search_path = public
as $$
    with selected as (
        select id
        from public.automation_jobs
        where (status = 'pending' and due_at <= clock_timestamp())
           or (status = 'processing' and claimed_at < clock_timestamp() - interval '5 minutes')
        order by due_at, created_at
        for update skip locked
        limit greatest(n, 0)
    )
    update public.automation_jobs job
    set status = 'processing',
        claimed_at = clock_timestamp(),
        attempts = job.attempts + 1
    from selected
    where job.id = selected.id
    returning job.*;
$$;

create or replace function public.mark_no_show(p_appointment_id uuid)
returns public.appointments
language plpgsql
security definer
set search_path = public
as $$
declare
    v_appointment public.appointments%rowtype;
begin
    update public.appointments
    set status = 'no_show'
    where id = p_appointment_id and status = 'booked'
    returning * into v_appointment;

    if not found then
        return null;
    end if;

    update public.patients
    set no_show_count = no_show_count + 1
    where id = v_appointment.patient_id;

    return v_appointment;
end;
$$;

revoke all on function public.finalize_booking(
    uuid, uuid, uuid, timestamptz, timestamptz, text, text, text, text, jsonb, text, jsonb
) from public, anon, authenticated;
revoke all on function public.pop_unprocessed_events(integer) from public, anon, authenticated;
revoke all on function public.pop_pending_outbox(integer) from public, anon, authenticated;
revoke all on function public.pop_due_jobs(integer) from public, anon, authenticated;
revoke all on function public.mark_no_show(uuid) from public, anon, authenticated;
grant execute on function public.finalize_booking(
    uuid, uuid, uuid, timestamptz, timestamptz, text, text, text, text, jsonb, text, jsonb
) to service_role;
grant execute on function public.pop_unprocessed_events(integer) to service_role;
grant execute on function public.pop_pending_outbox(integer) to service_role;
grant execute on function public.pop_due_jobs(integer) to service_role;
grant execute on function public.mark_no_show(uuid) to service_role;

commit;
