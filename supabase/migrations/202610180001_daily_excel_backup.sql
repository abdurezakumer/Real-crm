-- Durable claim/retry state for the scheduled full CRM Excel backup.
create table if not exists crm_backup_state (
    id boolean primary key default true check (id),
    last_success_at timestamptz,
    last_notified_at timestamptz,
    last_storage_path text,
    last_file_size_bytes bigint check (last_file_size_bytes is null or last_file_size_bytes >= 0),
    last_table_count integer check (last_table_count is null or last_table_count >= 0),
    last_row_count bigint check (last_row_count is null or last_row_count >= 0),
    notification_pending boolean not null default false,
    lease_until timestamptz,
    retry_after timestamptz,
    last_error text,
    updated_at timestamptz not null default now()
);

insert into crm_backup_state (id)
values (true)
on conflict (id) do nothing;

alter table crm_backup_state enable row level security;

comment on table crm_backup_state is
    'Private scheduler state for the daily full CRM Excel snapshot and Telegram delivery retry.';
