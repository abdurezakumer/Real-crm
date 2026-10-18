-- Persist user-visible Telegram replies atomically with their business updates.
-- Bot API sends are at-least-once because Telegram does not accept an idempotency key.

create table if not exists telegram_outbox (
    id uuid primary key default gen_random_uuid(),
    telegram_user_id bigint not null,
    -- Keep retries durable even after processed update IDs are pruned after 30 days.
    update_id bigint not null,
    sequence smallint not null check (sequence between 0 and 99),
    action_type text not null check (action_type in ('SEND','EDIT','FINISH_PREVIEW','DIALER')),
    payload jsonb not null,
    status text not null default 'QUEUED' check (status in ('QUEUED','SENDING','SENT','FAILED')),
    attempts smallint not null default 0 check (attempts between 0 and 8),
    available_at timestamptz not null default now(),
    locked_until timestamptz,
    last_error text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    sent_at timestamptz,
    unique(update_id,sequence)
);
create index if not exists telegram_outbox_pending_idx
    on telegram_outbox(available_at,created_at,id)
    where status in ('QUEUED','SENDING');
create index if not exists telegram_outbox_retention_idx
    on telegram_outbox(updated_at,id) where status in ('SENT','FAILED');
alter table telegram_outbox enable row level security;

comment on table telegram_outbox is
    'Private server-side queue for Telegram replies; payloads are removed 30 days after terminal delivery.';
