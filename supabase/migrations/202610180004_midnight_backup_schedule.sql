-- Anchor daily backup runs to the configured application timezone's calendar date.
alter table public.crm_backup_state
    add column if not exists last_run_local_date date;

update public.crm_backup_state
   set last_run_local_date = coalesce(
       (last_success_at at time zone 'Africa/Addis_Ababa')::date,
       (now() at time zone 'Africa/Addis_Ababa')::date
   )
 where last_run_local_date is null;

alter table public.crm_backup_state
    alter column last_run_local_date set default (now() at time zone 'Africa/Addis_Ababa')::date,
    alter column last_run_local_date set not null;

comment on column public.crm_backup_state.last_run_local_date is
    'Local calendar date when the current daily backup pair was saved, scheduled once at midnight.';
