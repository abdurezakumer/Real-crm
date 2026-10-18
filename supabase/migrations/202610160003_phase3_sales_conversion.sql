-- Phase 3 is additive. Existing contacts, calls, properties, and follow-ups keep
-- their identities and remain the source records for earlier workflow stages.

create table if not exists sales_pipeline_stages (
    stage_code text primary key,
    display_name text not null,
    sort_order smallint not null unique,
    is_terminal boolean not null default false,
    enabled boolean not null default true
);

insert into sales_pipeline_stages (stage_code, display_name, sort_order, is_terminal) values
    ('NEW_OPPORTUNITY', 'New opportunity', 10, false),
    ('BUYER_QUALIFICATION', 'Buyer qualification', 20, false),
    ('PROPERTY_MATCHING', 'Property matching', 30, false),
    ('PROPERTY_INFORMATION_SHARED', 'Property information shared', 40, false),
    ('FOLLOW_UP', 'Follow-up', 50, false),
    ('VISIT_SCHEDULED', 'Visit scheduled', 60, false),
    ('VISIT_COMPLETED', 'Visit completed', 70, false),
    ('NEGOTIATION', 'Negotiation', 80, false),
    ('RESERVATION_IN_PROGRESS', 'Reservation in progress', 90, false),
    ('BOOKED', 'Booked', 100, false),
    ('WON', 'Won', 110, true),
    ('LOST', 'Lost', 120, true),
    ('ON_HOLD', 'On hold', 130, false)
on conflict (stage_code) do nothing;

create table if not exists sales_opportunities (
    id uuid primary key default gen_random_uuid(),
    contact_id uuid not null references contacts(id) on delete restrict,
    primary_property_id uuid references properties(id) on delete restrict,
    campaign_id uuid references campaigns(id) on delete set null,
    current_stage text not null default 'NEW_OPPORTUNITY'
        references sales_pipeline_stages(stage_code),
    estimated_sale_value numeric(16,2) check (estimated_sale_value is null or estimated_sale_value >= 0),
    currency char(3) not null default 'ETB' check (currency ~ '^[A-Z]{3}$'),
    expected_closing_date date,
    next_action text,
    next_action_due timestamptz,
    assigned_telegram_user_id bigint,
    created_by bigint not null,
    final_outcome text check (final_outcome is null or final_outcome in ('WON', 'LOST')),
    lost_reason text check (lost_reason is null or lost_reason in (
        'PRICE_TOO_HIGH', 'DOWN_PAYMENT_TOO_HIGH', 'PAYMENT_PLAN_UNSUITABLE',
        'PROPERTY_LOCATION', 'PROPERTY_TYPE_MISMATCH', 'PROPERTY_SIZE_MISMATCH',
        'BUYER_NOT_READY', 'BUYER_PURCHASED_ELSEWHERE', 'BUYER_UNRESPONSIVE',
        'TRUST_OR_DOCUMENTATION_CONCERN', 'PROPERTY_UNAVAILABLE', 'COMPETITOR_SELECTED',
        'DUPLICATE_OPPORTUNITY', 'OTHER', 'UNKNOWN', 'UNCONFIRMED'
    )),
    lost_explanation text,
    future_followup_allowed boolean not null default true,
    notes text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    last_interaction_at timestamptz not null default now(),
    closed_at timestamptz,
    check ((final_outcome is null and closed_at is null) or (final_outcome is not null and closed_at is not null)),
    check (final_outcome <> 'LOST' or lost_reason is not null),
    check ((final_outcome is null and current_stage not in ('WON', 'LOST'))
        or (final_outcome is not null and current_stage = final_outcome))
);
create index if not exists sales_opportunities_contact_idx
    on sales_opportunities (contact_id, created_at desc, id desc);
create index if not exists sales_opportunities_stage_due_idx
    on sales_opportunities (current_stage, next_action_due, id)
    where final_outcome is null;
create index if not exists sales_opportunities_closing_idx
    on sales_opportunities (expected_closing_date, id)
    where final_outcome is null and expected_closing_date is not null;
create index if not exists sales_opportunities_property_idx
    on sales_opportunities (primary_property_id, current_stage, id);

create table if not exists opportunity_events (
    id uuid primary key default gen_random_uuid(),
    opportunity_id uuid not null references sales_opportunities(id) on delete restrict,
    event_type text not null check (event_type in (
        'CREATED', 'STAGE_CHANGED', 'NOTE_ADDED', 'ACTION_UPDATED', 'PROPERTY_LINKED',
        'VALUE_UPDATED', 'CLOSING_DATE_UPDATED', 'WON', 'LOST', 'REOPENED', 'AI_SUGGESTION'
    )),
    previous_stage text references sales_pipeline_stages(stage_code),
    new_stage text references sales_pipeline_stages(stage_code),
    details jsonb not null default '{}'::jsonb,
    actor_telegram_user_id bigint not null,
    telegram_update_id bigint unique,
    created_at timestamptz not null default now()
);
create index if not exists opportunity_events_timeline_idx
    on opportunity_events (opportunity_id, created_at desc, id desc);
alter table opportunity_events drop constraint if exists opportunity_events_event_type_check;
alter table opportunity_events add constraint opportunity_events_event_type_check check (event_type in (
    'CREATED', 'STAGE_CHANGED', 'NOTE_ADDED', 'ACTION_UPDATED', 'PROPERTY_LINKED',
    'VALUE_UPDATED', 'CLOSING_DATE_UPDATED', 'WON', 'LOST', 'REOPENED', 'AI_SUGGESTION',
    'VISIT_UPDATED', 'NEGOTIATION_UPDATED', 'RESERVATION_UPDATED'
));

alter table follow_ups
    add column if not exists opportunity_id uuid references sales_opportunities(id) on delete restrict,
    add column if not exists reason text,
    add column if not exists priority smallint not null default 50 check (priority between 0 and 100),
    add column if not exists completion_outcome text,
    add column if not exists completed_by bigint;
create index if not exists follow_ups_opportunity_idx
    on follow_ups (opportunity_id, scheduled_at desc) where opportunity_id is not null;
create index if not exists follow_ups_due_priority_idx
    on follow_ups (scheduled_at, priority desc, id) where status = 'PENDING';

create table if not exists property_visits (
    id uuid primary key default gen_random_uuid(),
    opportunity_id uuid not null references sales_opportunities(id) on delete restrict,
    contact_id uuid not null references contacts(id) on delete restrict,
    property_id uuid not null references properties(id) on delete restrict,
    scheduled_at timestamptz not null,
    meeting_point text,
    internal_note text,
    buyer_instructions text,
    confirmation_status text not null default 'NOT_CONFIRMED' check (confirmation_status in (
        'NOT_CONFIRMED', 'CONFIRMED', 'RESCHEDULE_REQUESTED', 'CANCELLED'
    )),
    visit_status text not null default 'SCHEDULED' check (visit_status in (
        'SCHEDULED', 'BUYER_ARRIVED', 'COMPLETED', 'DID_NOT_ATTEND', 'CANCELLED', 'RESCHEDULED'
    )),
    attendance_outcome text check (attendance_outcome is null or attendance_outcome in (
        'ATTENDED', 'DID_NOT_ATTEND', 'RESCHEDULE_REQUIRED'
    )),
    duplicate_confirmed boolean not null default false,
    buyer_liked_property boolean,
    feedback jsonb not null default '{}'::jsonb,
    next_action text,
    created_by bigint not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    completed_at timestamptz
);
create unique index if not exists property_visits_active_slot_uq
    on property_visits (opportunity_id, scheduled_at)
    where visit_status in ('SCHEDULED', 'BUYER_ARRIVED') and not duplicate_confirmed;
create index if not exists property_visits_schedule_idx
    on property_visits (scheduled_at, id) where visit_status in ('SCHEDULED', 'BUYER_ARRIVED');
create index if not exists property_visits_contact_idx
    on property_visits (contact_id, scheduled_at desc, id desc);

create table if not exists property_visit_events (
    id uuid primary key default gen_random_uuid(),
    visit_id uuid not null references property_visits(id) on delete restrict,
    event_type text not null,
    details jsonb not null default '{}'::jsonb,
    actor_telegram_user_id bigint not null,
    telegram_update_id bigint unique,
    created_at timestamptz not null default now()
);
create index if not exists property_visit_events_timeline_idx
    on property_visit_events (visit_id, created_at, id);

create table if not exists negotiation_records (
    id uuid primary key default gen_random_uuid(),
    opportunity_id uuid not null unique references sales_opportunities(id) on delete restrict,
    property_id uuid references properties(id) on delete restrict,
    asking_price numeric(16,2) check (asking_price is null or asking_price >= 0),
    buyer_offer numeric(16,2) check (buyer_offer is null or buyer_offer >= 0),
    seller_counteroffer numeric(16,2) check (seller_counteroffer is null or seller_counteroffer >= 0),
    current_discussed_price numeric(16,2) check (current_discussed_price is null or current_discussed_price >= 0),
    currency char(3) not null default 'ETB' check (currency ~ '^[A-Z]{3}$'),
    down_payment numeric(16,2) check (down_payment is null or down_payment >= 0),
    payment_plan text,
    proposed_reservation_amount numeric(16,2) check (proposed_reservation_amount is null or proposed_reservation_amount >= 0),
    offer_date date,
    offer_expiry_date date,
    negotiation_status text not null default 'NOT_STARTED' check (negotiation_status in (
        'NOT_STARTED', 'BUYER_OFFER_RECEIVED', 'SELLER_COUNTEROFFER_RECORDED', 'NEGOTIATING',
        'PRICE_AGREED', 'PAYMENT_PLAN_DISCUSSION', 'AWAITING_DOCUMENTS',
        'AWAITING_BUYER_DECISION', 'AWAITING_SELLER_DECISION', 'ACCEPTED_RECORDED',
        'REJECTED', 'EXPIRED', 'CANCELLED'
    )),
    next_action text,
    next_action_due timestamptz,
    updated_by bigint not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check (offer_expiry_date is null or offer_date is null or offer_expiry_date >= offer_date),
    check (down_payment is null or current_discussed_price is null or down_payment <= current_discussed_price)
);
create index if not exists negotiation_records_due_idx
    on negotiation_records (negotiation_status, next_action_due, id);

create table if not exists negotiation_events (
    id uuid primary key default gen_random_uuid(),
    negotiation_id uuid not null references negotiation_records(id) on delete restrict,
    event_type text not null check (event_type in (
        'INITIAL_OFFER', 'COUNTEROFFER', 'PRICE_DISCUSSION', 'PAYMENT_PLAN_DISCUSSION',
        'BUYER_RESPONSE', 'SELLER_RESPONSE', 'DOCUMENTS_REQUESTED', 'NEXT_ACTION',
        'STATUS_CHANGED', 'FINAL_RESULT', 'NOTE_ADDED'
    )),
    details jsonb not null default '{}'::jsonb,
    actor_telegram_user_id bigint not null,
    telegram_update_id bigint unique,
    created_at timestamptz not null default now()
);
create index if not exists negotiation_events_timeline_idx
    on negotiation_events (negotiation_id, created_at, id);

create table if not exists reservations (
    id uuid primary key default gen_random_uuid(),
    opportunity_id uuid not null references sales_opportunities(id) on delete restrict,
    property_id uuid not null references properties(id) on delete restrict,
    reservation_date date not null,
    reservation_amount numeric(16,2) not null check (reservation_amount >= 0),
    currency char(3) not null check (currency ~ '^[A-Z]{3}$'),
    payment_status text not null default 'NOT_RECORDED' check (payment_status in (
        'NOT_RECORDED', 'PENDING', 'PARTIALLY_PAID', 'PAID', 'REFUNDED', 'DISPUTED'
    )),
    reservation_status text not null default 'REQUESTED' check (reservation_status in (
        'NOT_STARTED', 'REQUESTED', 'AWAITING_CONFIRMATION', 'CONFIRMED', 'CANCELLED', 'EXPIRED'
    )),
    reservation_reference text,
    expires_at timestamptz,
    notes text,
    created_by bigint not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create unique index if not exists reservations_active_opportunity_uq
    on reservations (opportunity_id) where reservation_status not in ('CANCELLED', 'EXPIRED');
create index if not exists reservations_status_expiry_idx
    on reservations (reservation_status, expires_at, id);

create table if not exists sale_records (
    id uuid primary key default gen_random_uuid(),
    opportunity_id uuid not null references sales_opportunities(id) on delete restrict,
    property_id uuid not null references properties(id) on delete restrict,
    buyer_contact_id uuid not null references contacts(id) on delete restrict,
    recorded_sale_price numeric(16,2) not null check (recorded_sale_price > 0),
    currency char(3) not null check (currency ~ '^[A-Z]{3}$'),
    sale_date date not null,
    responsible_telegram_user_id bigint not null,
    payment_plan_summary text,
    supporting_reference text,
    notes text,
    verification_status text not null default 'PENDING_VERIFICATION' check (verification_status in (
        'DRAFT', 'PENDING_VERIFICATION', 'VERIFIED', 'CANCELLED', 'DISPUTED'
    )),
    verified_by bigint,
    verified_at timestamptz,
    created_by bigint not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    cancelled_at timestamptz
);
create unique index if not exists sale_records_live_opportunity_uq
    on sale_records (opportunity_id) where verification_status <> 'CANCELLED';
create index if not exists sale_records_date_status_idx
    on sale_records (sale_date desc, verification_status, id);

create table if not exists commission_rules (
    id uuid primary key default gen_random_uuid(),
    rule_name text not null,
    rule_type text not null check (rule_type in ('FIXED', 'PERCENTAGE')),
    amount_or_rate numeric(12,4) not null check (amount_or_rate >= 0),
    currency char(3) not null check (currency ~ '^[A-Z]{3}$'),
    project_name text,
    agent_telegram_user_id bigint,
    effective_from date,
    effective_to date,
    active boolean not null default true,
    created_by bigint not null,
    created_at timestamptz not null default now(),
    check (effective_to is null or effective_from is null or effective_to >= effective_from)
);
create index if not exists commission_rules_lookup_idx
    on commission_rules (active, currency, project_name, agent_telegram_user_id, effective_from desc);

create table if not exists commission_records (
    id uuid primary key default gen_random_uuid(),
    sale_record_id uuid not null unique references sale_records(id) on delete restrict,
    rule_id uuid references commission_rules(id) on delete set null,
    calculation_type text not null check (calculation_type in ('FIXED', 'PERCENTAGE', 'MANUAL')),
    rate_or_fixed_amount numeric(16,4) not null check (rate_or_fixed_amount >= 0),
    calculated_amount numeric(16,2) not null check (calculated_amount >= 0),
    currency char(3) not null check (currency ~ '^[A-Z]{3}$'),
    commission_status text not null default 'CALCULATED' check (commission_status in (
        'ESTIMATED', 'CALCULATED', 'CONFIRMED', 'PAID', 'CANCELLED', 'DISPUTED'
    )),
    paid_at timestamptz,
    notes text,
    updated_by bigint not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create index if not exists commission_records_status_idx
    on commission_records (commission_status, created_at desc, id);

create table if not exists notification_deliveries (
    id uuid primary key default gen_random_uuid(),
    notification_key text not null unique,
    notification_type text not null check (notification_type in (
        'FOLLOW_UP_SOON', 'FOLLOW_UP_OVERDUE', 'DAILY_OVERDUE_SUMMARY',
        'VISIT_SOON', 'VISIT_DUE', 'CLOSING_SOON', 'NEGOTIATION_DUE', 'RESERVATION_EXPIRY'
    )),
    related_entity_type text not null,
    related_entity_id uuid,
    message_context jsonb not null default '{}'::jsonb,
    telegram_user_id bigint not null,
    chat_id bigint not null,
    scheduled_delivery_at timestamptz not null,
    status text not null default 'QUEUED' check (status in ('QUEUED', 'SENDING', 'SENT', 'FAILED')),
    attempts smallint not null default 0 check (attempts between 0 and 5),
    next_attempt_at timestamptz not null default now(),
    delivered_at timestamptz,
    last_error text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create index if not exists notification_deliveries_pending_idx
    on notification_deliveries (next_attempt_at, scheduled_delivery_at, id)
    where status in ('QUEUED', 'SENDING');
create index if not exists notification_deliveries_entity_idx
    on notification_deliveries (related_entity_type, related_entity_id, created_at desc);

create table if not exists reactivation_recommendations (
    id uuid primary key default gen_random_uuid(),
    contact_id uuid not null references contacts(id) on delete restrict,
    opportunity_id uuid references sales_opportunities(id) on delete restrict,
    rule_code text not null check (rule_code in (
        'QUALIFIED_INACTIVE', 'CALLBACK_DUE', 'PROPERTY_AVAILABLE', 'LOWER_PRICE_ALTERNATIVE', 'TIMELINE_APPROACHING'
    )),
    rationale text not null,
    relevant_facts jsonb not null default '{}'::jsonb,
    suggested_action text not null,
    recommended_followup_at timestamptz,
    status text not null default 'OPEN' check (status in ('OPEN', 'ACTIONED', 'SCHEDULED', 'DISMISSED', 'CONVERTED')),
    generated_at timestamptz not null default now(),
    acted_at timestamptz,
    dismissed_reason text
);
create unique index if not exists reactivation_open_rule_uq
    on reactivation_recommendations (contact_id, rule_code) where status = 'OPEN';
create index if not exists reactivation_queue_idx
    on reactivation_recommendations (status, recommended_followup_at nulls first, generated_at desc, id);

create table if not exists sales_priority_weights (
    telegram_user_id bigint not null,
    priority_code text not null check (priority_code in (
        'BOOKING_DUE', 'VISIT_CONFIRMATION', 'OVERDUE_QUALIFIED', 'NEGOTIATION_DUE',
        'HIGH_QUALIFIED', 'OTHER_DUE', 'NEW_PROSPECT'
    )),
    weight smallint not null check (weight between 0 and 1000),
    updated_at timestamptz not null default now(),
    primary key (telegram_user_id, priority_code)
);

alter table user_settings
    add column if not exists daily_overdue_summary_enabled boolean not null default true,
    add column if not exists notification_minutes_before smallint not null default 30
        check (notification_minutes_before between 0 and 1440);

alter table audit_events
    add column if not exists entity_type text not null default 'CONTACT',
    add column if not exists entity_id uuid,
    add column if not exists actor_telegram_user_id bigint,
    add column if not exists reason text,
    add column if not exists request_key text;

alter table ai_jobs add column if not exists opportunity_id uuid
    references sales_opportunities(id) on delete set null;
alter table ai_jobs drop constraint if exists ai_jobs_job_type_check;
alter table ai_jobs add constraint ai_jobs_job_type_check check (job_type in (
    'CALL_SCRIPT', 'STRUCTURE_NOTES', 'OBJECTION', 'FOLLOWUP_DRAFT', 'SALES_ANALYSIS',
    'OPPORTUNITY_ASSIST'
));
create index if not exists ai_jobs_opportunity_recent_idx
    on ai_jobs (opportunity_id, created_at desc) where opportunity_id is not null;
create index if not exists audit_events_entity_timeline_idx
    on audit_events (entity_type, entity_id, created_at desc, id desc);
create unique index if not exists audit_events_request_key_uq
    on audit_events (request_key, event_type) where request_key is not null;

do $$
declare table_name text;
begin
    foreach table_name in array array[
        'sales_pipeline_stages', 'sales_opportunities', 'opportunity_events', 'property_visits',
        'property_visit_events', 'negotiation_records', 'negotiation_events', 'reservations',
        'sale_records', 'commission_rules', 'commission_records', 'notification_deliveries',
        'reactivation_recommendations', 'sales_priority_weights'
    ] loop
        execute format('alter table public.%I enable row level security', table_name);
    end loop;
end $$;
