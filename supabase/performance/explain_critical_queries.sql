-- Run only in the disposable performance database after seed_50000_contacts.sql.
-- Replace the campaign UUID with the row for "Performance Test 50k".
explain (analyze, buffers)
select c.id, c.name, c.phone_number
from contacts c
left join lateral (
    select f.id, f.scheduled_at
    from follow_ups f
    where f.contact_id = c.id and f.status = 'PENDING' and f.scheduled_at <= now()
    order by f.scheduled_at
    limit 1
) due on true
where c.contact_eligibility = 'ELIGIBLE'
  and c.lead_status not in ('DO_NOT_CONTACT', 'CONVERTED', 'DISQUALIFIED', 'NOT_INTERESTED')
  and c.conversion_state <> 'CONVERTED'
  and c.total_call_attempts < 8
  and exists (select 1 from campaign_contacts cc join campaigns ca on ca.id=cc.campaign_id
      where lower(ca.name)=lower('Performance Test 50k') and cc.contact_id=c.id and cc.campaign_status='ACTIVE')
  and not exists (
      select 1 from session_contacts sc
      where sc.session_id = '00000000-0000-0000-0000-000000000000'
        and sc.contact_id = c.id
  )
order by case when due.id is not null then 1
              when c.interest_level in ('HIGH','VERY_HIGH') then 3
              when c.lead_status in ('INTERESTED','QUALIFIED') then 4
              when c.last_called_at is null then 5 else 6 end,
         case when due.id is not null then c.next_follow_up_at end asc nulls last,
         case when c.last_called_at is null then c.created_at end asc nulls last,
         c.last_called_at asc nulls first,c.id
limit 1;

explain (analyze, buffers)
select count(*) from call_logs
where created_at >= date_trunc('day', now())
  and created_at < date_trunc('day', now()) + interval '1 day';

explain (analyze, buffers)
select id, contact_id, scheduled_at
from follow_ups
where status = 'PENDING' and scheduled_at <= now()
order by scheduled_at
limit 20;
