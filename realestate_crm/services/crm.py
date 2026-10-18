import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from psycopg import Connection

from realestate_crm.config import Settings
from realestate_crm.enums import Eligibility
from realestate_crm.phone import normalize_phone
from realestate_crm.timeutils import local_day_bounds


def ensure_user(conn: Connection, user_id: int, settings: Settings) -> dict[str, Any]:
    row = conn.execute(
        """insert into user_settings (telegram_user_id, timezone, daily_call_target, attempt_limit)
           values (%s, %s, %s, %s) on conflict (telegram_user_id) do update
           set telegram_user_id = excluded.telegram_user_id returning *""",
        (
            user_id,
            settings.app_timezone,
            settings.default_daily_call_target,
            settings.max_call_attempts,
        ),
    ).fetchone()
    return row


def dashboard(conn: Connection, user_id: int, settings: Settings) -> dict[str, Any]:
    prefs = ensure_user(conn, user_id, settings)
    now = datetime.now(UTC)
    day_start, day_end = local_day_bounds(now, prefs["timezone"])
    metrics = conn.execute(
        """select
          (select count(*) from contacts c where crm_current_user_id() is null
            or crm_can_access_contact(c.id,crm_current_user_id())) as total_contacts,
          (select count(*) from contacts c where (crm_current_user_id() is null
            or crm_can_access_contact(c.id,crm_current_user_id())) and contact_eligibility = 'ELIGIBLE'
            and conversion_state <> 'CONVERTED'
            and lead_status not in ('DO_NOT_CONTACT','CONVERTED','DISQUALIFIED','NOT_INTERESTED')
            and not exists (select 1 from lead_qualifications q where q.contact_id=c.id
              and q.qualification_status='DISQUALIFIED')) as eligible_contacts,
          (select count(*) from contacts c where c.last_called_at is null and
            (crm_current_user_id() is null or crm_can_access_contact(c.id,crm_current_user_id()))) as never_called,
          (select count(*) from call_logs l left join call_sessions cs on cs.id=l.call_session_id
            where l.created_at >= %s and l.created_at < %s and (crm_current_user_id() is null
              or exists(select 1 from crm_users u where u.telegram_user_id=crm_current_user_id() and u.role='SUPER_ADMIN')
              or cs.telegram_user_id=crm_current_user_id())) as calls_today,
          (select count(*) from call_logs l left join call_sessions cs on cs.id=l.call_session_id
            where l.created_at >= %s and l.created_at < %s and (l.connection_result = 'ANSWERED'
              or (l.outcome_category = 'CONNECTION' and l.outcome_code = 'ANSWERED'))
              and (crm_current_user_id() is null or exists(select 1 from crm_users u
                where u.telegram_user_id=crm_current_user_id() and u.role='SUPER_ADMIN')
                or cs.telegram_user_id=crm_current_user_id())) as answered_today,
          (select count(distinct l.contact_id) from call_logs l left join call_sessions cs on cs.id=l.call_session_id
            where l.created_at >= %s and l.created_at < %s and l.outcome_category = 'SALES_INTEREST'
              and (crm_current_user_id() is null or exists(select 1 from crm_users u
                where u.telegram_user_id=crm_current_user_id() and u.role='SUPER_ADMIN')
                or cs.telegram_user_id=crm_current_user_id())) as interested_today,
          (select count(*) from follow_ups f join contacts c on c.id=f.contact_id
            where f.status = 'PENDING' and f.scheduled_at >= %s and f.scheduled_at < %s
              and (crm_current_user_id() is null or crm_can_access_contact(c.id,crm_current_user_id()))) as due_today,
          (select count(*) from follow_ups f join contacts c on c.id=f.contact_id
            where f.status = 'PENDING' and f.scheduled_at < %s
              and (crm_current_user_id() is null or crm_can_access_contact(c.id,crm_current_user_id()))) as overdue,
          (select count(distinct l.contact_id) from call_logs l left join call_sessions cs on cs.id=l.call_session_id
            where l.created_at >= %s and l.created_at < %s
              and l.outcome_code in ('MEETING_SCHEDULED', 'REQUESTED_PROPERTY_VISIT')
              and (crm_current_user_id() is null or exists(select 1 from crm_users u
                where u.telegram_user_id=crm_current_user_id() and u.role='SUPER_ADMIN')
                or cs.telegram_user_id=crm_current_user_id())) as meetings_today""",
        (
            day_start,
            day_end,
            day_start,
            day_end,
            day_start,
            day_end,
            day_start,
            day_end,
            day_start,
            day_start,
            day_end,
        ),
    ).fetchone()
    metrics["daily_target"] = prefs["daily_call_target"]
    metrics["target_progress"] = min(
        100, round(metrics["calls_today"] * 100 / max(1, prefs["daily_call_target"]))
    )
    metrics["period_label"] = now.astimezone(ZoneInfo(prefs["timezone"])).strftime("%d %b %Y")
    return metrics


def active_campaigns(conn: Connection) -> list[dict[str, Any]]:
    return conn.execute(
        """select c.id,c.name,c.status,c.team_id from campaigns c
           where c.status='ACTIVE' and (c.starts_at is null or c.starts_at<=now())
             and (c.ends_at is null or c.ends_at>now()) and (
             crm_current_user_id() is null
             or exists(select 1 from crm_users u where u.telegram_user_id=crm_current_user_id()
               and u.account_status='ACTIVE' and u.role in ('SUPER_ADMIN','MARKETING_ADMIN'))
             or exists(select 1 from campaign_memberships m where m.campaign_id=c.id
               and m.telegram_user_id=crm_current_user_id() and m.active)
             or exists(select 1 from campaign_contacts cc join contacts x on x.id=cc.contact_id
               where cc.campaign_id=c.id and crm_can_access_contact(x.id,crm_current_user_id()))
             or exists(select 1 from crm_team_memberships m join crm_teams t on t.id=m.team_id and t.active
               where m.team_id=c.team_id and m.telegram_user_id=crm_current_user_id() and m.active)
           ) order by c.created_at desc limit 30"""
    ).fetchall()


def get_contact(conn: Connection, contact_id: UUID) -> dict[str, Any] | None:
    return conn.execute(
        """select c.*, camp.name as campaign_name,
              ls.score as lead_priority_score, ls.priority_category as lead_priority_category,
              ls.explanation as lead_score_explanation,
              q.qualification_status,
              (select n.note_text from contact_notes n where n.contact_id = c.id order by n.created_at desc limit 1) as latest_note,
              (select p.property_type || coalesce(' · ' || p.preferred_location, '')
                 from contact_property_interests p where p.contact_id = c.id order by p.updated_at desc limit 1) as property_interest
           from contacts c left join campaign_contacts cc on cc.contact_id = c.id
           left join lead_scores ls on ls.contact_id=c.id
           left join lead_qualifications q on q.contact_id=c.id
           left join campaigns camp on camp.id = cc.campaign_id and cc.campaign_id = (
             select cc2.campaign_id from campaign_contacts cc2 where cc2.contact_id = c.id
             order by cc2.created_at desc limit 1)
           where c.id = %s and (crm_current_user_id() is null
             or crm_can_access_contact(c.id,crm_current_user_id()))""",
        (contact_id,),
    ).fetchone()


def lead_history(conn: Connection, contact_id: UUID) -> list[dict[str, Any]]:
    return conn.execute(
        "select outcome_category, outcome_code, notes, created_at from call_logs where contact_id = %s order by created_at desc limit 10",
        (contact_id,),
    ).fetchall()


def search_contacts(
    conn: Connection, query: str, limit: int, after_id: UUID | None = None
) -> list[dict[str, Any]]:
    term = query.strip()
    if not term:
        return []
    pattern = f"%{term[:80]}%"
    phone = normalize_phone(term)
    if phone:
        return conn.execute(
            """select c.id,c.name,c.phone_number,c.lead_status,c.interest_level
               from contacts c where (lower(c.name) like lower(%s) or c.normalized_phone=%s)
                 and (crm_current_user_id() is null or crm_can_access_contact(c.id,crm_current_user_id()))
                 and (%s::uuid is null or c.id>%s)
               order by c.id limit %s""",
            (pattern, phone, after_id, after_id, limit),
        ).fetchall()
    return conn.execute(
        """select c.id,c.name,c.phone_number,c.lead_status,c.interest_level
           from contacts c where lower(c.name) like lower(%s)
             and (crm_current_user_id() is null or crm_can_access_contact(c.id,crm_current_user_id()))
             and (%s::uuid is null or c.id>%s)
           order by c.id limit %s""",
        (pattern, after_id, after_id, limit),
    ).fetchall()


def search_filtered(
    conn: Connection, mode: str, value: str, limit: int, after_id: UUID | None, timezone: str
) -> list[dict[str, Any]]:
    if mode == "name_phone":
        return search_contacts(conn, value, limit, after_id)
    args: list[Any] = []
    distinct = ""
    join = ""
    if mode == "campaign":
        clause = "exists (select 1 from campaign_contacts cc join campaigns ca on ca.id=cc.campaign_id where cc.contact_id=c.id and lower(ca.name) like lower(%s))"
        args.append(f"%{value[:80]}%")
    elif mode == "status":
        clause = "c.lead_status::text=%s"
        args.append(value.upper())
    elif mode == "interest":
        clause = "c.interest_level::text=%s"
        args.append(value.upper())
    elif mode in {"followup_date", "last_call_date"}:
        try:
            day = datetime.strptime(value.strip(), "%Y-%m-%d").date()
        except ValueError:
            return []
        start = datetime.combine(day, datetime.min.time(), tzinfo=ZoneInfo(timezone)).astimezone(
            UTC
        )
        end = start + timedelta(days=1)
        if mode == "followup_date":
            clause = "exists (select 1 from follow_ups f where f.contact_id=c.id and f.scheduled_at >= %s and f.scheduled_at < %s and f.status='PENDING')"
        else:
            clause = "c.last_called_at >= %s and c.last_called_at < %s"
        args.extend((start, end))
    elif mode == "property_type":
        distinct = "distinct "
        join = "join contact_property_interests p on p.contact_id=c.id"
        clause = "p.property_type=%s"
        args.append(value.upper())
    elif mode == "location":
        distinct = "distinct "
        join = "join contact_property_interests p on p.contact_id=c.id"
        clause = "lower(p.preferred_location) like lower(%s)"
        args.append(f"%{value[:80]}%")
    elif mode == "budget":
        parts = value.replace(",", "").split()
        if len(parts) != 3:
            return []
        try:
            low, high = Decimal(parts[0]), Decimal(parts[1])
        except InvalidOperation:
            return []
        if low < 0 or high < low or len(parts[2]) != 3:
            return []
        distinct = "distinct "
        join = "join contact_property_interests p on p.contact_id=c.id"
        clause = "p.currency=%s and (p.min_budget is null or p.min_budget<=%s) and (p.max_budget is null or p.max_budget>=%s)"
        args.extend((parts[2].upper(), high, low))
    else:
        return []
    clause += " and (%s::uuid is null or c.id>%s)"
    args.extend((after_id, after_id, limit))
    return conn.execute(
        f"""select {distinct}c.id,c.name,c.phone_number,c.lead_status::text lead_status,
                  c.interest_level::text interest_level
           from contacts c {join} where {clause}
             and (crm_current_user_id() is null or crm_can_access_contact(c.id,crm_current_user_id()))
           order by c.id limit %s""",
        args,
    ).fetchall()


def create_campaign(
    conn: Connection, name: str, source: str | None = None, actor_id: int | None = None
) -> dict[str, Any]:
    if actor_id is not None:
        from realestate_crm.services.team import permission_allowed

        if not (
            permission_allowed(conn, actor_id, "campaigns.manage")
            or permission_allowed(conn, actor_id, "imports.manage")
        ):
            raise PermissionError("You do not have permission to create campaigns")
    return conn.execute(
        """insert into campaigns (name, source, created_by, updated_by) values (%s, %s, %s, %s)
           on conflict (lower(name)) where status <> 'ARCHIVED' do update set updated_at = now()
           returning id, name, status""",
        (name[:120], source, actor_id, actor_id),
    ).fetchone()


def start_or_resume_session(
    conn: Connection, user_id: int, campaign_id: UUID | None = None
) -> dict[str, Any]:
    if campaign_id is not None:
        campaign = conn.execute(
            "select status from campaigns where id=%s", (campaign_id,)
        ).fetchone()
        if not campaign or campaign["status"] != "ACTIVE":
            raise ValueError("This campaign is not active for calling")
        from realestate_crm.services.team import require_campaign_scope

        require_campaign_scope(conn, user_id, campaign_id)
    session = conn.execute(
        "select * from call_sessions where telegram_user_id = %s and status in ('ACTIVE','PAUSED') order by started_at desc limit 1 for update",
        (user_id,),
    ).fetchone()
    if session:
        if campaign_id is not None and session["campaign_id"] != campaign_id:
            session = conn.execute(
                "update call_sessions set campaign_id = %s, current_contact_id = null, status = 'ACTIVE' where id = %s returning *",
                (campaign_id, session["id"]),
            ).fetchone()
        return session
    return conn.execute(
        "insert into call_sessions (telegram_user_id, campaign_id) values (%s, %s) returning *",
        (user_id, campaign_id),
    ).fetchone()


def _next_candidate(
    conn: Connection, session: dict[str, Any], settings: Settings
) -> dict[str, Any] | None:
    session_id = session["id"]
    campaign_id = session["campaign_id"]
    prefs = ensure_user(conn, session["telegram_user_id"], settings)
    allow_future = prefs["allow_call_during_future_followup"]
    now = datetime.now(UTC)
    queue_order = getattr(
        settings,
        "queue_priority_order",
        "overdue_high,due_followup,high_qualified,qualified,interested,new,retry",
    )
    _, local_day_end = local_day_bounds(now, settings.app_timezone)
    queue_filters = prefs.get("queue_filters") or {}
    location = queue_filters.get("preferred_location")
    followup_filter = queue_filters.get("followup_status")
    return conn.execute(
        """select c.id, c.name, c.phone_number, c.normalized_phone, c.lead_status,
                  c.contact_eligibility, c.interest_level, c.total_call_attempts, c.last_called_at,
                  c.next_follow_up_at,
                  c.created_at
           from contacts c
           left join lead_qualifications q on q.contact_id=c.id
           left join lead_scores ls on ls.contact_id=c.id
           left join lateral (select * from contact_property_interests p0 where p0.contact_id=c.id
             order by p0.updated_at desc,p0.id desc limit 1) bp on true
           left join lateral (select max(ca.calling_priority) calling_priority
             from campaign_contacts cc join campaigns ca on ca.id=cc.campaign_id
             where cc.contact_id=c.id and cc.campaign_status='ACTIVE' and ca.status='ACTIVE'
               and (ca.starts_at is null or ca.starts_at<=now())
               and (ca.ends_at is null or ca.ends_at>now())) cp on true
           left join lateral (select f.id,f.scheduled_at from follow_ups f where f.contact_id = c.id
                and f.status = 'PENDING' and f.scheduled_at < %s order by f.scheduled_at limit 1) due on true
           where c.contact_eligibility = 'ELIGIBLE'
             and crm_can_access_contact(c.id,%s)
             and c.lead_status not in ('DO_NOT_CONTACT', 'CONVERTED', 'DISQUALIFIED', 'NOT_INTERESTED')
             and c.conversion_state <> 'CONVERTED'
             and coalesce(q.qualification_status, '') <> 'DISQUALIFIED'
             and c.total_call_attempts < least(%s,
               coalesce((select ca.max_call_attempts from campaigns ca where ca.id=%s),%s))
             and (%s::uuid is null or c.last_called_at is null or c.last_called_at <=
               now()-coalesce((select make_interval(hours=>ca.retry_after_hours::int)
                 from campaigns ca where ca.id=%s),interval '0'))
             and c.normalized_phone is not null
             and (%s::uuid is null or exists (select 1 from campaign_contacts cc
                  join campaigns ca on ca.id=cc.campaign_id and ca.status='ACTIVE'
                    and (ca.starts_at is null or ca.starts_at<=now())
                    and (ca.ends_at is null or ca.ends_at>now())
                  where cc.campaign_id = %s and cc.contact_id = c.id and cc.campaign_status = 'ACTIVE'))
             and not exists (select 1 from session_contacts sc where sc.session_id = %s and sc.contact_id = c.id)
             and (%s or not exists (select 1 from follow_ups f where f.contact_id = c.id
                  and f.status = 'PENDING' and f.scheduled_at >= %s))
             and (%s::text is null or c.lead_status::text=%s)
             and (%s::text is null or ls.priority_category=%s)
             and (%s::text is null or bp.property_type=%s)
             and (%s::text is null or lower(bp.preferred_location) like lower(%s))
             and (%s::numeric is null or bp.max_budget is null or bp.max_budget >= %s)
             and (%s::numeric is null or bp.min_budget is null or bp.min_budget <= %s)
             and (%s::text is null or bp.currency=%s)
             and (%s::text is null or (%s='due' and c.next_follow_up_at < %s)
               or (%s='future' and c.next_follow_up_at >= %s)
               or (%s='overdue' and c.next_follow_up_at < %s)
               or (%s='none' and c.next_follow_up_at is null))
           order by coalesce(cp.calling_priority,50) desc,
             array_position(string_to_array(%s, ','), case
             when due.id is not null and due.scheduled_at < %s and q.qualification_status='HIGH_PRIORITY_QUALIFIED' then 'overdue_high'
             when due.id is not null then 'due_followup'
             when q.qualification_status='HIGH_PRIORITY_QUALIFIED' then 'high_qualified'
             when q.qualification_status='QUALIFIED' or c.lead_status='QUALIFIED' then 'qualified'
             when c.interest_level in ('HIGH','VERY_HIGH') or c.lead_status='INTERESTED' then 'interested'
             when c.last_called_at is null then 'new' else 'retry' end),
             case when due.id is not null then c.next_follow_up_at end asc nulls last,
             case when c.last_called_at is null then c.created_at end asc nulls last,
             c.last_called_at asc nulls first, c.id
           limit 1 for update of c skip locked""",
        (
            local_day_end,
            session["telegram_user_id"],
            prefs["attempt_limit"],
            campaign_id,
            prefs["attempt_limit"],
            campaign_id,
            campaign_id,
            campaign_id,
            campaign_id,
            session_id,
            allow_future,
            local_day_end,
            queue_filters.get("lead_status"),
            queue_filters.get("lead_status"),
            queue_filters.get("priority_category"),
            queue_filters.get("priority_category"),
            queue_filters.get("property_type"),
            queue_filters.get("property_type"),
            location,
            f"%{location}%" if location else None,
            queue_filters.get("min_budget"),
            queue_filters.get("min_budget"),
            queue_filters.get("max_budget"),
            queue_filters.get("max_budget"),
            queue_filters.get("currency"),
            queue_filters.get("currency"),
            followup_filter,
            followup_filter,
            local_day_end,
            followup_filter,
            local_day_end,
            followup_filter,
            now,
            followup_filter,
            queue_order,
            now,
        ),
    ).fetchone()


def next_lead(
    conn: Connection, session_id: UUID, user_id: int, settings: Settings
) -> dict[str, Any] | None:
    session = conn.execute(
        "select * from call_sessions where id = %s and telegram_user_id = %s and status = 'ACTIVE' for update",
        (session_id, user_id),
    ).fetchone()
    if not session:
        return None
    candidate = _next_candidate(conn, session, settings)
    conn.execute(
        "update call_sessions set current_contact_id = %s where id = %s",
        (candidate["id"] if candidate else None, session_id),
    )
    if candidate:
        conn.execute(
            "insert into session_contacts (session_id, contact_id, disposition) values (%s,%s,'PRESENTED') on conflict (session_id,contact_id) do update set disposition='PRESENTED', selected_at=now()",
            (session_id, candidate["id"]),
        )
    return candidate


def skip_current(conn: Connection, session_id: UUID, user_id: int) -> bool:
    row = conn.execute(
        """update session_contacts sc set disposition = 'SKIPPED' from call_sessions s
           where sc.session_id = s.id and sc.session_id = %s and s.telegram_user_id = %s
             and s.status = 'ACTIVE' and sc.contact_id = s.current_contact_id
           returning sc.contact_id""",
        (session_id, user_id),
    ).fetchone()
    return row is not None


def previous_lead(conn: Connection, session_id: UUID, user_id: int) -> dict[str, Any] | None:
    return conn.execute(
        """select c.id, c.name, c.phone_number, c.normalized_phone, c.lead_status,
                  c.contact_eligibility, c.interest_level, c.total_call_attempts, c.last_called_at,
                  c.next_follow_up_at
           from call_sessions s join session_contacts sc on sc.session_id = s.id
           join contacts c on c.id = sc.contact_id
           where s.id = %s and s.telegram_user_id = %s and sc.disposition = 'SKIPPED'
           order by sc.selected_at desc limit 1 for update of s""",
        (session_id, user_id),
    ).fetchone()


def pause_session(conn: Connection, user_id: int, status: str) -> dict[str, Any] | None:
    if status not in {"PAUSED", "ENDED", "ACTIVE"}:
        raise ValueError("Invalid call session status")
    return conn.execute(
        """with target as (
             select id from call_sessions where telegram_user_id=%s and status in ('ACTIVE','PAUSED')
             order by started_at desc limit 1 for update
           ) update call_sessions s set status=%s,
             ended_at=case when %s='ENDED' then now() else ended_at end
           from target where s.id=target.id returning s.*""",
        (user_id, status, status),
    ).fetchone()


def save_note(conn: Connection, contact_id: UUID, note: str, note_type: str = "GENERAL") -> None:
    conn.execute(
        "insert into contact_notes (contact_id, note_type, note_text) values (%s,%s,%s)",
        (contact_id, note_type, note[:4000]),
    )


def schedule_followup(
    conn: Connection,
    contact_id: UUID,
    campaign_id: UUID | None,
    scheduled_at: datetime,
    kind: str,
    notes: str | None = None,
    telegram_user_id: int | None = None,
) -> dict[str, Any]:
    previous = conn.execute(
        """update follow_ups set status = 'RESCHEDULED', updated_at = now()
             where contact_id = %s and status = 'PENDING'
           returning id""",
        (contact_id,),
    ).fetchall()
    created = conn.execute(
        """insert into follow_ups (contact_id,campaign_id,follow_up_type,scheduled_at,notes,created_by)
           values (%s,%s,%s,%s,%s,%s) returning *""",
        (contact_id, campaign_id, kind, scheduled_at, notes, telegram_user_id),
    ).fetchone()
    for row in previous:
        conn.execute(
            "update follow_ups set rescheduled_to = %s where id = %s", (created["id"], row["id"])
        )
    conn.execute(
        """update contacts set next_follow_up_at = %s, follow_up_state = 'PENDING',
             updated_at = now() where id = %s""",
        (scheduled_at, contact_id),
    )
    if telegram_user_id is not None:
        conn.execute(
            "insert into reminder_deliveries (follow_up_id, telegram_user_id) values (%s,%s)",
            (created["id"], telegram_user_id),
        )
    from realestate_crm.services.intelligence import recalculate_lead_score

    recalculate_lead_score(conn, contact_id)
    return created


def complete_followup(conn: Connection, follow_up_id: UUID, user_id: int | None = None) -> bool:
    row = conn.execute(
        """update follow_ups f set status='COMPLETED',completed_at=now(),updated_at=now(),
             completed_by=coalesce(%s,f.completed_by)
           from contacts c where f.id=%s and f.contact_id=c.id and f.status='PENDING'
             and (%s::bigint is null or crm_can_access_contact(c.id,%s)) returning f.contact_id""",
        (user_id, follow_up_id, user_id, user_id),
    ).fetchone()
    if not row:
        return False
    next_at = conn.execute(
        "select min(scheduled_at) as scheduled_at from follow_ups where contact_id=%s and status='PENDING'",
        (row["contact_id"],),
    ).fetchone()["scheduled_at"]
    conn.execute(
        "update contacts set next_follow_up_at=%s, follow_up_state=%s, updated_at=now() where id=%s",
        (next_at, "PENDING" if next_at else "NONE", row["contact_id"]),
    )
    from realestate_crm.services.intelligence import recalculate_lead_score

    recalculate_lead_score(conn, row["contact_id"])
    return True


def mark_followup_missed(conn: Connection, follow_up_id: UUID, user_id: int | None = None) -> bool:
    row = conn.execute(
        """update follow_ups f set status='MISSED',updated_at=now(),missed_by=coalesce(%s,f.missed_by)
           from contacts c where f.id=%s and f.contact_id=c.id and f.status='PENDING'
             and f.scheduled_at<now() and (%s::bigint is null or crm_can_access_contact(c.id,%s))
           returning f.contact_id""",
        (user_id, follow_up_id, user_id, user_id),
    ).fetchone()
    if not row:
        return False
    next_at = conn.execute(
        "select min(scheduled_at) as scheduled_at from follow_ups where contact_id=%s and status='PENDING'",
        (row["contact_id"],),
    ).fetchone()["scheduled_at"]
    state = (
        "OVERDUE" if next_at and next_at < datetime.now(UTC) else "PENDING" if next_at else "NONE"
    )
    conn.execute(
        "update contacts set next_follow_up_at=%s,follow_up_state=%s,updated_at=now() where id=%s",
        (next_at, state, row["contact_id"]),
    )
    from realestate_crm.services.intelligence import recalculate_lead_score

    recalculate_lead_score(conn, row["contact_id"])
    return True


def due_followups(
    conn: Connection, user_id: int, now: datetime, limit: int = 20
) -> list[dict[str, Any]]:
    return conn.execute(
        """select f.id, f.scheduled_at, f.follow_up_type, c.id as contact_id, c.name, c.phone_number
           from follow_ups f join contacts c on c.id=f.contact_id
           left join reminder_deliveries d on d.follow_up_id=f.id
           where f.status='PENDING' and f.scheduled_at <= %s and crm_can_access_contact(c.id,%s)
             and (d.status is null or d.status in ('PENDING','FAILED'))
             and coalesce(d.attempts,0) < 5
           order by f.scheduled_at limit %s""",
        (now, user_id, limit),
    ).fetchall()


def save_call_result(
    conn: Connection,
    *,
    contact_id: UUID,
    campaign_id: UUID | None,
    session_id: UUID | None,
    user_id: int,
    update_id: int,
    category: str,
    outcome: str,
    note: str | None = None,
    connection_result: str | None = None,
) -> bool:
    allowed_categories = {
        "CONNECTION",
        "SALES_INTEREST",
        "PROPERTY_REQUIREMENTS",
        "FOLLOW_UP",
        "DISQUALIFICATION",
    }
    if category not in allowed_categories:
        raise ValueError("Unknown outcome category")
    inserted = conn.execute(
        """insert into call_logs (contact_id, campaign_id, call_session_id, outcome_category, outcome_code,
               connection_result, notes, telegram_update_id)
           values (%s,%s,%s,%s,%s,%s,%s,%s) on conflict (telegram_update_id) do nothing returning id""",
        (
            contact_id,
            campaign_id,
            session_id,
            category,
            outcome,
            connection_result,
            note,
            update_id,
        ),
    ).fetchone()
    if not inserted:
        return False
    current = conn.execute(
        "select * from contacts where id=%s for update", (contact_id,)
    ).fetchone()
    if not current:
        return False
    lead_status = current["lead_status"]
    eligibility = current["contact_eligibility"]
    interest = current["interest_level"]
    conversion = current["conversion_state"]
    if outcome == "CONVERTED_TO_CUSTOMER":
        lead_status, conversion = "CONVERTED", "CONVERTED"
    elif outcome == "DO_NOT_CONTACT":
        lead_status, eligibility = "DO_NOT_CONTACT", "DO_NOT_CONTACT"
    elif outcome in {"INVALID_NUMBER", "INVALID_CONTACT"}:
        eligibility = "INVALID_NUMBER"
        if lead_status == "NEW":
            lead_status = "DISQUALIFIED"
    elif outcome in {
        "NOT_INTERESTED",
        "BUDGET_TOO_LOW",
        "WRONG_TARGET_CUSTOMER",
        "ALREADY_PURCHASED",
        "DUPLICATE_CONTACT",
    }:
        if lead_status not in {"CONVERTED", "DO_NOT_CONTACT"}:
            lead_status = "NOT_INTERESTED" if outcome == "NOT_INTERESTED" else "DISQUALIFIED"
    elif category == "SALES_INTEREST":
        protected_status = {"CONVERTED", "DO_NOT_CONTACT", "NOT_INTERESTED", "DISQUALIFIED"}
        if lead_status not in protected_status:
            lead_status = "QUALIFIED" if outcome == "QUALIFIED" else "INTERESTED"
            if outcome == "HIGHLY_INTERESTED":
                interest = "VERY_HIGH"
            elif outcome in {
                "QUALIFIED",
                "INTERESTED",
                "REQUESTED_PROPERTY_VISIT",
                "MEETING_SCHEDULED",
                "VERBAL_COMMITMENT",
            }:
                interest = "HIGH"
            else:
                interest = "MEDIUM"
            if outcome == "MEETING_SCHEDULED":
                lead_status = "MEETING_SCHEDULED"
            if outcome == "REQUESTED_PROPERTY_VISIT":
                lead_status = "PROPERTY_VISIT_SCHEDULED"
    elif category == "FOLLOW_UP":
        if lead_status not in {"CONVERTED", "DO_NOT_CONTACT", "NOT_INTERESTED", "DISQUALIFIED"}:
            lead_status = "FOLLOW_UP_REQUIRED"
    elif outcome not in {"NO_ANSWER", "BUSY", "PHONE_SWITCHED_OFF", "UNREACHABLE", "CALL_FAILED"}:
        if lead_status == "NEW":
            lead_status = "CONTACTED"
    now = datetime.now(UTC)
    attempt = category in {
        "CONNECTION",
        "SALES_INTEREST",
        "PROPERTY_REQUIREMENTS",
        "FOLLOW_UP",
        "DISQUALIFICATION",
    }
    conn.execute(
        """update contacts set lead_status=%s, contact_eligibility=%s, interest_level=%s,
             conversion_state=%s, last_call_outcome=%s,
             first_contact_at=coalesce(first_contact_at,%s), last_called_at=%s,
             total_call_attempts=total_call_attempts + %s, updated_at=now()
           where id=%s""",
        (
            lead_status,
            eligibility,
            interest,
            conversion,
            outcome,
            now if attempt else None,
            now if attempt else current["last_called_at"],
            int(attempt),
            contact_id,
        ),
    )
    if note:
        save_note(conn, contact_id, note, "CALL_RESULT")
    if session_id and attempt:
        conn.execute(
            "update session_contacts set disposition='CALLED' where session_id=%s and contact_id=%s",
            (session_id, contact_id),
        )
        conn.execute(
            "update call_sessions set calls_attempted=calls_attempted+1 where id=%s", (session_id,)
        )
    conn.execute(
        """insert into audit_events (telegram_user_id, contact_id, event_type, old_value, new_value, telegram_update_id)
           values (%s,%s,'CALL_RESULT',%s,%s,%s)""",
        (
            user_id,
            contact_id,
            {"lead_status": current["lead_status"], "eligibility": current["contact_eligibility"]},
            {"lead_status": lead_status, "eligibility": eligibility, "outcome": outcome},
            update_id,
        ),
    )
    from realestate_crm.services.intelligence import recalculate_lead_score

    recalculate_lead_score(conn, contact_id)
    return True


def set_do_not_contact(conn: Connection, contact_id: UUID, user_id: int, update_id: int) -> None:
    current = conn.execute(
        "select contact_eligibility, lead_status from contacts where id=%s for update",
        (contact_id,),
    ).fetchone()
    if not current:
        return
    conn.execute(
        "update contacts set contact_eligibility='DO_NOT_CONTACT', lead_status='DO_NOT_CONTACT', updated_at=now() where id=%s",
        (contact_id,),
    )
    conn.execute(
        "insert into audit_events (telegram_user_id,contact_id,event_type,old_value,new_value,telegram_update_id) values (%s,%s,'DO_NOT_CONTACT',%s,%s,%s)",
        (
            user_id,
            contact_id,
            current,
            {"contact_eligibility": Eligibility.DO_NOT_CONTACT.value},
            update_id,
        ),
    )


def property_interest(conn: Connection, contact_id: UUID) -> dict[str, Any] | None:
    return conn.execute(
        "select * from contact_property_interests where contact_id=%s order by updated_at desc limit 1",
        (contact_id,),
    ).fetchone()


def save_property_interest(
    conn: Connection,
    contact_id: UUID,
    values: dict[str, Any],
    score_weights: dict[str, int] | None = None,
    user_id: int | None = None,
) -> None:
    fields = (
        "property_type",
        "preferred_location",
        "accepted_locations",
        "min_budget",
        "max_budget",
        "currency",
        "bedrooms",
        "min_bedrooms",
        "max_bedrooms",
        "min_area_sqm",
        "max_area_sqm",
        "purchase_purpose",
        "floor_preference",
        "new_or_resale",
        "profile_complete",
        "preferred_payment_plan",
        "readiness",
        "expected_timeline",
        "additional_requirements",
    )
    supplied = {field: values.get(field) for field in fields}
    params = {"contact_id": contact_id, **supplied}
    previous = conn.execute(
        "select * from contact_property_interests where contact_id=%s "
        "order by updated_at desc,id desc limit 1",
        (contact_id,),
    ).fetchone()
    assignments = ",".join(f"{field}=%({field})s" for field in fields)
    if previous:
        saved = conn.execute(
            f"update contact_property_interests set {assignments},updated_at=now() "
            "where id=%(id)s returning id",
            {**params, "id": previous["id"]},
        ).fetchone()
    else:
        columns = ",".join(("contact_id", *fields))
        binds = ",".join(f"%({field})s" for field in ("contact_id", *fields))
        saved = conn.execute(
            f"insert into contact_property_interests ({columns}) values ({binds}) returning id",
            params,
        ).fetchone()
    if user_id is not None and saved:
        old_value = {field: previous.get(field) for field in fields} if previous else None
        conn.execute(
            """insert into audit_events (telegram_user_id,contact_id,event_type,old_value,new_value)
               values (%s,%s,'BUYER_PROFILE_UPDATED',%s,%s)""",
            (
                user_id,
                contact_id,
                json.dumps(old_value, default=str) if old_value is not None else None,
                json.dumps(supplied, default=str),
            ),
        )
    from realestate_crm.services.intelligence import recalculate_lead_score

    recalculate_lead_score(conn, contact_id, score_weights)


def recent_followups(
    conn: Connection, limit: int = 20, user_id: int | None = None
) -> list[dict[str, Any]]:
    return conn.execute(
        """select f.id, f.scheduled_at, f.follow_up_type, c.id as contact_id, c.name, c.phone_number, f.status
           from follow_ups f join contacts c on c.id=f.contact_id where f.status='PENDING'
             and (%s::bigint is null or crm_can_access_contact(c.id,%s))
           order by f.scheduled_at limit %s""",
        (user_id, user_id, limit),
    ).fetchall()


def interested_contacts(
    conn: Connection, limit: int = 10, after_id: UUID | None = None, user_id: int | None = None
) -> list[dict[str, Any]]:
    return conn.execute(
        """select id, name, phone_number, lead_status, interest_level from contacts
           where lead_status in ('INTERESTED','QUALIFIED','NEGOTIATION')
             and contact_eligibility='ELIGIBLE'
             and (%s::bigint is null or crm_can_access_contact(id,%s))
             and (%s::uuid is null or id>%s)
           order by id limit %s""",
        (user_id, user_id, after_id, after_id, limit),
    ).fetchall()


def queue_export(
    conn: Connection,
    *,
    user_id: int,
    chat_id: int,
    export_format: str,
    filter_name: str,
    timezone: str,
    campaign_id: UUID | None = None,
    date_from: str | None = None,
    date_to_exclusive: str | None = None,
) -> dict[str, Any]:
    if export_format not in {"csv", "xlsx"}:
        raise ValueError("Unsupported export format")
    if filter_name not in {"eligible", "uncalled", "interested", "due", "converted", "all"}:
        raise ValueError("Unsupported export filter")
    return conn.execute(
        """insert into export_jobs (telegram_user_id, chat_id, export_format, filter_name,
             campaign_id, date_from, date_to_exclusive, timezone)
           values (%s,%s,%s,%s,%s,%s,%s,%s) returning id""",
        (
            user_id,
            chat_id,
            export_format,
            filter_name,
            campaign_id,
            date_from,
            date_to_exclusive,
            timezone,
        ),
    ).fetchone()


def current_session(conn: Connection, user_id: int) -> dict[str, Any] | None:
    return conn.execute(
        "select * from call_sessions where telegram_user_id=%s and status in ('ACTIVE','PAUSED') order by started_at desc limit 1",
        (user_id,),
    ).fetchone()


def save_queue_filters(
    conn: Connection, user_id: int, values: dict[str, Any]
) -> dict[str, Any] | None:
    allowed = {
        "lead_status",
        "priority_category",
        "property_type",
        "preferred_location",
        "min_budget",
        "max_budget",
        "currency",
        "followup_status",
    }
    if not set(values).issubset(allowed):
        raise ValueError("Unsupported queue filter")
    return conn.execute(
        """update user_settings set queue_filters=queue_filters || %s::jsonb,updated_at=now()
           where telegram_user_id=%s returning queue_filters""",
        (json.dumps(values, default=str), user_id),
    ).fetchone()


def clear_queue_filter(
    conn: Connection, user_id: int, key: str | None = None
) -> dict[str, Any] | None:
    allowed = {
        "lead_status",
        "priority_category",
        "property_type",
        "preferred_location",
        "min_budget",
        "max_budget",
        "currency",
        "followup_status",
        "budget",
    }
    if key is not None and key not in allowed:
        raise ValueError("Unsupported queue filter")
    if key is None:
        return conn.execute(
            "update user_settings set queue_filters='{}'::jsonb,updated_at=now() where telegram_user_id=%s returning queue_filters",
            (user_id,),
        ).fetchone()
    if key == "budget":
        return conn.execute(
            """update user_settings set queue_filters=queue_filters-'min_budget'-'max_budget'-'currency',updated_at=now()
               where telegram_user_id=%s returning queue_filters""",
            (user_id,),
        ).fetchone()
    return conn.execute(
        "update user_settings set queue_filters=queue_filters - %s,updated_at=now() where telegram_user_id=%s returning queue_filters",
        (key, user_id),
    ).fetchone()


def report(
    conn: Connection, period: str, timezone: str, user_id: int | None = None
) -> dict[str, Any]:
    now = datetime.now(ZoneInfo(timezone))
    start_local = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        start_local -= timedelta(days=start_local.weekday())
    elif period == "month":
        start_local = start_local.replace(day=1)
    start = start_local.astimezone(UTC)
    end = now.astimezone(UTC) + timedelta(seconds=1)
    row = conn.execute(
        """with calls as (
             select count(*) attempted, count(distinct contact_id) unique_called,
             count(*) filter (where connection_result='ANSWERED' or (outcome_category='CONNECTION' and outcome_code='ANSWERED')) answered,
               count(*) filter (where outcome_code='NO_ANSWER') no_answer,
               count(distinct contact_id) filter (where outcome_category='SALES_INTEREST') interested,
               count(distinct contact_id) filter (where outcome_code='QUALIFIED') qualified,
               count(distinct contact_id) filter (where outcome_code='MEETING_SCHEDULED') meetings,
               count(distinct contact_id) filter (where outcome_code='REQUESTED_PROPERTY_VISIT') visits,
               count(distinct contact_id) filter (where outcome_code='CONVERTED_TO_CUSTOMER') converted
             from call_logs l join call_sessions cs on cs.id=l.call_session_id
             where l.created_at >= %s and l.created_at < %s
               and (%s::bigint is null or cs.telegram_user_id=%s
                 or crm_can_access_contact(l.contact_id,%s))
           ), f as (
             select count(*) filter (where f.created_at >= %s and f.created_at < %s) scheduled,
               count(*) filter (where f.scheduled_at >= %s and f.scheduled_at < %s
                 and f.scheduled_at <= %s and f.status in ('PENDING','COMPLETED','MISSED')) due,
               count(*) filter (where f.scheduled_at >= %s and f.scheduled_at < %s
                 and f.scheduled_at <= %s and f.status='COMPLETED') completed,
               count(*) filter (where f.status='PENDING' and f.scheduled_at < %s) overdue
             from follow_ups f join contacts c on c.id=f.contact_id
             where (%s::bigint is null or crm_can_access_contact(c.id,%s))
           ) select * from calls cross join f""",
        (
            start,
            end,
            user_id,
            user_id,
            user_id,
            start,
            end,
            start,
            end,
            datetime.now(UTC),
            start,
            end,
            datetime.now(UTC),
            datetime.now(UTC),
            user_id,
            user_id,
        ),
    ).fetchone()
    row["answer_rate"] = (
        round(100 * row["answered"] / row["attempted"], 1) if row["attempted"] else 0
    )
    row["interest_rate"] = (
        round(100 * row["interested"] / row["answered"], 1) if row["answered"] else 0
    )
    row["conversion_rate"] = (
        round(100 * row["converted"] / row["unique_called"], 1) if row["unique_called"] else 0
    )
    row["followup_completion_rate"] = (
        round(100 * row["completed"] / row["due"], 1) if row["due"] else 0
    )
    row["period"] = period
    row["start"] = start_local.strftime("%d %b %Y")
    row["end"] = now.strftime("%d %b %Y")
    return row


def export_contacts(
    conn: Connection,
    filter_name: str,
    campaign_id: UUID | None,
    max_rows: int,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    after_id: UUID | None = None,
    page_size: int = 1000,
    user_id: int | None = None,
):
    where = {
        "eligible": "c.contact_eligibility='ELIGIBLE' and c.conversion_state<>'CONVERTED'",
        "uncalled": "c.last_called_at is null",
        "interested": "c.lead_status in ('INTERESTED','QUALIFIED','NEGOTIATION')",
        "converted": "c.conversion_state='CONVERTED'",
        "due": "exists (select 1 from follow_ups f where f.contact_id=c.id and f.status='PENDING' and f.scheduled_at<=now())",
        "all": "true",
    }
    clause = where.get(filter_name, where["eligible"])
    args: list[Any] = []
    if campaign_id:
        clause += " and exists (select 1 from campaign_contacts cc where cc.contact_id=c.id and cc.campaign_id=%s)"
        args.append(campaign_id)
    if date_from:
        clause += " and c.created_at >= %s"
        args.append(date_from)
    if date_to:
        clause += " and c.created_at < %s"
        args.append(date_to)
    if after_id:
        clause += " and c.id > %s"
        args.append(after_id)
    clause += " and (%s::bigint is null or crm_can_access_contact(c.id,%s))"
    args.extend((user_id, user_id))
    args.append(min(max_rows, page_size))
    return conn.execute(
        f"""select c.id::text id, c.name, c.phone_number, c.lead_status::text lead_status,
                c.interest_level::text interest_level, c.total_call_attempts, c.last_called_at,
                c.next_follow_up_at, c.contact_eligibility::text contact_eligibility
            from contacts c where {clause} order by c.id limit %s""",
        args,
    ).fetchall()
