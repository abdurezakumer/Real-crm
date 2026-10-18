-- Optional development data. Contact names and telephone numbers are deliberately omitted.
insert into campaigns (name, description, source)
values ('Development Campaign', 'Empty campaign for local setup verification', 'seed')
on conflict do nothing;
