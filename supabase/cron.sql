-- Run in the Supabase SQL Editor after replacing the two placeholder values.
-- Store the worker URL and secret in Vault; do not place them in the application schema.
select vault.create_secret('https://YOUR-PROJECT.vercel.app/api/worker', 'crm_worker_url');
select vault.create_secret('REPLACE_WITH_FOLLOW_UP_CRON_SECRET', 'crm_worker_secret');

create extension if not exists pg_cron with schema pg_catalog;
create extension if not exists pg_net with schema extensions;

select cron.unschedule(jobid)
from cron.job
where jobname = 'real-estate-crm-worker';

select cron.schedule(
    'real-estate-crm-worker',
    '* * * * *',
    $$
    select net.http_post(
        url := (select decrypted_secret from vault.decrypted_secrets where name = 'crm_worker_url'),
        headers := jsonb_build_object(
            'Authorization', 'Bearer ' ||
                (select decrypted_secret from vault.decrypted_secrets where name = 'crm_worker_secret'),
            'Content-Type', 'application/json'
        ),
        body := '{}'::jsonb
    );
    $$
);
