begin;

alter table public.clinics
    alter column reminder_offsets_h set default '{24,2}';

update public.clinics
set reminder_offsets_h = '{24,2}'
where reminder_offsets_h = '{24,3}';

create or replace function public.reschedule_appointment(
    p_appointment_id uuid,
    p_patient_id uuid,
    p_starts_at timestamptz,
    p_ends_at timestamptz
) returns public.appointments
language plpgsql
security definer
set search_path = public
as $$
declare
    v_appointment public.appointments%rowtype;
    v_service public.services%rowtype;
    v_clinic public.clinics%rowtype;
    v_offset integer;
    v_suffix text := p_starts_at::text;
begin
    select * into v_appointment
    from public.appointments
    where id = p_appointment_id
      and patient_id = p_patient_id
      and status in ('booked', 'confirmed')
    for update;

    if not found then
        return null;
    end if;

    select * into strict v_service
    from public.services
    where id = v_appointment.service_id
      and clinic_id = v_appointment.clinic_id;

    select * into strict v_clinic
    from public.clinics
    where id = v_appointment.clinic_id;

    if p_ends_at <= p_starts_at
       or p_ends_at <> p_starts_at + make_interval(mins => v_service.duration_min) then
        raise exception using errcode = '22023', message = 'invalid appointment duration';
    end if;

    if exists (
        select 1 from public.appointments a
        where a.clinic_id = v_appointment.clinic_id
          and a.id <> p_appointment_id
          and a.status in ('booked', 'confirmed')
          and tstzrange(a.starts_at, a.ends_at, '[)')
              && tstzrange(p_starts_at, p_ends_at, '[)')
    ) then
        raise exception using errcode = '23P01', message = 'booking slot is no longer available';
    end if;

    update public.appointments
    set starts_at = p_starts_at,
        ends_at = p_ends_at,
        status = 'booked'
    where id = p_appointment_id
    returning * into v_appointment;

    delete from public.automation_jobs
    where appointment_id = p_appointment_id
      and job_type in ('reminder', 'no_show_check', 'review_request');

    foreach v_offset in array v_clinic.reminder_offsets_h loop
        insert into public.automation_jobs (
            clinic_id, appointment_id, patient_id, job_type, due_at, dedupe_key
        ) values (
            v_appointment.clinic_id, v_appointment.id, p_patient_id, 'reminder',
            p_starts_at - make_interval(hours => v_offset),
            'reminder:' || v_appointment.id::text || ':' || v_offset::text || ':' || v_suffix
        );
    end loop;

    insert into public.automation_jobs (
        clinic_id, appointment_id, patient_id, job_type, due_at, dedupe_key
    ) values (
        v_appointment.clinic_id, v_appointment.id, p_patient_id, 'no_show_check',
        p_starts_at + interval '15 minutes',
        'attendance:' || v_appointment.id::text || ':' || v_suffix
    );

    insert into public.automation_jobs (
        clinic_id, appointment_id, patient_id, job_type, due_at, dedupe_key
    ) values (
        v_appointment.clinic_id, v_appointment.id, p_patient_id, 'review_request',
        p_ends_at + interval '2 hours',
        'review:' || v_appointment.id::text || ':' || v_suffix
    );

    return v_appointment;
end;
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
    where id = p_appointment_id and status in ('booked', 'confirmed')
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

revoke all on function public.reschedule_appointment(
    uuid, uuid, timestamptz, timestamptz
) from public, anon, authenticated;
grant execute on function public.reschedule_appointment(
    uuid, uuid, timestamptz, timestamptz
) to service_role;

revoke all on function public.mark_no_show(uuid) from public, anon, authenticated;
grant execute on function public.mark_no_show(uuid) to service_role;

commit;
