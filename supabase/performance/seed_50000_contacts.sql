-- Run only in a disposable development/test Supabase project.
do $$
declare benchmark_campaign uuid;
begin
    select id into benchmark_campaign from campaigns
      where lower(name) = lower('Performance Test 50k') and status <> 'ARCHIVED' limit 1;
    if benchmark_campaign is null then
        insert into campaigns (name, description, source)
        values ('Performance Test 50k', 'Synthetic benchmark rows; never production data', 'benchmark')
        returning id into benchmark_campaign;
    end if;

    insert into contacts (source_row_number, name, phone_number, normalized_phone)
    select n, 'Benchmark Contact ' || n,
           '+2519' || lpad(n::text, 8, '0'),
           '+2519' || lpad(n::text, 8, '0')
    from generate_series(1, 50000) as series(n)
    on conflict (normalized_phone) where normalized_phone is not null do nothing;

    insert into campaign_contacts (campaign_id, contact_id)
    select benchmark_campaign, c.id
    from contacts c
    where c.name like 'Benchmark Contact %'
    on conflict do nothing;
end $$;
