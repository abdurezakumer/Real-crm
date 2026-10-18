-- Phase 4 adds a single-tenant team model around the existing CRM records.
-- Existing data and IDs remain intact. The configured bootstrap administrator
-- is created by the application on its first authenticated request.

create table if not exists crm_users (
    telegram_user_id bigint primary key,
    display_name text not null default '',
    role text not null default 'SALESPERSON' check (role in (
        'SUPER_ADMIN','SALES_MANAGER','SALESPERSON','MARKETING_ADMIN','OBSERVER','CUSTOM'
    )),
    account_status text not null default 'ACTIVE' check (account_status in ('ACTIVE','SUSPENDED')),
    can_receive_leads boolean not null default true,
    invited_by bigint,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create index if not exists crm_users_active_role_idx on crm_users (role,telegram_user_id)
    where account_status='ACTIVE';

create table if not exists crm_teams (
    id uuid primary key default gen_random_uuid(),
    name text not null unique check (length(trim(name)) between 1 and 100),
    description text,
    active boolean not null default true,
    manager_telegram_user_id bigint references crm_users(telegram_user_id) on delete set null,
    created_by bigint not null references crm_users(telegram_user_id),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists crm_team_memberships (
    team_id uuid not null references crm_teams(id) on delete restrict,
    telegram_user_id bigint not null references crm_users(telegram_user_id) on delete restrict,
    is_default boolean not null default false,
    active boolean not null default true,
    added_by bigint not null references crm_users(telegram_user_id),
    created_at timestamptz not null default now(),
    primary key (team_id,telegram_user_id)
);
create unique index if not exists crm_one_default_team_per_user_idx
    on crm_team_memberships(telegram_user_id) where active and is_default;
create index if not exists crm_memberships_user_idx
    on crm_team_memberships(telegram_user_id,team_id) where active;

create table if not exists crm_invitations (
    id uuid primary key default gen_random_uuid(),
    token_hash char(64) not null unique,
    role text not null check (role in ('SALES_MANAGER','SALESPERSON','MARKETING_ADMIN','OBSERVER','CUSTOM')),
    team_id uuid references crm_teams(id) on delete restrict,
    issued_by bigint not null references crm_users(telegram_user_id),
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    status text not null default 'PENDING' check (status in ('PENDING','ACCEPTED','EXPIRED','REVOKED')),
    accepted_at timestamptz,
    accepted_telegram_user_id bigint references crm_users(telegram_user_id) on delete set null,
    check (expires_at > created_at),
    check ((status='ACCEPTED') = (accepted_at is not null))
);
create index if not exists crm_invitations_pending_idx
    on crm_invitations(expires_at,created_at) where status='PENDING';

create table if not exists crm_user_permissions (
    telegram_user_id bigint not null references crm_users(telegram_user_id) on delete cascade,
    permission_code text not null,
    allowed boolean not null,
    updated_by bigint not null references crm_users(telegram_user_id),
    updated_at timestamptz not null default now(),
    primary key (telegram_user_id,permission_code)
);

alter table follow_ups
    add column if not exists created_by bigint references crm_users(telegram_user_id) on delete set null,
    add column if not exists missed_by bigint references crm_users(telegram_user_id) on delete set null;
create index if not exists follow_ups_created_by_idx on follow_ups(created_by,created_at desc);
create index if not exists follow_ups_completed_by_idx on follow_ups(completed_by,completed_at desc);

create table if not exists crm_team_settings (
    singleton boolean primary key default true check (singleton),
    max_active_leads_per_salesperson integer not null default 500 check (max_active_leads_per_salesperson between 1 and 100000),
    unassigned_visible_to_salespeople boolean not null default false,
    salespeople_can_request_reassignment boolean not null default true,
    manager_can_override_ownership boolean not null default true,
    auto_reassignment_enabled boolean not null default false,
    active_negotiation_requires_super_admin boolean not null default true,
    default_distribution_method text not null default 'ROUND_ROBIN' check (default_distribution_method in ('ROUND_ROBIN','LOWEST_WORKLOAD','MANUAL')),
    updated_by bigint references crm_users(telegram_user_id),
    updated_at timestamptz not null default now()
);
insert into crm_team_settings(singleton) values (true) on conflict do nothing;

alter table contacts
    add column if not exists assigned_telegram_user_id bigint references crm_users(telegram_user_id) on delete set null,
    add column if not exists assigned_at timestamptz,
    add column if not exists assignment_source text,
    add column if not exists ownership_restricted boolean not null default false,
    add column if not exists import_batch_id uuid references import_jobs(id) on delete set null,
    add column if not exists lead_source text;
create index if not exists contacts_assigned_queue_idx
    on contacts(assigned_telegram_user_id,lead_status,id);
create index if not exists contacts_import_batch_idx on contacts(import_batch_id,id) where import_batch_id is not null;
create index if not exists contacts_lead_source_idx on contacts(lead_source,id) where lead_source is not null;

create table if not exists lead_assignment_history (
    id uuid primary key default gen_random_uuid(),
    contact_id uuid not null references contacts(id) on delete restrict,
    opportunity_id uuid references sales_opportunities(id) on delete restrict,
    previous_owner_telegram_user_id bigint references crm_users(telegram_user_id) on delete set null,
    new_owner_telegram_user_id bigint references crm_users(telegram_user_id) on delete set null,
    actor_telegram_user_id bigint not null references crm_users(telegram_user_id),
    assignment_source text not null check (assignment_source in ('MANUAL','ROUND_ROBIN','LOWEST_WORKLOAD','BULK','HANDOVER','BOOTSTRAP','IMPORT')),
    reason text,
    decision_facts jsonb not null default '{}'::jsonb,
    idempotency_key text unique,
    created_at timestamptz not null default now()
);
create index if not exists lead_assignment_history_contact_idx
    on lead_assignment_history(contact_id,created_at desc,id desc);

create table if not exists lead_assignment_jobs (
    id uuid primary key default gen_random_uuid(),
    created_by bigint not null references crm_users(telegram_user_id),
    team_id uuid references crm_teams(id) on delete set null,
    method text not null check (method in ('ROUND_ROBIN','LOWEST_WORKLOAD','MANUAL')),
    filters jsonb not null default '{}'::jsonb,
    preview_assignments jsonb not null default '[]'::jsonb,
    selected_count integer not null default 0 check (selected_count between 0 and 500),
    status text not null default 'PREVIEW' check (status in ('PREVIEW','PROCESSING','COMPLETED','CANCELLED','FAILED')),
    applied_count integer not null default 0,
    idempotency_key text not null unique,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    completed_at timestamptz
);
create index if not exists lead_assignment_jobs_pending_idx
    on lead_assignment_jobs(created_at,id) where status in ('PREVIEW','PROCESSING');

create table if not exists lead_handover_requests (
    id uuid primary key default gen_random_uuid(),
    contact_id uuid not null references contacts(id) on delete restrict,
    requested_by bigint not null references crm_users(telegram_user_id) on delete restrict,
    current_owner bigint references crm_users(telegram_user_id) on delete set null,
    reason text not null check (reason in ('UNAVAILABLE','LEAVING_TEAM','SPECIALIST','WORKLOAD','CUSTOMER_REQUEST','MANAGER_INTERVENTION','OTHER')),
    notes text,
    task_snapshot jsonb not null default '{}'::jsonb,
    status text not null default 'PENDING' check (status in ('PENDING','APPROVED','REJECTED','CANCELLED')),
    assigned_to bigint references crm_users(telegram_user_id) on delete set null,
    reviewed_by bigint references crm_users(telegram_user_id) on delete set null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create unique index if not exists lead_handover_pending_contact_uq
    on lead_handover_requests(contact_id) where status='PENDING';
create index if not exists lead_handover_queue_idx
    on lead_handover_requests(status,created_at desc);

create table if not exists team_distribution_state (
    team_id uuid primary key references crm_teams(id) on delete restrict,
    last_assigned_telegram_user_id bigint references crm_users(telegram_user_id) on delete set null,
    updated_at timestamptz not null default now()
);

create table if not exists campaign_memberships (
    campaign_id uuid not null references campaigns(id) on delete cascade,
    telegram_user_id bigint not null references crm_users(telegram_user_id) on delete restrict,
    active boolean not null default true,
    added_by bigint not null references crm_users(telegram_user_id),
    created_at timestamptz not null default now(),
    primary key(campaign_id,telegram_user_id)
);
alter table campaigns
    add column if not exists team_id uuid references crm_teams(id) on delete restrict,
    add column if not exists starts_at timestamptz,
    add column if not exists ends_at timestamptz,
    add column if not exists calling_priority smallint not null default 50 check (calling_priority between 0 and 100),
    add column if not exists max_call_attempts smallint not null default 8 check (max_call_attempts between 1 and 50),
    add column if not exists retry_after_hours smallint not null default 24 check (retry_after_hours between 0 and 720),
    add column if not exists performance_target numeric(16,2) check (performance_target is null or performance_target >= 0),
    add column if not exists created_by bigint references crm_users(telegram_user_id) on delete set null,
    add column if not exists updated_by bigint references crm_users(telegram_user_id) on delete set null;
alter table campaigns drop constraint if exists campaigns_dates_check;
alter table campaigns add constraint campaigns_dates_check
    check (ends_at is null or starts_at is null or ends_at > starts_at);
alter table campaigns drop constraint if exists campaigns_status_check;
alter table campaigns add constraint campaigns_status_check check (
    status in ('DRAFT','READY','ACTIVE','PAUSED','COMPLETED','CANCELLED','ARCHIVED')
);
create index if not exists campaigns_team_status_idx on campaigns(team_id,status,created_at desc);
create index if not exists campaign_memberships_user_idx
    on campaign_memberships(telegram_user_id,campaign_id) where active;

create table if not exists team_targets (
    id uuid primary key default gen_random_uuid(),
    target_type text not null check (target_type in (
        'CALLS_LOGGED','UNIQUE_CONTACTS_REACHED','QUALIFIED_BUYERS','FOLLOWUPS_COMPLETED',
        'VISITS_COMPLETED','OPPORTUNITIES_CREATED','RESERVATIONS_RECORDED','VERIFIED_SALES_COUNT','VERIFIED_SALES_VALUE'
    )),
    target_amount numeric(16,2) not null check (target_amount >= 0),
    currency char(3) check (currency is null or currency ~ '^[A-Z]{3}$'),
    period_type text not null check (period_type in ('DAILY','WEEKLY','MONTHLY','CAMPAIGN','CUSTOM')),
    telegram_user_id bigint references crm_users(telegram_user_id) on delete restrict,
    team_id uuid references crm_teams(id) on delete restrict,
    campaign_id uuid references campaigns(id) on delete restrict,
    starts_at timestamptz not null,
    ends_at timestamptz not null,
    status text not null default 'ACTIVE' check (status in ('ACTIVE','COMPLETED','CANCELLED')),
    created_by bigint not null references crm_users(telegram_user_id),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check (ends_at > starts_at),
    check (num_nonnulls(telegram_user_id,team_id,campaign_id)=1)
);
create index if not exists team_targets_active_idx
    on team_targets(status,starts_at,ends_at,target_type);

create table if not exists team_target_history (
    id uuid primary key default gen_random_uuid(),
    target_id uuid not null references team_targets(id) on delete restrict,
    previous_value jsonb,
    new_value jsonb not null,
    changed_by bigint not null references crm_users(telegram_user_id),
    reason text not null,
    created_at timestamptz not null default now()
);

alter table commission_records
    add column if not exists responsible_telegram_user_id bigint references crm_users(telegram_user_id) on delete set null,
    add column if not exists approved_amount numeric(16,2) check (approved_amount is null or approved_amount >= 0),
    add column if not exists paid_amount numeric(16,2) not null default 0 check (paid_amount >= 0),
    add column if not exists policy_snapshot jsonb not null default '{}'::jsonb,
    add column if not exists approved_by bigint references crm_users(telegram_user_id) on delete set null,
    add column if not exists approved_at timestamptz,
    add column if not exists payment_reference text;
update commission_records cr set responsible_telegram_user_id=s.responsible_telegram_user_id
  from sale_records s where s.id=cr.sale_record_id and cr.responsible_telegram_user_id is null;
update commission_records cr set policy_snapshot=jsonb_build_object(
    'rule_id',r.id,'rule_name',r.rule_name,'rule_type',r.rule_type,
    'amount_or_rate',r.amount_or_rate,'currency',r.currency,'project_name',r.project_name,
    'agent_telegram_user_id',r.agent_telegram_user_id,'effective_from',r.effective_from,
    'effective_to',r.effective_to)
  from commission_rules r where r.id=cr.rule_id and cr.policy_snapshot='{}'::jsonb;

create table if not exists commission_approval_history (
    id uuid primary key default gen_random_uuid(),
    commission_record_id uuid not null references commission_records(id) on delete restrict,
    actor_telegram_user_id bigint not null references crm_users(telegram_user_id),
    action text not null check (action in ('REQUESTED','APPROVED','REJECTED','ADJUSTED','PAYMENT_RECORDED','DISPUTED')),
    previous_value jsonb,
    new_value jsonb,
    reason text not null,
    created_at timestamptz not null default now()
);
create index if not exists commission_approval_history_record_idx
    on commission_approval_history(commission_record_id,created_at desc);

create or replace function crm_can_access_contact(p_contact_id uuid,p_actor_id bigint)
returns boolean language sql stable security invoker set search_path=public as $$
    select exists (
        select 1 from contacts c join crm_users actor on actor.telegram_user_id=p_actor_id
        where c.id=p_contact_id and actor.account_status='ACTIVE'
          and (
            actor.role='SUPER_ADMIN'
            or (c.assigned_telegram_user_id=p_actor_id and coalesce(
                 (select p.allowed from crm_user_permissions p
                  where p.telegram_user_id=p_actor_id and p.permission_code='contacts.read_assigned'),
                 actor.role in ('SALESPERSON','SALES_MANAGER','MARKETING_ADMIN','CUSTOM')))
            or (
              c.assigned_telegram_user_id is not null
              and coalesce((select p.allowed from crm_user_permissions p
                            where p.telegram_user_id=p_actor_id and p.permission_code='contacts.read_team'),
                           actor.role in ('SALES_MANAGER','OBSERVER'))
              and exists (
                select 1 from crm_team_memberships owner_member
                join crm_team_memberships actor_member on actor_member.team_id=owner_member.team_id
                  and actor_member.telegram_user_id=p_actor_id and actor_member.active
                join crm_teams t on t.id=owner_member.team_id and t.active
                where owner_member.telegram_user_id=c.assigned_telegram_user_id and owner_member.active
                  and (actor.role='OBSERVER' or t.manager_telegram_user_id=p_actor_id
                       or exists(select 1 from crm_team_memberships manager_member
                                 where manager_member.team_id=t.id and manager_member.telegram_user_id=p_actor_id
                                   and manager_member.active))
              )
            )
            or (c.assigned_telegram_user_id is null and actor.role='SALES_MANAGER'
                and coalesce((select p.allowed from crm_user_permissions p
                              where p.telegram_user_id=p_actor_id and p.permission_code='contacts.read_team'),true))
            or (c.assigned_telegram_user_id is null and actor.role='SALESPERSON'
                and coalesce((select s.unassigned_visible_to_salespeople from crm_team_settings s where s.singleton),false))
            or (c.assigned_telegram_user_id is null and actor.role='CUSTOM'
                and coalesce((select p.allowed from crm_user_permissions p
                              where p.telegram_user_id=p_actor_id and p.permission_code='contacts.read_unassigned'),false))
          )
    )
$$;

create or replace function crm_current_user_id()
returns bigint language sql stable as $$
    select nullif(current_setting('crm.telegram_user_id',true),'')::bigint
$$;

create or replace function crm_require_contact_access(p_contact_id uuid,p_actor_id bigint)
returns void language plpgsql stable security invoker set search_path=public as $$
begin
    if not crm_can_access_contact(p_contact_id,p_actor_id) then
        raise exception 'Lead is outside the authorized user scope' using errcode='42501';
    end if;
end
$$;

-- Keep Phase 3's existing status values valid while adding governance states.
alter table commission_records drop constraint if exists commission_records_commission_status_check;
alter table commission_records add constraint commission_records_commission_status_check check (
    commission_status in ('ESTIMATED','CALCULATED','CONFIRMED','PENDING_REVIEW','APPROVED','PARTIALLY_PAID','PAID','CANCELLED','DISPUTED')
);

do $$ declare t text; begin
    foreach t in array array[
        'crm_users','crm_teams','crm_team_memberships','crm_invitations','crm_user_permissions',
        'crm_team_settings','lead_assignment_history','lead_assignment_jobs','team_distribution_state',
        'lead_handover_requests','campaign_memberships','team_targets','team_target_history','commission_approval_history'
    ] loop
        execute format('alter table %I enable row level security',t);
    end loop;
end $$;

-- The bot currently uses a private server-side database connection and checks
-- crm_users in every request. No client role receives table grants through this
-- migration. Supabase dashboard deployments should review RLS before enabling
-- any direct client access.
