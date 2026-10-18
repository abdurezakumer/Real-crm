create extension if not exists pgcrypto;
create extension if not exists pg_trgm;

do $$ begin
    create type lead_status as enum (
        'NEW', 'CONTACTED', 'INTERESTED', 'QUALIFIED', 'FOLLOW_UP_REQUIRED',
        'MEETING_SCHEDULED', 'PROPERTY_VISIT_SCHEDULED', 'NEGOTIATION',
        'CONVERTED', 'NOT_INTERESTED', 'DISQUALIFIED', 'DO_NOT_CONTACT'
    );
exception when duplicate_object then null; end $$;

do $$ begin
    create type contact_eligibility as enum (
        'ELIGIBLE', 'TEMPORARILY_PAUSED', 'INVALID_NUMBER', 'DO_NOT_CONTACT'
    );
exception when duplicate_object then null; end $$;

do $$ begin
    create type interest_level as enum ('UNKNOWN', 'LOW', 'MEDIUM', 'HIGH', 'VERY_HIGH');
exception when duplicate_object then null; end $$;

create table if not exists campaigns (
    id uuid primary key default gen_random_uuid(),
    name text not null,
    description text,
    source text,
    status text not null default 'ACTIVE' check (status in ('ACTIVE', 'PAUSED', 'COMPLETED', 'ARCHIVED')),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create unique index if not exists campaigns_active_name_uq on campaigns (lower(name)) where status <> 'ARCHIVED';
create index if not exists campaigns_name_trgm_idx on campaigns using gin (lower(name) gin_trgm_ops);

create table if not exists contacts (
    id uuid primary key default gen_random_uuid(),
    source_row_number integer,
    name text not null check (length(trim(name)) > 0),
    phone_number text not null,
    normalized_phone text,
    email text,
    lead_status lead_status not null default 'NEW',
    contact_eligibility contact_eligibility not null default 'ELIGIBLE',
    interest_level interest_level not null default 'UNKNOWN',
    conversion_state text not null default 'NOT_CONVERTED' check (conversion_state in ('NOT_CONVERTED', 'CONVERTED')),
    first_contact_at timestamptz,
    last_called_at timestamptz,
    next_follow_up_at timestamptz,
    total_call_attempts integer not null default 0 check (total_call_attempts >= 0),
    last_call_outcome text,
    follow_up_state text not null default 'NONE' check (follow_up_state in ('NONE', 'PENDING', 'OVERDUE')),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create unique index if not exists contacts_normalized_phone_uq
    on contacts (normalized_phone) where normalized_phone is not null;
create index if not exists contacts_status_idx on contacts (lead_status, id);
create index if not exists contacts_eligibility_idx on contacts (contact_eligibility, id);
create index if not exists contacts_interest_idx on contacts (interest_level, updated_at desc);
create index if not exists contacts_last_called_idx on contacts (last_called_at nulls first, id);
create index if not exists contacts_next_followup_idx on contacts (next_follow_up_at) where next_follow_up_at is not null;
create index if not exists contacts_name_trgm_idx on contacts using gin (lower(name) gin_trgm_ops);

create table if not exists campaign_contacts (
    campaign_id uuid not null references campaigns(id) on delete cascade,
    contact_id uuid not null references contacts(id) on delete cascade,
    campaign_status text not null default 'ACTIVE' check (campaign_status in ('ACTIVE', 'PAUSED', 'COMPLETED')),
    created_at timestamptz not null default now(),
    primary key (campaign_id, contact_id)
);
create index if not exists campaign_contacts_contact_idx on campaign_contacts (contact_id, campaign_id);

create table if not exists call_sessions (
    id uuid primary key default gen_random_uuid(),
    telegram_user_id bigint not null,
    campaign_id uuid references campaigns(id) on delete set null,
    current_contact_id uuid references contacts(id) on delete set null,
    started_at timestamptz not null default now(),
    ended_at timestamptz,
    calls_attempted integer not null default 0 check (calls_attempted >= 0),
    status text not null default 'ACTIVE' check (status in ('ACTIVE', 'PAUSED', 'ENDED'))
);
create index if not exists call_sessions_user_active_idx
    on call_sessions (telegram_user_id, started_at desc) where status in ('ACTIVE', 'PAUSED');
create unique index if not exists call_sessions_one_open_per_user_idx
    on call_sessions (telegram_user_id) where status in ('ACTIVE', 'PAUSED');

create table if not exists session_contacts (
    session_id uuid not null references call_sessions(id) on delete cascade,
    contact_id uuid not null references contacts(id) on delete cascade,
    disposition text not null check (disposition in ('PRESENTED', 'SKIPPED', 'CALLED')),
    selected_at timestamptz not null default now(),
    primary key (session_id, contact_id)
);

create table if not exists call_logs (
    id uuid primary key default gen_random_uuid(),
    contact_id uuid not null references contacts(id) on delete restrict,
    campaign_id uuid references campaigns(id) on delete set null,
    call_session_id uuid references call_sessions(id) on delete set null,
    outcome_category text not null check (outcome_category in ('CONNECTION', 'SALES_INTEREST', 'PROPERTY_REQUIREMENTS', 'FOLLOW_UP', 'DISQUALIFICATION')),
    outcome_code text not null,
    connection_result text,
    started_at timestamptz,
    ended_at timestamptz not null default now(),
    duration_seconds integer check (duration_seconds is null or duration_seconds >= 0),
    notes text,
    telegram_update_id bigint unique,
    created_at timestamptz not null default now()
);
create index if not exists call_logs_contact_created_idx on call_logs (contact_id, created_at desc);
create index if not exists call_logs_campaign_created_idx on call_logs (campaign_id, created_at desc);
create index if not exists call_logs_created_idx on call_logs (created_at desc);

create table if not exists follow_ups (
    id uuid primary key default gen_random_uuid(),
    contact_id uuid not null references contacts(id) on delete restrict,
    campaign_id uuid references campaigns(id) on delete set null,
    follow_up_type text not null,
    scheduled_at timestamptz not null,
    status text not null default 'PENDING' check (status in ('PENDING', 'COMPLETED', 'RESCHEDULED', 'CANCELLED', 'MISSED')),
    notes text,
    completed_at timestamptz,
    rescheduled_to uuid references follow_ups(id) on delete set null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create index if not exists follow_ups_due_idx on follow_ups (scheduled_at, id) where status = 'PENDING';
create index if not exists follow_ups_contact_idx on follow_ups (contact_id, scheduled_at desc);

create table if not exists contact_notes (
    id uuid primary key default gen_random_uuid(),
    contact_id uuid not null references contacts(id) on delete cascade,
    call_log_id uuid references call_logs(id) on delete set null,
    note_type text not null default 'GENERAL',
    note_text text not null,
    created_at timestamptz not null default now()
);
create index if not exists contact_notes_contact_idx on contact_notes (contact_id, created_at desc);

create table if not exists contact_property_interests (
    id uuid primary key default gen_random_uuid(),
    contact_id uuid not null references contacts(id) on delete cascade,
    property_type text not null check (property_type in ('APARTMENT', 'COMMERCIAL_SHOP', 'OFFICE', 'HOUSE_VILLA', 'LAND', 'OTHER')),
    preferred_location text,
    min_budget numeric(14,2),
    max_budget numeric(14,2),
    currency char(3) not null default 'ETB',
    bedrooms smallint check (bedrooms is null or bedrooms >= 0),
    preferred_payment_plan text,
    readiness text,
    expected_timeline text,
    additional_requirements text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check (min_budget is null or max_budget is null or min_budget <= max_budget)
);
create index if not exists property_interest_type_idx on contact_property_interests (property_type, contact_id);
create index if not exists property_interest_location_trgm_idx on contact_property_interests using gin (lower(preferred_location) gin_trgm_ops);
create index if not exists property_interest_budget_idx on contact_property_interests (currency, min_budget, max_budget);

create table if not exists user_settings (
    telegram_user_id bigint primary key,
    timezone text not null default 'Africa/Addis_Ababa',
    currency char(3) not null default 'ETB',
    daily_call_target integer not null default 50 check (daily_call_target > 0),
    attempt_limit integer not null default 8 check (attempt_limit > 0),
    allow_call_during_future_followup boolean not null default false,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists import_jobs (
    id uuid primary key default gen_random_uuid(),
    telegram_user_id bigint not null,
    chat_id bigint not null,
    telegram_file_id text,
    original_filename text not null,
    storage_path text,
    file_sha256 text,
    campaign_id uuid references campaigns(id) on delete set null,
    import_policy text not null default 'ADD_TO_CAMPAIGN' check (import_policy in ('SKIP_EXISTING', 'UPDATE_EXISTING', 'ADD_TO_CAMPAIGN')),
    status text not null default 'RECEIVED' check (status in ('RECEIVED', 'VALIDATING', 'PREVIEW', 'QUEUED', 'PROCESSING', 'COMPLETED', 'FAILED', 'CANCELLED')),
    total_rows integer,
    imported_rows integer not null default 0,
    skipped_rows integer not null default 0,
    duplicate_rows integer not null default 0,
    invalid_rows integer not null default 0,
    next_row_number integer not null default 2,
    header_mapping jsonb,
    preview jsonb,
    error_summary jsonb not null default '[]'::jsonb,
    created_at timestamptz not null default now(),
    completed_at timestamptz,
    updated_at timestamptz not null default now()
);
create index if not exists import_jobs_pending_idx on import_jobs (created_at) where status in ('RECEIVED', 'VALIDATING', 'QUEUED', 'PROCESSING');

create table if not exists export_jobs (
    id uuid primary key default gen_random_uuid(),
    telegram_user_id bigint not null,
    chat_id bigint not null,
    export_format text not null check (export_format in ('csv', 'xlsx')),
    filter_name text not null check (filter_name in ('eligible', 'uncalled', 'interested', 'due', 'converted', 'all')),
    campaign_id uuid references campaigns(id) on delete set null,
    date_from date,
    date_to_exclusive date,
    timezone text not null,
    status text not null default 'QUEUED' check (status in ('QUEUED', 'PROCESSING', 'COMPLETED', 'FAILED')),
    attempts integer not null default 0 check (attempts >= 0),
    last_error text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    completed_at timestamptz,
    check (date_from is null or date_to_exclusive is null or date_from < date_to_exclusive)
);
create index if not exists export_jobs_pending_idx on export_jobs (created_at)
    where status in ('QUEUED', 'PROCESSING');

create table if not exists processed_telegram_updates (
    update_id bigint primary key,
    telegram_user_id bigint,
    processed_at timestamptz not null default now()
);
create index if not exists processed_updates_retention_idx on processed_telegram_updates (processed_at);

create table if not exists telegram_rate_limits (
    telegram_user_id bigint primary key,
    window_started_at timestamptz not null,
    request_count integer not null default 0 check (request_count >= 0),
    updated_at timestamptz not null default now()
);

create table if not exists user_input_state (
    telegram_user_id bigint primary key,
    purpose text not null,
    contact_id uuid references contacts(id) on delete cascade,
    payload jsonb not null default '{}'::jsonb,
    expires_at timestamptz not null default now() + interval '30 minutes',
    updated_at timestamptz not null default now()
);

create table if not exists reminder_deliveries (
    follow_up_id uuid primary key references follow_ups(id) on delete cascade,
    telegram_user_id bigint not null,
    status text not null default 'PENDING' check (status in ('PENDING', 'SENDING', 'SENT', 'FAILED')),
    attempts integer not null default 0,
    last_error text,
    sent_at timestamptz,
    updated_at timestamptz not null default now()
);

create table if not exists audit_events (
    id uuid primary key default gen_random_uuid(),
    telegram_user_id bigint not null,
    contact_id uuid references contacts(id) on delete set null,
    event_type text not null,
    old_value jsonb,
    new_value jsonb,
    telegram_update_id bigint,
    created_at timestamptz not null default now()
);
create index if not exists audit_events_contact_idx on audit_events (contact_id, created_at desc);

-- Database-side safeguards still apply if a handler is retried or two callbacks race.
create index if not exists call_sessions_current_idx on call_sessions (current_contact_id) where current_contact_id is not null;

-- The bot uses a restricted server-side database connection. No browser/anon policy is granted.
do $$
declare table_name text;
begin
    foreach table_name in array array[
        'campaigns', 'contacts', 'campaign_contacts', 'call_sessions', 'session_contacts',
        'call_logs', 'follow_ups', 'contact_notes', 'contact_property_interests',
        'user_settings', 'import_jobs', 'processed_telegram_updates', 'telegram_rate_limits',
        'user_input_state', 'reminder_deliveries', 'audit_events', 'export_jobs'
    ] loop
        execute format('alter table public.%I enable row level security', table_name);
    end loop;
end $$;

