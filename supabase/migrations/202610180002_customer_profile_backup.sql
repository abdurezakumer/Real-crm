-- Track the separate customer profile workbook as part of each daily backup.
alter table public.crm_backup_state
    add column if not exists last_customer_storage_path text,
    add column if not exists last_customer_file_size_bytes bigint
        check (last_customer_file_size_bytes is null or last_customer_file_size_bytes >= 0),
    add column if not exists last_customer_count bigint
        check (last_customer_count is null or last_customer_count >= 0),
    add column if not exists last_customer_related_row_count bigint
        check (last_customer_related_row_count is null or last_customer_related_row_count >= 0),
    add column if not exists last_customer_relation_count integer
        check (last_customer_relation_count is null or last_customer_relation_count >= 0);

comment on column public.crm_backup_state.last_customer_storage_path is
    'Private Storage object path for the separately delivered customer profile workbook.';
