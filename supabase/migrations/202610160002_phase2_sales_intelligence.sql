-- Phase 2 is additive. Existing contacts, call logs, campaigns, and follow-ups
-- keep their IDs and relationships.

alter table user_settings add column if not exists queue_filters jsonb not null default '{}'::jsonb;

alter table contact_property_interests
    alter column property_type drop not null;
alter table contact_property_interests
    drop constraint if exists contact_property_interests_property_type_check;
alter table contact_property_interests
    add constraint contact_property_interests_property_type_check
    check (property_type is null or property_type in (
        'APARTMENT', 'COMMERCIAL_SHOP', 'OFFICE', 'HOUSE_VILLA', 'LAND',
        'COMMERCIAL_BUILDING', 'OTHER'
    ));
alter table contact_property_interests
    add column if not exists accepted_locations text[] not null default '{}',
    add column if not exists purchase_purpose text,
    add column if not exists min_bedrooms smallint check (min_bedrooms is null or min_bedrooms >= 0),
    add column if not exists max_bedrooms smallint check (max_bedrooms is null or max_bedrooms >= 0),
    add column if not exists min_area_sqm numeric(12,2) check (min_area_sqm is null or min_area_sqm >= 0),
    add column if not exists max_area_sqm numeric(12,2) check (max_area_sqm is null or max_area_sqm >= 0),
    add column if not exists floor_preference text,
    add column if not exists new_or_resale text,
    add column if not exists profile_complete boolean not null default false;
alter table contact_property_interests
    add constraint contact_property_interests_bedroom_range_check
    check (min_bedrooms is null or max_bedrooms is null or min_bedrooms <= max_bedrooms);
alter table contact_property_interests
    add constraint contact_property_interests_area_range_check
    check (min_area_sqm is null or max_area_sqm is null or min_area_sqm <= max_area_sqm);
alter table contact_property_interests
    add constraint contact_property_interests_purchase_purpose_check
    check (purchase_purpose is null or purchase_purpose in (
        'PERSONAL_RESIDENCE', 'INVESTMENT', 'RENTAL_INCOME', 'COMMERCIAL_BUSINESS',
        'LAND_INVESTMENT', 'OTHER', 'UNKNOWN'
    ));
alter table contact_property_interests
    add constraint contact_property_interests_condition_check
    check (new_or_resale is null or new_or_resale in ('NEW', 'RESALE', 'EITHER', 'UNKNOWN'));

create index if not exists property_interest_type_location_idx
    on contact_property_interests (property_type, lower(preferred_location));
create index if not exists property_interest_alternatives_gin_idx
    on contact_property_interests using gin (accepted_locations);
create index if not exists property_interest_contact_latest_idx
    on contact_property_interests (contact_id, updated_at desc, id desc);
create index if not exists property_interest_match_candidate_idx
    on contact_property_interests (property_type, currency, contact_id);

create table if not exists properties (
    id uuid primary key default gen_random_uuid(),
    project_name text,
    title text not null check (length(trim(title)) > 0),
    property_type text not null check (property_type in (
        'APARTMENT', 'COMMERCIAL_SHOP', 'OFFICE', 'HOUSE_VILLA', 'LAND',
        'COMMERCIAL_BUILDING', 'OTHER'
    )),
    developer_or_seller text,
    location text,
    sub_location text,
    price numeric(16,2) check (price is null or price >= 0),
    currency char(3) not null check (currency ~ '^[A-Z]{3}$'),
    minimum_negotiable_price numeric(16,2)
        check (minimum_negotiable_price is null or minimum_negotiable_price >= 0),
    bedrooms smallint check (bedrooms is null or bedrooms >= 0),
    bathrooms smallint check (bathrooms is null or bathrooms >= 0),
    floor_number text,
    area_sqm numeric(12,2) check (area_sqm is null or area_sqm >= 0),
    available_payment_plans text[] not null default '{}',
    payment_plans_verified boolean not null default false,
    construction_status text not null default 'UNKNOWN' check (construction_status in (
        'UNDER_CONSTRUCTION', 'NEAR_COMPLETION', 'READY_FOR_DELIVERY', 'COMPLETED', 'UNKNOWN'
    )),
    delivery_date date,
    availability_status text not null default 'UNVERIFIED' check (availability_status in (
        'AVAILABLE', 'RESERVED', 'SOLD', 'TEMPORARILY_UNAVAILABLE', 'UNVERIFIED'
    )),
    description text,
    internal_notes text,
    listing_source text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check (minimum_negotiable_price is null or price is null or minimum_negotiable_price <= price)
);
create index if not exists properties_availability_created_idx
    on properties (availability_status, created_at desc, id);
create index if not exists properties_type_price_idx
    on properties (property_type, currency, price) where availability_status = 'AVAILABLE';
create index if not exists properties_location_trgm_idx
    on properties using gin (lower(location) gin_trgm_ops);
create index if not exists properties_title_trgm_idx
    on properties using gin (lower(title) gin_trgm_ops);

create table if not exists property_recommendations (
    id uuid primary key default gen_random_uuid(),
    contact_id uuid not null references contacts(id) on delete cascade,
    property_id uuid not null references properties(id) on delete cascade,
    match_score smallint not null check (match_score between 0 and 100),
    match_explanation jsonb not null default '{}'::jsonb,
    recommendation_status text not null default 'RECOMMENDED' check (recommendation_status in (
        'RECOMMENDED', 'VIEWED', 'INTERESTED', 'VISIT_SCHEDULED', 'REJECTED', 'NO_LONGER_AVAILABLE'
    )),
    user_decision text,
    rejection_reason text,
    recommended_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create index if not exists recommendations_contact_recent_idx
    on property_recommendations (contact_id, recommended_at desc);
create index if not exists recommendations_property_recent_idx
    on property_recommendations (property_id, recommended_at desc);
create index if not exists recommendations_status_recent_idx
    on property_recommendations (recommendation_status, recommended_at desc);

create table if not exists lead_qualifications (
    contact_id uuid primary key references contacts(id) on delete cascade,
    property_requirement_known boolean not null default false,
    budget_disclosed boolean not null default false,
    budget_declined boolean not null default false,
    timeline_confirmed boolean not null default false,
    genuine_interest boolean not null default false,
    next_action text,
    requested_property_visit boolean not null default false,
    qualification_status text not null default 'UNQUALIFIED' check (qualification_status in (
        'UNQUALIFIED', 'IN_PROGRESS', 'QUALIFIED', 'HIGH_PRIORITY_QUALIFIED', 'DISQUALIFIED'
    )),
    updated_by bigint not null,
    updated_at timestamptz not null default now(),
    check (not (budget_disclosed and budget_declined))
);
create index if not exists lead_qualifications_status_idx
    on lead_qualifications (qualification_status, updated_at desc);

create table if not exists lead_scores (
    contact_id uuid primary key references contacts(id) on delete cascade,
    score smallint not null check (score between 0 and 100),
    priority_category text not null check (priority_category in ('A', 'B', 'C', 'NEEDS_INFO')),
    explanation jsonb not null default '{}'::jsonb,
    rule_version text not null default 'phase2-v1',
    calculated_at timestamptz not null default now()
);
create index if not exists lead_scores_priority_idx
    on lead_scores (priority_category, score desc, calculated_at desc);

create table if not exists ai_jobs (
    id uuid primary key default gen_random_uuid(),
    telegram_user_id bigint not null,
    chat_id bigint not null,
    contact_id uuid references contacts(id) on delete set null,
    property_id uuid references properties(id) on delete set null,
    job_type text not null check (job_type in (
        'CALL_SCRIPT', 'STRUCTURE_NOTES', 'OBJECTION', 'FOLLOWUP_DRAFT', 'SALES_ANALYSIS'
    )),
    input_payload jsonb not null default '{}'::jsonb,
    result_payload jsonb,
    status text not null default 'QUEUED' check (status in ('QUEUED', 'PROCESSING', 'COMPLETED', 'FAILED', 'CANCELLED')),
    attempts smallint not null default 0 check (attempts between 0 and 2),
    last_error text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    completed_at timestamptz
);
create index if not exists ai_jobs_pending_idx on ai_jobs (created_at, id)
    where status in ('QUEUED', 'PROCESSING');
create index if not exists ai_jobs_user_recent_idx on ai_jobs (telegram_user_id, created_at desc);
create index if not exists ai_jobs_retention_idx on ai_jobs (created_at, id)
    where status in ('COMPLETED', 'FAILED', 'CANCELLED');

create table if not exists ai_usage (
    id bigint generated always as identity primary key,
    job_id uuid not null references ai_jobs(id) on delete cascade,
    telegram_user_id bigint not null,
    operation text not null,
    attempt smallint not null default 1 check (attempt between 1 and 2),
    status text not null check (status in ('RESERVED', 'SUCCEEDED', 'FAILED')),
    input_tokens integer,
    output_tokens integer,
    recorded_at timestamptz not null default now(),
    unique (job_id, attempt)
);
create index if not exists ai_usage_daily_user_idx
    on ai_usage (telegram_user_id, recorded_at desc);

alter table audit_events add column if not exists property_id uuid references properties(id) on delete set null;
create index if not exists audit_events_property_idx on audit_events (property_id, created_at desc);

do $$
declare table_name text;
begin
    foreach table_name in array array[
        'properties', 'property_recommendations', 'lead_qualifications', 'lead_scores', 'ai_jobs', 'ai_usage'
    ] loop
        execute format('alter table public.%I enable row level security', table_name);
    end loop;
end $$;
