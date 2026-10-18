-- Durable outbox support for Telegram pasted-lead staging.
alter table telegram_outbox
    drop constraint if exists telegram_outbox_action_type_check;

alter table telegram_outbox
    add constraint telegram_outbox_action_type_check
    check (action_type in ('SEND','EDIT','FINISH_PREVIEW','DIALER','STAGE_TEXT_IMPORT'));
