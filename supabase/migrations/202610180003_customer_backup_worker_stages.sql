-- Split the daily backup into two worker runs to fit serverless request limits.
alter table public.crm_backup_state
    add column if not exists customer_backup_pending boolean not null default false;
