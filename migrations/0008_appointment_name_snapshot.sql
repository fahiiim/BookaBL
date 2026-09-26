begin;

alter table public.appointments
    add column if not exists patient_name text;

update public.appointments appointment
set patient_name = patient.name
from public.patients patient
where patient.id = appointment.patient_id
  and appointment.patient_name is null;

create or replace function public.snapshot_appointment_patient_name()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
    if new.patient_name is null or btrim(new.patient_name) = '' then
        select patient.name into strict new.patient_name
        from public.patients patient
        where patient.id = new.patient_id
          and patient.clinic_id = new.clinic_id;
    end if;
    return new;
end;
$$;

drop trigger if exists appointments_snapshot_patient_name on public.appointments;
create trigger appointments_snapshot_patient_name
before insert on public.appointments
for each row execute function public.snapshot_appointment_patient_name();

alter table public.appointments
    alter column patient_name set not null;

comment on column public.appointments.patient_name is
    'Immutable patient name captured when the appointment was created.';

revoke all on function public.snapshot_appointment_patient_name()
    from public, anon, authenticated;
grant execute on function public.snapshot_appointment_patient_name()
    to service_role;

commit;
